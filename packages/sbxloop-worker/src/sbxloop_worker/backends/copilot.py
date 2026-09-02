"""GitHub Copilot SDK backend.

Runs one agentic session inside the sandbox via the ``github-copilot-sdk``
package (installed with the ``[copilot]`` extra; wheels bundle the Copilot
CLI runtime). Imports are deferred so the worker package works without the
SDK for shell/github jobs and for test environments.

Auth: the SDK auto-detects ``COPILOT_GITHUB_TOKEN``, which the sbxloop
provisioner injects into the agent sandbox (proxy-bound to the Copilot API
hosts under the default secret strategy).

This module is exercised by the real-sbx e2e workflow rather than unit
tests (it needs the SDK runtime + a Copilot subscription), and is excluded
from unit coverage accordingly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from collections import Counter
from typing import Any, get_args

from sbxloop_worker._json import extract_json
from sbxloop_worker.backends import BackendResult, BackendUnavailableError, EmitFn
from sbxloop_worker.hosttools import HostToolTimeout, request_tool, safe_call_id
from sbxloop_worker.protocol import (
    EventTypes,
    HostToolCall,
    HostToolSpec,
    JobRequest,
    SessionHealth,
    Usage,
)
from sbxloop_worker.secrets import looks_like_github_token, redact_secrets

# The SDK's permission-request ``kind`` vocabulary, field-verified against
# github-copilot-sdk 1.0.9 (2026-08-13): ``copilot.session.PermissionRequest``
# is a union of request dataclasses, each carrying its wire discriminator as
# a ``kind`` ClassVar (the same strings the SDK's ``_load_PermissionRequest``
# switch matches on; 1.0.9 added ``factory`` — subagent fan-out — and every
# request class carries a ``tool_call_id``). ``sbxloop doctor`` compares the
# installed SDK against this snapshot so vocabulary drift on an SDK bump is
# surfaced there instead of as a silently degraded critic in the field.
SDK_PERMISSION_KINDS = frozenset(
    {
        "shell",
        "factory",
        "write",
        "read",
        "mcp",
        "url",
        "memory",
        "custom-tool",
        "hook",
        "extension-management",
        "extension-permission-access",
    }
)

# The read-only critic barrier (SCRUTINIZE/VALIDATE sessions) is an allowlist
# with default-deny: an unknown or novel kind fails closed, so the worst case
# is the critic losing a read capability and saying so — never the critic
# silently editing the work under review. ``read`` (workspace files) and
# ``url`` (web fetches, which cannot mutate the workspace and stay inside the
# sandbox network policy) are true reads. ``shell`` is allowed so the critic
# can run inspection commands (ls, grep, interpreter version probes); this
# trades away the hard mutation guarantee — a shell can edit the workspace —
# and relies on the session prompt to keep the critic hands-off. Everything
# else mutates state (write, memory, extension-management) or has unbounded
# effect (mcp, custom-tool, hook, extension-permission-access).
READ_ONLY_ALLOWED_KINDS = frozenset({"read", "url", "shell"})


# -- bundled-ripgrep page-size guard (issue #122) ----------------------------
# The Copilot CLI's glob/grep tools spawn a bundled ripgrep that is a
# musl-static, jemalloc-linked build compiled for 4 KiB pages (verified
# against @github/copilot 1.0.73: the binary carries jemalloc's "Unsupported
# system page size" abort string). On guests with a larger page size (16 KiB
# is common for Apple-silicon microVMs) that binary aborts at startup, so
# the agent silently loses its search tools — fatal for the read-only critic,
# which cannot fall back to shell. The CLI documents USE_BUILTIN_RIPGREP:
# when set to exactly "false" it spawns `rg` from PATH instead of the
# bundled binary (same release, `process.env.USE_BUILTIN_RIPGREP==="false"`).
EXPECTED_PAGE_SIZE = 4096
RIPGREP_ENV = "USE_BUILTIN_RIPGREP"


def ripgrep_page_size_plan(
    page_size: int, system_rg: str | None, current: str | None
) -> tuple[dict[str, str], str | None]:
    """Env updates + warning for the bundled-ripgrep page-size hazard.

    Pure decision logic so the polarity is unit-testable: on a 4 KiB guest
    (or when the operator already set ``USE_BUILTIN_RIPGREP``) nothing
    changes; on a larger page size the plan reroutes glob/grep to the
    system ripgrep when one exists, and warns that search tooling is lost
    when none does.
    """
    if page_size == EXPECTED_PAGE_SIZE or current is not None:
        return {}, None
    if system_rg:
        return {RIPGREP_ENV: "false"}, (
            f"guest page size is {page_size} (not {EXPECTED_PAGE_SIZE}): the Copilot "
            "CLI's bundled ripgrep would abort (jemalloc 'Unsupported system page "
            f"size'); rerouting glob/grep to the system ripgrep at {system_rg} "
            f"via {RIPGREP_ENV}=false"
        )
    return {}, (
        f"guest page size is {page_size} (not {EXPECTED_PAGE_SIZE}) and no system "
        "ripgrep is on PATH: the agent's glob/grep tools will fail (jemalloc "
        "'Unsupported system page size'); install ripgrep in the sandbox "
        "(sudo apt-get install ripgrep) to restore them"
    )


def read_only_denial(request: Any) -> str | None:
    """Rejection feedback for ``request`` in a read-only session, or None to allow.

    Pure decision logic (no SDK types) so the default-deny polarity is
    unit-testable with stub request objects.
    """
    kind = getattr(request, "kind", None)
    if isinstance(kind, str) and kind in READ_ONLY_ALLOWED_KINDS:
        return None
    return (
        f"this is a read-only review session; permission kind {kind!r} is not "
        "in the read allowlist — do not modify anything, report findings instead"
    )


def installed_sdk_permission_kinds() -> frozenset[str] | None:
    """The installed SDK's permission-request ``kind`` vocabulary, or None.

    Introspects ``copilot.session.PermissionRequest`` — a union of request
    dataclasses each carrying its wire ``kind`` as a ClassVar. Returns None
    when github-copilot-sdk is not installed. Doctor compares the result
    against :data:`SDK_PERMISSION_KINDS` to catch vocabulary drift on SDK
    bumps before it degrades the read-only critic in the field.
    """
    try:
        from copilot.session import PermissionRequest
    except ImportError:
        return None
    kinds: set[str] = set()
    for member in get_args(PermissionRequest):
        kind = getattr(member, "kind", None)
        if isinstance(kind, str):
            kinds.add(kind)
    return frozenset(kinds)


def _arguments_dict(raw: Any) -> dict[str, Any]:
    """Tool arguments as a plain dict: the SDK hands over parsed JSON, but a
    JSON string or None must not crash the relay."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"input": raw}
        return parsed if isinstance(parsed, dict) else {"input": parsed}
    return {}


