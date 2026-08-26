"""Pure Discord formatting for the daemon bridge — no discord.py import.

Everything the bridge says is shaped here, as data: ``Chunk`` (a message
part with a text body and/or an ``EmbedSpec``) and a few pure helpers. The
transport (``daemon/discord.py``) only decides *where* and *when* to send;
it converts ``EmbedSpec`` into a ``discord.Embed`` at the send seam. Keeping
this layer free of the optional extra means the exact output is unit-tested
on every CI matrix entry, not only where ``sbxloop[discord]`` is installed.

Discord's Markdown subset is the target: ``**bold**``, ```` `code` ````,
fenced blocks with a language, ``> quotes``, masked links ``[text](url)``,
and ``<url>`` to suppress unfurling. Agent prose is never escaped — the
bridge sends every message with mentions disabled instead, so a stray
``@everyone`` in model output cannot ping anyone.

Redaction guarantee
-------------------
Every command line and every tool-output excerpt rendered here passes
through :func:`sbxloop.log.redact_text` before it can reach a Discord
thread — :func:`_tool_line`, :func:`output_excerpt` and
:meth:`ToolDigest.render` all apply it. Upstream redaction (the event
payload) still applies; this is a second, render-time belt so that
surfacing tool stdout/stderr in a thread cannot publish a credential that
upstream missed. It is idempotent, so already-masked text is unchanged.
"""

from __future__ import annotations

import json
import re
import socket
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sbxloop.cli.cmdfmt import format_command
from sbxloop.cli.tui import (
    _LIFECYCLE_PREFIXES,
    _TRANSCRIPT_SKIP,
    TOOL_ARGS_LINE_CLIP,
)
from sbxloop.cli.tui import _one_line as _one_line_mid
from sbxloop.daemon.model import ReviewOutcome, RunReport, WorkItem
from sbxloop.events import Event, HostEventTypes
from sbxloop.log import redact_text

# Discord's hard cap per message; _clip never returns more than this even
# if a caller passes a nonsense limit.
DISCORD_MAX_MESSAGE = 2000

# Embed limits (Discord API): title 256, description 4096, field name 256,
# field value 1024, 25 fields, 6000 characters in total.
EMBED_TITLE_MAX = 256
EMBED_DESCRIPTION_MAX = 4096
EMBED_FIELD_NAME_MAX = 256
EMBED_FIELD_VALUE_MAX = 1024
EMBED_FIELDS_MAX = 25
EMBED_TOTAL_MAX = 6000
EMBED_FOOTER_MAX = 2048

# -- tool output excerpt budget -------------------------------------------------------------
# What a completed tool call is allowed to publish back into the thread.
# Deliberately asymmetric: a success is noise once you know it succeeded, a
# failure is the thing the watcher opened the thread for. Every one of these
# is an upper bound only -- the renderer additionally clamps the finished
# message to DISCORD_MAX_MESSAGE, so no combination can overflow Discord.

# Tail lines shown for a *successful* call. 0 (the default) means a success
# renders no block of its own: its outcome and exit status already appear on
# the batched ``$ bash …  ✓ 1.2s`` line, and giving every success a block
# would break batching and flood the thread. Raise it to echo output.
TOOL_OUTPUT_LINES_DEFAULT = 0
# Total head+tail lines shown for a *failed* call: enough stderr to act on.
TOOL_FAIL_OUTPUT_LINES_DEFAULT = 20
# Per-line character clip inside an excerpt, so one 1MB line cannot eat the
# whole budget before the total-chars cap is applied.
TOOL_EXCERPT_LINE_CLIP = 300
# Total characters of fenced body across all excerpt lines.
TOOL_EXCERPT_MAX_CHARS = 1200

COLOR_RUNNING = 0x3498DB
COLOR_OK = 0x2ECC71
COLOR_FAIL = 0xE74C3C
COLOR_WARN = 0xE67E22
COLOR_DIM = 0x95A5A6

# The daemon-side view of "how did the run end".
STATE_MARKER = {"completed": "✅", "failed": "❌", "delivery_failed": "⚠", "cancelled": "⏹"}
STATE_COLOR = {
    "completed": COLOR_OK,
    "failed": COLOR_FAIL,
    "delivery_failed": COLOR_WARN,
    "cancelled": COLOR_DIM,
}

_FENCE_RE = re.compile(r"^\s*(```|~~~)\s*([\w+#.-]*)")
_URL_RE = re.compile(r"(?<![<(\[])https?://[^\s<>()\[\]]+")


# -- primitives ---------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    """Tail-truncate to ``limit`` (never above Discord's cap) with an ellipsis."""
    limit = max(1, min(int(limit), DISCORD_MAX_MESSAGE))
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…" if limit > 1 else "…"


def _cut(text: str, limit: int) -> str:
    """Tail-truncate to an embed-part limit (which may exceed the message cap)."""
    limit = max(1, int(limit))
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…" if limit > 1 else "…"


def _one_line(text: str, limit: int = 160) -> str:
    """Whitespace-collapsed, tail-clipped single line."""
    return _clip(" ".join(str(text).split()), limit)


def link(text: str, url: str | None) -> str:
    """A masked link, or the bare text when there is no URL."""
    return f"[{text}]({url})" if url else text


def nolink(url: str) -> str:
    """A raw URL Discord will not unfurl."""
    return f"<{url}>"


def mask_urls(text: str) -> str:
    """Wrap bare URLs in ``<>`` so notices do not sprout link previews."""
    return _URL_RE.sub(lambda m: f"<{m.group(0)}>", text)


def code(text: object) -> str:
    """Inline code; backticks inside are replaced so the span cannot break."""
    return "`" + str(text if text is not None else "").replace("`", "'") + "`"


# -- data model -----------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbedSpec:
    """A Discord embed as plain data (converted at the send seam)."""

    title: str | None = None
    description: str | None = None
    url: str | None = None
    color: int | None = None
    fields: tuple[tuple[str, str, bool], ...] = ()  # (name, value, inline)
    footer: str | None = None

    def clamped(self) -> EmbedSpec:
        """A copy that respects every embed limit (drops fields past 25,
        clips each part, then trims the description if the total is over
        the 6000-character ceiling)."""
        title = _cut(self.title, EMBED_TITLE_MAX) if self.title else None
        description = _cut(self.description, EMBED_DESCRIPTION_MAX) if self.description else None
        footer = _cut(self.footer, EMBED_FOOTER_MAX) if self.footer else None
        fields = tuple(
            (_cut(n or "​", EMBED_FIELD_NAME_MAX), _cut(v or "​", EMBED_FIELD_VALUE_MAX), i)
            for n, v, i in self.fields[:EMBED_FIELDS_MAX]
        )
        total = (
            len(title or "")
            + len(description or "")
            + len(footer or "")
            + sum(len(n) + len(v) for n, v, _ in fields)
        )
        # Over the total ceiling: shrink the description first (down to a
        # readable floor), then drop trailing fields — the leading ones
        # (source, run, state) matter most — then the footer.
        kept = list(fields)
        while total > EMBED_TOTAL_MAX:
            over = total - EMBED_TOTAL_MAX
            if description and len(description) > 200:
                new = _cut(description, max(200, len(description) - over))
                total -= len(description) - len(new)
                description = new
            elif kept:
                n, v, _ = kept.pop()
                total -= len(n) + len(v)
            elif footer:
                total -= len(footer)
                footer = None
            else:
                break
        return EmbedSpec(title, description, self.url, self.color, tuple(kept), footer)

    def as_text(self) -> str:
        """A text rendering for clients/tests without embed support."""
        lines: list[str] = []
        if self.title:
            lines.append(f"**{self.title}**" + (f" {nolink(self.url)}" if self.url else ""))
        if self.description:
            lines.append(self.description)
        for name, value, _ in self.fields:
            lines.append(f"{name}: {value}")
        if self.footer:
            lines.append(f"_{self.footer}_")
        return "\n".join(lines)


@dataclass(frozen=True)
class Chunk:
    """One message part.

    ``kind``: ``line`` chunks coalesce with neighbours into one message;
    ``block`` chunks are sent on their own (fenced code, long prose);
    ``embed`` chunks carry an ``EmbedSpec`` (``text`` is the content
    fallback shown in notifications and by clients that hide embeds).
    ``flush`` asks the pump to send everything buffered so far right away.
    """

    text: str = ""
    embed: EmbedSpec | None = None
    kind: str = "line"
    flush: bool = False
    suppress_embeds: bool = False


def line(text: str, *, flush: bool = False) -> Chunk:
    return Chunk(text=text, kind="line", flush=flush)


def block(text: str, *, flush: bool = True) -> Chunk:
    return Chunk(text=text, kind="block", flush=flush)


