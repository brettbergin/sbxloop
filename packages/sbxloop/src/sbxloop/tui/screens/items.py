"""Queue: what is stuck, and what runs next.

The screen used to be two tables — the dispatch order, and a dump of every
work item that ever existed — with no filter, half its verbs hidden from
the footer, and the merge gates and review holds loaded into state but
never drawn at all. Pressing ``m`` on the wrong row answered "no open merge
gate", which is the console telling you to guess again.

It is grouped by *what you would do about it* now. Four sections, in the
order a person triages: what is waiting on you, what is running, what goes
next, and what stopped. ``done`` items are not here — they are finished
work, and Runs already lists them.

Two layers decide whether anything moves, and both are shown because both
are true. An item has its own reason (a retry clock, an attempt backoff)
and the daemon has a global one (its breaker, the daily cap, a pause).
When the daemon is blocked nothing dispatches whatever the rows say, so
the block is stated once at the top and the queued rows are dimmed — they
keep their own reason rather than all claiming the daemon's.
"""

from __future__ import annotations

import time
from typing import ClassVar, NamedTuple

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Input, Static

from sbxloop.daemon.discord_format import ITEM_STATE_MARKER
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import MergeGate, ReviewHold
from sbxloop.tui import actions
from sbxloop.tui.data import ConsoleState, ItemsSnapshot
from sbxloop.tui.format import age
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.screens.modals import TextPromptScreen
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable

#: How far back the parked section reaches, matching Overview's window so
#: the two screens describe the same week. `/` lifts it: a search that
#: cannot reach the item you typed is not a search.
PARKED_WINDOW_S = 7 * 86400.0


class Section(NamedTuple):
    """One group of items, and the columns that describe them."""

    key: str
    title: str
    states: tuple[str, ...]
    columns: tuple[str, ...]


SECTIONS: tuple[Section, ...] = (
    Section(
        "waiting",
        "waiting on you",
        ("gated", "awaiting_review", "paused_review"),
        ("item", "repo", "blocked on", "title", "waiting", "press"),
    ),
    Section("running", "running now", ("running",), ("item", "repo", "run", "title", "updated")),
    Section(
        "queued",
        "queued next",
        ("queued",),
        ("#", "item", "repo", "title", "attempts", "why"),
    ),
    Section(
        "parked",
        "parked / failed",
        ("failed", "blocked", "cancelled"),
        ("item", "repo", "state", "title", "last error", "updated"),
    ),
)

#: Which verb applies in which state. The footer is built from this, so a
#: key that cannot work is never offered — `m` used to be advertised on
#: every row and worked only on the few with an open gate.
VERB_STATES: dict[str, tuple[str, ...]] = {
    "retry": ("failed", "blocked", "cancelled", "queued"),
    # `running` belongs here: a run pinned by a daemon that died is the
    # thing you requeue, and unsticking it is what this screen is for.
    "requeue": ("failed", "blocked", "cancelled", "running"),
    "abandon": ("queued", "running", "failed", "blocked", "gated", "awaiting_review"),
    "resume_review": ("awaiting_review", "paused_review"),
}

#: The verbs whose offer depends on the row under the cursor.
CONTEXTUAL = frozenset({"retry", "requeue", "abandon", "resume_review", "merge", "open"})


class SectionTable(ConsoleTable):
    """A section's rows, with the cursor free to leave at either end.

    The sections are four tables and the cursor should read as one ring
    across them. The screen cannot do that from a binding of its own:
    `ConsoleTable` already binds `j`/`k`, and a focused widget's bindings
    beat the screen's. Rebinding them on the screen with ``priority`` would
    win — and would also take `j` away from the filter box, which is a
    worse bug than the one it fixes. So the hand-off lives here, on the
    table, where the key already arrives; arrow keys get it too, because
    they run the same two actions."""

    def _hop(self, delta: int) -> bool:
        hop = getattr(self.screen, "hop", None)
        return bool(hop(self, delta)) if hop is not None else False

    def action_cursor_down(self) -> None:
        if self.cursor_row >= self.row_count - 1 and self._hop(1):
            return
        super().action_cursor_down()

    def action_cursor_up(self) -> None:
        if self.cursor_row <= 0 and self._hop(-1):
            return
        super().action_cursor_up()


def clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 1)] + "…"


def median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else 0.0


def waited(seconds: float) -> str:
    """A span of waiting. Not `age`: that reads a timestamp and says "2d
    ago", which is the wrong tense for "the oldest has waited 2d"."""
    total = int(max(seconds, 0))
    if total >= 86400:
        return f"{total // 86400}d"
    if total >= 3600:
        return f"{total // 3600}h"
    if total >= 60:
        return f"{total // 60}m"
    return f"{total}s"