def _auth_diagnostic() -> str:
    """Describe the COPILOT_GITHUB_TOKEN this process can actually see."""
    token = os.environ.get("COPILOT_GITHUB_TOKEN", "")
    if not token:
        return (
            "auth diagnostic: COPILOT_GITHUB_TOKEN is NOT set in the worker "
            "environment - sbx secret injection did not reach this process; "
            'try secret_strategy="plain-env"'
        )
    if looks_like_github_token(token):
        return (
            "auth diagnostic: COPILOT_GITHUB_TOKEN is set with a recognized "
            "format; the failure is likely subscription/permissions "
            "(the PAT needs the Copilot Requests permission) or network policy"
        )
    return (
        f"auth diagnostic: COPILOT_GITHUB_TOKEN is set but has an unrecognized "
        f"format ({token[:6]}...) - likely the sbx secret-proxy sentinel. The "
        "Copilot SDK validates token format client-side and cannot use proxy "
        'sentinels; set secret_strategy="plain-env" for the agent sandbox'
    )


# Transcript-facing clips: the event stream is telemetry, not the artifact
# channel, so payloads are bounded aggressively.
TOOL_ARGS_CLIP = 400
# Hard char cap on a rendered excerpt. The outer bound: whatever the line
# budget below admits, the excerpt can never approach Discord's 2000-char
# message limit.
TOOL_OUTPUT_CLIP = 1_000
# How much of the output is shown when it is longer than the budget: the
# first N and last M lines, with an explicit elision marker between them.
TOOL_OUTPUT_HEAD_LINES = 20
TOOL_OUTPUT_TAIL_LINES = 20
# Per-line cap inside an excerpt. Without it a single wide line (a pytest
# progress bar, a minified blob) would eat the whole char budget and force
# the head or the elision marker out. Must stay well under half of
# TOOL_OUTPUT_CLIP so one head line, the marker and one tail line always fit.
TOOL_OUTPUT_LINE_CLIP = 200