# -- markdown splitting ---------------------------------------------------------------------


def _fence_state(text: str) -> tuple[bool, str]:
    """(inside_fence, language) after scanning ``text`` line by line."""
    inside, lang = False, ""
    for ln in text.splitlines():
        m = _FENCE_RE.match(ln)
        if m:
            if inside:
                inside = False
            else:
                inside, lang = True, m.group(2)
    return inside, lang


def split_markdown(text: str, limit: int, *, header: str = "", cont: str = "") -> list[str]:
    """Split Markdown into messages of at most ``limit`` characters.

    Break preference: blank line outside a fence → line boundary outside a
    fence → line boundary inside a fence (the fence is closed at the end of
    the chunk and re-opened with the same language at the start of the
    next) → hard clip of a single oversize line. ``header`` is prepended to
    the first chunk, ``cont`` (with ``{i}``/``{n}`` placeholders) to every
    later one; each chunk stays within ``limit`` and fence-balanced.
    """
    limit = max(1, min(int(limit), DISCORD_MAX_MESSAGE))
    text = text.strip("\n")
    head = f"{header}\n" if header else ""
    if len(head) + len(text) <= limit:
        return [head + text] if (head or text) else []
    # Continuation marker: reserve a fixed budget so the placeholder
    # substitution never pushes a chunk over the limit.
    cont_budget = len(cont.format(i=99, n=99)) + 1 if cont else 0
    # Hard-wrap any single line that could never fit a chunk on its own
    # (widest prefix + fence closer + reopened fence), so the main loop
    # only ever deals with lines that fit.
    wrap = max(1, limit - max(len(head), cont_budget) - 24)
    lines: list[str] = []
    for ln in text.split("\n"):
        while len(ln) > wrap:
            lines.append(ln[: wrap - 1] + "…")
            ln = "…" + ln[wrap - 1 :]
        lines.append(ln)
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    inside, lang = False, ""
    prefix_len = len(head)

    def room() -> int:
        return limit - prefix_len - (4 if inside else 0)  # 4 = "\n```" closer

    for ln in lines:
        m = _FENCE_RE.match(ln)
        add = len(ln) + (1 if cur else 0)
        if cur_len + add > room():
            # Prefer breaking at the last blank line outside a fence.
            cut = _last_blank(cur) if not inside else -1
            if cut > 0:
                head_part, tail_part = cur[:cut], cur[cut + 1 :]
                _close(chunks, head_part, False)
                cur = tail_part
            else:
                _close(chunks, cur, inside)
                cur = [f"```{lang}"] if inside else []
            prefix_len = cont_budget
            cur_len = sum(len(x) + 1 for x in cur) - (1 if cur else 0)
            add = len(ln) + (1 if cur else 0)
        cur.append(ln)
        cur_len += add
        if m:
            if inside:
                inside = False
            else:
                inside, lang = True, m.group(2)
    if cur:
        _close(chunks, cur, False)
    out: list[str] = []
    n = len(chunks)
    for i, part in enumerate(chunks, start=1):
        body = "\n".join(part)
        if i == 1:
            out.append(head + body)
        else:
            marker = cont.format(i=i, n=n) if cont else ""
            out.append(f"{marker}\n{body}" if marker else body)
    return [_clip(c, limit) for c in out]


def _last_blank(lines: list[str]) -> int:
    for idx in range(len(lines) - 1, 0, -1):
        if not lines[idx].strip():
            return idx
    return -1


def _close(chunks: list[list[str]], part: list[str], inside: bool) -> None:
    part = list(part)
    while part and not part[-1].strip():
        part.pop()
    if not part:
        return
    if inside:
        part.append("```")
    chunks.append(part)


# -- structured payload -----------------------------------------------------------------

# A bare JSON document starts at the left margin of its own line: models that
# skip the fence write "Here is the plan:" and then the object.
_JSON_OPENER_RE = re.compile(r"^[ \t]*([{\[])", re.MULTILINE)
_json_decoder = json.JSONDecoder()


def _is_json_doc(text: str) -> bool:
    """True when ``text`` is exactly one JSON object or array."""
    try:
        return isinstance(json.loads(text.strip()), dict | list)
    except (ValueError, RecursionError):
        return False


def strip_json_payload(text: str) -> str:
    """``text`` without the machine-readable JSON the engine already read.

    Every structured phase — decompose, steer —
    asks its agent for one fenced JSON block, and agents narrate around it.
    The engine parses that block and the bridge already renders what it
    *means*: the status line, the phase lines, the steering reply, the
    report card. Posting the block as well says the same thing twice, in
    the shape a human reads least — so the thread keeps the narration and
    drops the payload, and a reply that was payload only leaves nothing to
    post at all. The block is not lost: it is in the run's event store
    (``sbxloop logs``) and the phase ledger.

    Mirrors :func:`sbxloop_worker._json.extract_json` in what it counts as
    the payload — fenced blocks first (tagged ``json``, or simply parsing
    as one), then a bare document running to the end of the reply.
    """
    lines = text.splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        match = _FENCE_RE.match(lines[i])
        if match is None:
            kept.append(lines[i])
            i += 1
            continue
        marker, lang = match.group(1), match.group(2)
        close = i + 1
        while close < len(lines) and not lines[close].strip().startswith(marker):
            close += 1
        # An unterminated fence runs to the end of the message; its body is
        # everything after the opener and there is no closer to keep.
        end = min(close + 1, len(lines))
        if lang.lower() != "json" and not _is_json_doc("\n".join(lines[i + 1 : close])):
            kept.extend(lines[i:end])
        i = end
    return _strip_bare_json("\n".join(kept)).strip()


def _strip_bare_json(text: str) -> str:
    """``text`` without an unfenced JSON document that ends it.

    The fence scan alone would let the payload through when the agent skips
    the fence — the field failure ``extract_json`` grew its prose fallback
    for. Anchored at a line start and required to run to the end, so a list
    item or a brace inside prose is never mistaken for it.
    """
    for match in _JSON_OPENER_RE.finditer(text):
        try:
            value, end = _json_decoder.raw_decode(text, match.start(1))
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict | list) and not text[end:].strip():
            return text[: match.start()]
    return text


# -- tool batching --------------------------------------------------------------------------


def _excerpt_lines(
    lines: list[str], max_lines: int, total: int, line_clip: int, *, tail_only: bool = False
) -> list[str]:
    """Head+tail selection of at most ``max_lines`` lines with a middle
    ``… N lines elided …`` marker; ``total`` is the true line count of the
    original output (which may exceed what survived upstream clipping)."""
    if max_lines <= 0:
        return []
    if len(lines) <= max_lines:
        picked = list(lines)
        cut = len(picked)
    elif tail_only or max_lines == 1:
        picked = lines[-max_lines:]
        cut = 0
    else:
        head_n = max_lines // 2
        tail_n = max_lines - head_n
        picked = lines[:head_n] + lines[-tail_n:]
        cut = head_n
    omitted = max(0, total - len(picked))
    out = [_clip(ln.rstrip(), line_clip).replace("```", "'''") for ln in picked]
    if omitted > 0:
        out = [*out[:cut], f"… {omitted} lines elided …", *out[cut:]]
    return out


def output_excerpt(
    tool: str,
    exit_code: int | None,
    detail: str,
    *,
    success: bool | None = False,
    output_lines: int | None = None,
    max_lines: int | None = None,
    line_clip: int = TOOL_EXCERPT_LINE_CLIP,
    max_chars: int = TOOL_EXCERPT_MAX_CHARS,
) -> Chunk | None:
    """The block a completed tool call gets to itself: an outcome header
    plus a bounded excerpt of its output.

    A failure shows ``TOOL_FAIL_OUTPUT_LINES_DEFAULT`` head+tail lines (the
    caller should pass the event's ``error``, falling back to ``output``, so
    stderr is what appears); a success shows at most
    ``TOOL_OUTPUT_LINES_DEFAULT`` tail lines and renders nothing at all when
    that budget is 0 and there is nothing to say. Elision is marked with
    ``… N lines elided …``, counted from ``output_lines`` (the event's true
    line count) when the stored text was itself truncated.

    Bounds: each line is clipped to ``line_clip``, the fenced body to
    ``max_chars``, and the finished message to ``DISCORD_MAX_MESSAGE`` --
    so no input, however pathological, can exceed Discord's limit.

    Redaction: upstream masks the event payload, and this renderer
    additionally runs the detail through :func:`sbxloop.log.redact_text`
    before splitting it into lines, so a credential shape that survived
    upstream (or that spans a clipped line) still cannot reach the thread.
    """
    failed = success is False
    if max_lines is None:
        max_lines = TOOL_FAIL_OUTPUT_LINES_DEFAULT if failed else TOOL_OUTPUT_LINES_DEFAULT
    exit_part = f" (exit {exit_code})" if exit_code is not None else ""
    head = f"✗ {code(tool)} failed{exit_part}" if failed else f"✓ {code(tool)}{exit_part}"
    lines = [ln for ln in redact_text(str(detail or "")).strip().splitlines() if ln.strip()]
    total = max(len(lines), int(output_lines or 0))
    picked = _excerpt_lines(lines, int(max_lines), total, line_clip, tail_only=not failed)
    if not picked:
        return block(head) if failed else None
    body = "\n".join(picked)
    # Body cap, then a whole-message clamp that also re-closes the fence if
    # the clamp landed inside it.
    body = _cut(body, max(1, int(max_chars)))
    text = f"{head}\n```text\n{body}\n```"
    if len(text) > DISCORD_MAX_MESSAGE:
        text = _clip(text, DISCORD_MAX_MESSAGE - 4).rstrip("\n") + "\n```"
    return block(text)


