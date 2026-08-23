"""Rich rendering for runs: a permanent scrollback transcript plus a small
pinned status region.

Agent output is conversational (markdown, fenced code blocks), so the
transcript renders it as a chat thread — markdown panels with
syntax-highlighted code — rather than truncated log lines. Transcript
entries are printed to the terminal's normal scrollback as they happen and
are never rewritten or dropped: the full history of a run stays scrollable.
Only the compact status region (run state + task table) is live-updated in
place at the bottom. Host lifecycle events stay compact one-liners.
``format_event`` remains the dense single-line form used by
``sbxloop logs``.

The status region also hosts the interactive chat form (``ChatInput``):
keystrokes are captured in cbreak mode (echo off — the Live region owns the
screen) and the in-progress line is rendered inside the pinned panel, with
queued/answering messages shown above it. Chat turns appear in the
transcript as ``chat.message``/``chat.reply`` panels.
"""

from __future__ import annotations

import codecs
import datetime
import os
import select
import sys
from collections.abc import Callable
from types import TracebackType
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sbxloop.events import Event, HostEventTypes, summarize_event
from sbxloop_worker.protocol import EventTypes

# Live task states only; historical events replayed from a pre-BUILD run
# may carry retired states (planning/scrutinizing/validating), which fall
# through to the "dim" default in every lookup.
TASK_STATE_STYLES = {
    "pending": "dim",
    "executing": "cyan",
    "verifying": "blue",
    "done": "green",
    "failed": "red",
    "skipped": "dim red",
}

# Compact one-liner lifecycle events (chat "system" messages).
_LIFECYCLE_PREFIXES = ("run.", "task.", "phase.", "sandbox.", "gh.", "policy.")

# Chat message text shown in the pinned panel is clipped to one line.
CHAT_PENDING_CLIP = 80

# High-volume noise excluded from the transcript (still queryable via
# `sbxloop logs`): streaming deltas, raw stdout passthrough, heartbeats,
# per-beat resource samples (they live in the pinned gauge instead —
# threshold *crossings* arrive as sandbox.resources_warning and do print).
_TRANSCRIPT_SKIP = {
    EventTypes.AGENT_MESSAGE_DELTA,
    EventTypes.WORKER_STDOUT,
    EventTypes.WORKER_HEARTBEAT,
    EventTypes.WORKER_START,
    EventTypes.WORKER_RESULT,
    EventTypes.WORKER_END,
    EventTypes.AGENT_USAGE,
    EventTypes.SANDBOX_RESOURCES,
}

RESOURCE_LEVEL_STYLES = {"ok": "dim", "warn": "yellow", "abort": "bold red"}

AGENT_MESSAGE_CLIP = 4000
# One-line clip for tool arguments; output tail lines shown on tool failure.
TOOL_ARGS_LINE_CLIP = 160
TOOL_FAIL_TAIL_LINES = 6


def _stamp(event: Event) -> str:
    return datetime.datetime.fromtimestamp(event.ts).strftime("%H:%M:%S")


