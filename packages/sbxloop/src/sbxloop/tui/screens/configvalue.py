"""One setting, edited on its own.

The dialog the Config screen opens on a key: what the key holds, what it
holds *now* and which layer said so, which file the answer is written to,
and a widget shaped by the key's type — a picker for a bool or a fixed
set, one item per line for a list, a line of text for everything else.
``^U`` unsets the key instead, so the file stops saying anything about it
and the layer beneath is what the loader sees.

Nothing here writes: the dialog returns a :class:`ValueEdit`, and the
screen that opened it puts the change through the loader before the file
is touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Select, Static, TextArea

from sbxloop.tui.configkeys import FieldSpec, parse_value, render_value


@dataclass(frozen=True)
class ValueEdit:
    """What the operator asked for: a new value, or the key removed."""

    path: str
    value: Any = None
    unset: bool = False


class ValueScreen(ModalScreen[ValueEdit | None]):
    """Edit one key. ``Esc`` cancels; nothing is written from here."""

    DEFAULT_CSS = """
    ValueScreen { align: center middle; }
    ValueScreen #dialog {
        width: 88; max-width: 95%; height: auto; max-height: 80%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    ValueScreen .key { text-style: bold; padding: 0; }
    ValueScreen .facts { color: $text-muted; padding: 0 0 1 0; }
    ValueScreen .hint { color: $text-muted; padding: 1 0 0 0; }
    ValueScreen #value-list { height: 8; border: tall $primary-darken-2; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        # priority: a TextArea would otherwise eat every one of these.
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("ctrl+s", "apply", "Apply", show=False, priority=True),
        Binding("ctrl+u", "unset", "Unset", show=False, priority=True),
    ]

    def __init__(
        self,
        spec: FieldSpec,
        value: Any,
        *,
        source: str,
        target: str,
        in_file: bool,
    ) -> None:
        super().__init__()
        self.spec = spec
        self.value = value
        self.source = source
        self.target = target
        self.in_file = in_file
        #: A picker fires Changed once while it is being built; only a
        #: choice the operator made should apply the edit.
        self._ready = False

    # -- layout ------------------------------------------------------------------

    @property
    def can_unset(self) -> bool:
        """Removing the key is offered when the model allows it, and
        always when this file is the one saying it — the way back out of
        an edit that should never have been made here."""
        return self.spec.optional or self.in_file

    def _facts(self) -> Text:
        spec = self.spec
        text = Text()
        text.append(spec.summary, style="italic")
        shown = render_value(self.value, spec).replace("\n", ", ") or "(unset)"
        text.append(f"\nnow {shown[:200]} · from {self.source}")
        text.append(f"\nwrites to {self.target}")
        if not self.in_file:
            text.append(" (this file does not set it yet)")
        return text

    def _hint(self) -> str:
        parts = {
            "choice": "pick a value · ^S applies",
            "list": "one item per line · ^S applies",
        }.get(self.spec.kind, "Enter applies")
        if self.can_unset:
            parts += " · ^U unsets the key"
        return f"{parts} · Esc cancels"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(self.spec.path), classes="key")
            yield Static(self._facts(), classes="facts")
            yield from self._value_widget()
            yield Static(self._hint(), classes="hint")

    def _value_widget(self) -> ComposeResult:
        spec = self.spec
        current = render_value(self.value, spec)
        if spec.kind == "choice":
            options = [(choice, choice) for choice in spec.choices]
            yield Select(
                options,
                value=current if current in spec.choices else Select.BLANK,
                allow_blank=True,
                id="value-choice",
            )
        elif spec.kind == "list":
            yield TextArea(current, soft_wrap=False, id="value-list")
        else:
            yield Input(value=current, placeholder=spec.type_label, id="value-text")

    def on_mount(self) -> None:
        for widget in self.query("#value-choice, #value-list, #value-text"):
            widget.focus()
            break
        self.set_timer(0.05, self._arm)

    def _arm(self) -> None:
        self._ready = True

    # -- what the operator typed -------------------------------------------------

    def draft_text(self) -> str:
        for select in self.query(Select):
            chosen = select.value
            return "" if chosen is Select.BLANK else str(chosen)
        for area in self.query(TextArea):
            return area.text
        for box in self.query(Input):
            return box.value
        return ""  # pragma: no cover - one widget is always composed

    def action_apply(self) -> None:
        text = self.draft_text()
        if not text.strip():
            # An empty answer says nothing, so it can only mean "unset".
            self.action_unset()
            return
        try:
            value = parse_value(text, self.spec)
        except ValueError as exc:
            self.notify(str(exc)[:300], title="value", severity="error", timeout=10)
            return
        self.dismiss(ValueEdit(self.spec.path, value))

    def action_unset(self) -> None:
        if not self.can_unset:
            self.notify(
                f"{self.spec.path} always has a value: give it one, or Esc to leave it",
                title="value",
                severity="warning",
            )
            return
        self.dismiss(ValueEdit(self.spec.path, unset=True))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_apply()

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        if self._ready:
            self.action_apply()


__all__ = ["ValueEdit", "ValueScreen"]