def failure_detail(tool: str, exit_code: int | None, detail: str) -> Chunk:
    """Backwards-compatible failure renderer: :func:`output_excerpt` with
    the failure budget. Always returns a chunk."""
    chunk = output_excerpt(tool, exit_code, detail, success=False)
    assert chunk is not None
    return chunk


def _duration(ms: float | int | None) -> str:
    """``1.2s`` / ``840ms`` for a call's duration; empty when unknown."""
    if ms is None:
        return ""
    try:
        value = float(ms)
    except (TypeError, ValueError):
        return ""
    if value < 1000:
        return f"{round(value)}ms"
    return f"{value / 1000:.1f}s"


def _tool_line(
    tool: str,
    args: str,
    *,
    success: bool | None = None,
    exit_code: int | None = None,
    duration_ms: float | int | None = None,
    running: bool = False,
) -> str:
    """One rendered line for a tool call: ``$ bash  <command>  ✓ 1.2s``.

    The command goes through :func:`format_command`, so the boilerplate
    ``cd <run path> &&`` prefix collapses to ``cd $RUN &&``, the leading
    verb always survives and no token is cut without a ``…`` marker.

    The raw command is scrubbed with :func:`sbxloop.log.redact_text` first
    (before formatting, so token boundaries are still intact), so no
    credential can be published even if one reached the event payload.
    """
    text = f"$ {tool}"
    args_line = format_command(redact_text(str(args or "")), TOOL_ARGS_LINE_CLIP - 40)
    if args_line:
        text += f"  {args_line}"
    dur = _duration(duration_ms)
    if running:
        text += "  … running"
    elif success is False:
        marker = f"✗ exit {exit_code}" if exit_code is not None else "✗ failed"
        text += f"  {marker}" + (f" · {dur}" if dur else "")
    elif success is True:
        text += "  ✓" + (f" {dur}" if dur else "")
    elif dur:
        text += f"  {dur}"
    return text.replace("```", "'''")


class ToolBatcher:
    """Collects tool calls into one fenced block, one line per call.

    A call is rendered exactly once, when it completes: ``add_start``
    records it as pending (emitting nothing) and ``add_end`` resolves it
    by ``tool_call_id`` and appends the single line carrying the command,
    the outcome (``✓`` / ``✗ exit N``) and the duration. Correlation is
    strictly by call id, so concurrent calls that finish out of order
    still pair up and each line carries its own command.

    An end with no matching start renders from the end event's own tool
    and args; if the end carries neither an id nor args (an older worker
    that predates ``tool_call_id``), it falls back to the oldest in-flight
    start for the same tool. In-flight starts survive routine flushes —
    their line is still written on completion — and a start that never ends
    is rendered as ``… running`` by the run-end ``flush(final=True)``, so
    nothing in flight is lost and no call is rendered twice.

    Volume is bounded: the command is clipped to ``TOOL_ARGS_LINE_CLIP -
    40`` characters, a failed call's detail chunk keeps at most
    ``fail_output_lines`` output lines (``TOOL_EXCERPT_LINE_CLIP``
    characters each, ``TOOL_EXCERPT_MAX_CHARS`` in total), and a
    batch holds at most ``max_lines`` lines (the daemon's
    ``discord.tool_batch_lines``) before the pump flushes it.

    Redaction: upstream masks the event payload, and every line and
    excerpt this class renders additionally passes through
    :func:`sbxloop.log.redact_text` (in :func:`_tool_line` and
    :func:`output_excerpt`), so a credential in a command or in captured
    output cannot reach the thread.
    """

    def __init__(
        self,
        *,
        max_lines: int = 8,
        quiet: bool = False,
        output_lines: int = TOOL_OUTPUT_LINES_DEFAULT,
        fail_output_lines: int = TOOL_FAIL_OUTPUT_LINES_DEFAULT,
    ) -> None:
        self.max_lines = max_lines
        self.quiet = quiet
        # Excerpt budgets (discord.tool_output_lines / tool_fail_output_lines).
        self.output_lines = output_lines
        self.fail_output_lines = fail_output_lines
        self._lines: list[str] = []
        self._pending: dict[str, tuple[str, str]] = {}
        self._synthetic = 0

    def __len__(self) -> int:
        return len(self._lines)

    @property
    def full(self) -> bool:
        return len(self._lines) >= self.max_lines

    def add_start(self, tool: str, args: str, call_id: str | None) -> None:
        """Record a call as in flight. Emits no line -- the line is written
        when the call ends (or when a flush finds it still running)."""
        if self.quiet:
            return
        key = str(call_id) if call_id else self._next_key()
        self._pending[key] = (tool, str(args or ""))

    def _next_key(self) -> str:
        self._synthetic += 1
        return f"\x00anon{self._synthetic}"

    def _pop_oldest(self, tool: str) -> tuple[str, str] | None:
        """The oldest in-flight start for ``tool`` (dicts keep insertion
        order), used only when correlation by id is impossible."""
        for key, value in self._pending.items():
            if value[0] == tool:
                del self._pending[key]
                return value
        return None

    def add_end(
        self,
        tool: str,
        call_id: str | None,
        *,
        success: bool | None,
        exit_code: int | None,
        detail: str,
        args: str = "",
        duration_ms: float | int | None = None,
        output_lines: int | None = None,
    ) -> Chunk | None:
        """Resolve the pending start for ``call_id`` and append its one line.

        ``output_lines`` is the event's true output line count, used for the
        elided-line count when the stored text was truncated upstream."""
        pending = self._pending.pop(str(call_id), None) if call_id else None
        if pending is None and not args:
            # An old worker emits no ``tool_call_id`` and no ``args`` on the
            # end event, so there is nothing to correlate on: fall back to the
            # oldest in-flight start for the same tool. Ordering is only
            # approximate, but it beats dropping the command entirely and
            # leaving a phantom "… running" line behind at flush.
            pending = self._pop_oldest(tool)
        line_tool, line_args = pending if pending is not None else (tool, str(args or ""))
        if not self.quiet:
            self._lines.append(
                _tool_line(
                    line_tool or tool,
                    line_args,
                    success=success,
                    exit_code=exit_code,
                    duration_ms=duration_ms,
                )
            )
        if success is not False and (self.quiet or self.output_lines <= 0):
            return None
        return output_excerpt(
            tool,
            exit_code,
            detail,
            success=success,
            output_lines=output_lines,
            max_lines=self.fail_output_lines if success is False else self.output_lines,
        )

    def flush(self, *, final: bool = False) -> Chunk | None:
        """The batched lines as one fenced chunk (None when there are none).

        A routine flush renders only *completed* calls; anything still in
        flight stays pending so its one line is written by ``add_end`` when
        it completes — otherwise a mid-run flush (a failed sibling call, the
        coalesce timer) would print an in-flight call twice, first as
        ``… running`` and again on completion. Only a ``final`` flush (the
        run is over, no end event can arrive) renders the leftovers as
        ``… running`` and clears them."""
        lines = list(self._lines)
        self._lines = []
        if final:
            if not self.quiet:
                lines += [
                    _tool_line(tool, args, running=True) for tool, args in self._pending.values()
                ]
            self._pending = {}
            self._synthetic = 0
        if not lines:
            return None
        body = "\n".join(lines)
        return block(f"```text\n{body}\n```", flush=False)


# -- tool digest (normal level) -----------------------------------------------------------

# A burst is "repetitive" when its trailing commands share a head token and
# every REPETITION_WINDOW-sized slice of them reads as near-copies of one
# another (mean adjacent-pair similarity >= REPETITION_RATIO). Sized on the
# field shape: the re59gj4vq spiral was runs of `grep`/`od` variants
# differing in a flag or a path.
REPETITION_WINDOW = 6
REPETITION_RATIO = 0.6
DIGEST_LAST_CLIP = 100


