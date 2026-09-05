"""Chat: the control channel — the concierge, `!sbx` verbs and daemon
notices — as the local bridge's rows."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical

from sbxloop.config import TUI_CONTROL_CHANNEL
from sbxloop.tui.data import ConsoleState
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.widgets.chat_input import ChatInput
from sbxloop.tui.widgets.thread import ThreadView


class ChatScreen(ConsoleScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "reply", "Reply to the bot", show=True),
        Binding("i", "compose", "Type", show=True),
        *[Binding(str(n), f"pick({n})", "Pick", show=False) for n in range(1, 6)],
    ]

    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with Vertical(id="body"):
            yield ThreadView(TUI_CONTROL_CHANNEL)
        yield from self.compose_footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one(ChatInput).focus()

    def refresh_data(self, state: ConsoleState) -> None:
        super().refresh_data(state)
        self.query_one(ThreadView).pull()

    def action_reply(self) -> None:
        self.query_one(ThreadView).reply_to_focused()

    def action_pick(self, index: int) -> None:
        # Reached only when the form is not focused: a focused Input keeps
        # every printable key. Esc leaves the form, `i` returns to it. With
        # no question open the number is the mode key it is everywhere else.
        if self.query_one(ThreadView).pick(index):
            return
        modes = {1: "overview", 2: "runs", 3: "items", 4: "chat"}
        if index in modes:
            self.console_app.action_mode(modes[index])

    def action_compose(self) -> None:
        self.query_one(ChatInput).focus()
