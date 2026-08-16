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
"""

from __future__ import annotations

import re
import socket
from collections import deque
from dataclasses import dataclass
from typing import Any

from sbxloop.cli.tui import (
    _LIFECYCLE_PREFIXES,
    _TRANSCRIPT_SKIP,
    TOOL_ARGS_LINE_CLIP,
    TOOL_FAIL_TAIL_LINES,
)
from sbxloop.cli.tui import _one_line as _one_line_mid
from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.events import Event, HostEventTypes

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


# -- tool batching --------------------------------------------------------------------------


def failure_detail(tool: str, exit_code: int | None, detail: str) -> Chunk:
    """The block a failed tool call gets to itself: marker line plus the
    last few output lines, so the ✗ and its explanation sit together."""
    tail = [ln for ln in str(detail or "").strip().splitlines() if ln.strip()][
        -TOOL_FAIL_TAIL_LINES:
    ]
    head = f"✗ {code(tool)} failed" + (f" (exit {exit_code})" if exit_code is not None else "")
    if not tail:
        return block(head)
    body = "\n".join(_clip(ln.rstrip(), 300).replace("```", "'''") for ln in tail)
    return block(f"{head}\n```text\n{body}\n```")


class ToolBatcher:
    """Collects consecutive tool calls into one fenced block.

    ``add_start``/``add_end`` feed it; ``flush()`` renders the block. A
    failed call marks its own line with ``✗ exit N`` and yields a detail
    chunk (last few output lines) that the pump sends right after the
    batch, so the marker and its explanation sit together.
    """

    def __init__(self, *, max_lines: int = 8, quiet: bool = False) -> None:
        self.max_lines = max_lines
        self.quiet = quiet
        self._lines: list[str] = []
        self._by_call: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._lines)

    @property
    def full(self) -> bool:
        return len(self._lines) >= self.max_lines

    def add_start(self, tool: str, args: str, call_id: str | None) -> None:
        if self.quiet:
            return
        text = f"$ {tool}"
        args_line = _one_line_mid(str(args or ""), TOOL_ARGS_LINE_CLIP - 40)
        if args_line:
            text += f"  {args_line}"
        if call_id:
            self._by_call[str(call_id)] = len(self._lines)
        self._lines.append(text.replace("```", "'''"))

    def add_end(
        self,
        tool: str,
        call_id: str | None,
        *,
        success: bool | None,
        exit_code: int | None,
        detail: str,
    ) -> Chunk | None:
        if success is not False:
            return None
        marker = f"   ✗ exit {exit_code}" if exit_code is not None else "   ✗ failed"
        idx = self._by_call.get(str(call_id)) if call_id else None
        if idx is not None and idx < len(self._lines):
            self._lines[idx] += marker
        elif not self.quiet:
            self._lines.append(f"$ {tool}{marker}")
        return failure_detail(tool, exit_code, detail)

    def flush(self) -> Chunk | None:
        if not self._lines:
            return None
        body = "\n".join(self._lines)
        self._lines = []
        self._by_call = {}
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

    def __init__(self, *, cancel_hint: str = "!sbx cancel") -> None:
        self.cancel_hint = cancel_hint
        self.count = 0
        self.failed = 0
        self.by_tool: dict[str, int] = {}
        self.last: tuple[str, str] | None = None
        self._repetition = RepetitionDetector()
        self._dirty = False

    def __len__(self) -> int:
        return self.count

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def repetitive(self) -> int:
        return self._repetition.streak

    def add_start(self, tool: str, args: str) -> None:
        self.count += 1
        self.by_tool[tool] = self.by_tool.get(tool, 0) + 1
        self.last = (tool, " ".join(str(args or "").split()))
        # Fed incrementally so a 200-call spiral still reports a 200-long
        # streak (a bounded tail would cap it and never collapse the line).
        self._repetition.add(*self.last)
        self._dirty = True

    def add_end(
        self,
        tool: str,
        *,
        success: bool | None,
        exit_code: int | None,
        detail: str,
    ) -> Chunk | None:
        """A failed call is the one thing that stays individual: returns
        its detail chunk (as the batcher would) and counts it in the line."""
        if success is not False:
            return None
        self.failed += 1
        self._dirty = True
        return failure_detail(tool, exit_code, detail)

    def render(self) -> str:
        self._dirty = False
        if not self.count:
            return ""
        streak = self.repetitive
        last_tool, last_args = self.last or ("?", "")
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
            head = "⏳ planning"
        return head + (f"\n{totals}" if totals else "")


# Task states are the engine's phase boundaries; a queued steer is answered
# when the current one ends, so this is what "how long until my steer lands"
# is measured against.
_STATE_PHASE = {
    "planning": "plan",
    "executing": "execute",
    "scrutinizing": "scrutinize",
    "verifying": "verify",
    "validating": "validate",
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
            self.task_id, self.title = tid, str(d.get("title") or "") or None
            self.phase, self.tool_calls, self.capped = None, 0, False
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
            return "⚠ steer failed — see the reply above"
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
            where += f" ({calls} tool calls{tail}"
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
    """
    if event.type in _TRANSCRIPT_SKIP:
        return []
    data = event.data
    t = event.type
    verbose = level == "verbose"
    if t == "agent.message":
        content = str(data.get("content") or "").strip()
        if not content:
            return []
        who = str(data.get("agent") or "agent")
        model = data.get("model")
        header = f"**{who}**" + (f" · {code(model)}" if model else "")
        cont = f"**{who}** *(cont. {{i}}/{{n}})*"
        return [
            block(part) for part in split_markdown(content, max_chars, header=header, cont=cont)
        ]
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
        if status == "degraded":
            return [line(f"⚠ **{phase} degraded**{where}" + (f" — {msg}" if msg else ""))]
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