def _head(args: str) -> str:
    """The command's leading word — what a human would call it ('grep')."""
    for tok in str(args).split():
        # `cd x && grep ...` and `FOO=1 grep ...` are still grep calls.
        if tok in ("cd", "&&", ";", "|", "||") or "=" in tok:
            continue
        return tok
    return ""


class RepetitionDetector:
    """Trailing-run detector for near-identical commands, fed one call at a
    time so a burst of any length costs O(window) memory.

    A call extends the current run when it has the same tool and head word
    as the previous one. The run is *repetitive* once it holds ``window``
    calls and every trailing ``window``-sized slice of it has a mean
    adjacent-pair similarity >= ``ratio``; ``streak`` is how many trailing
    calls satisfy that (0 while it does not). A slice whose mean dips below
    the threshold ends the streak but not the run — the next qualifying
    slice starts a fresh ``window``-long streak.
    """

    def __init__(self, *, window: int = REPETITION_WINDOW, ratio: float = REPETITION_RATIO) -> None:
        self.window = window
        self.ratio = ratio
        self.streak = 0
        self._head: tuple[str, str] | None = None
        self._prev: str | None = None
        self._ratios: deque[float] = deque(maxlen=max(window - 1, 1))

    def add(self, tool: str, args: str) -> int:
        from difflib import SequenceMatcher  # local: keep import cost off the hot path

        head = (tool, _head(args))
        if head != self._head:
            self._head, self._prev, self.streak = head, args, 0
            self._ratios.clear()
            return self.streak
        assert self._prev is not None
        self._ratios.append(SequenceMatcher(None, self._prev, args).ratio())
        self._prev = args
        if len(self._ratios) < self.window - 1:
            return self.streak  # the run is shorter than a window
        if sum(self._ratios) / len(self._ratios) >= self.ratio:
            self.streak = self.streak + 1 if self.streak else self.window
        else:
            self.streak = 0
        return self.streak


def repetitive_streak(commands: list[tuple[str, str]], *, window: int = REPETITION_WINDOW) -> int:
    """How many trailing ``(tool, args)`` calls are near-identical to one
    another (same tool, same head word, similar text); 0 if fewer than
    ``window`` are. Batch form of :class:`RepetitionDetector`."""
    det = RepetitionDetector(window=window)
    for tool, args in commands:
        det.add(tool, args)
    return det.streak


class ToolDigest:
    """One burst of tool activity, summarised into a single line the pump
    keeps editing in place (#235).

    Field: two threads filled with hundreds of ``⚙ bash …`` lines during
    an executor forensic spiral, drowning the agent messages, verdicts and
    links a human reads a channel for. So at the normal level a burst is
    one message — count, per-tool breakdown, the last command — that grows
    by edits; failed calls still get their own detail chunk (``add_end``)
    and a burst is closed by whatever non-tool line comes next. The full
    stream stays in ``sbxloop logs`` and the verbose level.

    ``repetitive`` flags a trailing run of near-identical commands (an
    agent re-proving a fact it already has); the rendered line then carries
    the "may be stuck; cancel to stop" nudge for the human.
    """

    def __init__(
        self,
        *,
        cancel_hint: str = "!sbx cancel",
        output_lines: int = 0,
        fail_output_lines: int = TOOL_FAIL_OUTPUT_LINES_DEFAULT,
    ) -> None:
        self.cancel_hint = cancel_hint
        # At the normal level a success adds nothing by default: the digest
        # line already says the burst ran. Failures keep the full budget.
        self.output_lines = output_lines
        self.fail_output_lines = fail_output_lines
        self.count = 0
        self.failed = 0
        self.by_tool: dict[str, int] = {}
        self.last: tuple[str, str] | None = None
        self._repetition = RepetitionDetector()
        self._dirty = False
        self._seen: set[str] = set()

    def __len__(self) -> int:
        return self.count

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def repetitive(self) -> int:
        return self._repetition.streak

    def add_start(self, tool: str, args: str, call_id: str | None = None) -> None:
        if call_id:
            self._seen.add(str(call_id))
        self.count += 1
        self.by_tool[tool] = self.by_tool.get(tool, 0) + 1
        self.last = (tool, " ".join(redact_text(str(args or "")).split()))
        # Fed incrementally so a 200-call spiral still reports a 200-long
        # streak (a bounded tail would cap it and never collapse the line).
        self._repetition.add(*self.last)
        self._dirty = True

    def add_end(
        self,
        tool: str,
        call_id: str | None = None,
        *,
        success: bool | None,
        exit_code: int | None,
        detail: str,
        output_lines: int | None = None,
    ) -> Chunk | None:
        """A failed call is the one thing that stays individual: returns
        its detail chunk (as the batcher would) and counts it in the line.

        Pairing is by ``call_id``: an end whose id was never started still
        counts as a call, and a duplicate end for an id already resolved
        is ignored so ``failed`` never double-counts."""
        key = str(call_id) if call_id else ""
        if key:
            if key in self._seen:
                self._seen.discard(key)
            else:
                # An end with no start: count the call so the digest total
                # matches what actually ran.
                self.count += 1
                self.by_tool[tool] = self.by_tool.get(tool, 0) + 1
                self._dirty = True
        if success is not False:
            return output_excerpt(
                tool,
                exit_code,
                detail,
                success=success,
                output_lines=output_lines,
                max_lines=self.output_lines,
            )
        self.failed += 1
        self._dirty = True
        return output_excerpt(
            tool,
            exit_code,
            detail,
            success=False,
            output_lines=output_lines,
            max_lines=self.fail_output_lines,
        )

    def render(self) -> str:
        self._dirty = False
        if not self.count:
            return ""
        streak = self.repetitive
        last_tool, last_args = self.last or ("?", "")
        # Belt to add_start's braces: the rendered command is scrubbed again
        # so no path into ``last`` can publish a credential.
        last_args = redact_text(last_args)
        last = f" — last: {code(_one_line_mid(last_args, DIGEST_LAST_CLIP))}" if last_args else ""
        if streak and streak == self.count:
            head = f"⚙ {last_tool} x{streak} similar commands{last}"
        else:
            breakdown = ", ".join(
                f"{tool} x{n}" if n > 1 else tool
                for tool, n in sorted(self.by_tool.items(), key=lambda kv: (-kv[1], kv[0]))
            )
            noun = "tool call" if self.count == 1 else "tool calls"
            head = f"⚙ {self.count} {noun} ({breakdown}){last}"
        if self.failed:
            head += f" · ✗ {self.failed} failed"
        if streak:
            head += (
                f"\n⚠ the last {streak} {last_tool} calls are near-identical — the agent may be "
                f"stuck; `{self.cancel_hint}` stops the run"
            )
        return head


# -- live status line -----------------------------------------------------------------------


_TASK_TERMINAL = {"done", "failed", "skipped", "cancelled", "abandoned"}


class StatusLine:
    """One message per run, edited in place, summarising where the run is.

    Fed by task/phase events; ``render()`` is the current text and
    ``dirty`` says whether it changed since the last ``render()``.
    """

    def __init__(self) -> None:
        self.roster: dict[str, str] = {}  # task_id -> title (in roster order)
        self.states: dict[str, str] = {}
        self.revisions: dict[str, int] = {}
        self.current: str | None = None
        self.phase: str | None = None
        self.finished: str | None = None
        self._dirty = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    def observe(self, event: Event) -> None:
        d = event.data
        t = event.type
        tid = str(d.get("task_id") or "")
        if t == "task.state" and tid:
            if d.get("title") and tid not in self.roster:
                self.roster[tid] = str(d["title"])
            if d.get("state"):
                self.states[tid] = str(d["state"])
            if d.get("revisions") is not None:
                self.revisions[tid] = int(d["revisions"] or 0)
            self._dirty = True
        elif t == "task.start" and tid:
            self.roster.setdefault(tid, str(d.get("title") or tid))
            self.current, self.phase = tid, None
            self._dirty = True
        elif t == "phase.end" and tid:
            self.phase = str(d.get("phase") or "")
            self._dirty = True
        elif t == "task.end" and tid:
            self.roster.setdefault(tid, str(d.get("title") or tid))
            if d.get("state"):
                self.states[tid] = str(d["state"])
            if self.current == tid:
                self.current, self.phase = None, None
            self._dirty = True
        elif t in ("run.end", "run.state") and d.get("state") in (
            "completed",
            "failed",
            "cancelled",
        ):
            self.finished = str(d["state"])
            self._dirty = True

    def finish(self, state: str) -> None:
        self.finished = state
        self._dirty = True

    def render(self) -> str:
        self._dirty = False
        ids = list(self.roster)
        total = len(ids) or len(self.states)
        counts = dict.fromkeys(("done", "failed", "skipped"), 0)
        for st in self.states.values():
            if st in counts:
                counts[st] += 1
        totals = " · ".join(
            f"{emoji} {counts[k]} {k}"
            for k, emoji in (("done", "✅"), ("failed", "❌"), ("skipped", "⏭"))
            if counts[k]
        )
        if self.finished:
            marker = STATE_MARKER.get(self.finished, "🏁")
            head = f"{marker} finished · {counts['done']}/{total} tasks done"
            return head + (f" · {totals}" if totals else "")
        if self.current and self.current in self.roster:
            idx = ids.index(self.current) + 1
            title = _one_line(self.roster[self.current], 80)
            head = f"⏳ task {idx}/{total} · **{title}**"
            if self.phase:
                head += f" · {self.phase}"
            rev = self.revisions.get(self.current, 0)
            if rev:
                head += f" · rev {rev}"
        elif total:
            head = f"⏳ {total} task(s) planned"
        else:
            head = "⏳ decomposing"
        return head + (f"\n{totals}" if totals else "")


