"""The chat form: an Input with the address-the-bot toggle, a reply
target, and a history — the console's twin of the run TUI's ``ChatInput``."""

from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.widgets import Input

from sbxloop.tui.chat import MENTION


class ChatInput(Input):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+t", "toggle_address", "Address the bot", show=True),
        Binding("up", "history_back", "History", show=False),
        Binding("down", "history_forward", "History", show=False),
        Binding("ctrl+u", "clear_line", "Clear", show=False),
        Binding("escape", "leave", "Leave the form", show=False),
    ]
    DEFAULT_CSS = """
    ChatInput { dock: bottom; }
    ChatInput.addressed { border: tall $success; }
    """

    def __init__(self, *, thread: bool = False, prefix: str = "!sbx") -> None:
        super().__init__(placeholder="")
        self.thread = thread
        self.prefix = prefix
        self.addressed = False
        self.reply_to: int | None = None
        self.history: list[str] = []
        self._cursor = 0
        self._draft = ""
        self._hint()

    def _hint(self) -> None:
        what = "steer this run" if self.thread else "ask the concierge"
        if self.addressed or self.reply_to is not None:
            self.placeholder = (
                f"{MENTION} ▸ {what}… (ctrl+t: addressed ✓ · {self.prefix} for commands)"
            )
        else:
            self.placeholder = (
                f"{what} with {MENTION} or ctrl+t · {self.prefix} for commands · "
                "plain text is left alone"
            )
        self.set_class(self.addressed or self.reply_to is not None, "addressed")
        self.border_title = (
            f"reply → {self.reply_to}"
            if self.reply_to is not None
            else ("addressed" if self.addressed else "")
        )

    def action_toggle_address(self) -> None:
        self.addressed = not self.addressed
        self._hint()

    def set_reply(self, row_id: int | None) -> None:
        self.reply_to = row_id
        self._hint()

    def action_clear_line(self) -> None:
        self.value = ""

    def action_leave(self) -> None:
        """``Esc``: hand the keys back to the screen (``r``, ``1``-``5``,
        the mode numbers); a reply target is cleared first."""
        if self.reply_to is not None:
            self.set_reply(None)
            return
        self.screen.set_focus(None)

    def action_history_back(self) -> None:
        if not self.history:
            return
        if self._cursor == len(self.history):
            self._draft = self.value
        self._cursor = max(0, self._cursor - 1)
        self.value = self.history[self._cursor]

    def action_history_forward(self) -> None:
        if not self.history or self._cursor >= len(self.history):
            return
        self._cursor += 1
        self.value = (
            self._draft if self._cursor == len(self.history) else self.history[self._cursor]
        )

    def take(self) -> tuple[str, bool, int | None]:
        """The typed text with the gesture state, then reset for the next."""
        text = self.value.strip()
        addressed = self.addressed
        reply = self.reply_to
        if text:
            self.history.append(text)
            self.history = self.history[-100:]
        self._cursor = len(self.history)
        self.value = ""
        self.reply_to = None
        self._hint()
        return text, addressed, reply
