"""The console's dialogs: a yes/no confirmation, a typed confirmation for
the destructive tier, a one-line prompt, and a scrollable outcome."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

_DIALOG_CSS = """
ModalScreen { align: center middle; }
#dialog {
    width: 80; max-width: 95%; height: auto; max-height: 80%;
    border: thick $primary; background: $surface; padding: 1 2;
}
#dialog.danger { border: thick $error; }
#dialog .title { text-style: bold; padding: 0; }
#dialog .body { padding: 1 0; }
#dialog .hint { color: $text-muted; }
"""


class _Dialog[T](ModalScreen[T]):
    """A title, a prompt, an optional line of input, a hint."""

    DEFAULT_CSS = _DIALOG_CSS
    DANGER: ClassVar[bool] = False
    HINT: ClassVar[str] = ""

    def __init__(self, title: str, prompt: str) -> None:
        super().__init__()
        self.title_text = title
        self.prompt = prompt

    def input_widget(self) -> Input | None:
        return None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="danger" if self.DANGER else ""):
            yield Static(Text(self.title_text), classes="title")
            yield Static(self.prompt, classes="body")
            box = self.input_widget()
            if box is not None:
                yield box
            yield Static(self.HINT, classes="hint")

    def on_mount(self) -> None:
        for box in self.query(Input):
            box.focus()


class ConfirmScreen(_Dialog[bool]):
    """``y`` confirms, ``n``/``Esc`` declines."""

    HINT = "y confirms · n cancels"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "yes", "Yes"),
        Binding("enter", "yes", "Yes", show=False),
        Binding("n", "no", "No"),
        Binding("escape", "no", "No", show=False),
    ]

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class TypedConfirmScreen(_Dialog[bool]):
    """The destructive tier: the operator types ``word`` exactly."""

    DANGER = True
    HINT = "Enter confirms · Esc cancels"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "no", "Cancel", show=False)]

    def __init__(self, title: str, prompt: str, word: str) -> None:
        super().__init__(title, prompt)
        self.word = word

    def input_widget(self) -> Input:
        return Input(placeholder=f"type {self.word} to confirm", id="typed")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip() == self.word:
            self.dismiss(True)
            return
        self.notify(f"type {self.word} exactly to confirm", severity="warning")

    def action_no(self) -> None:
        self.dismiss(False)


class TextPromptScreen(_Dialog[str | None]):
    """One line of text; ``Esc`` yields None."""

    HINT = "Enter submits · Esc cancels"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self, title: str, prompt: str, *, placeholder: str = "", password: bool = False
    ) -> None:
        super().__init__(title, prompt)
        self.placeholder = placeholder
        self.password = password

    def input_widget(self) -> Input:
        return Input(placeholder=self.placeholder, password=self.password, id="text")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class OutcomeScreen(ModalScreen[None]):
    """Long output — a prune table, an upgrade log — to read and close."""

    DEFAULT_CSS = (
        _DIALOG_CSS
        + """
    OutcomeScreen #dialog { width: 110; }
    OutcomeScreen VerticalScroll { height: auto; max-height: 30; }
    """
    )
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
        Binding("enter", "close", "Close", show=False),
    ]

    def __init__(self, title: str, text: str, *, ok: bool = True) -> None:
        super().__init__()
        self.title_text = title
        self.text = text
        self.ok = ok

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="" if self.ok else "danger"):
            yield Static(
                Text(self.title_text, style="bold" if self.ok else "bold red"), classes="title"
            )
            with VerticalScroll():
                yield Static(Text(self.text), classes="body")
            yield Static("Esc closes", classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)


__all__ = ["ConfirmScreen", "OutcomeScreen", "TextPromptScreen", "TypedConfirmScreen"]