# Task states are the engine's phase boundaries; a queued steer is answered
# when the current one ends, so this is what "how long until my steer lands"
# is measured against.
_STATE_PHASE = {
    "executing": "build",
    "verifying": "verify",
}
_PHASE_STATES = frozenset(_STATE_PHASE)


class SteerProgress:
    """Where the agent is *right now*, for the "⏳ queued" note under a steer.

    Field: a steer posted during a 15-minute execute phase looked stuck —
    the ⏳ reaction says "received", nothing said how long. This tracks the
    current task, its phase, and the tool calls made since the last
    checkpoint (plus the #228 ceiling, when configured) so the note can
    say "mid-**execute** on t2 (12/40 tool calls); answered at the next
    checkpoint" and the human can decide whether to wait or ``cancel``.
    """

    def __init__(self, cap: int | None = None) -> None:
        self.cap = cap or None
        self.task_id: str | None = None
        self.title: str | None = None
        self.phase: str | None = None
        self.tool_calls = 0
        self.capped = False
        self._dirty = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    def observe(self, event: Event) -> None:
        d = event.data
        t = event.type
        tid = str(d.get("task_id") or "")
        if t == "task.start" and tid:
            # The engine emits ``task.state=executing`` (and, on resume, the
            # persisted phase) *before* ``task.start``, so a start for the
            # task already being tracked only supplies the title — the
            # phase and counters observed for it stay put.
            if tid != self.task_id:
                self.phase, self.tool_calls, self.capped = None, 0, False
            self.task_id, self.title = tid, str(d.get("title") or "") or None
            self._dirty = True
        elif t == "task.state" and tid and d.get("state") in _PHASE_STATES:
            # Every state change is a checkpoint: the per-phase job (and its
            # tool-call ceiling) restarts, so the count restarts with it.
            self.task_id = tid
            self.phase = _STATE_PHASE[str(d["state"])]
            self.tool_calls, self.capped = 0, False
            self._dirty = True
        elif t == "task.end" and tid == self.task_id:
            self.task_id, self.title, self.phase = None, None, None
            self.tool_calls, self.capped = 0, False
            self._dirty = True
        elif t == "agent.tool_start":
            self.tool_calls += 1
            self._dirty = True
        elif t == "agent.tool_cap":
            self.capped = True
            if d.get("cap"):
                self.cap = int(d["cap"])
            self._dirty = True

    def render(self, *, state: str = "queued") -> str:
        """``state`` is ``queued`` (waiting for a checkpoint), ``answering``
        (the engine picked it up), ``answered``, ``failed`` or ``unanswered``
        (the run ended first)."""
        self._dirty = False
        if state == "answering":
            return "🧭 steer picked up — the agent is answering now"
        if state == "answered":
            return "✅ steer answered"
        if state == "failed":
            return "⚠ steer failed — see the error below"
        if state == "unanswered":
            return "⚠ steer not answered — the run ended first"
        where = ""
        if self.task_id and self.phase:
            where = f" — agent is mid-**{self.phase}** on {code(self.task_id)}"
        elif self.task_id:
            where = f" — agent is on {code(self.task_id)}"
        if where and self.title:
            where += f" · {_one_line(self.title, 60)}"
        if where and (self.tool_calls or self.capped):
            calls = f"{self.tool_calls}/{self.cap}" if self.cap else str(self.tool_calls)
            tail = " — ceiling reached)" if self.capped else " so far)"
            noun = "tool call" if self.tool_calls == 1 and not self.cap else "tool calls"
            where += f" ({calls} {noun}{tail}"
        return f"⏳ steer queued{where}; answered at the next checkpoint"


# -- per-event formatting -----------------------------------------------------------------


def format_for_discord(
    event: Event, *, level: str = "normal", max_chars: int = 1900
) -> list[Chunk]:
    """The Discord chunk(s) for one run event (empty = drop).

    Mirrors ``render_event`` (cli/tui.py) with Discord Markdown. Tool
    events are NOT rendered here — the pump feeds them to a ``ToolBatcher``
    (verbose: every call, batched into code blocks) or a ``ToolDigest``
    (normal: one summary line per burst, edited in place); the same goes
    for the task/phase events the ``StatusLine`` absorbs at the normal level.
    Agent messages arrive without their JSON payload
    (:func:`strip_json_payload`) — the channel gets the narration, not the
    engine's copy of what it already rendered.
    """
    if event.type in _TRANSCRIPT_SKIP:
        return []
    data = event.data
    t = event.type
    verbose = level == "verbose"
    if t == "agent.message":
        # The narration only: the structured phases' JSON payload is the
        # engine's copy, and the bridge renders what it means elsewhere.
        content = strip_json_payload(str(data.get("content") or ""))
        if not content:
            return []
        who = str(data.get("agent") or "agent")
        model = data.get("model")
        header = f"**{who}**" + (f" · {code(model)}" if model else "")
        cont = f"**{who}** *(cont. {{i}}/{{n}})*"
        return [
            block(part) for part in split_markdown(content, max_chars, header=header, cont=cont)
        ]
    if t == HostEventTypes.RUN_TASKS:
        return [block(part) for part in split_markdown(roster_text(data), max_chars)]
    if t == HostEventTypes.RUN_REPORT:
        return [line(f"📋 tracking issue {link(f'#{data.get("issue")}', data.get('url'))}")]
    if t == HostEventTypes.RUN_DELIVER:
        if data.get("created"):
            repo = str(data.get("repo") or "")
            return [line(f"📦 created repository {link(repo, f'https://github.com/{repo}')}")]
        if data.get("error"):
            return [line(f"⚠ **delivery failed:** {_one_line(data['error'], 300)}", flush=True)]
        if data.get("url"):
            label = f"#{data.get('pr')}" + (f" · {data['repo']}" if data.get("repo") else "")
            return [line(f"🔀 PR {link(label, data['url'])}", flush=True)]
        return []
    if t == "sandbox.workspace_clone":
        text = f"🌿 branch {code(data.get('branch'))} · clone of {code(data.get('source'))}"
        if data.get("reused"):
            text += " (reused)"
        return [line(text)]
    if t == HostEventTypes.CHAT_MESSAGE:
        quoted = _one_line(data.get("text") or "", 200)
        ack = (
            "💬 received — answered at the next checkpoint "
            "(may take a few minutes during a long step)"
        )
        return [line(f"> {quoted}\n{ack}" if quoted else ack, flush=True)]
    if t == HostEventTypes.CHAT_ACTION:
        return [
            line(
                f"↪ applied {code(data.get('action'))} — "
                f"{_one_line(data.get('guidance') or '', 300)}"
            )
        ]
    if t == HostEventTypes.CHAT_REPLY:
        if data.get("error"):
            return [line(f"⚠ **steering failed:** {_one_line(data['error'], 300)}", flush=True)]
        reply = str(data.get("reply") or "").strip()
        return [
            block(part)
            for part in split_markdown(
                reply, max_chars, header="🧭 **steering reply**", cont="🧭 *(cont. {i}/{n})*"
            )
        ]
    if t == "phase.end":
        status = str(data.get("status") or "")
        phase = str(data.get("phase") or "phase")
        tid = data.get("task_id")
        where = f" · task {code(tid)}" if tid else ""
        msg = _one_line(data.get("message") or "", 300)
        if status == "failed":
            return [line(f"✗ **{phase}**{where}" + (f" — {msg}" if msg else ""))]
        if status == "ok" and phase == "build" and msg:
            # The builder's report excerpt is the chronology's record of what
            # the attempt did — the plan card's replacement now that the
            # approach is narrated in prose instead of structured JSON.
            return [line(f"🔨 **build**{where} — {msg}")]
        if verbose and msg:
            return [line(f"· {phase}{where} — {msg}")]
        return []
    if t == "worker.error":
        msg = _one_line(
            data.get("message") or data.get("error") or data.get("error_type") or "", 600
        )
        return [block(f"🛑 **worker error:** {msg}")]
    if t == "agent.tool_cap":
        # The #228 ceiling tripped: the human should know the agent was
        # told to wrap up, since the digest line will stop growing.
        if level == "quiet":
            return []
        return [
            line(
                f"⛔ tool-call ceiling ({data.get('cap')}) reached — further calls are turned "
                "away; the agent was told to wrap up and report",
                flush=True,
            )
        ]
    if t == "agent.permission_denied":
        if level == "quiet":
            return []
        return [line(f"🚫 {code(data.get('kind'))}: {_one_line(data.get('feedback') or '', 200)}")]
    if t == "policy.deny":
        if level == "quiet":
            return []
        reason = _one_line(data.get("reason") or data.get("message") or "", 200)
        return [
            line(
                f"⛔ egress denied {code(data.get('domain'))}" + (f" ({reason})" if reason else "")
            )
        ]
    if t == "sandbox.tooling_warning":
        if level == "quiet":
            return []
        return [line(f"⚠ tooling: {_one_line(data.get('message') or '', 300)}")]
    if t == "task.state":
        if verbose:
            return [line(f"· task {data.get('task_id')} → {data.get('state')}")]
        return []
    if t in ("task.start", "task.end", "run.end", "run.state"):
        if not verbose:
            return []
        bits = [t]
        for key in ("task_id", "title", "state"):
            if data.get(key):
                bits.append(str(data[key]))
        return [line("· " + " ".join(bits))]
    if t.startswith(_LIFECYCLE_PREFIXES):
        if not verbose:
            return []
        return [line(f"· {t} {_one_line(' '.join(f'{k}={v}' for k, v in data.items()), 200)}")]
    return []