def _one_line(text: str, limit: int = TOOL_ARGS_LINE_CLIP) -> str:
    """Collapse to a single display line, eliding the middle beyond limit."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    keep = (limit - 1) // 2
    return f"{flat[:keep]}…{flat[-keep:]}"


def render_event(event: Event) -> RenderableType | None:
    """One transcript entry for an event, or None when it should be skipped."""
    if event.type in _TRANSCRIPT_SKIP:
        return None
    data = event.data

    if event.type == EventTypes.AGENT_MESSAGE:
        content = str(data.get("content", "")).strip()
        if not content:
            return None
        if len(content) > AGENT_MESSAGE_CLIP:
            content = content[:AGENT_MESSAGE_CLIP] + "\n\n*…truncated — see `sbxloop logs`*"
        speaker = str(data.get("agent") or "agent")
        model = str(data.get("model") or "").strip()
        title = f"[bold cyan]{speaker}[/]"
        if model:
            title += f" [dim]· {model}[/]"
        return Panel(
            Markdown(content),
            title=f"{title} [dim]{_stamp(event)}[/]",
            title_align="left",
            border_style="cyan",
            padding=(0, 1),
        )

    if event.type == EventTypes.WORKER_ERROR:
        message = str(data.get("message", "")) or str(data.get("error_type", "error"))
        return Panel(
            Text(message, style="red", overflow="fold"),
            title=f"[bold red]error[/] [dim]{_stamp(event)}[/]",
            title_align="left",
            border_style="red",
            padding=(0, 1),
        )

    if event.type == EventTypes.AGENT_TOOL_START:
        tool = data.get("tool") or "tool"
        start = Text(f"{_stamp(event)}  ⚙ {tool}", style="yellow")
        args = _one_line(str(data.get("args") or ""))
        if args:
            start.append(" $ ", style="bold yellow")
            start.append(args, style="yellow dim")
        start.overflow = "fold"
        return start

    if event.type == EventTypes.AGENT_TOOL_CAP:
        return Text(
            f"{_stamp(event)}  ⛔ tool-call ceiling ({data.get('cap')}) reached — "
            "further calls are turned away; the agent was told to wrap up and report",
            style="yellow",
        )

    if event.type == EventTypes.AGENT_TOOL_END:
        tool = data.get("tool") or "tool"
        success = data.get("success")
        exit_code = data.get("exit_code")
        error = str(data.get("error") or "").strip()
        if success is None and exit_code is None and not error:
            return None  # nothing informative beyond the start line
        if success or (success is None and exit_code == 0 and not error):
            suffix = " exit 0" if exit_code == 0 else ""
            return Text(f"{_stamp(event)}  ✓ {tool}{suffix}", style="green dim")
        suffix = f" exit {exit_code}" if exit_code is not None else ""
        failure = Text(f"{_stamp(event)}  ✗ {tool}{suffix}", style="red")
        args = _one_line(str(data.get("args") or ""))
        if args:
            failure.append(" $ ", style="bold red")
            failure.append(args, style="red dim")
        failure.overflow = "fold"
        # Failed executions carry the reason in `error` (the SDK omits
        # `output` on failure); prefer real output when both exist.
        tail = str(data.get("output") or "").strip() or error
        if tail:
            return Group(
                failure,
                Text(
                    "\n".join(tail.splitlines()[-TOOL_FAIL_TAIL_LINES:]),
                    style="red dim",
                    overflow="fold",
                ),
            )
        return failure

    if event.type == HostEventTypes.CHAT_MESSAGE:
        return Panel(
            Text(str(data.get("text", "")), overflow="fold"),
            title=f"[bold green]you[/] [dim]{_stamp(event)}[/]",
            title_align="left",
            border_style="green",
            padding=(0, 1),
        )

    if event.type == HostEventTypes.CHAT_REPLY:
        error = str(data.get("error") or "").strip()
        if error:
            return Panel(
                Text(f"steering failed: {error}", style="red", overflow="fold"),
                title=f"[bold red]agent · reply[/] [dim]{_stamp(event)}[/]",
                title_align="left",
                border_style="red",
                padding=(0, 1),
            )
        return Panel(
            Markdown(str(data.get("reply", "")).strip() or "*(no reply)*"),
            title=f"[bold cyan]agent · reply[/] [dim]{_stamp(event)}[/]",
            title_align="left",
            border_style="cyan",
            padding=(0, 1),
        )

    if event.type == HostEventTypes.CHAT_ACTION:
        return Text(
            f"{_stamp(event)}  ↪ {data.get('message', 'user steering applied')}",
            style="bold yellow",
            overflow="fold",
        )

    if event.type.startswith(_LIFECYCLE_PREFIXES):
        line = format_event(event)
        style = "dim"
        if event.type == HostEventTypes.TASK_STATE:
            style = TASK_STATE_STYLES.get(str(data.get("state", "")), "dim")
        elif event.type == HostEventTypes.PHASE_END and data.get("status") == "failed":
            style = "red"
        elif (
            "fallback" in event.type
            or "missing" in event.type
            or "warning" in event.type
            or event.type == HostEventTypes.POLICY_DENY
        ):
            style = "yellow"
        return Text(line, style=style, overflow="fold")

    return None


class ChatInput:
    """The run TUI's chat form: cbreak-mode keystroke capture on stdin.

    Terminal echo is off (the Live region owns the screen), so the
    in-progress line is rendered inside the pinned panel via
    ``renderable()`` instead. ``pump()`` replaces the drive loop's sleep:
    it waits on stdin with a timeout and feeds any bytes through ``feed()``.
    Enter submits the stripped line to ``on_submit``; Backspace edits;
    Ctrl-U clears; arrow/function-key escape sequences are swallowed.
    POSIX-with-a-TTY only — gate construction on ``available()``. cbreak
    keeps ISIG, so Ctrl-C still interrupts the run as before.
    """

    def __init__(self, on_submit: Callable[[str], None]) -> None:
        self.on_submit = on_submit
        self.buffer = ""
        self._saved: list[Any] | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        # "" (normal) | "esc" (just saw ESC) | "csi" (inside a CSI/SS3
        # sequence, skipping until its terminator).
        self._esc_state = ""

    @staticmethod
    def available() -> bool:
        try:
            import termios  # noqa: F401
            import tty  # noqa: F401
        except ImportError:  # pragma: no cover - non-POSIX platform
            return False
        try:
            return sys.stdin.isatty()
        except (ValueError, OSError):  # pragma: no cover - closed stdin
            return False

    def __enter__(self) -> ChatInput:
        import termios
        import tty

        fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        import termios

        if self._saved is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)
            self._saved = None

    def pump(self, timeout: float) -> bool:
        """Wait up to ``timeout`` for keystrokes and absorb them, then keep
        absorbing whatever is already buffered (fast typing, pastes) without
        waiting again. Returns True when anything was consumed so the caller
        can repaint the input line immediately instead of on the next
        refresh tick."""
        consumed = False
        wait = timeout
        while True:
            try:
                ready, _, _ = select.select([sys.stdin], [], [], wait)
            except (ValueError, OSError):  # pragma: no cover - stdin went away
                return consumed
            if not ready:
                return consumed
            data = os.read(sys.stdin.fileno(), 1024)
            if not data:  # pragma: no cover - EOF on stdin
                return consumed
            self.feed(data)
            consumed = True
            wait = 0.0

    def feed(self, data: bytes) -> None:
        for ch in self._decoder.decode(data):
            if self._esc_state == "esc":
                # '['/'O' opens a CSI/SS3 sequence (arrows, Home, F-keys);
                # anything else was Alt+key or a bare Esc — drop just it.
                self._esc_state = "csi" if ch in "[O" else ""
                continue
            if self._esc_state == "csi":
                # Sequences end on a letter or '~'.
                if ch.isalpha() or ch == "~":
                    self._esc_state = ""
                continue
            if ch == "\x1b":
                self._esc_state = "esc"
            elif ch in ("\r", "\n"):
                text = self.buffer.strip()
                self.buffer = ""
                if text:
                    self.on_submit(text)
            elif ch in ("\x7f", "\x08"):
                self.buffer = self.buffer[:-1]
            elif ch == "\x15":  # Ctrl-U
                self.buffer = ""
            elif ch.isprintable():
                self.buffer += ch

    def renderable(self) -> Text:
        if not self.buffer:
            return Text.assemble(
                ("> ", "bold green"),
                ("type to chat with the agent · Enter to send", "dim italic"),
            )
        return Text.assemble(("> ", "bold green"), (self.buffer, "bold"), ("▌", "blink green"))


class Dashboard:
    """Accumulates run status from the event stream and renders the pinned
    region. Transcript entries are NOT kept here — the drive loop prints
    them straight to scrollback via ``render_event`` so history is never
    truncated or rewritten."""

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.run_state: str = "starting"
        # Why the run reached a terminal state (cancellation attribution or
        # reconciliation of an orphan, #374); None while in flight.
        self.run_reason: str | None = None
        self.outcome: str = ""
        self.tasks: dict[str, dict[str, Any]] = {}
        # Latest resource sample per sandbox role, rendered as one compact
        # gauge line each; only escalates visually past warn/abort levels.
        self.resources: dict[str, dict[str, Any]] = {}
        # Chat lifecycle for the pinned panel: messages submitted from the
        # form but not yet absorbed by the engine (message_id → text), and
        # the text of the message a STEER session is currently answering.
        self.chat_pending: dict[str, str] = {}
        self.chat_processing: str | None = None

    def post_chat(self, message_id: str, text: str) -> None:
        """Record a just-submitted message as queued (fed by the chat form)."""
        self.chat_pending[message_id] = text

    # -- event intake ------------------------------------------------------

    def on_event(self, event: Event) -> None:
        self.run_id = self.run_id or event.run_id
        data = event.data
        if event.type == HostEventTypes.CHAT_MESSAGE:
            # The engine picked the message up: no longer queued, now the
            # one being answered.
            self.chat_processing = self.chat_pending.pop(
                str(data.get("message_id")), str(data.get("text", ""))
            )
        elif event.type == HostEventTypes.CHAT_REPLY:
            self.chat_processing = None
        if event.type == EventTypes.SANDBOX_RESOURCES:
            self.resources[str(data.get("role") or "sandbox")] = dict(data)
        elif event.type == HostEventTypes.RUN_START:
            self.outcome = str(data.get("outcome", ""))
        elif event.type in (HostEventTypes.RUN_STATE, HostEventTypes.RUN_END):
            self.run_state = str(data.get("state", self.run_state))
            if data.get("reason"):
                self.run_reason = str(data["reason"])
        elif event.type == HostEventTypes.RUN_RECONCILED:
            self.run_state = str(data.get("state", self.run_state))
            self.run_reason = str(data.get("reason", "")) or None
        elif event.type in (HostEventTypes.TASK_START, HostEventTypes.TASK_STATE):
            task_id = str(data.get("task_id"))
            entry = self.tasks.setdefault(task_id, {"title": "", "state": "pending"})
            if data.get("title"):
                entry["title"] = str(data["title"])
            if data.get("state"):
                entry["state"] = str(data["state"])
            entry["revisions"] = data.get("revisions", entry.get("revisions", 0))
            entry["replans"] = data.get("replans", entry.get("replans", 0))
        elif event.type == HostEventTypes.TASK_END:
            task_id = str(data.get("task_id"))
            entry = self.tasks.setdefault(task_id, {"title": "", "state": "pending"})
            if data.get("title"):
                entry["title"] = str(data["title"])
            entry["state"] = str(data.get("state", entry["state"]))

    # -- rendering ---------------------------------------------------------

    def renderable(self, chat_line: RenderableType | None = None) -> RenderableType:
        header = Text.assemble(
            ("run ", "bold"),
            (self.run_id or "…", "bold cyan"),
            ("  state: ", ""),
            (self.run_state, "bold yellow"),
        )
        if self.run_reason:
            header.append(f"  ({self.run_reason})", style="dim")
        outcome = Text(self.outcome[:200], style="italic dim")

        table = Table(expand=True, box=None, pad_edge=False)
        table.add_column("task", style="bold", no_wrap=True)
        table.add_column("title", ratio=2)
        table.add_column("state", no_wrap=True)
        table.add_column("rev/replan", no_wrap=True, justify="right")
        for task_id, entry in self.tasks.items():
            state = str(entry.get("state", "?"))
            # The engine announces the whole roster as "pending" right after
            # decompose; show those rows as "waiting" until their turn.
            label = "waiting" if state == "pending" else state
            table.add_row(
                task_id,
                str(entry.get("title", "")),
                Text(label, style=TASK_STATE_STYLES.get(state, "")),
                f"{entry.get('revisions', 0)}/{entry.get('replans', 0)}",
            )
        if not self.tasks:
            table.add_row("…", "decomposing outcome", Text("pending", style="dim"), "")

        gauges = [self._gauge_line(role) for role in sorted(self.resources)]
        chat_lines: list[RenderableType] = []
        if self.chat_processing is not None:
            chat_lines.append(
                Text(
                    f"✉ steering: {_one_line(self.chat_processing, CHAT_PENDING_CLIP)}",
                    style="yellow",
                )
            )
        for text in self.chat_pending.values():
            chat_lines.append(
                Text(
                    f"✉ queued (pauses at the next checkpoint): "
                    f"{_one_line(text, CHAT_PENDING_CLIP)}",
                    style="dim",
                )
            )
        if chat_line is not None:
            chat_lines.append(chat_line)
        return Panel(
            Group(header, outcome, table, *gauges, *chat_lines),
            border_style="cyan",
            padding=(0, 1),
        )

    def _gauge_line(self, role: str) -> Text:
        sample = self.resources[role]
        level = str(sample.get("level", "ok"))
        parts = []
        if sample.get("disk_used_pct") is not None:
            parts.append(f"disk {sample['disk_used_pct']:.0f}%")
        if sample.get("mem_used_pct") is not None:
            parts.append(f"mem {sample['mem_used_pct']:.0f}%")
        if sample.get("load1") is not None:
            parts.append(f"load {sample['load1']}")
        suffix = "  ⚠ " + level if level != "ok" else ""
        return Text(
            f"{role}: " + " · ".join(parts) + suffix,
            style=RESOURCE_LEVEL_STYLES.get(level, "dim"),
        )


def format_event(event: Event) -> str:
    """Dense single-line form (used by `sbxloop logs` and lifecycle lines)."""
    fields = summarize_event(event)
    parts = [_stamp(event), event.type]
    if "task" in fields:
        parts.append(f"[{fields['task']}]")
    if "agent" in fields:
        parts.append(f"[{fields['agent']}]")
    if "summary" in fields:
        parts.append(fields["summary"])
    if "disk" in fields:
        # Resource samples: make `sbxloop logs --type-prefix sandbox.resources`
        # answer "what did disk look like before the failure" at a glance.
        summary = [f"disk={fields['disk']}"]
        if "mem" in fields:
            summary.append(f"mem={fields['mem']}")
        if "load" in fields:
            summary.append(f"load={fields['load']}")
        if "resource_level" in fields:
            summary.append(str(fields["resource_level"]))
        parts.append(" ".join(summary))
    if "args" in fields:
        parts.append(fields["args"])
    if "error" in fields:
        parts.append(fields["error"])
    return " ".join(parts)


def plain_printer(console: Console) -> Any:
    """--no-tui mode: print the same chat-style entries sequentially."""

    def print_event(event: Event) -> None:
        rendered = render_event(event)
        if rendered is not None:
            console.print(rendered)

    return print_event
