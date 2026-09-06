"""What every console screen shares: the navigation rail down the left,
the status bar on top of what is left, the footer below, and a
``refresh_data`` hook the app calls after each snapshot.

The rail is docked, so it costs a screen nothing: a screen composes its
body exactly as it did before and the rail takes its column beside it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer

from sbxloop.tui.context import console_of
from sbxloop.tui.data import ConsoleState
from sbxloop.tui.widgets.navrail import MIN_WIDTH_FOR_RAIL, NavButton, NavRail
from sbxloop.tui.widgets.statusbar import StatusBar

if TYPE_CHECKING:
    from sbxloop.tui.app import SbxloopTui


class ConsoleScreen(Screen[Any]):
    DEFAULT_CSS = """
    ConsoleScreen { layout: vertical; }
    #body { height: 1fr; }
    ConsoleScreen.-narrow NavRail { display: none; }
    """

    @property
    def console_app(self) -> SbxloopTui:
        return console_of(self)

    def compose_frame(self) -> Any:
        yield NavRail(id="navrail")
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
        """Repaint from the given snapshot; the base repaints the bar and
        the rail."""
        unread = self.console_app.unread()
        bar = self.query_one("#statusbar", StatusBar)
        bar.show(state, emoji=self.console_app.emoji, unread=unread)
        try:
            rail = self.query_one("#navrail", NavRail)
        except NoMatches:
            return
        rail.show(active=self.console_app.current_mode, state=state, unread=unread)

    def on_resize(self) -> None:
        self._fit_rail()

    def _fit_rail(self) -> None:
        """A rail costs columns a narrow terminal has not got; below the
        threshold it is hidden and the number keys still reach everything."""
        self.set_class(self.size.width < MIN_WIDTH_FOR_RAIL, "-narrow")

    def on_nav_button_selected(self, event: NavButton.Selected) -> None:
        """Clicking a rail row is the same verb as pressing its key."""
        event.stop()
        self.console_app.action_mode(event.mode)
