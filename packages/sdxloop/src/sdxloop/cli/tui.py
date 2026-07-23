"""Rich rendering for runs: live dashboard and plain event logging."""

from __future__ import annotations

import datetime
from collections import deque
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sdxloop.events import Event, HostEventTypes

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

INTERESTING_EVENTS = (
    "run.",
    "task.",
    "sandbox.",
    "agent.message",
    "agent.tool",
    "gh.",
    "worker.error",
)


class Dashboard:
    """Accumulates run state from the event stream and renders it."""

    def __init__(self, max_events: int = 12) -> None:
        self.run_id: str | None = None
        self.run_state: str = "starting"
        self.outcome: str = ""
        self.tasks: dict[str, dict[str, Any]] = {}
        self.recent: deque[str] = deque(maxlen=max_events)

    # -- event intake ------------------------------------------------------

    def on_event(self, event: Event) -> None:
        self.run_id = self.run_id or event.run_id
        data = event.data
        if event.type == HostEventTypes.RUN_START:
            self.outcome = str(data.get("outcome", ""))
        elif event.type == HostEventTypes.RUN_STATE or event.type == HostEventTypes.RUN_END:
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
        if event.type.startswith(INTERESTING_EVENTS):
            self.recent.append(format_event(event))

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

        events_panel = Panel(
            Text("\n".join(self.recent) or "waiting for events…", overflow="ellipsis"),
            title="events",
            border_style="dim",
        )
        return Group(Panel(Group(header, outcome), border_style="cyan"), table, events_panel)


def format_event(event: Event) -> str:
    stamp = datetime.datetime.fromtimestamp(event.ts).strftime("%H:%M:%S")
    parts = [stamp, event.type]
    if event.data.get("task_id"):
        parts.append(f"[{event.data['task_id']}]")
    for key in ("state", "content", "tool", "op", "line", "message", "outcome"):
        if event.data.get(key):
            value = str(event.data[key]).replace("\n", " ")
            parts.append(value[:120])
            break
    return " ".join(parts)


def plain_printer(console: Console) -> Any:
    def print_event(event: Event) -> None:
        if event.type.startswith(INTERESTING_EVENTS):
            console.print(format_event(event), highlight=False)

    return print_event
