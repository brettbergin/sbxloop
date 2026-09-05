"""DataTables with the console's navigation: vim keys beside the arrows,
``g``/``G`` for the ends, a row cursor, and a stable-key refresh that
keeps the cursor on the same row across repaints."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, ClassVar

from textual.binding import Binding, BindingType
from textual.widgets import DataTable


class ConsoleTable(DataTable[Any]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "scroll_top", "Top", show=False),
        Binding("G", "scroll_bottom", "Bottom", show=False),
        Binding("ctrl+d", "page_down", "Page down", show=False),
        Binding("ctrl+u", "page_up", "Page up", show=False),
    ]

    def __init__(self, *columns: str, **kwargs: Any) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
        self._columns = columns
        self._keys: list[str] = []

    def on_mount(self) -> None:
        if self._columns and not self.columns:
            self.add_columns(*self._columns)

    def replace_rows(self, rows: Iterable[tuple[str, Sequence[Any]]]) -> None:
        """Rows keyed by an id: the table is rebuilt, and the cursor lands
        back on the row it was on when that row survived."""
        selected = self.selected_key()
        self.clear()
        self._keys = []
        for key, cells in rows:
            self.add_row(*cells, key=key)
            self._keys.append(key)
        if selected is not None and selected in self._keys:
            self.move_cursor(row=self._keys.index(selected))

    def selected_key(self) -> str | None:
        if not self._keys or self.row_count == 0:
            return None
        row = self.cursor_row
        if 0 <= row < len(self._keys):
            return self._keys[row]
        return None