# -- what the structured phases decided ------------------------------------------------

# DECOMPOSE answers in JSON and decides the thing a human watching a thread
# most wants to read first: what the run will do. Its reply is not posted
# (see strip_json_payload), so the engine emits the parsed roster as its own
# event and this renders it. The build phase narrates in prose — its
# streamed messages and its phase.end report excerpt are the chronology.

TASK_STATE_MARKER = {"done": "✅", "failed": "❌", "skipped": "⏭"}


def _list(data: dict[str, Any], key: str) -> list[Any]:
    """``data[key]`` when it is a list — event data is agent-shaped, so a
    renderer never assumes a field arrived in the shape it was emitted in."""
    value = data.get(key)
    return value if isinstance(value, list) else []


def roster_text(data: dict[str, Any]) -> str:
    """The task roster the run will work, one line per task.

    Posted once when the graph is known (and again on resume, where each
    task carries the state it was left in).
    """
    tasks = _list(data, "tasks")
    lines = [f"🧩 **{len(tasks)} task(s)**"]
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            continue
        marker = TASK_STATE_MARKER.get(str(task.get("state") or ""), "")
        head = f"{index}. " + (f"{marker} " if marker else "")
        after = [str(d) for d in _list(task, "depends_on") if d]
        tail = f" — after {', '.join(code(d) for d in after)}" if after else ""
        lines.append(
            f"{head}{code(task.get('id'))} {_one_line(task.get('title') or '', 120)}{tail}"
        )
    return "\n".join(lines)


# -- headline / finish / status cards --------------------------------------------------


def _origin(item: WorkItem) -> tuple[str, str]:
    """(label, kind) for where a work item came from."""
    if item.source == "github":
        prefix = "audit" if item.kind == "audit" else "issue"
        return f"{prefix} #{item.source_key}", "github"
    return f"inbox {code(item.source_key)}", "inbox"


def headline_text(item: WorkItem, run_id: str, state: str | None = None) -> str:
    """The headline's content= text (notification preview / embed fallback)."""
    origin, _ = _origin(item)
    origin = link(origin, item.url) if item.url else origin
    marker = STATE_MARKER.get(state or "", "▶")
    return f"{marker} run {code(run_id)} — **{_one_line(item.title, 120)}** · {origin}"


def headline_embed(
    item: WorkItem,
    run_id: str,
    state: str | None = None,
    *,
    branch: str | None = None,
    tracking: tuple[int, str] | None = None,
    pr: tuple[int, str] | None = None,
    summary: str | None = None,
    hostname: str | None = None,
) -> EmbedSpec:
    origin, _kind = _origin(item)
    fields: list[tuple[str, str, bool]] = [
        ("Source", link(origin, item.url) if item.url else origin, True),
        ("Run", code(run_id), True),
        ("State", state or "running", True),
    ]
    if branch:
        fields.append(("Branch", code(branch), True))
    if tracking:
        fields.append(("Tracking issue", link(f"#{tracking[0]}", tracking[1]), True))
    if pr:
        fields.append(("PR", link(f"#{pr[0]}", pr[1]), True))
    if summary:
        fields.append(("Tasks", summary, True))
    host = hostname if hostname is not None else socket.gethostname()
    return EmbedSpec(
        title=_one_line(item.title, 200),
        url=item.url or None,
        color=STATE_COLOR.get(state or "", COLOR_RUNNING),
        fields=tuple(fields),
        footer=f"sbxloop · {host}",
    ).clamped()


def finish_text(state: str, report: RunReport) -> str:
    text = f"**finished: {state}** — {report.task_summary}"
    if report.cancelled_by:
        text += f" · {_cancel_note(item_id=None, report=report)}"
    return text


def _cancel_note(item_id: str | None, report: RunReport) -> str:
    """A cancel is not a failure: say who, and that the work is not lost."""
    note = f"cancelled by {report.cancelled_by}"
    if report.requeued:
        return note + " — re-queued; a fresh run starts on the next tick"
    note += f" — {code(f'sbxloop resume {report.run_id}')} continues the run"
    if item_id:
        note += f"; `!sbx retry {item_id}` reruns it fresh"
    return note


def finish_embed(
    item: WorkItem,
    report: RunReport,
    state: str,
    unanswered: int = 0,
    *,
    repo: str | None = None,
) -> EmbedSpec:
    fields: list[tuple[str, str, bool]] = []
    if report.cancelled_by:
        fields.append(("Cancelled", _cancel_note(item.item_id, report), False))
    if report.tracking_issue:
        fields.append(
            ("Tracking issue", link(f"#{report.tracking_issue[0]}", report.tracking_issue[1]), True)
        )
    if report.delivery:
        fields.append(("PR", link(f"#{report.delivery[0]}", report.delivery[1]), True))
    # What the run filed is an audit's deliverable (and a patch run's side
    # findings): show it where the PR is shown.
    if report.review is not None:
        # A review's deliverable is the review, not filed issues (#469).
        fields.append(("Review", _review_field(report.review), False))
    elif report.filed:
        fields.append(("Filed", refs_text(report.filed, repo), True))
    elif item.kind == "audit":
        fields.append(("Filed", "no findings", True))
    if report.review is not None and report.filed:
        fields.append(("Filed", refs_text(report.filed, repo), True))
    if report.tool_filed:
        fields.append(("Upstream", refs_text(report.tool_filed, repo), True))
    if report.tool_noted:
        fields.append(("Noted", _noted_note(len(report.tool_noted)), False))
    if report.delivery_error:
        fields.append(("Delivery", f"⚠ {_one_line(report.delivery_error, 600)}", False))
    if unanswered:
        fields.append(
            ("Steering", f"⚠ {unanswered} message(s) were not answered before the run ended", False)
        )
    marker = STATE_MARKER.get(state, "🏁")
    return EmbedSpec(
        title=f"{marker} finished: {state}",
        description=report.task_summary,
        color=STATE_COLOR.get(state, COLOR_DIM),
        fields=tuple(fields),
        footer=f"run {report.run_id} · {_one_line(item.title, 80)}",
    ).clamped()


# -- end-of-run summary --------------------------------------------------------------


