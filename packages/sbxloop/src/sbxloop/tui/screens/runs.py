"""Runs: every run the store knows — what it was asked to do, how it went,
what it cost, and one keystroke to whichever of those you want to chase.

Three things this screen had wrong, each of which made it less useful the
longer the host ran.

**It was ordered by the wrong clock.** Runs came back newest-*started*
first while the last column showed when each was last *touched*, so a run
that began on Tuesday and merged this morning sat far down a list whose
own column said "2m ago". The limit was applied to that order too, so a
long-lived run could fall off the end while still being worked on.

**One long reason ate the table.** The state cell carried its failure
reason inline and unclipped; a 409-character `github op` error sized the
column to fit and pushed the other eight off the screen. The state is a
word now, and the reason — in full, unclipped — goes under the table for
the row the cursor is on, where there is room for it.

**It never showed what a run cost.** Turns and working time are in the
store per run; nothing in a list had ever shown them.

The columns adapt to the width rather than overflowing: the ones you
cannot reconstruct from an id survive longest.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar, NamedTuple

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Input

from sbxloop.engine.model import RunRecord
from sbxloop.tui import actions
from sbxloop.tui.data import ConsoleState, RunsSnapshot
from sbxloop.tui.format import age, run_title, state_label
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable


class Column(NamedTuple):
    """One column, and how hard it fights for its width.

    ``priority`` orders what survives a narrow terminal: the run's id and
    what it was asked to do cannot be reconstructed from anything else, so
    they go last; the repository is near-noise on a single-repo host, so it
    goes first.
    """

    key: str
    title: str
    width: int
    priority: int


COLUMNS: tuple[Column, ...] = (
    Column("run", "run", 10, 100),
    Column("state", "state", 13, 95),
    Column("title", "title", 40, 90),
    Column("updated", "updated", 9, 85),
    Column("pr", "PR", 6, 70),
    Column("turns", "turns", 6, 65),
    Column("took", "took", 8, 60),
    Column("stage", "stage", 10, 50),
    Column("rounds", "rounds", 7, 40),
    Column("item", "item", 15, 30),
    Column("repo", "repo", 18, 20),
)


def fit(width: int) -> tuple[Column, ...]:
    """The columns that fit in ``width``, in their display order.

    Dropping the lowest-priority column until the rest fit is what keeps a
    long value from pushing everything else off the screen — the failure
    this replaces did the opposite and let one cell win."""
    chosen = list(COLUMNS)
    while chosen and sum(c.width for c in chosen) > width:
        weakest = min(chosen, key=lambda c: c.priority)
        chosen.remove(weakest)
    return tuple(c for c in COLUMNS if c in chosen)


def clip(value: Any, width: int) -> Any:
    """Hold a cell to its column's width.

    ``fit`` budgets the columns, but a DataTable sizes a column to its
    widest cell — so without this a long title simply takes the width the
    budget gave to everything after it, which is the failure this screen
    is fixing in the first place."""
    if isinstance(value, Text):
        return value if len(value.plain) <= width else Text(value.plain[: width - 1] + "…")
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def took(seconds: float) -> str:
    total = int(max(seconds, 0))
    if not total:
        return "—"
    if total >= 3600:
        return f"{total // 3600}h{total % 3600 // 60:02d}"
    if total >= 60:
        return f"{total // 60}m"
    return f"{total}s"


class RunsScreen(ConsoleScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("slash", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("enter", "open", "Open", show=True),
        Binding("p", "open_pr", "Open PR"),
    ]
    DEFAULT_CSS = """
    RunsScreen #runs { height: 1fr; }
    RunsScreen #detail { height: auto; max-height: 4; padding: 0 1; color: $text-muted; }
    RunsScreen #filter { display: none; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.filter_text = ""
        self._columns: tuple[Column, ...] = ()

    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with Vertical(id="body"):
            yield Input(placeholder="filter runs (id, state, item, title, reason)", id="filter")
            yield ConsoleTable(id="runs")
            yield TextPanel("", id="detail")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#filter", Input).display = False
        self.query_one("#runs", ConsoleTable).focus()

    def on_resize(self) -> None:
        super().on_resize()
        self.repaint()

    # -- rows --------------------------------------------------------------------

    def order(self, runs: RunsSnapshot, live: str | None) -> list[RunRecord]:
        """Most recently touched first, with the run in flight pinned on
        top: observing wants the live one, reviewing wants what moved."""
        rows = sorted(runs.runs, key=lambda r: -r.updated_at)
        if live is None:
            return rows
        current = [r for r in rows if r.run_id == live]
        return current + [r for r in rows if r.run_id != live]

    def cells(self, record: RunRecord, runs: RunsSnapshot, now: float) -> dict[str, Any]:
        turns, active = runs.cost_for(record.run_id)
        item = runs.item_for(record.run_id) or "—"
        return {
            "run": record.run_id,
            # The state alone. Its reason is under the table, in full.
            "state": state_label(record.state, None, emoji=self.console_app.emoji),
            "title": run_title(record),
            "updated": age(record.updated_at, now),
            "pr": f"#{record.pr_number}" if record.pr_number else "—",
            "turns": str(turns) if turns else "—",
            "took": took(active),
            "stage": record.stage or "—",
            "rounds": f"{record.review_rounds}/{record.ci_rounds}",
            "item": item,
            "repo": runs.repo_for(record.run_id) or "—",
        }

    def refresh_data(self, state: ConsoleState) -> None:
        super().refresh_data(state)
        runs = state.runs
        if runs is None:
            return
        table = self.query_one("#runs", ConsoleTable)
        columns = fit(max(table.size.width, 40))
        if columns != self._columns:
            self._columns = columns
            table.set_columns(*(c.title for c in columns))
        status = (state.daemon.status if state.daemon and state.daemon.live else None) or {}
        live = ((status.get("current") or {}) or {}).get("run_id")
        now = time.time()
        needle = self.filter_text.lower()
        rows = []
        for record in self.order(runs, str(live) if live else None):
            item = runs.item_for(record.run_id) or ""
            hay = (
                f"{record.run_id} {record.state} {item} {run_title(record)} {record.reason or ''}"
            ).lower()
            if needle and needle not in hay:
                continue
            cells = self.cells(record, runs, now)
            rows.append((record.run_id, tuple(clip(cells[c.key], c.width) for c in columns)))
        table.replace_rows(rows)
        self.show_detail()

    # -- the row under the cursor -------------------------------------------------

    def selected(self) -> RunRecord | None:
        key = self.query_one("#runs", ConsoleTable).selected_key()
        runs = self.console_app.state.runs
        if key is None or runs is None:
            return None
        return next((r for r in runs.runs if r.run_id == key), None)

    def show_detail(self) -> None:
        """Everything the row could not carry, for the run the cursor is
        on: the reason in full, where the work went, and what to press."""
        record = self.selected()
        panel = self.query_one("#detail", TextPanel)
        if record is None:
            panel.update(Text("no run selected", style="dim"))
            return
        text = Text()
        text.append(record.run_id, style="bold")
        if record.reason:
            # In full. This is the cell that used to size the table.
            text.append(f" · {record.reason}")
        elif record.last_verdict:
            text.append(f" · last verdict {record.last_verdict}", style="dim")
        else:
            text.append(f" · {run_title(record)}", style="dim")
        second = Text()
        if record.branch:
            second.append(f"{record.branch}  ", style="dim")
        if record.pr_url:
            second.append(f"{record.pr_url}  ", style="dim")
        if record.exhausted:
            second.append(f"exhausted: {record.exhausted}", style="yellow")
        if second.plain.strip():
            text.append("\n")
            text.append_text(second)
        panel.update(text)

    def on_data_table_row_highlighted(self, event: ConsoleTable.RowHighlighted) -> None:
        self.show_detail()

    # -- actions -----------------------------------------------------------------

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.display = True
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.value = ""
        self.filter_text = ""
        box.display = False
        self.query_one("#runs", ConsoleTable).focus()
        self.refresh_data(self.console_app.state)

    def on_input_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value
        self.refresh_data(self.console_app.state)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#runs", ConsoleTable).focus()

    def action_open(self) -> None:
        key = self.query_one("#runs", ConsoleTable).selected_key()
        if key:
            self.console_app.open_run(key)

    def action_open_pr(self) -> None:
        record = self.selected()
        if record is None or not record.pr_url:
            self.app.notify("this run has no pull request", title="runs", severity="warning")
            return
        self.console_app.perform(actions.open_url(record.pr_url))

    def on_data_table_row_selected(self, event: ConsoleTable.RowSelected) -> None:
        if event.row_key.value is not None:
            self.console_app.open_run(str(event.row_key.value))


__all__ = ["COLUMNS", "Column", "RunsScreen", "clip", "fit", "took"]