# -- headline / finish / status cards --------------------------------------------------


def _origin(item: WorkItem) -> tuple[str, str]:
    """(label, kind) for where a work item came from."""
    if item.source == "github":
        return f"issue #{item.source_key}", "github"
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


def finish_embed(item: WorkItem, report: RunReport, state: str, unanswered: int = 0) -> EmbedSpec:
    fields: list[tuple[str, str, bool]] = []
    if report.cancelled_by:
        fields.append(("Cancelled", _cancel_note(item.item_id, report), False))
    if report.tracking_issue:
        fields.append(
            ("Tracking issue", link(f"#{report.tracking_issue[0]}", report.tracking_issue[1]), True)
        )
    if report.delivery:
        fields.append(("PR", link(f"#{report.delivery[0]}", report.delivery[1]), True))
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


def status_embed(status: dict[str, Any]) -> EmbedSpec:
    cur = status.get("current")
    current = f"{code(cur['run_id'])} — {_one_line(cur.get('title') or '', 120)}" if cur else "idle"
    breaker = "open" if status.get("breaker_open") else "closed"
    resumes = status.get("resumes_today", 0)
    fields = (
        ("Current", current, False),
        ("Queued", str(status.get("queued", 0)), True),
        (
            "Runs today",
            f"{status.get('runs_today', 0)}/{status.get('max_runs_per_day', '?')}"
            + (f" ({resumes} resumed)" if resumes else ""),
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


__all__ = [
    "COLOR_FAIL",
    "COLOR_OK",
    "COLOR_RUNNING",
    "COLOR_WARN",
    "DISCORD_MAX_MESSAGE",
    "Chunk",
    "EmbedSpec",
    "StatusLine",
    "ToolBatcher",
    "block",
    "code",
    "daemon_notice",
    "finish_embed",
    "finish_text",
    "format_for_discord",
    "headline_embed",
    "headline_text",
    "items_lines",
    "line",
    "link",
    "mask_urls",
    "nolink",
    "queue_lines",
    "split_markdown",
    "status_embed",
]