class ItemsScreen(ConsoleScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("slash", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("enter", "open", "Open latest run"),
        Binding("t", "retry", "Retry"),
        Binding("u", "requeue", "Requeue"),
        Binding("A", "abandon", "Abandon"),
        Binding("w", "resume_review", "Check review now"),
        Binding("m", "merge", "Approve merge"),
        Binding("n", "new_run", "New run"),
        Binding("N", "run_here", "Run here", show=False),
    ]
    DEFAULT_CSS = """
    ItemsScreen #stats { height: 1; padding: 0 1; color: $text-muted; }
    ItemsScreen #banner { height: auto; padding: 0 1; }
    ItemsScreen #filter { display: none; }
    ItemsScreen #sections { height: 1fr; }
    ItemsScreen .title { text-style: bold; padding: 1 1 0 1; }
    ItemsScreen .empty { color: $text-muted; padding: 0 2; }
    ItemsScreen .held { opacity: 0.6; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.filter_text = ""
        #: Section key -> the items it drew, in the order it drew them, so
        #: a cursor row resolves back to an item without re-deriving it.
        self.shown: dict[str, list[WorkItem]] = {}
        self.blocked = ""

    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with Vertical(id="body"):
            yield TextPanel("", id="stats")
            yield TextPanel("", id="banner")
            yield Input(placeholder="filter the queue (id, repo, state, title, error)", id="filter")
            with VerticalScroll(id="sections"):
                for section in SECTIONS:
                    yield Static("", id=f"h_{section.key}", classes="title")
                    yield SectionTable(*section.columns, id=section.key)
                    yield TextPanel("", id=f"e_{section.key}", classes="empty")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#filter", Input).display = False
        self.query_one("#banner", TextPanel).display = False
        self.query_one("#waiting", ConsoleTable).focus()

    # -- what goes where -----------------------------------------------------------

    def _match(self, item: WorkItem) -> bool:
        needle = self.filter_text.lower()
        if not needle:
            return True
        hay = (
            f"{item.item_id} {item.repo or ''} {item.state} {item.title} {item.last_error or ''}"
        ).lower()
        return needle in hay

    def _sorted(self, section: Section, items: ItemsSnapshot, now: float) -> list[WorkItem]:
        """The section's items, in the order that section is read in."""
        rows = [i for i in items.items if i.state in section.states and self._match(i)]
        if section.key == "queued":
            # The daemon's own dispatch order, never one re-derived here.
            order = {i.item_id: n for n, i in enumerate(items.queued)}
            return sorted(rows, key=lambda i: order.get(i.item_id, len(order)))
        if section.key == "parked" and not self.filter_text:
            rows = [i for i in rows if now - i.updated_at <= PARKED_WINDOW_S]
        return sorted(rows, key=lambda i: -i.updated_at)

    @staticmethod
    def _gate(item_id: str, items: ItemsSnapshot) -> MergeGate | None:
        return next((g for g in items.gates if g.item_id == item_id), None)

    @staticmethod
    def _hold(item_id: str, items: ItemsSnapshot) -> ReviewHold | None:
        return next((h for h in items.holds if h.item_id == item_id), None)

    def _blocked_on(self, item: WorkItem, items: ItemsSnapshot) -> tuple[str, str]:
        """What is holding this item, and the key that clears it."""
        gate = self._gate(item.item_id, items)
        if gate is not None:
            kind = "publish" if gate.kind == "publish" else "merge"
            return f"{kind} #{gate.pr_number}", "m"
        hold = self._hold(item.item_id, items)
        if hold is not None:
            return f"review #{hold.pr_number}", "w"
        return item.state, "w" if item.state in VERB_STATES["resume_review"] else "—"

    def _why(self, item: WorkItem, items: ItemsSnapshot, now: float) -> str:
        """The item's own reason, never the daemon's — the banner says that
        one, and a row repeating it would lose its own state."""
        if item.run_id:
            return "resume, first"
        at = items.eligible_at.get(item.item_id, 0.0)
        if at <= now:
            return "now"
        clock = time.strftime("%H:%M", time.localtime(at))
        if item.not_before is not None and item.not_before >= at:
            return f"retry {clock}"
        return f"backoff {item.attempts} · {clock}"

    def _cells(
        self, section: Section, item: WorkItem, items: ItemsSnapshot, now: float, index: int
    ) -> tuple[str, ...]:
        repo = item.repo or "—"
        if section.key == "waiting":
            what, key = self._blocked_on(item, items)
            return (item.item_id, repo, what, clip(item.title, 40), age(item.updated_at, now), key)
        if section.key == "running":
            return (
                item.item_id,
                repo,
                item.run_id or "—",
                clip(item.title, 46),
                age(item.updated_at, now),
            )
        if section.key == "queued":
            return (
                str(index + 1),
                item.item_id,
                repo,
                clip(item.title, 42),
                str(item.attempts),
                self._why(item, items, now),
            )
        marker = ITEM_STATE_MARKER.get(item.state, "·") if self.console_app.emoji else ""
        return (
            item.item_id,
            repo,
            f"{marker} {item.state}".strip(),
            clip(item.title, 34),
            clip(item.last_error or "—", 34),
            age(item.updated_at, now),
        )

    # -- the daemon's own block ----------------------------------------------------

    @staticmethod
    def _daemon_block(state: ConsoleState) -> str:
        """Why nothing dispatches, whatever the queued rows say."""
        daemon = state.daemon
        if daemon is None or not daemon.live:
            return "no daemon answered — nothing dispatches"
        status = daemon.status or {}
        if status.get("breaker_open"):
            fails = status.get("consecutive_failures") or 0
            return f"breaker open after {fails} consecutive failures — nothing dispatches"
        if status.get("paused"):
            holds = ", ".join(status.get("holds") or []) or "paused"
            return f"paused: {holds} — nothing dispatches"
        today = status.get("runs_today")
        cap = status.get("max_runs_per_day")
        if isinstance(today, int) and isinstance(cap, int) and cap and today >= cap:
            return f"daily cap reached ({today}/{cap}) — nothing dispatches until it resets"
        return ""

    # -- paint ---------------------------------------------------------------------

    def refresh_data(self, state: ConsoleState) -> None:
        super().refresh_data(state)
        items = state.items
        if items is None:
            return
        now = time.time()
        self.blocked = self._daemon_block(state)
        banner = self.query_one("#banner", TextPanel)
        banner.display = bool(self.blocked)
        if self.blocked:
            banner.update(Text.assemble(("⚠ ", "bold yellow"), (self.blocked, "yellow")))
        self.shown = {}
        for section in SECTIONS:
            rows = self._sorted(section, items, now)
            self.shown[section.key] = rows
            table = self.query_one(f"#{section.key}", ConsoleTable)
            table.replace_rows(
                (item.item_id, self._cells(section, item, items, now, index))
                for index, item in enumerate(rows)
            )
            table.display = bool(rows)
            # A queued backlog the daemon cannot dispatch is dimmed: every
            # row is accurate and not one of them is going anywhere.
            table.set_class(bool(self.blocked) and section.key == "queued", "held")
            self.query_one(f"#h_{section.key}", Static).update(
                self._heading(section, len(rows), items, now)
            )
            hint = self.query_one(f"#e_{section.key}", TextPanel)
            hint.display = not rows
            hint.update(Text(EMPTY[section.key], style="dim"))
        self.query_one("#stats", TextPanel).update(self._stats(now))
        self.refresh_bindings()

    def _heading(self, section: Section, count: int, items: ItemsSnapshot, now: float) -> Text:
        text = Text()
        text.append(section.title, style="bold")
        text.append(f"   {count}", style="bold" if count else "dim")
        if section.key == "parked" and not self.filter_text:
            older = sum(
                1
                for i in items.items
                if i.state in section.states and now - i.updated_at > PARKED_WINDOW_S
            )
            if older:
                text.append(f"   last 7 days · {older} older hidden, / to search all", style="dim")
        return text

    def _stats(self, now: float) -> Text:
        queued = self.shown.get("queued", [])
        waiting = self.shown.get("waiting", [])
        ages = [now - i.created_at for i in queued if i.created_at]
        text = Text()
        text.append(f"{len(queued)} queued", style="bold")
        if ages:
            text.append("  ·  oldest ", style="dim")
            text.append(waited(max(ages)), style="bold")
            # The age of what is waiting *now*. A queued-to-dispatched time
            # would need a dispatch timestamp, and a work item has none.
            text.append("  ·  median wait ", style="dim")
            text.append(waited(median(ages)), style="bold")
        text.append("  ·  ", style="dim")
        text.append(f"{len(waiting)} waiting on you", style="bold" if waiting else "dim")
        return text

    # -- one focus ring across the sections ----------------------------------------

    def _tables(self) -> list[ConsoleTable]:
        return [self.query_one(f"#{s.key}", ConsoleTable) for s in SECTIONS]

    def _focused(self) -> ConsoleTable | None:
        return next((t for t in self._tables() if t.has_focus), None)

    def hop(self, table: ConsoleTable, delta: int) -> bool:
        """Move the cursor to the next section with rows in it.

        ``False`` when there is none, which leaves the table to handle the
        key itself — at the very top and very bottom of the screen the
        cursor should stay put rather than wrap."""
        tables = [t for t in self._tables() if t.display and t.row_count]
        if table not in tables:
            return False
        index = tables.index(table) + delta
        if not 0 <= index < len(tables):
            return False
        following = tables[index]
        following.focus()
        following.move_cursor(row=0 if delta > 0 else following.row_count - 1)
        return True

    # -- the row under the cursor --------------------------------------------------

    def _selected(self) -> WorkItem | None:
        table = self._focused()
        if table is None or table.id is None:
            return None
        key = table.selected_key()
        rows = self.shown.get(table.id, [])
        return next((i for i in rows if i.item_id == key), None)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Only offer a key that would do something to the selected row.

        The old footer advertised `m` on every row and it worked on the few
        with an open gate, which is the console asking you to guess.

        ``False``, not ``None``: `Screen.active_bindings` drops a binding
        only on ``is False``, and treats ``None`` as shown-but-disabled.
        The two are easy to swap and the difference does not show in a
        text dump, so `test_tui_queue.py` asserts on the footer itself."""
        if action not in CONTEXTUAL:
            return True
        item = self._selected()
        if item is None:
            return False
        if action == "open":
            return bool(item.run_id)
        if action == "merge":
            items = self.console_app.state.items
            return items is not None and self._gate(item.item_id, items) is not None
        return item.state in VERB_STATES[action]

    def on_data_table_row_highlighted(self, event: ConsoleTable.RowHighlighted) -> None:
        self.refresh_bindings()

    def on_descendant_focus(self) -> None:
        self.refresh_bindings()

    # -- verbs ---------------------------------------------------------------------

    def action_open(self) -> None:
        item = self._selected()
        if item is None:
            return
        if not item.run_id:
            self.app.notify("this item has no run yet", severity="warning")
            return
        self.console_app.open_run(item.run_id)

    def on_data_table_row_selected(self, event: ConsoleTable.RowSelected) -> None:
        self.action_open()

    def action_retry(self) -> None:
        item = self._selected()
        if item is not None:
            self.console_app.perform(actions.retry(self.console_app.deps, item.item_id))

    def action_requeue(self) -> None:
        item = self._selected()
        if item is not None:
            self.console_app.perform(actions.requeue(self.console_app.deps, item.item_id))

    def action_abandon(self) -> None:
        item = self._selected()
        if item is not None:
            self.console_app.perform(actions.abandon(self.console_app.deps, item.item_id))

    def action_resume_review(self) -> None:
        item = self._selected()
        if item is not None:
            self.console_app.perform(actions.resume_review(self.console_app.deps, item.item_id))

    def action_merge(self) -> None:
        item = self._selected()
        if item is None:
            return
        items = self.console_app.state.items
        gate = self._gate(item.item_id, items) if items else None
        if gate is None:
            self.app.notify(f"{item.item_id} has no open merge gate", severity="warning")
            return
        self.console_app.perform(
            actions.merge(self.console_app.deps, item.item_id, held=gate.kind == "publish")
        )

    # -- filter --------------------------------------------------------------------

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.display = True
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.value = ""
        self.filter_text = ""
        box.display = False
        self.query_one("#waiting", ConsoleTable).focus()
        self.refresh_data(self.console_app.state)

    def on_input_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value
        self.refresh_data(self.console_app.state)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#waiting", ConsoleTable).focus()

    # -- new work ------------------------------------------------------------------

    def action_new_run(self) -> None:
        """A run the daemon's way: ask the concierge to file the issue."""

        def submitted(text: str | None) -> None:
            if text:
                self.console_app.perform(actions.ask_concierge_to_file(self.console_app.deps, text))

        self.app.push_screen(
            TextPromptScreen(
                "new run",
                "Describe the outcome. The concierge files it as an issue with the trigger "
                "label and the daemon picks it up.",
                placeholder="the outcome you want",
            ),
            submitted,
        )

    def action_run_here(self) -> None:
        """A run outside the daemon: a detached `sbxloop run` on this host."""

        def submitted(text: str | None) -> None:
            if text:
                self.console_app.perform(actions.run_text(self.console_app.deps, text))

        self.app.push_screen(
            TextPromptScreen(
                "run here, detached",
                "Describe the outcome. `sbxloop run` starts in its own session with this "
                "checkout's config; the daemon is not involved.",
                placeholder="the outcome you want",
            ),
            submitted,
        )


#: What a section says when it has nothing, so an empty screen still
#: reports rather than going blank.
EMPTY = {
    "waiting": "nothing is waiting on you",
    "running": "nothing running",
    "queued": "nothing queued",
    "parked": "nothing parked or failed",
}


__all__ = [
    "CONTEXTUAL",
    "EMPTY",
    "PARKED_WINDOW_S",
    "SECTIONS",
    "VERB_STATES",
    "ItemsScreen",
    "Section",
    "SectionTable",
    "clip",
    "median",
    "waited",
]