class RunStats:
    """Counters behind the end-of-run summary card, fed the same live event
    stream the chronology renders.

    Observed rather than mined from the state store: the pump already sees
    every event, and the summary must stay postable when the engine store is
    not reachable from the bridge. The price is scope: a daemon restarted
    mid-run starts counting again, so ``resumed`` is remembered and the card
    says the stats cover the watched leg instead of passing partial numbers
    off as the whole run.
    """

    def __init__(self) -> None:
        self.first_ts: float | None = None
        self.last_ts: float | None = None
        self.resumed = False
        # One ``agent.usage`` event is one assistant turn — the unit runs
        # are billed and timed by. Token/cost fields stay None until a
        # backend actually reports them: "not reported" is not zero.
        self.turns = 0
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.cost: float | None = None
        self.tool_calls = 0
        self.capped = False
        self.denies = 0
        self.resource_warnings = 0
        self.steers = 0
        self.steers_answered = 0
        self.steers_failed = 0
        # (task_id, phase, status, reason) for every verify failure — the
        # rework signal now that no critic issues verdicts in-run.
        self.rework: list[tuple[str, str, str, str]] = []
        self.total_tasks = 0
        self.states: dict[str, str] = {}
        self.revisions: dict[str, int] = {}

    def observe(self, event: Event) -> None:
        if self.first_ts is None:
            self.first_ts = event.ts
        self.last_ts = event.ts
        d = event.data
        t = event.type
        if t == HostEventTypes.RUN_START and d.get("resumed"):
            self.resumed = True
        elif t == "agent.usage":
            self.turns += 1
            if d.get("input_tokens") is not None:
                self.input_tokens = (self.input_tokens or 0) + int(d["input_tokens"])
            if d.get("output_tokens") is not None:
                self.output_tokens = (self.output_tokens or 0) + int(d["output_tokens"])
            if d.get("cost") is not None:
                self.cost = (self.cost or 0.0) + float(d["cost"])
        elif t == "agent.tool_start":
            self.tool_calls += 1
        elif t == "agent.tool_cap":
            self.capped = True
        elif t == HostEventTypes.POLICY_DENY:
            self.denies += 1
        elif t == "sandbox.resources_warning":
            self.resource_warnings += 1
        elif t == HostEventTypes.CHAT_MESSAGE:
            self.steers += 1
        elif t == HostEventTypes.CHAT_REPLY:
            if d.get("error"):
                self.steers_failed += 1
            else:
                self.steers_answered += 1
        elif t == HostEventTypes.RUN_TASKS and isinstance(d.get("tasks"), list):
            self.total_tasks = max(self.total_tasks, len(d["tasks"]))
        elif t in (HostEventTypes.TASK_STATE, HostEventTypes.TASK_END):
            tid = str(d.get("task_id") or "")
            if tid and d.get("state"):
                self.states[tid] = str(d["state"])
            if tid and d.get("revisions") is not None:
                self.revisions[tid] = int(d["revisions"] or 0)
        elif t == HostEventTypes.PHASE_END:
            phase = str(d.get("phase") or "?")
            if phase == "verify" and str(d.get("status") or "") == "failed":
                self.rework.append(
                    (str(d.get("task_id") or "?"), phase, "failed", str(d.get("message") or ""))
                )

    @property
    def duration_s(self) -> float | None:
        if self.first_ts is None or self.last_ts is None:
            return None
        return max(0.0, self.last_ts - self.first_ts)

    def task_counts(self) -> tuple[int, int]:
        """(done, total) as observed; the roster event pins the total."""
        done = sum(1 for s in self.states.values() if s == "done")
        return done, max(self.total_tasks, len(self.states))


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"


def _fmt_count(value: int | None) -> str:
    return f"{value:,}" if value is not None else "—"


def summary_text(stats: RunStats | None, state: str) -> str:
    """The plain-text lead over the summary embed: the headline numbers, so
    the card still reads where embeds are suppressed."""
    head = "📊 **run summary**"
    if stats is None:
        return head
    bits: list[str] = []
    if (dur := stats.duration_s) is not None:
        bits.append(_fmt_duration(dur))
    if stats.turns:
        bits.append(f"{stats.turns} turn(s)")
    if stats.tool_calls:
        bits.append(f"{stats.tool_calls} tool call(s)")
    if stats.input_tokens is not None or stats.output_tokens is not None:
        bits.append(
            f"{_fmt_count(stats.input_tokens)} in / {_fmt_count(stats.output_tokens)} out tokens"
        )
    if stats.cost is not None:
        bits.append(f"${stats.cost:,.2f}")
    return head + (f" — {' · '.join(bits)}" if bits else "")


def _stat_rows(stats: RunStats) -> list[str]:
    rows: list[str] = []
    counters = []
    if stats.turns:
        counters.append(f"turns {stats.turns}")
    if stats.tool_calls:
        counters.append(f"tool calls {stats.tool_calls}")
    if counters:
        rows.append(" · ".join(counters))
    if stats.input_tokens is not None or stats.output_tokens is not None:
        spend = (
            f"tokens {_fmt_count(stats.input_tokens)} in / {_fmt_count(stats.output_tokens)} out"
        )
        if stats.cost is not None:
            spend += f" · cost ${stats.cost:,.2f}"
        rows.append(spend)
    elif stats.cost is not None:
        rows.append(f"cost ${stats.cost:,.2f}")
    if stats.steers:
        rows.append(f"steering {stats.steers} asked / {stats.steers_answered} answered")
    return rows


def _went_well(stats: RunStats, report: RunReport) -> list[str]:
    out: list[str] = []
    if report.delivery:
        out.append(f"delivered PR {link(f'#{report.delivery[0]}', report.delivery[1])}")
    if report.filed:
        out.append(f"filed {len(report.filed)} finding(s)")
    done, total = stats.task_counts()
    if total and done == total:
        out.append(f"all {total} task(s) completed")
    elif done:
        out.append(f"{done}/{total} task(s) completed")
    clean = sum(
        1 for tid, s in stats.states.items() if s == "done" and not stats.revisions.get(tid)
    )
    if clean:
        out.append(f"{clean} task(s) verified without revision")
    if stats.steers and stats.steers_answered == stats.steers:
        out.append(f"answered all {stats.steers} steering message(s)")
    return out


def _needed_work(stats: RunStats, report: RunReport, unanswered: int) -> list[str]:
    out: list[str] = []
    for tid, phase, verdict, reason in stats.rework[:4]:
        line = f"{code(tid)} {phase}: **{verdict}**"
        if reason:
            line += f" — {_one_line(reason, 160)}"
        out.append(line)
    if len(stats.rework) > 4:
        out.append(f"… and {len(stats.rework) - 4} more verify failure(s)")
    failed = sum(1 for s in stats.states.values() if s == "failed")
    if failed:
        out.append(f"{failed} task(s) failed")
    if report.delivery_error:
        out.append(f"delivery failed — {_one_line(report.delivery_error, 160)}")
    if unanswered:
        out.append(f"{unanswered} steering message(s) went unanswered")
    if stats.steers_failed:
        out.append(f"{stats.steers_failed} steer(s) errored")
    if stats.capped:
        out.append("hit the per-phase tool-call ceiling")
    if stats.denies:
        out.append(f"{stats.denies} policy denial(s)")
    if stats.resource_warnings:
        out.append(f"{stats.resource_warnings} sandbox resource warning(s)")
    return out


def _bullets(lines: list[str], empty: str) -> str:
    return "\n".join(f"• {line}" for line in lines) if lines else empty


def summary_embed(
    stats: RunStats | None,
    report: RunReport,
    state: str,
    unanswered: int = 0,
) -> EmbedSpec:
    """The end-of-run summary card — the last thing posted in a run thread:
    the run's numbers, what went well, and what needed work."""
    stats = stats or RunStats()
    description = f"**{state}** — {report.task_summary}"
    if (dur := stats.duration_s) is not None:
        description += f" in {_fmt_duration(dur)}"
    if stats.resumed:
        description += "\n_stats cover the run since the daemon last picked it up_"
    fields: list[tuple[str, str, bool]] = []
    if rows := _stat_rows(stats):
        fields.append(("Stats", "\n".join(rows), False))
    fields.append(("Went well", _bullets(_went_well(stats, report), "nothing stood out"), False))
    fields.append(
        (
            "Needed work",
            _bullets(_needed_work(stats, report, unanswered), "no setbacks observed"),
            False,
        )
    )
    return EmbedSpec(
        title="📊 run summary",
        description=description,
        color=STATE_COLOR.get(state, COLOR_DIM),
        fields=tuple(fields),
        footer=f"run {report.run_id}",
    ).clamped()


def status_embed(status: dict[str, Any]) -> EmbedSpec:
    cur = status.get("current")
    current = f"{code(cur['run_id'])} — {_one_line(cur.get('title') or '', 120)}" if cur else "idle"
    breaker = "open" if status.get("breaker_open") else "closed"
    resumes = status.get("resumes_today", 0)
    tz = status.get("run_cap_timezone", "UTC")
    fields = (
        ("Current", current, False),
        ("Queued", str(status.get("queued", 0)), True),
        (
            f"Runs today ({tz})",
            f"{status.get('runs_today', 0)}/{status.get('max_runs_per_day', '?')}"
            f" · resets 00:00 {tz}" + (f" ({resumes} resumed)" if resumes else ""),
            True,
        ),
        ("Breaker", breaker, True),
        ("Paused", "yes" if status.get("paused") else "no", True),
    )
    color = (
        COLOR_FAIL
        if status.get("breaker_open")
        else (COLOR_WARN if status.get("paused") else COLOR_OK)
    )
    return EmbedSpec(title="sbxloop daemon", color=color, fields=fields).clamped()


