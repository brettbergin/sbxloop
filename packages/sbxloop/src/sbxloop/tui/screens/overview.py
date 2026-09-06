"""Overview: how the loop has been performing, a page at a time.

The screen used to be four panels — the run in flight, the queue, recent
runs, and who is waiting on you — three of which have their own screen now
(Runs, Queue, Daemon), and none of which answered the question an operator
actually opens a console with: *is this working well?*

It answers that in prose and proportion rather than in a grid. One live
line on top, a narrow rail of pages beside the console's own, and a page
that states its finding in a sentence before it draws a bar. Five pages,
because five metric classes fought over one screen and lost; given a page
each, every one of them fits in a few lines.

The numbers are :mod:`sbxloop.tui.analytics`, recomputed on a slow timer of
its own — nothing in a week-long window changes between console ticks.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar, NamedTuple

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Sparkline, Static
from textual.worker import get_current_worker

from sbxloop.tui import analytics
from sbxloop.tui.analytics import Analytics, Lane
from sbxloop.tui.data import ConsoleState
from sbxloop.tui.format import age
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.widgets.band import (
    BAD_COLOUR,
    IDLE_COLOUR,
    OK_COLOUR,
    PALETTE,
    PARKED_COLOUR,
    TRACK_COLOUR,
    WAIT_COLOUR,
    Band,
    Segment,
    legend,
)
from sbxloop.tui.widgets.panel import TextPanel


class PageItem(NamedTuple):
    key: str
    name: str
    title: str


#: Overview's own pages. The keys are letters: the digits belong to the
#: console's rail, and a screen may not take them back.
PAGES: tuple[PageItem, ...] = (
    PageItem("s", "summary", "Summary"),
    PageItem("f", "flow", "Flow"),
    PageItem("c", "cost", "Cost"),
    PageItem("t", "time", "Time"),
    PageItem("h", "health", "Health"),
)


def hm(seconds: float) -> str:
    """A duration read at a glance, not to the second."""
    total = int(max(seconds, 0))
    if total >= 3600:
        return f"{total // 3600}h {total % 3600 // 60:02d}m"
    if total >= 60:
        return f"{total // 60}m"
    return f"{total}s"


def count(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return f"{value:.0f}"


class PageRail(Static):
    """Overview's pages, beside the console's screens."""

    # Deliberately not docked. The console's rail is already docked to this
    # screen's left edge, and two widgets docked to the same edge of the
    # same container overlay each other rather than stacking — this rail
    # drew straight over that one. It sits in a Horizontal beside the page
    # instead, in the space the docked rail left.
    DEFAULT_CSS = """
    PageRail { width: 11; background: $boost; padding: 1 0 0 0; }
    """

    def __init__(self, active: str) -> None:
        super().__init__(id="pagerail")
        self.active = active

    def show(self, active: str) -> None:
        self.active = active
        self.refresh()

    def render(self) -> Text:
        text = Text()
        for item in PAGES:
            if item.name == self.active:
                text.append(f" ▸ {item.title}\n", style="bold")
            else:
                text.append(f" {item.key} {item.title}\n", style="dim")
        return text


class OverviewScreen(ConsoleScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        *(Binding(item.key, f"page({item.name!r})", item.title) for item in PAGES),
        Binding("o", "open_outlier", "Costliest run"),
    ]
    DEFAULT_CSS = """
    OverviewScreen #live { height: 1; color: $text-muted; padding: 0 1; }
    OverviewScreen #page { width: 1fr; height: 1fr; padding: 1 2; }
    OverviewScreen .h { text-style: bold; }
    OverviewScreen .gap { height: 1; }
    OverviewScreen .r { height: 1; }
    OverviewScreen .lab { width: 13; color: $text-muted; }
    OverviewScreen .val { width: 11; text-style: bold; }
    OverviewScreen Sparkline { height: 1; width: 28; }
    OverviewScreen.-narrow PageRail { display: none; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.page = "summary"
        self.cache = analytics.Cache()
        self.data: Analytics | None = None

    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        yield TextPanel("", id="live")
        with Horizontal(id="body"):
            yield PageRail(self.page)
            yield VerticalScroll(id="page")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.load()
        self.set_interval(analytics.CACHE_TTL_S, self.load)

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        self.load()

    # -- data --------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="analytics")
    def load(self) -> None:
        """Recompute the window off the UI thread, at most once per TTL."""
        now = time.time()
        if not self.cache.stale(now):
            return
        with self.console_app.deps.mailbox.read_engine() as store:
            data = analytics.compute(store, now=now)
        if get_current_worker().is_cancelled:
            return
        self.cache.put(data, now)
        self.app.call_from_thread(self._apply, data)

    def _apply(self, data: Analytics) -> None:
        self.data = data
        self.render_page()

    def refresh_data(self, state: ConsoleState) -> None:
        super().refresh_data(state)
        self.query_one("#live", Static).update(self._live(state))

    def _live(self, state: ConsoleState) -> Text:
        """The one line of *now* on an otherwise historical screen."""
        text = Text()
        daemon = state.daemon
        status = (daemon.status if daemon else None) or {}
        current = status.get("current")
        if daemon is None:
            text.append("  probing the daemon…", style="dim")
            return text
        if daemon.starting:
            text.append("  daemon starting", style="yellow")
            text.append(" — commands wait until it answers", style="dim")
            return text
        if not daemon.live:
            text.append("  no daemon answered", style="red")
            text.append(" — the history below is still browsable", style="dim")
            return text
        if daemon.status is None:
            # Took the request, too busy to answer in time. Not down: the
            # ctl queue's one genuinely confusing state.
            text.append("  daemon busy", style="yellow")
            text.append(" — status not answered in time", style="dim")
            return text
        if not current:
            text.append("  idle", style="green")
            if status.get("paused"):
                text.append(f" · paused: {', '.join(status.get('holds') or [])}", style="yellow")
            return text
        run_id = str(current.get("run_id"))
        runs = state.runs
        record = next((r for r in (runs.runs if runs else ()) if r.run_id == run_id), None)
        text.append("  running ", style="green")
        text.append(run_id, style="bold")
        if record is not None:
            if record.stage:
                text.append(f" · {record.stage}", style="dim")
            last = (runs.last_event_by_run.get(run_id) if runs else None) or record.updated_at
            text.append(f" · last event {age(last, time.time())}", style="dim")
        return text

    # -- pages -------------------------------------------------------------------

    def action_page(self, name: str) -> None:
        self.page = name
        self.query_one(PageRail).show(name)
        self.render_page()

    def render_page(self) -> None:
        body = self.query_one("#page", VerticalScroll)
        body.remove_children()
        data = self.data
        if data is None:
            body.mount(TextPanel(Text("reading the store…", style="dim")))
            return
        if data.empty:
            body.mount(
                TextPanel(
                    Text.assemble(
                        ("No runs in the last 7 days.", "bold"),
                        (" Nothing to report yet — this fills in as the loop works.", "dim"),
                    )
                )
            )
            return
        builder = {
            "summary": self._summary,
            "flow": self._flow,
            "cost": self._cost,
            "time": self._time,
            "health": self._health,
        }[self.page]
        for widget in builder(data):
            body.mount(widget)

    @staticmethod
    def _say(*parts: tuple[str, str]) -> Static:
        """A page's finding, in a sentence, before any bar."""
        return TextPanel(Text.assemble(*parts))

    @staticmethod
    def _row(label: str, value: str, segments: list[Segment]) -> Horizontal:
        return Horizontal(
            TextPanel(label, classes="lab"),
            TextPanel(value, classes="val"),
            Band(segments),
            classes="r",
        )

    @staticmethod
    def _ranked(rows: list[tuple[str, float, str]], colour: str) -> list[Any]:
        """A short ranked list, each row a bar against the biggest."""
        if not rows:
            return [TextPanel(Text("nothing to rank", style="dim"))]
        peak = max(value for _label, value, _note in rows) or 1.0
        return [
            Horizontal(
                TextPanel(f"{label} ", classes="lab"),
                TextPanel(note, classes="val"),
                Band([Segment("v", value, colour), Segment("rest", peak - value, TRACK_COLOUR)]),
                classes="r",
            )
            for label, value, note in rows
        ]

    @staticmethod
    def _outcome(lane: Lane) -> list[Segment]:
        return [
            Segment("landed", lane.landed, OK_COLOUR),
            Segment("failed", lane.failed, BAD_COLOUR),
            Segment("cancelled", lane.cancelled, IDLE_COLOUR),
        ]

    @staticmethod
    def _phases(data: Analytics) -> list[Segment]:
        return [
            Segment(slice_.phase, slice_.seconds, PALETTE[i % len(PALETTE)])
            for i, slice_ in enumerate(data.phases[:6])
        ]

    def _summary(self, data: Analytics) -> list[Any]:
        total = data.total
        rate = total.ok_rate
        phases = self._phases(data)
        return [
            self._say(
                (f"{total.runs} runs this week. ", "bold"),
                (f"{total.landed} landed, {total.failed} failed, ", ""),
                (f"{total.cancelled} you cancelled.\n", ""),
                ("They cost ", "dim"),
                (f"{total.turns:,} turns", "bold"),
                (" and spent ", "dim"),
                (hm(total.active), "bold"),
                (" working — but ", "dim"),
                (hm(total.parked), f"bold {WAIT_COLOUR}"),
                (" waiting on you.", "dim"),
            ),
            TextPanel("", classes="gap"),
            self._row(
                "outcome", f"{rate:.0%} ok" if rate is not None else "—", self._outcome(total)
            ),
            TextPanel("", classes="gap"),
            self._row(
                "time",
                hm(total.elapsed),
                [
                    Segment("active", total.active, PALETTE[0]),
                    Segment("parked", total.parked, PARKED_COLOUR),
                ],
            ),
            TextPanel("", classes="gap"),
            self._row("phases", hm(data.active_seconds), phases),
            TextPanel(legend(phases)),
        ]

    def _flow(self, data: Analytics) -> list[Any]:
        out: list[Any] = [
            TextPanel("runs per day", classes="h"),
            Sparkline([float(x) for x in data.daily["runs"]], summary_function=max),
            TextPanel("", classes="gap"),
        ]
        for kind in sorted(data.lanes):
            lane = data.lane(kind)
            rate = lane.ok_rate
            out.append(self._row(kind, f"{lane.runs} runs", self._outcome(lane)))
            out.append(
                TextPanel(
                    Text.assemble(
                        ("             ", ""),
                        (f"{rate:.0%} ok" if rate is not None else "no judged runs", "dim"),
                        (f" · {lane.turns_per_run:.0f} turns/run", "dim"),
                        (f" · {hm(lane.active_per_run)} active/run", "dim"),
                    )
                )
            )
        return out

    def _cost(self, data: Analytics) -> list[Any]:
        total = data.total
        out: list[Any] = [
            self._say(
                (f"{total.turns:,} turns", "bold"),
                (f" across {total.runs} runs — ", "dim"),
                (f"{total.turns_per_run:.0f} per run", "bold"),
                (", ", "dim"),
                (f"{count(total.tokens_per_turn)} tokens each", "bold"),
                (".", "dim"),
            ),
            TextPanel("", classes="gap"),
            TextPanel("turns per day", classes="h"),
            Sparkline([float(x) for x in data.daily["turns"]], summary_function=max),
            TextPanel("", classes="gap"),
            TextPanel("costliest runs", classes="h"),
        ]
        out.extend(
            self._ranked(
                [(r.run_id, float(r.turns), f"{r.turns} turns") for r in data.costliest], PALETTE[1]
            )
        )
        if data.costliest and total.turns:
            top = data.costliest[0]
            out.append(TextPanel("", classes="gap"))
            out.append(
                TextPanel(
                    Text.assemble(
                        (f"{top.run_id} alone is ", "dim"),
                        (
                            f"{top.turns / total.turns:.0%} of the week's turns",
                            f"bold {WAIT_COLOUR}",
                        ),
                        (f" — {count(top.tokens)} tokens. o opens it.", "dim"),
                    )
                )
            )
        return out

    def _time(self, data: Analytics) -> list[Any]:
        total = data.total
        phases = self._phases(data)
        out: list[Any] = [
            self._say(
                ("The loop worked ", "dim"),
                (hm(total.active), "bold"),
                (". Runs waited on a human ", "dim"),
                (hm(total.parked), f"bold {WAIT_COLOUR}"),
                (f" — {total.parked_share:.0%} of elapsed.", "dim"),
            ),
            TextPanel("", classes="gap"),
            self._row(
                "elapsed",
                hm(total.elapsed),
                [
                    Segment("active", total.active, PALETTE[0]),
                    Segment("parked", total.parked, PARKED_COLOUR),
                ],
            ),
            TextPanel("", classes="gap"),
            TextPanel("longest parked", classes="h"),
        ]
        out.extend(
            self._ranked(
                [(r.run_id, r.parked, hm(r.parked)) for r in data.longest_parked], WAIT_COLOUR
            )
        )
        out.append(TextPanel("", classes="gap"))
        out.append(TextPanel("where the working time went", classes="h"))
        out.append(Band(phases))
        out.append(TextPanel(legend(phases)))
        return out

    def _health(self, data: Analytics) -> list[Any]:
        total = data.total
        failures = data.failures
        if not failures:
            head = self._say(
                ("Nothing failed this week.", f"bold {OK_COLOUR}"),
                (f" {total.landed} runs landed, {total.cancelled} you cancelled.", "dim"),
            )
        elif len(failures) == 1 and failures[0][1] == total.failed:
            head = self._say(
                (f"All {total.failed} failures", f"bold {BAD_COLOUR}"),
                (" this week had the same cause.", "dim"),
            )
        else:
            head = self._say(
                (f"{total.failed} runs failed", f"bold {BAD_COLOUR}"),
                (f" across {len(failures)} causes.", "dim"),
            )
        out: list[Any] = [head, TextPanel("", classes="gap")]
        if failures:
            out.append(TextPanel("why runs failed", classes="h"))
            out.extend(
                self._ranked(
                    [
                        (reason[:26], float(n), f"{n} run{'s' if n != 1 else ''}")
                        for reason, n in failures
                    ],
                    BAD_COLOUR,
                )
            )
            out.append(TextPanel("", classes="gap"))
        out.append(TextPanel("what the week spent itself on", classes="h"))
        out.append(
            TextPanel(
                Text.assemble(
                    (f"{sum(p.attempts for p in data.phases)}", "bold"),
                    (" phase attempts   ", "dim"),
                    (f"{len(data.phases)}", "bold"),
                    (" phases used   ", "dim"),
                    (f"{total.cancelled}", "bold"),
                    (" cancelled by you", "dim"),
                )
            )
        )
        return out

    # -- drill-down ---------------------------------------------------------------

    def action_open_outlier(self) -> None:
        """The run behind the spike, without hunting for it in Runs."""
        data = self.data
        if data is None or not data.costliest:
            self.app.notify("no runs in the window", title="overview")
            return
        self.console_app.open_run(data.costliest[0].run_id)


__all__ = ["PAGES", "OverviewScreen", "PageRail", "count", "hm"]
