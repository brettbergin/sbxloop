"""One mailbox row on screen: who said it and when, the body, the
reactions, the ``(edited)`` mark, and — for a question or a gate prompt —
the buttons, each carrying what a click means."""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static

from sbxloop.daemon.discord_format import embed_from_json
from sbxloop.daemon.store import LocalMessage
from sbxloop.tui.chat import choice_spec
from sbxloop.tui.format import card, clock, to_commonmark, to_rich

#: Rows longer than this, or with several lines, render as Markdown.
_PANEL_THRESHOLD = 160


class ChoiceButton(Button):
    """One answer to a clarifying question; the click carries the value."""

    def __init__(self, row_id: int, index: int, value: str, label: str) -> None:
        super().__init__(f"{index} {label}"[:30], id=f"choice-{row_id}-{index}")
        self.row_id = row_id
        self.value = value


class ApproveButton(Button):
    """The merge gate's approve button, carrying the prompt row."""

    def __init__(self, row_id: int) -> None:
        super().__init__("Approve merge", id=f"approve-{row_id}", variant="success")
        self.row_id = row_id


class MessageWidget(Vertical):
    """A message row. ``show`` repaints it in place from a fresh row."""

    DEFAULT_CSS = """
    MessageWidget { height: auto; padding: 0 1; }
    MessageWidget.own { color: $text-muted; }
    MessageWidget.pending { opacity: 0.6; }
    MessageWidget Horizontal { height: auto; }
    MessageWidget Button { margin: 0 1 0 0; min-width: 8; }
    """

    def __init__(self, row: LocalMessage, *, clock: Callable[[], float]) -> None:
        pending = row.direction == "in" and row.taken_at is None
        classes = " ".join(
            c for c in ("own" if row.direction == "in" else "", "pending" if pending else "") if c
        )
        super().__init__(id=f"msg-{row.id}", classes=classes)
        self.row = row
        self.clock = clock
        self.pending = pending

    def compose(self) -> ComposeResult:
        yield Static(self.render_body(), id=f"body-{self.row.id}")
        spec = choice_spec(self.row)
        if spec is not None and spec.open(self.clock()):
            with Horizontal(id=f"choices-{self.row.id}"):
                for index, choice in enumerate(spec.question.choices, start=1):
                    yield ChoiceButton(self.row.id, index, choice.value, choice.label)
        # The row is the daemon's own authority on whether the prompt still
        # offers approval: it clears gate_run_id when the gate resolves.
        if self.row.kind == "gate" and self.row.gate_run_id:
            with Horizontal(id=f"gate-{self.row.id}"):
                yield ApproveButton(self.row.id)

    def show(self, row: LocalMessage) -> None:
        """Repaint from a newer copy of the same row."""
        self.row = row
        self.pending = row.direction == "in" and row.taken_at is None
        self.set_class(self.pending, "pending")
        try:
            self.query_one(f"#body-{row.id}", Static).update(self.render_body())
        except Exception:
            return
        spec = choice_spec(row)
        closed = (spec is not None and not spec.open(self.clock())) or (
            row.kind == "gate" and not row.gate_run_id
        )
        for button in self.query(Button):
            button.disabled = closed

    def render_body(self) -> RenderableType:
        row = self.row
        title = Text()
        title.append(clock(row.created_at), style="dim")
        if row.direction == "in":
            title.append(f"  {row.author_name}", style="bold green")
            if self.pending:
                title.append("  (sending…)", style="dim italic")
        else:
            title.append("  sbx", style="bold cyan")
        if row.reply_to_id:
            title.append(f"  ↩ {row.reply_to_id}", style="dim")
        if row.reactions:
            title.append("  " + "".join(row.reactions), style="yellow")
        if row.edited_at:
            title.append("  (edited)", style="dim italic")
        parts: list[RenderableType] = [title]
        text = row.text
        if row.embed_json:
            spec = embed_from_json(row.embed_json)
            if spec is not None:
                if text.strip():
                    parts.append(to_rich(text))
                parts.append(card(spec))
                return Group(*parts)
        if text.strip():
            if len(text) > _PANEL_THRESHOLD or "\n" in text or "```" in text:
                border = "cyan" if row.direction == "out" else "green"
                parts.append(
                    Panel(Markdown(to_commonmark(text)), border_style=border, padding=(0, 1))
                )
            else:
                parts.append(to_rich(text))
        return Group(*parts)


def choice_value(row: LocalMessage, index: int) -> str | None:
    """The value of a question row's 1-based choice, or None."""
    spec = choice_spec(row)
    return None if spec is None else spec.value_for(index)


__all__: list[str] = ["ApproveButton", "ChoiceButton", "MessageWidget", "choice_value"]
