"""A Static that remembers what it shows, so a screen (or a test) can read
it back without reaching into Textual's rendering internals."""

from __future__ import annotations

from typing import Any

from textual.widgets import Static


class TextPanel(Static):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.content_value: Any = args[0] if args else ""

    def update(self, content: Any = "", *, layout: bool = True) -> None:
        self.content_value = content
        super().update(content, layout=layout)

    @property
    def content_text(self) -> str:
        value = self.content_value
        plain = getattr(value, "plain", None)
        if isinstance(plain, str):
            return plain
        return str(value)
