"""Runs: every run the store knows, newest first; Enter opens one."""

from __future__ import annotations

import time
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Input

from sbxloop.tui.data import ConsoleState
from sbxloop.tui.format import age, run_title, state_label
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.widgets.tables import ConsoleTable


class RunsScreen(ConsoleScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("slash", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("enter", "open", "Open", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.filter_text = ""

    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with Vertical(id="body"):
            yield Input(placeholder="filter runs (id, state, item, title)", id="filter")
            yield ConsoleTable(
                "run",
                "state",
                "stage",
                "item",
                "repo",
                "title",
                "PR",
                "rounds",
                "updated",
                id="runs",
            )
        yield from self.compose_footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#filter", Input).display = False
        self.query_one("#runs", ConsoleTable).focus()

    def refresh_data(self, state: ConsoleState) -> None:
        super().refresh_data(state)
        runs = state.runs
        if runs is None:
            return
        emoji = self.console_app.emoji
        now = time.time()
        needle = self.filter_text.lower()
        rows = []
        for r in runs.runs:
            item = runs.item_for(r.run_id) or ""
            hay = f"{r.run_id} {r.state} {item} {run_title(r)} {r.reason or ''}".lower()
            if needle and needle not in hay:
                continue
            rows.append(
                (
                    r.run_id,
                    (
                        r.run_id,
                        state_label(r.state, r.reason, emoji=emoji),
                        r.stage or "—",
                        item or "—",
                        runs.repo_for(r.run_id) or "—",
                        run_title(r),
                        f"#{r.pr_number}" if r.pr_number else "—",
                        f"{r.review_rounds}/{r.ci_rounds}",
                        age(r.updated_at, now),
                    ),
                )
            )
        self.query_one("#runs", ConsoleTable).replace_rows(rows)

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.display = True
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.value = ""
        self.filter_text = ""
        box.display = False
        self.query_one("#runs", ConsoleTable).focus()
        self.refresh_data(self.console_app.state)

    def on_input_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value
        self.refresh_data(self.console_app.state)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#runs", ConsoleTable).focus()

    def action_open(self) -> None:
        key = self.query_one("#runs", ConsoleTable).selected_key()
        if key:
            self.console_app.open_run(key)

    def on_data_table_row_selected(self, event: ConsoleTable.RowSelected) -> None:
        if event.row_key.value is not None:
            self.console_app.open_run(str(event.row_key.value))
