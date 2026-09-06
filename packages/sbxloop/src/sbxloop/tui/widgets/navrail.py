"""The console's navigation: one rail down the left of every screen.

The screens were reachable only by their number keys, listed in a footer
that also carries whatever the current screen binds — so where you *are*
and where you can *go* were mixed in with what you can *do*. The rail
separates them: it is the map, the footer stays the verbs.

It is also where a screen says it wants attention without you being on it.
A badge on Queue is the depth, on Chat the unread control-channel rows, on
Daemon the gates and holds waiting for a human — the three things an
operator otherwise finds only by looking.

:data:`NAV` is the single source of the console's shape: the rail renders
it and the app builds its bindings from it, so a screen cannot appear in
one and not the other.
"""

from __future__ import annotations

from typing import NamedTuple

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from sbxloop.tui.data import ConsoleState


class NavItem(NamedTuple):
    """One screen: the key that reaches it, its mode name, its label."""

    key: str
    mode: str
    label: str


#: Every screen the console has, in the order the rail lists them.
NAV: tuple[NavItem, ...] = (
    NavItem("1", "overview", "Overview"),
    NavItem("2", "runs", "Runs"),
    NavItem("3", "items", "Queue"),
    NavItem("4", "chat", "Chat"),
    NavItem("5", "sandboxes", "Sandboxes"),
    NavItem("6", "daemon", "Daemon"),
    NavItem("7", "config", "Config"),
    NavItem("8", "doctor", "Doctor"),
    NavItem("?", "help", "Help"),
)

#: The rail's width, and the narrowest screen that still gets one. Below
#: this the rail is hidden: 80 columns minus the rail leaves too little for
#: a run's thread or the config table, and every screen is still one
#: keystroke away.
RAIL_WIDTH = 15
MIN_WIDTH_FOR_RAIL = 90


def badges(state: ConsoleState, unread: int) -> dict[str, int]:
    """What each screen wants to say from a rail you are not looking at."""
    items = state.items
    out: dict[str, int] = {}
    if items is not None and items.queued:
        out["items"] = len(items.queued)
    if unread:
        out["chat"] = unread
    if items is not None and (items.gates or items.holds):
        out["daemon"] = len(items.gates) + len(items.holds)
    return out


class NavButton(Static):
    """One row of the rail. Clicking it switches to that screen."""

    class Selected(Message):
        def __init__(self, mode: str) -> None:
            super().__init__()
            self.mode = mode

    active: reactive[bool] = reactive(False)
    badge: reactive[int] = reactive(0)

    def __init__(self, item: NavItem) -> None:
        super().__init__(id=f"nav-{item.mode}")
        self.item = item

    def render(self) -> Text:
        text = Text()
        text.append(f" {self.item.key} ", style="bold" if self.active else "dim")
        text.append(f"{self.item.label:<10}", style="bold" if self.active else "")
        # A Rich style, not CSS: `$`-variables are the stylesheet's and Rich
        # cannot parse them. Palette names follow the terminal's theme.
        text.append(f"{self.badge or '':>2} ", style="bold yellow" if self.badge else "dim")
        return text

    def watch_active(self) -> None:
        self.set_class(self.active, "-active")

    def on_click(self) -> None:
        self.post_message(self.Selected(self.item.mode))


class NavRail(Widget):
    """The rail itself: one button per screen, the current one marked."""

    DEFAULT_CSS = f"""
    NavRail {{
        dock: left; width: {RAIL_WIDTH}; background: $panel; padding: 1 0 0 0;
    }}
    NavRail NavButton {{ height: 1; color: $text-muted; }}
    NavRail NavButton.-active {{ background: $primary; color: $text; }}
    NavRail NavButton:hover {{ background: $boost; }}
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            for item in NAV:
                yield NavButton(item)

    def show(self, *, active: str | None, state: ConsoleState, unread: int) -> None:
        """Mark the screen being shown and repaint the badges."""
        counts = badges(state, unread)
        for button in self.query(NavButton):
            button.active = button.item.mode == active
            button.badge = counts.get(button.item.mode, 0)


__all__ = [
    "MIN_WIDTH_FOR_RAIL",
    "NAV",
    "RAIL_WIDTH",
    "NavButton",
    "NavItem",
    "NavRail",
    "badges",
]
