"""DataTables with the console's navigation: vim keys beside the arrows,
``g``/``G`` for the ends, a row cursor, and a keyed refresh that repaints
cells in place — the cursor and the scroll position survive a tick."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, ClassVar

from textual.binding import Binding, BindingType
from textual.widgets import DataTable
from textual.widgets.data_table import ColumnKey


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
        self._column_keys: list[ColumnKey] = []
        self._keys: list[str] = []
        self._cells: dict[str, tuple[Any, ...]] = {}

    def on_mount(self) -> None:
        if self._columns and not self.columns:
            self._column_keys = self.add_columns(*self._columns)

    def replace_rows(self, rows: Iterable[tuple[str, Sequence[Any]]]) -> None:
        """Rows keyed by an id. When the key order is unchanged — the usual
        tick — only the cells that differ are updated, so nothing moves.
        Otherwise the table is rebuilt with the cursor back on the row it
        was on and the scroll offset kept."""
        wanted = [(key, tuple(cells)) for key, cells in rows]
        keys = [key for key, _ in wanted]
        if keys == self._keys and self._column_keys:
            for key, cells in wanted:
                old = self._cells.get(key)
                if old == cells:
                    continue
                for column_key, value in zip(self._column_keys, cells, strict=False):
                    self.update_cell(key, column_key, value)
                self._cells[key] = cells
            return
        selected = self.selected_key()
        scroll_y = self.scroll_y
        self.clear()
        self._keys = []
        self._cells = {}
        for key, cells in wanted:
            self.add_row(*cells, key=key)
            self._keys.append(key)
            self._cells[key] = cells
        if selected is not None and selected in self._keys:
            self.move_cursor(row=self._keys.index(selected), scroll=False)
        self.scroll_to(y=min(scroll_y, self.max_scroll_y), animate=False, force=True)

    def selected_key(self) -> str | None:
        if not self._keys or self.row_count == 0:
            return None
        row = self.cursor_row
        if 0 <= row < len(self._keys):
            return self._keys[row]
        return None
