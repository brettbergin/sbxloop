"""Rich rendering for runs: a chat-style live transcript and plain logging.

Agent output is conversational (markdown, fenced code blocks), so the live
view renders it as a chat thread — markdown panels with syntax-highlighted
code — rather than truncated log lines. Host lifecycle events stay compact
one-liners. ``format_event`` remains the dense single-line form used by
``sbxloop logs``.
"""

from __future__ import annotations

import datetime
from collections import deque
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


def _stamp(event: Event) -> str:
    return datetime.datetime.fromtimestamp(event.ts).strftime("%H:%M:%S")


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
        return Text(f"{_stamp(event)}  ⚙ {tool}", style="yellow", overflow="fold")

    if event.type == EventTypes.AGENT_TOOL_END:
        return None  # start lines carry enough signal for the transcript

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
    """Accumulates run state from the event stream and renders it."""

    def __init__(self, max_entries: int = 6) -> None:
        self.run_id: str | None = None
        self.run_state: str = "starting"
        self.outcome: str = ""
        self.tasks: dict[str, dict[str, Any]] = {}
        self.transcript: deque[RenderableType] = deque(maxlen=max_entries)

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
        rendered = render_event(event)
        if rendered is not None:
            self.transcript.append(rendered)

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

        transcript: RenderableType
        if self.transcript:
            transcript = Group(*self.transcript)
        else:
            transcript = Text("waiting for events…", style="dim")
        return Group(
            Panel(Group(header, outcome), border_style="cyan"),
            table,
            transcript,
        )


def format_event(event: Event) -> str:
    """Dense single-line form (used by `sbxloop logs` and lifecycle lines)."""
    parts = [_stamp(event), event.type]
    if event.data.get("task_id"):
        parts.append(f"[{event.data['task_id']}]")
    for key in ("state", "content", "tool", "op", "line", "message", "outcome", "path"):
        if event.data.get(key):
            value = str(event.data[key]).replace("\n", " ")
            parts.append(value[:160])
            break
    return " ".join(parts)


def plain_printer(console: Console) -> Any:
    """--no-tui mode: print the same chat-style entries sequentially."""

    def print_event(event: Event) -> None:
        rendered = render_event(event)
        if rendered is not None:
            console.print(rendered)

    return print_event
