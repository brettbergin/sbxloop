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
"""

from __future__ import annotations

import datetime
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sbxloop.events import Event, HostEventTypes
from sbxloop_worker.protocol import EventTypes

TASK_STATE_STYLES = {
    "pending": "dim",
    "planning": "yellow",
    "executing": "cyan",
    "scrutinizing": "magenta",
    "verifying": "blue",
    "validating": "magenta",
    "done": "green",
    "failed": "red",
    "skipped": "dim red",
}

# Compact one-liner lifecycle events (chat "system" messages).
_LIFECYCLE_PREFIXES = ("run.", "task.", "phase.", "sandbox.", "gh.")

# High-volume noise excluded from the transcript (still queryable via
# `sbxloop logs`): streaming deltas, raw stdout passthrough, heartbeats.
_TRANSCRIPT_SKIP = {
    EventTypes.AGENT_MESSAGE_DELTA,
    EventTypes.WORKER_STDOUT,
    EventTypes.WORKER_HEARTBEAT,
    EventTypes.WORKER_START,
    EventTypes.WORKER_RESULT,
    EventTypes.WORKER_END,
    EventTypes.AGENT_USAGE,
}

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
        return Panel(
            Markdown(content),
            title=f"[bold cyan]agent[/] [dim]{_stamp(event)}[/]",
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

    if event.type.startswith(_LIFECYCLE_PREFIXES):
        line = format_event(event)
        style = "dim"
        if event.type == HostEventTypes.TASK_STATE:
            style = TASK_STATE_STYLES.get(str(data.get("state", "")), "dim")
        elif "fallback" in event.type or "missing" in event.type:
            style = "yellow"
        return Text(line, style=style, overflow="fold")

    return None


class Dashboard:
    """Accumulates run status from the event stream and renders the pinned
    region. Transcript entries are NOT kept here — the drive loop prints
    them straight to scrollback via ``render_event`` so history is never
    truncated or rewritten."""

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.run_state: str = "starting"
        self.outcome: str = ""
        self.tasks: dict[str, dict[str, Any]] = {}

    # -- event intake ------------------------------------------------------

    def on_event(self, event: Event) -> None:
        self.run_id = self.run_id or event.run_id
        data = event.data
        if event.type == HostEventTypes.RUN_START:
            self.outcome = str(data.get("outcome", ""))
        elif event.type in (HostEventTypes.RUN_STATE, HostEventTypes.RUN_END):
            self.run_state = str(data.get("state", self.run_state))
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

    def renderable(self) -> RenderableType:
        header = Text.assemble(
            ("run ", "bold"),
            (self.run_id or "…", "bold cyan"),
            ("  state: ", ""),
            (self.run_state, "bold yellow"),
        )
        outcome = Text(self.outcome[:200], style="italic dim")

        table = Table(expand=True, box=None, pad_edge=False)
        table.add_column("task", style="bold", no_wrap=True)
        table.add_column("title", ratio=2)
        table.add_column("state", no_wrap=True)
        table.add_column("rev/replan", no_wrap=True, justify="right")
        for task_id, entry in self.tasks.items():
            state = str(entry.get("state", "?"))
            table.add_row(
                task_id,
                str(entry.get("title", "")),
                Text(state, style=TASK_STATE_STYLES.get(state, "")),
                f"{entry.get('revisions', 0)}/{entry.get('replans', 0)}",
            )
        if not self.tasks:
            table.add_row("…", "decomposing outcome", Text("pending", style="dim"), "")

        return Panel(
            Group(header, outcome, table),
            border_style="cyan",
            padding=(0, 1),
        )


def format_event(event: Event) -> str:
    """Dense single-line form (used by `sbxloop logs` and lifecycle lines)."""
    parts = [_stamp(event), event.type]
    if event.data.get("task_id"):
        parts.append(f"[{event.data['task_id']}]")
    keys = ("state", "content", "tool", "op", "line", "message", "outcome", "error", "url", "path")
    picked = ""
    for key in keys:
        if event.data.get(key):
            picked = key
            value = str(event.data[key]).replace("\n", " ")
            parts.append(value[:160])
            break
    if event.data.get("args"):
        parts.append(_one_line(str(event.data["args"]), 120))
    if picked != "error" and event.data.get("error"):
        parts.append(_one_line(str(event.data["error"]), 160))
    return " ".join(parts)


def plain_printer(console: Console) -> Any:
    """--no-tui mode: print the same chat-style entries sequentially."""

    def print_event(event: Event) -> None:
        rendered = render_event(event)
        if rendered is not None:
            console.print(rendered)

    return print_event
