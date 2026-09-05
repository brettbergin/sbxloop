"""What every console screen shares: the status bar on top, the footer
below, and a ``refresh_data`` hook the app calls after each snapshot."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer

from sbxloop.tui.context import console_of
from sbxloop.tui.data import ConsoleState
from sbxloop.tui.widgets.statusbar import StatusBar

if TYPE_CHECKING:
    from sbxloop.tui.app import SbxloopTui


class ConsoleScreen(Screen[Any]):
    DEFAULT_CSS = """
    ConsoleScreen { layout: vertical; }
    #body { height: 1fr; }
    """

    @property
    def console_app(self) -> SbxloopTui:
        return console_of(self)

    def compose_frame(self) -> Any:
        yield StatusBar(id="statusbar")

    def compose_footer(self) -> Any:
        yield Footer()

    def on_mount(self) -> None:
        self.repaint()

    def on_screen_resume(self) -> None:
        self.repaint()

    def repaint(self) -> None:
        """Repaint from the app's latest snapshot, once this screen has its
        widgets (a mode switch repaints before the new screen composes)."""
        if not self.is_mounted:
            return
        try:
            self.refresh_data(self.console_app.state)
        except NoMatches:
            return

    def refresh_data(self, state: ConsoleState) -> None:
        """Repaint from the given snapshot; the base repaints the bar."""
        bar = self.query_one("#statusbar", StatusBar)
        bar.show(state, emoji=self.console_app.emoji, unread=self.console_app.unread())
