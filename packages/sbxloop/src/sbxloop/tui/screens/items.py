"""Queue: what the daemon will dispatch next, and every work item with
its attempts, pinned run and last error."""

from __future__ import annotations

import time
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Static

from sbxloop.daemon.discord_format import ITEM_STATE_MARKER
from sbxloop.tui.data import ConsoleState
from sbxloop.tui.format import age
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.widgets.tables import ConsoleTable


class ItemsScreen(ConsoleScreen):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("enter", "open", "Open latest run")]

    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with Vertical(id="body"):
            yield Static("queued, in dispatch order", classes="title")
            yield ConsoleTable("item", "repo", "title", "attempts", "not before", id="queued")
            yield Static("all items", classes="title")
            yield ConsoleTable(
                "item",
                "repo",
                "state",
                "attempts",
                "run",
                "title",
                "last error",
                "updated",
                id="items",
            )
        yield from self.compose_footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#items", ConsoleTable).focus()

    def refresh_data(self, state: ConsoleState) -> None:
        super().refresh_data(state)
        items = state.items
        if items is None:
            return
        now = time.time()
        emoji = self.console_app.emoji
        self.query_one("#queued", ConsoleTable).replace_rows(
            (
                i.item_id,
                (
                    i.item_id,
                    i.repo or "—",
                    i.title[:60],
                    str(i.attempts),
                    (
                        time.strftime("%H:%M", time.localtime(i.not_before))
                        if i.not_before and i.not_before > now
                        else "now"
                    ),
                ),
            )
            for i in items.queued
        )
        self.query_one("#items", ConsoleTable).replace_rows(
            (
                i.item_id,
                (
                    i.item_id,
                    i.repo or "—",
                    f"{ITEM_STATE_MARKER.get(i.state, '·') if emoji else ''} {i.state}".strip(),
                    str(i.attempts),
                    i.run_id or "—",
                    i.title[:50],
                    (i.last_error or "")[:60],
                    age(i.updated_at, now),
                ),
            )
            for i in items.items
        )

    def action_open(self) -> None:
        for table_id in ("items", "queued"):
            table = self.query_one(f"#{table_id}", ConsoleTable)
            if table.has_focus:
                self._open_item(table.selected_key())
                return

    def on_data_table_row_selected(self, event: ConsoleTable.RowSelected) -> None:
        self._open_item(None if event.row_key.value is None else str(event.row_key.value))

    def _open_item(self, item_id: str | None) -> None:
        if not item_id:
            return
        items = self.console_app.state.items
        item = next((i for i in (items.items if items else ()) if i.item_id == item_id), None)
        if item is None or not item.run_id:
            self.app.notify("this item has no run yet", severity="warning")
            return
        self.console_app.open_run(item.run_id)