def queue_lines(items: list[WorkItem], limit: int = 15) -> str:
    if not items:
        return "queue is empty."
    rows = [
        f"• {code(i.item_id)} "
        + ("🔎 audit · " if i.kind == "audit" else "")
        + (link(_one_line(i.title, 80), i.url) if i.url else _one_line(i.title, 80))
        for i in items[:limit]
    ]
    if len(items) > limit:
        rows.append(f"… and {len(items) - limit} more")
    return "\n".join(rows)


ITEM_STATE_MARKER = {
    "queued": "⏳",
    "running": "▶",
    "done": "✅",
    "abandoned": "❌",
    "failed": "❌",
}


def items_lines(items: list[WorkItem], limit: int = 20) -> str:
    """One row per work item: state, id, title, attempts, pinned run, last
    error — what an operator needs to decide between abandon/retry/requeue."""
    if not items:
        return "no work items."
    rows = []
    for i in items[:limit]:
        row = f"{ITEM_STATE_MARKER.get(i.state, '•')} {code(i.item_id)} {i.state} · "
        if i.kind == "audit":
            row += "🔎 audit · "
        row += link(_one_line(i.title, 60), i.url) if i.url else _one_line(i.title, 60)
        row += f" · attempts {i.attempts}"
        if i.run_id:
            row += f" · run {code(i.run_id)}"
        if i.last_error:
            row += f" · {_one_line(i.last_error, 80)}"
        rows.append(row)
    if len(items) > limit:
        rows.append(f"… and {len(items) - limit} more")
    return "\n".join(rows)


def daemon_notice(text: str, *, thread_id: int | None = None) -> str:
    """A control-channel notice: URLs masked, optional pointer to the run's thread."""
    out = mask_urls(str(text))
    if thread_id:
        out += f" · <#{thread_id}>"
    return out


# -- filed refs (audits, reviews, post-mortems, backlog) -----------------------------

_GH_REF_RE = re.compile(r"^gh:(\d+)$")
_UPSTREAM_REF_RE = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


def issue_url(ref: str, repo: str | None = None) -> str | None:
    """The GitHub URL behind a filed ref: ``gh:12`` needs ``repo``; an
    upstream ``owner/name#5`` ref carries its own; anything else has none."""
    if (m := _GH_REF_RE.match(ref)) and repo:
        return f"https://github.com/{repo}/issues/{m.group(1)}"
    if m := _UPSTREAM_REF_RE.match(ref):
        return f"https://github.com/{m.group(1)}/issues/{m.group(2)}"
    return None


def _ref_label(ref: str) -> str | None:
    if m := _GH_REF_RE.match(ref):
        return f"#{m.group(1)}"
    if _UPSTREAM_REF_RE.match(ref):
        return ref
    return None


def ref_link(ref: str, repo: str | None = None) -> str:
    """A filed ref as Discord text: a masked link when the URL is known,
    ``#12`` when the repo is not, inline code for anything else."""
    label = _ref_label(ref)
    if label is None:
        return code(ref)
    return link(label, issue_url(ref, repo))


def _ref_plain(ref: str, repo: str | None = None) -> str:
    """``#12 <url>`` — the no-unfurl form for text fallbacks."""
    label = _ref_label(ref)
    if label is None:
        return code(ref)
    url = issue_url(ref, repo)
    return f"{label} {nolink(url)}" if url else label


def refs_text(refs: Sequence[str], repo: str | None = None, *, limit: int = 6) -> str:
    """Filed refs as a comma list (a list inside one field, not fields)."""
    shown = [ref_link(r, repo) for r in refs[:limit]]
    if len(refs) > limit:
        shown.append(f"… +{len(refs) - limit}")
    return ", ".join(shown)


def _noted_note(n: int) -> str:
    return (
        f"{n} finding(s) about sbxloop noted, not filed — "
        "set `[daemon] tool_repo` to route them upstream"
    )


def filed_notice(
    kind: str, ref: str, *, repo: str | None = None, target: str = "", detail: str = ""
) -> str:
    """Control-channel notice for a charter the daemon opened: ``🔎 audit
    [#701](…) filed for charter `x` · audit: x``."""
    text = f"🔎 {kind} {ref_link(ref, repo)} filed"
    if target:
        text += f" for {target}"
    if detail:
        text += f" · {_one_line(detail, 80)}"
    return text


def _review_verdict(review: ReviewOutcome) -> str:
    return "approved" if review.approved else "requested changes"


def _review_comments(n: int) -> str:
    return "no comments" if n == 0 else f"{n} inline comment(s)"


def _non_gating_note(review: ReviewOutcome) -> str:
    """The operator needs to know a `request_changes` review does not block
    the merge, which is only true when GitHub refused the requested event."""
    return (
        f"⚠ posted as a non-gating {code(review.posted_event)} — "
        f"{code(review.requested_event)} was refused, so nothing on the PR blocks the merge"
    )


def review_summary(review: ReviewOutcome) -> str:
    """The ``·``-joinable tail describing a posted review."""
    parts = [f"review {_review_verdict(review)}", _review_comments(review.comments)]
    if review.url:
        parts.append(nolink(review.url))
    if not review.gates_merge:
        parts.append(_non_gating_note(review))
    return " · ".join(parts)


def _review_field(review: ReviewOutcome) -> str:
    head = f"{_review_verdict(review)} · {_review_comments(review.comments)}"
    text = link(head, review.url) if review.url else head
    if not review.gates_merge:
        text += f"\n{_non_gating_note(review)}"
    return text


def findings_summary(report: RunReport, *, repo: str | None = None, kind: str = "patch") -> str:
    """The ``·``-joinable tail saying what a run filed; empty when a patch
    run filed nothing, ``no findings`` when an audit did. A review run
    reports its review instead — its deliverable is never a filed issue."""
    parts = []
    if report.review is not None:
        parts.append(review_summary(report.review))
    if report.filed:
        parts.append(f"filed {refs_text(report.filed, repo)}")
    if report.tool_filed:
        parts.append(f"upstream {refs_text(report.tool_filed, repo)}")
    if report.tool_noted:
        parts.append(
            f"noted {len(report.tool_noted)} finding(s) about sbxloop — "
            "set `[daemon] tool_repo` to file them upstream"
        )
    if not parts and kind == "audit" and report.review is None:
        return "no findings"
    return " · ".join(parts)


def filed_lines(report: RunReport, *, repo: str | None = None) -> list[str]:
    """Finish-text fallback lines, in the ``🔀 PR #34 <url>`` style."""
    out = []
    if report.filed:
        out.append("🔎 filed " + ", ".join(_ref_plain(r, repo) for r in report.filed))
    if report.tool_filed:
        out.append("🔎 filed upstream " + ", ".join(_ref_plain(r, repo) for r in report.tool_filed))
    if report.tool_noted:
        out.append(f"⚠ {_noted_note(len(report.tool_noted))}")
    return out


def charter_skipped_notice(problem: str, audit_dir: object) -> str:
    return (
        f"⚠ audit charter skipped: {_one_line(problem, 200)}"
        f" · fix or remove it under {code(audit_dir)}"
    )


__all__ = [
    "COLOR_FAIL",
    "COLOR_OK",
    "COLOR_RUNNING",
    "COLOR_WARN",
    "DISCORD_MAX_MESSAGE",
    "TOOL_EXCERPT_LINE_CLIP",
    "TOOL_EXCERPT_MAX_CHARS",
    "TOOL_FAIL_OUTPUT_LINES_DEFAULT",
    "TOOL_OUTPUT_LINES_DEFAULT",
    "Chunk",
    "EmbedSpec",
    "RunStats",
    "StatusLine",
    "ToolBatcher",
    "block",
    "charter_skipped_notice",
    "code",
    "daemon_notice",
    "filed_lines",
    "filed_notice",
    "findings_summary",
    "finish_embed",
    "finish_text",
    "format_for_discord",
    "headline_embed",
    "headline_text",
    "issue_url",
    "items_lines",
    "line",
    "link",
    "mask_urls",
    "nolink",
    "output_excerpt",
    "queue_lines",
    "ref_link",
    "refs_text",
    "roster_text",
    "split_markdown",
    "status_embed",
    "strip_json_payload",
    "summary_embed",
    "summary_text",
]