def _tool_args(arguments: Any) -> str | None:
    """The tool's arguments as one displayable string (best-effort).

    ``ToolExecutionStartData.arguments`` is typed ``Any`` by the SDK: shell
    tools carry ``{"command": ...}``-shaped dicts, others arbitrary JSON.
    Prefer the human-relevant fields, fall back to compact JSON.
    """
    if arguments is None:
        return None
    if isinstance(arguments, dict):
        for key in ("command", "cmd", "input", "query", "pattern", "path"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value[:TOOL_ARGS_CLIP]
        try:
            return json.dumps(arguments, separators=(",", ":"))[:TOOL_ARGS_CLIP]
        except (TypeError, ValueError):
            return str(arguments)[:TOOL_ARGS_CLIP]
    text = str(arguments)
    return text[:TOOL_ARGS_CLIP] if text.strip() else None


def _tool_exit_code(data: Any) -> int | None:
    """Shell exit code from a ToolExecutionComplete, when present.

    Shell completions carry a ShellExit entry in ``result.contents``
    (verified against github-copilot-sdk: ``exit_code: int``).
    """
    result = getattr(data, "result", None)
    for entry in getattr(result, "contents", None) or []:
        code = getattr(entry, "exit_code", None)
        if isinstance(code, int):
            return code
    return None


class ToolCallRegistry:
    """Correlates tool completions with their starts by ``tool_call_id``.

    Parallel calls interleave and complete out of order, so a completion
    recovers the tool name, displayed args and elapsed time from the id it
    carries rather than by comparing command text. Entries are dropped on
    completion, bounding memory over a long session.
    """

    def __init__(self) -> None:
        self._starts: dict[str, tuple[str | None, str | None, float]] = {}

    def start(self, call_id: Any, tool: Any, args: str | None) -> None:
        if call_id:
            self._starts[str(call_id)] = (
                str(tool) if tool else None,
                args,
                time.monotonic(),
            )

    def end(self, call_id: Any) -> tuple[str | None, str | None, int | None]:
        """(tool, args, duration_ms) for a completion; blanks when unmatched."""
        started = self._starts.pop(str(call_id), None)
        if started is None:
            return (None, None, None)
        tool, args, began = started
        return (tool, args, max(0, int((time.monotonic() - began) * 1000)))


def excerpt_output(text: str) -> str:
    """A bounded, secret-redacted head+tail excerpt of tool output.

    Long output keeps the first :data:`TOOL_OUTPUT_HEAD_LINES` and last
    :data:`TOOL_OUTPUT_TAIL_LINES` lines, separated by an explicit
    ``… N lines elided …`` marker naming the omitted line count. Redaction
    runs on the full text before any cutting, so no credential shape is
    split across a cut boundary.

    The char cap (:data:`TOOL_OUTPUT_CLIP`) is enforced structurally, never
    by slicing the finished string: each line is first clipped to
    :data:`TOOL_OUTPUT_LINE_CLIP`, then whole lines are dropped from the
    middle — the end of the head and the start of the tail — with the
    marker recounted, so however wide the input lines are, the first line,
    the elision marker and the last line always survive.
    """
    half = TOOL_OUTPUT_LINE_CLIP // 2
    lines = [
        ln if len(ln) <= TOOL_OUTPUT_LINE_CLIP else ln[:half] + "…" + ln[-(half - 1) :]
        for ln in redact_secrets(text).splitlines()
    ]
    head = lines[:TOOL_OUTPUT_HEAD_LINES]
    tail = lines[TOOL_OUTPUT_HEAD_LINES:][-TOOL_OUTPUT_TAIL_LINES:]
    if not tail and len(head) > 1:
        # Short-but-wide output lands entirely in `head`; keep the last line
        # on the tail side so char-budget trimming can never drop it.
        head, tail = head[:-1], head[-1:]

    def render(h: list[str], t: list[str]) -> str:
        omitted = len(lines) - len(h) - len(t)
        marker = [f"… {omitted} lines elided …"] if omitted > 0 else []
        return "\n".join([*h, *marker, *t])

    out = render(head, tail)
    while len(out) > TOOL_OUTPUT_CLIP and (len(head) > 1 or len(tail) > 1):
        if len(head) >= len(tail):
            head = head[:-1]
        else:
            tail = tail[1:]
        out = render(head, tail)
    return out


def _tool_output_text(data: Any) -> str | None:
    """The tool's raw output text, unbounded (None when absent/blank)."""
    result = getattr(data, "result", None)
    content = getattr(result, "content", None)
    if not content and result is not None:
        content = getattr(result, "detailed_content", None)
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def _tool_output(data: Any) -> str | None:
    """A bounded excerpt of the tool's output, for transcript display."""
    content = _tool_output_text(data)
    return None if content is None else excerpt_output(content)


def _tool_output_lines(data: Any) -> int | None:
    """Total line count of the tool's untruncated output."""
    content = _tool_output_text(data)
    return None if content is None else len(content.splitlines())


# The Copilot CLI's built-in command validator declines to run some commands
# outright and reports the refusal as a failed tool call with this prefix
# (observed on 1.0.9: `kill` without a literal numeric PID, e.g.
# ``kill $(cat server.pid)``). Nothing is broken — the agent can rephrase and
# retry, and does — so counting these as tool failures would degrade a critic
# every time it cleans up a server process (field failure retn41aa6).
# Backend identity stamped onto agent events and usage samples, so chat
# can name provider+model.
BACKEND_NAME = "copilot"

_REFUSAL_PREFIX = "Command not executed."


def tool_refusal(error: str | None, output: str | None) -> bool:
    """Whether a failed tool end is the CLI validator declining to execute
    the command, as opposed to the command running and failing."""
    return any(
        isinstance(text, str) and text.lstrip().startswith(_REFUSAL_PREFIX)
        for text in (error, output)
    )


class ToolCallGovernor:
    """Per-session tool-call ceiling (#228).

    Field failure re59gj4vq: an executor that had established a fact kept
    re-establishing it — 30+ near-identical bash calls per phase against a
    check it could not fix — until the job timeout. Nothing bounded that
    but ``per_job_timeout_s``. This bounds it: the first ``cap`` calls are
    approved; every call after that is turned away with a nudge (as the
    tool's own feedback, so the model reads it in-session) telling it to
    stop investigating and report what it has. Pure counters, testable
    without the SDK.
    """

    def __init__(self, cap: int | None) -> None:
        self.cap = cap if cap and cap > 0 else None
        self.calls = 0
        self.denied = 0

    def decide(self) -> str | None:
        """None to approve this call; the nudge text to turn it away."""
        self.calls += 1
        if self.cap is None or self.calls <= self.cap:
            return None
        self.denied += 1
        return self.nudge()

    @property
    def tripped(self) -> bool:
        return self.cap is not None and self.calls > self.cap

    def nudge(self) -> str:
        assert self.cap is not None
        return (
            f"Tool-call ceiling reached: you have made {self.cap} tool calls in this phase "
            f"and further calls are not executed. Stop investigating now. Summarize what you "
            f"have established, state plainly anything you could not resolve (for example a "
            f"verify command that appears incorrect or unrunnable), and finish with your best "
            f"result in the required output format."
        )


class SessionHealthTracker:
    """Tallies permission denials, validator refusals, and tool-call
    failures for one session.

    A critic session that loses its inspection tooling must not look
    identical to a thorough one (#123): the tallies ride back on the
    JobResult so the engine can judge (and persist) how blind the session
    actually was. Pure counters, unit-testable without the SDK.
    """

    def __init__(self) -> None:
        self.denials: Counter[str] = Counter()
        self.failures: Counter[str] = Counter()
        self.refusals: Counter[str] = Counter()
        self._denied_calls: set[str] = set()

    def record_denial(self, kind: Any, tool_call_id: Any = None) -> None:
        self.denials[str(kind)] += 1
        if tool_call_id:
            self._denied_calls.add(str(tool_call_id))

    def record_tool_end(
        self,
        tool: str | None,
        success: Any,
        tool_call_id: Any = None,
        *,
        refused: bool = False,
    ) -> None:
        """Count a completed tool call iff it reported failure; the SDK
        leaves ``success`` None on events that carry no such signal.

        A call whose permission request we rejected also completes with
        ``success=False`` — that echo must not count as a tool failure, or
        every denial would trip the degraded-critic guard the denial carve-out
        exists to avoid (the field failure behind run raa2g67kw). Every
        PermissionRequest carries the ``tool_call_id`` of the call it gates,
        so denials are excluded by exact id, never by heuristics.

        ``refused`` marks the CLI validator declining to execute the command
        (see :func:`tool_refusal`) — policy, not lost tooling, so it lands in
        its own non-degrading tally."""
        if tool_call_id and str(tool_call_id) in self._denied_calls:
            self._denied_calls.discard(str(tool_call_id))
            return
        if success is False:
            if refused:
                self.refusals[tool or "(unknown)"] += 1
            else:
                self.failures[tool or "(unknown)"] += 1

    def health(self, governor: ToolCallGovernor | None = None) -> SessionHealth | None:
        calls = governor.calls if governor is not None else 0
        capped = governor.denied if governor is not None else 0
        if not self.denials and not self.failures and not self.refusals and not capped:
            return None
        return SessionHealth(
            permission_denials=dict(self.denials),
            tool_failures=dict(self.failures),
            tool_refusals=dict(self.refusals),
            tool_calls=calls,
            tool_cap_denials=capped,
        )


def _tool_error(data: Any) -> str | None:
    """The failure reason from a ToolExecutionComplete, when present.

    On failed executions the SDK leaves ``result`` unset (it is documented
    as "tool execution result on success") and reports the reason in
    ``error.message`` instead, so this is the only failure text available.
    """
    error = getattr(data, "error", None)
    message = getattr(error, "message", None)
    if not isinstance(message, str) or not message.strip():
        return None
    return excerpt_output(message)


def system_message_config(content: str | None) -> dict[str, Any] | None:
    """The SDK ``system_message`` kwarg for one job, or None to leave the
    SDK's default prompt untouched.

    ``append`` mode keeps the SDK-managed prompt structure intact and adds
    the job's extra content after it; with no extra content there is nothing
    to send.
    """
    return {"mode": "append", "content": content} if content else None


def _sdk_field(data: Any, name: str) -> Any:
    """One SDK field by its snake_case name, falling back to camelCase.

    The SDK's generated dataclasses are snake_case, but events that arrive
    straight off the wire have been seen carrying the camelCase spelling;
    both are tried rather than betting on one. Distinguishes "absent" from
    a falsy value — a turn that genuinely read 0 cache tokens must report
    0, not None, or the cache question this exists to answer stays open.
    """
    value = getattr(data, name, None)
    if value is not None:
        return value
    head, *rest = name.split("_")
    return getattr(data, head + "".join(part.title() for part in rest), None)


def usage_from_sdk_sample(data: Any) -> Usage:
    """One ``AssistantUsageData`` turn sample as a protocol :class:`Usage`.

    The prompt-cache counters ride on every sample (field-verified against
    github-copilot-sdk 1.0.9, ``copilot.generated.session_events``) and are
    read here because they are genuine per-turn deltas that the whole chain
    downstream already carries (``Usage`` in the worker protocol, the
    concierge's ``_USAGE_FIELDS``).

    The sample's per-turn spend attribute is deliberately NOT read, and
    ``Usage`` carries no field for it (#386, #439). It is not a per-turn
    delta: it was observed as the same constant 15.0 on every turn of every
    session in run rrhb28j7n (sbxloop 0.7.26), so folding it through
    ``Usage.merged`` fabricated a run total the concierge then repeated as
    fact. A value identical on every turn is far more likely a
    premium-request multiplier or quota unit than a currency amount, so it
    stays unread until its unit is established; "spend: not reported by the
    agent backend" is the honest answer. If it is ever surfaced it must be
    carried non-additively (last/max wins, as ``Usage.merged`` already does
    for ``model``) and never rendered in a currency shape.
    """
    return Usage(
        model=_sdk_field(data, "model"),
        backend=BACKEND_NAME,
        input_tokens=_sdk_field(data, "input_tokens"),
        output_tokens=_sdk_field(data, "output_tokens"),
        cache_read_tokens=_sdk_field(data, "cache_read_tokens"),
        cache_write_tokens=_sdk_field(data, "cache_write_tokens"),
    )


def available_tool_count(data: Any) -> int | None:
    """How many tools the SDK offered the model on this turn, when reported.

    ``_available_tool_count`` is marked internal by the SDK, so it may
    vanish without notice — but it is the only direct measure of how much
    of a turn's context is tool schema, which is what the per-phase system
    message trimming is aimed at. Rides on the ``agent.usage`` event rather
    than on :class:`Usage`, which forbids extras.
    """
    value = getattr(data, "_available_tool_count", None)
    return value if isinstance(value, int) else None


class CopilotBackend:
    name = "copilot"

    def ensure_available(self) -> None:
        """What this backend needs before a session can start (see
        ``backends.ensure_available``): the SDK, which the worker's
        ``[copilot]`` extra installs."""
        try:
            import copilot  # noqa: F401
        except ImportError as exc:
            raise BackendUnavailableError(
                "github-copilot-sdk is not installed; install sbxloop-worker[copilot]"
            ) from exc

    def run_session(self, job: JobRequest, emit: EmitFn) -> BackendResult:
        self.ensure_available()
        self._guard_bundled_ripgrep(emit)
        try:
            return asyncio.run(self._run(job, emit))
        except Exception as exc:
            # Auth failures surface as opaque SDK errors ("Session was not
            # created with authentication info..."); say what the token
            # environment actually looks like from inside the sandbox.
            raise RuntimeError(f"{exc} | {_auth_diagnostic()}") from exc

    @staticmethod
    def _guard_bundled_ripgrep(emit: EmitFn) -> None:
        """Apply :func:`ripgrep_page_size_plan` to this process's environment.

        The SDK's CLI subprocess inherits ``os.environ``, so setting
        ``USE_BUILTIN_RIPGREP`` here reaches the glob/grep tool spawns.
        Best-effort: a host without ``sysconf`` support just skips the guard.
        """
        try:
            page_size = os.sysconf("SC_PAGESIZE")
        except (AttributeError, OSError, ValueError):
            return
        updates, warning = ripgrep_page_size_plan(
            page_size, shutil.which("rg"), os.environ.get(RIPGREP_ENV)
        )
        os.environ.update(updates)
        if warning:
            emit(EventTypes.SANDBOX_TOOLING_WARNING, message=warning)

    async def _run(self, job: JobRequest, emit: EmitFn) -> BackendResult:
        from copilot import CopilotClient

        usage = Usage()
        turns = 0
        tracker = SessionHealthTracker()
        governor = ToolCallGovernor(job.max_tool_calls)
        final_text: list[str] = []
        # The model slug that is actually answering, for transcript
        # attribution on agent.message events. Seeded from the job request
        # ("auto" means the SDK picks, so it names nothing) and refined by
        # per-turn usage samples, which carry the model the SDK resolved to.
        model_slug = job.model if job.model and job.model != "auto" else None
        # tool_call_id -> (tool name, displayed args), so completion events
        # can say what ran (the SDK's Complete event carries only the call id).
        # Completions correlate to their start by id (not by comparing
        # command text), which also yields a per-call duration.
        tool_calls = ToolCallRegistry()

        def on_event(event: Any) -> None:
            nonlocal usage, turns, model_slug
            data = getattr(event, "data", None)
            type_name = type(data).__name__ if data is not None else type(event).__name__
            if type_name == "AssistantMessageDeltaData":
                emit(
                    EventTypes.AGENT_MESSAGE_DELTA,
                    delta=getattr(data, "delta_content", "") or "",
                )
            elif type_name == "AssistantMessageData":
                content = getattr(data, "content", "") or ""
                if content:
                    final_text.append(content)
                emit(
                    EventTypes.AGENT_MESSAGE,
                    content=content,
                    model=getattr(data, "model", None) or model_slug,
                    backend=BACKEND_NAME,
                )
            elif type_name.startswith("ToolExecutionStart"):
                tool = getattr(data, "tool_name", None) or getattr(data, "toolName", None)
                call_id = getattr(data, "tool_call_id", None) or getattr(data, "toolCallId", None)
                args = _tool_args(getattr(data, "arguments", None))
                tool_calls.start(call_id, tool, args)
                emit(
                    EventTypes.AGENT_TOOL_START,
                    tool=tool,
                    tool_call_id=call_id,
                    args=args,
                )
            elif type_name.startswith("ToolExecutionComplete"):
                call_id = getattr(data, "tool_call_id", None) or getattr(data, "toolCallId", None)
                tool, args, duration_ms = tool_calls.end(call_id)
                success = getattr(data, "success", None)
                output = _tool_output(data)
                error = _tool_error(data)
                tracker.record_tool_end(
                    tool,
                    success,
                    tool_call_id=call_id,
                    refused=tool_refusal(error, output),
                )
                emit(
                    EventTypes.AGENT_TOOL_END,
                    tool_call_id=call_id,
                    tool=tool,
                    args=args,
                    success=success,
                    exit_code=_tool_exit_code(data),
                    output=output,
                    error=error,
                    output_lines=_tool_output_lines(data),
                    duration_ms=duration_ms,
                )
            elif type_name == "AssistantUsageData":
                sample = usage_from_sdk_sample(data)
                usage = usage.merged(sample)
                turns += 1
                if sample.model:
                    model_slug = sample.model
                payload = sample.model_dump(exclude_none=True)
                payload["backend"] = BACKEND_NAME
                tools = available_tool_count(data)
                if tools is not None:
                    payload["available_tools"] = tools
                emit(EventTypes.AGENT_USAGE, **payload)

        async with CopilotClient() as client:
            session = await self._open_session(
                client, job, emit=emit, tracker=tracker, governor=governor
            )
            try:
                session.on(on_event)
                assert job.prompt is not None
                response = await session.send_and_wait(job.prompt, timeout=job.timeout_s)
                text = self._response_text(response) or "\n".join(final_text)
                output_json = extract_json(text) if job.expect == "json" else None
                session_id = getattr(session, "session_id", None) or getattr(session, "id", None)
                return BackendResult(
                    output_text=text,
                    output_json=output_json,
                    session_id=session_id,
                    usage=usage.merged(Usage(backend=BACKEND_NAME)) if usage != Usage() else None,
                    turns=turns or None,
                    health=tracker.health(governor),
                )
            finally:
                await self._close_session(session)

    async def _open_session(
        self,
        client: Any,
        job: JobRequest,
        *,
        emit: EmitFn | None = None,
        tracker: SessionHealthTracker | None = None,
        governor: ToolCallGovernor | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "on_permission_request": self._permission_handler(
                job, emit=emit, tracker=tracker, governor=governor
            ),
            "streaming": True,
        }
        if job.model and job.model != "auto":
            kwargs["model"] = job.model
        system_message = system_message_config(job.system_message)
        if system_message is not None:
            kwargs["system_message"] = system_message
        if job.cwd:
            # Belt-and-braces alongside the worker-level chdir; the kwarg
            # name is unverified against the SDK (e2e-validated), so an
            # unsupported signature must not fail the session.
            kwargs["working_directory"] = job.cwd
        kwargs.update(self._tool_kwargs(job, emit))
        try:
            return await self._open(client, job, kwargs)
        except TypeError:
            if "working_directory" not in kwargs:
                raise
            del kwargs["working_directory"]
            return await self._open(client, job, kwargs)

    def _tool_kwargs(self, job: JobRequest, emit: EmitFn | None) -> dict[str, Any]:
        """Session kwargs for host tools and the built-in tool allowlist.

        Host tools are registered as SDK custom tools whose handler relays
        the call to the host (``sbxloop_worker.hosttools``). The SDK's
        ``available_tools`` allowlist covers custom tools too, so host tool
        names are appended whenever the caller restricts built-ins.
        """
        kwargs: dict[str, Any] = {}
        if job.host_tools:
            if emit is None or not job.host_tools_dir:
                raise RuntimeError("host_tools need an event emitter and host_tools_dir")
            kwargs["tools"] = [self._host_tool(spec, job, emit) for spec in job.host_tools]
        if job.available_tools is not None:
            kwargs["available_tools"] = [
                *job.available_tools,
                *(spec.name for spec in job.host_tools),
            ]
        return kwargs

    @staticmethod
    def _host_tool(spec: HostToolSpec, job: JobRequest, emit: EmitFn) -> Any:
        """One SDK ``Tool`` whose handler round-trips to the host.

        ``skip_permission`` — the host decided which tools exist, and the
        read-only allowlist would otherwise turn every ``custom-tool``
        request away. The wait runs in a thread so the SDK's event loop
        keeps streaming while the host works.
        """
        from copilot.tools import Tool, ToolResult

        assert job.host_tools_dir is not None
        tools_dir = job.host_tools_dir

        async def handler(invocation: Any) -> Any:
            call = HostToolCall(
                call_id=safe_call_id(getattr(invocation, "tool_call_id", None)),
                name=spec.name,
                arguments=_arguments_dict(getattr(invocation, "arguments", None)),
            )
            try:
                response = await asyncio.to_thread(
                    request_tool, emit, tools_dir, call, job.host_tool_timeout_s
                )
            except HostToolTimeout as exc:
                return ToolResult(
                    text_result_for_llm=str(exc), result_type="timeout", error=str(exc)
                )
            if response.ok:
                return ToolResult(text_result_for_llm=response.text)
            return ToolResult(
                text_result_for_llm=response.text or response.error or f"{spec.name} failed",
                result_type="failure",
                error=response.error,
            )

        return Tool(
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters,
            handler=handler,
            skip_permission=True,
        )

    @staticmethod
    async def _open(client: Any, job: JobRequest, kwargs: dict[str, Any]) -> Any:
        if job.resume_session_id:
            # Resume is an optimisation, never a requirement: it saves a
            # revision from re-deriving what its own previous attempt
            # established. A session the SDK has expired, evicted, or never
            # heard of must cost that saving and nothing more — falling
            # through to a fresh session is exactly the behaviour before
            # resume existed, and the prompt still carries the previous
            # attempt's report either way.
            #
            # Not reported from here (the worker has no logger; its channel
            # is events): the host sees it for free, because a fresh session
            # comes back with a different id than the one it asked to
            # resume, which is what `phase.resume_missed` keys on.
            with contextlib.suppress(Exception):
                return await client.resume_session(job.resume_session_id, **kwargs)
        return await client.create_session(**kwargs)

    def _permission_handler(
        self,
        job: JobRequest,
        *,
        emit: EmitFn | None = None,
        tracker: SessionHealthTracker | None = None,
        governor: ToolCallGovernor | None = None,
    ) -> Any:
        governor = governor or ToolCallGovernor(job.max_tool_calls)

        def capped(request: Any) -> Any:
            """The ceiling's decision for this call, or None to fall through.
            Turned-away calls are tallied separately from policy denials so
            they never read as a degraded session."""
            nudge = governor.decide()
            if nudge is None:
                return None
            from copilot.rpc import PermissionDecisionReject

            if governor.denied == 1 and emit is not None:
                emit(
                    EventTypes.AGENT_TOOL_CAP,
                    cap=governor.cap,
                    calls=governor.calls,
                    tool=getattr(request, "kind", None),
                )
            return PermissionDecisionReject(feedback=nudge)

        if job.permission_mode == "auto":
            # The microVM (network policy + secret proxy) is the security
            # boundary; inside it the agent runs unattended — only the
            # tool-call ceiling stands between it and a forensic spiral.
            if governor.cap is None:
                from copilot.session import PermissionHandler

                return PermissionHandler.approve_all

            def auto_handler(request: Any, invocation: Any = None) -> Any:
                from copilot.rpc import PermissionDecisionApproveOnce

                return capped(request) or PermissionDecisionApproveOnce()

            return auto_handler

        def read_only_handler(request: Any, invocation: Any = None) -> Any:
            from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

            decision = capped(request)
            if decision is not None:
                return decision
            feedback = read_only_denial(request)
            if feedback is not None:
                # Denials must leave a trace: the session's health tally and
                # the event stream both record them, so a critic that lost
                # capabilities is auditable after the fact (#123).
                kind = getattr(request, "kind", None)
                if tracker is not None:
                    tracker.record_denial(kind, tool_call_id=getattr(request, "tool_call_id", None))
                if emit is not None:
                    emit(EventTypes.AGENT_PERMISSION_DENIED, kind=str(kind), feedback=feedback)
                return PermissionDecisionReject(feedback=feedback)
            return PermissionDecisionApproveOnce()

        return read_only_handler

    @staticmethod
    def _response_text(response: Any) -> str:
        data = getattr(response, "data", None)
        return getattr(data, "content", None) or getattr(response, "content", None) or ""

    @staticmethod
    async def _close_session(session: Any) -> None:
        disconnect = getattr(session, "disconnect", None)
        if disconnect is not None:
            # Best-effort teardown: a failed disconnect must never mask the
            # job outcome already produced.
            with contextlib.suppress(Exception):
                result = disconnect()
                if asyncio.iscoroutine(result):
                    await result
