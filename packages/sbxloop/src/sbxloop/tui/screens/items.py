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
from sbxloop.daemon.model import WorkItem
from sbxloop.tui import actions
from sbxloop.tui.data import ConsoleState
from sbxloop.tui.format import age
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.screens.modals import TextPromptScreen
from sbxloop.tui.widgets.tables import ConsoleTable


class ItemsScreen(ConsoleScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "open", "Open latest run"),
        Binding("t", "retry", "Retry"),
        Binding("u", "requeue", "Requeue", show=False),
        Binding("A", "abandon", "Abandon"),
        Binding("w", "resume_review", "Check review now", show=False),
        Binding("m", "merge", "Approve merge", show=False),
        Binding("n", "new_run", "New run (ask the concierge)"),
        Binding("N", "run_here", "Run here, detached", show=False),
    ]

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
                    self._eligible(items.eligible_at.get(i.item_id, 0.0), now, i),
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

    @staticmethod
    def _eligible(at: float, now: float, item: WorkItem) -> str:
        """When the daemon's own rule lets the item go — a resume first,
        a scheduled retry or a backoff at its clock, else now."""
        if item.run_id:
            return "resume, first"
        if at <= now:
            return "now"
        return time.strftime("%H:%M", time.localtime(at))

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
        item = self._item(item_id)
        if item is None or not item.run_id:
            self.app.notify("this item has no run yet", severity="warning")
            return
        self.console_app.open_run(item.run_id)

    # -- item verbs ------------------------------------------------------------------

    def _item(self, item_id: str) -> WorkItem | None:
        items = self.console_app.state.items
        return next((i for i in (items.items if items else ()) if i.item_id == item_id), None)

    def _selected(self) -> WorkItem | None:
        for table_id in ("items", "queued"):
            table = self.query_one(f"#{table_id}", ConsoleTable)
            if table.has_focus:
                key = table.selected_key()
                return self._item(key) if key else None
        key = self.query_one("#items", ConsoleTable).selected_key()
        return self._item(key) if key else None

    def action_retry(self) -> None:
        item = self._selected()
        if item is not None:
            self.console_app.perform(actions.retry(self.console_app.deps, item.item_id))

    def action_requeue(self) -> None:
        item = self._selected()
        if item is not None:
            self.console_app.perform(actions.requeue(self.console_app.deps, item.item_id))

    def action_abandon(self) -> None:
        item = self._selected()
        if item is not None:
            self.console_app.perform(actions.abandon(self.console_app.deps, item.item_id))

    def action_resume_review(self) -> None:
        item = self._selected()
        if item is not None:
            self.console_app.perform(actions.resume_review(self.console_app.deps, item.item_id))

    def action_merge(self) -> None:
        item = self._selected()
        if item is None:
            return
        items = self.console_app.state.items
        gates = items.gates if items else ()
        if not any(g.item_id == item.item_id for g in gates):
            self.app.notify(f"{item.item_id} has no open merge gate", severity="warning")
            return
        self.console_app.perform(actions.merge(self.console_app.deps, item.item_id))

    def action_new_run(self) -> None:
        """A run the daemon's way: ask the concierge to file the issue."""

        def submitted(text: str | None) -> None:
            if text:
                self.console_app.perform(actions.ask_concierge_to_file(self.console_app.deps, text))

        self.app.push_screen(
            TextPromptScreen(
                "new run",
                "Describe the outcome. The concierge files it as an issue with the trigger "
                "label and the daemon picks it up.",
                placeholder="the outcome you want",
            ),
            submitted,
        )

    def action_run_here(self) -> None:
        """A run outside the daemon: a detached `sbxloop run` on this host."""

        def submitted(text: str | None) -> None:
            if text:
                self.console_app.perform(actions.run_text(self.console_app.deps, text))

        self.app.push_screen(
            TextPromptScreen(
                "run here, detached",
                "Describe the outcome. `sbxloop run` starts in its own session with this "
                "checkout's config; the daemon is not involved.",
                placeholder="the outcome you want",
            ),
            submitted,
        )
