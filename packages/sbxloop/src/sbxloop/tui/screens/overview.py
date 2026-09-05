"""Overview: the run in flight, the queue, who is waiting on a human,
recent runs, and the last control-channel notices."""

from __future__ import annotations

import time
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Grid, Vertical
from textual.widgets import Static

from sbxloop.tui.data import ConsoleState
from sbxloop.tui.format import age, run_title, state_label, to_rich
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable


class OverviewScreen(ConsoleScreen):
    DEFAULT_CSS = """
    OverviewScreen Grid { grid-size: 2 2; grid-gutter: 0 1; height: 1fr; }
    OverviewScreen .panel { border: round $primary; padding: 0 1; height: 1fr; }
    OverviewScreen #current { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with Grid(id="body"):
            yield TextPanel(Text("probing…", style="dim"), id="current", classes="panel")
            with Vertical(id="queue-box", classes="panel"):
                yield Static("queue", classes="title")
                yield ConsoleTable("item", "repo", "state", "title", id="queue")
            with Vertical(id="runs-box", classes="panel"):
                yield Static("recent runs", classes="title")
                yield ConsoleTable("run", "state", "item", "updated", id="recent")
            yield TextPanel(Text("no notices yet", style="dim"), id="notices", classes="panel")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#current", TextPanel).border_title = "current"
        self.query_one("#queue-box").border_title = "queue"
        self.query_one("#runs-box").border_title = "recent runs"
        self.query_one("#notices", TextPanel).border_title = "waiting on a human"

    def refresh_data(self, state: ConsoleState) -> None:
        super().refresh_data(state)
        emoji = self.console_app.emoji
        now = time.time()
        self.query_one("#current", TextPanel).update(self._current(state, now))
        queue = self.query_one("#queue", ConsoleTable)
        items = state.items
        if items is not None:
            queue.replace_rows(
                (i.item_id, (i.item_id, i.repo or "—", i.state, i.title[:50])) for i in items.queued
            )
        recent = self.query_one("#recent", ConsoleTable)
        runs = state.runs
        if runs is not None:
            recent.replace_rows(
                (
                    r.run_id,
                    (
                        r.run_id,
                        state_label(r.state, r.reason, emoji=emoji),
                        runs.item_for(r.run_id) or "—",
                        age(r.updated_at, now),
                    ),
                )
                for r in runs.runs[:8]
            )
        self.query_one("#notices", TextPanel).update(self._waits(state))

    def _current(self, state: ConsoleState, now: float) -> Text:
        daemon = state.daemon
        status = (daemon.status if daemon else None) or {}
        current = status.get("current")
        runs = state.runs
        text = Text()
        if daemon is None:
            text.append("probing the daemon…", style="dim")
            return text
        if daemon.starting:
            text.append("daemon starting ", style="yellow")
            text.append("(recovery); commands wait until it answers", style="dim")
            return text
        if not daemon.live:
            text.append("no daemon answered ", style="bold red")
            text.append("(ctl status); history below is still browsable", style="dim")
            return text
        if daemon.status is None:
            text.append("daemon busy ", style="yellow")
            text.append("(status not answered in time); the store below is live", style="dim")
            return text
        if not current:
            text.append("idle", style="green")
            if status.get("paused"):
                text.append(f" — paused: {', '.join(status.get('holds') or [])}", style="yellow")
            return text
        run_id = str(current.get("run_id"))
        record = next((r for r in (runs.runs if runs else ()) if r.run_id == run_id), None)
        text.append(run_id, style="bold cyan")
        text.append(f" · {current.get('item_id')}", style="dim")
        if record is not None:
            text.append("\n")
            text.append_text(state_label(record.state, record.reason, emoji=self.console_app.emoji))
            if record.stage:
                text.append(f" · stage {record.stage}", style="dim")
            text.append(f"\n{run_title(record)}")
            if record.pr_number:
                text.append(f"\nPR #{record.pr_number}", style="bold")
                if record.branch:
                    text.append(f" · {record.branch}", style="dim")
            rounds = f"rounds review {record.review_rounds} · ci {record.ci_rounds}"
            text.append(f"\n{rounds}", style="dim")
            last = (runs.last_event_by_run.get(run_id) if runs else None) or record.updated_at
            text.append(f"\nlast event {age(last, now)}", style="dim")
        else:
            text.append(f"\n{current.get('title', '')}")
        return text

    def _waits(self, state: ConsoleState) -> Any:
        items = state.items
        if items is None or (not items.gates and not items.holds):
            return Text("nobody is waiting on you", style="dim")
        text = Text()
        emoji = self.console_app.emoji
        gate_mark = "⏸ " if emoji else ""
        hold_mark = "👀 " if emoji else ""
        for gate in items.gates:
            line = (
                f"{gate_mark}**{gate.item_id}** ready to merge · PR #{gate.pr_number} "
                f"· run `{gate.run_id}`"
            )
            text.append_text(to_rich(line))
            text.append("\n")
        for hold in items.holds:
            what = "held in draft" if hold.held_by_draft else "awaiting review"
            line = (
                f"{hold_mark}**{hold.item_id}** {what} · PR #{hold.pr_number} · {hold.state} "
                f"· run `{hold.run_id}`"
            )
            text.append_text(to_rich(line))
            text.append("\n")
        return text

    def on_data_table_row_selected(self, event: ConsoleTable.RowSelected) -> None:
        key = event.row_key.value
        if key is None:
            return
        if event.data_table.id == "recent":
            self.console_app.open_run(str(key))
            return
        state_runs = self.console_app.state.runs
        if event.data_table.id == "queue" and state_runs is not None:
            run = next((r for r, i in state_runs.item_by_run.items() if i == key), None)
            if run:
                self.console_app.open_run(run)
