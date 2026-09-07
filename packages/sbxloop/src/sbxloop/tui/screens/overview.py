"""Overview: how the loop has been performing, a page at a time.

The screen used to be four panels — the run in flight, the queue, recent
runs, and who is waiting on you — three of which have their own screen now
(Runs, Queue, Daemon), and none of which answered the question an operator
actually opens a console with: *is this working well?*

It answers that in prose and proportion rather than in a grid. One live
line on top, a narrow rail of pages beside the console's own, and a page
that states its finding in a sentence before it draws a bar. Six pages,
because that many metric classes fought over one screen and lost; given a
page each, every one of them fits in a few lines.

Most of the drawing is :mod:`sbxloop.tui.widgets.band` — one row, solid
colour, no axis, and the right answer to every "what share" question here.
Spread is the exception: *what shape* needs a scale, so that page and the
Cost trend use :mod:`sbxloop.tui.widgets.chart`.

The numbers are :mod:`sbxloop.tui.analytics`, recomputed on a slow timer of
its own — nothing in a week-long window changes between console ticks.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, ClassVar, NamedTuple

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static
from textual.worker import get_current_worker

from sbxloop.tui import analytics
from sbxloop.tui.analytics import Analytics, Lane, RunRow
from sbxloop.tui.data import ConsoleState
from sbxloop.tui.format import age
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.widgets import chart
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
    PageItem("d", "spread", "Spread"),
)


def hm(seconds: float) -> str:
    """A duration read at a glance, not to the second."""
    total = int(max(seconds, 0))
    if total >= 3600:
        return f"{total // 3600}h {total % 3600 // 60:02d}m"
    if total >= 60:
        return f"{total // 60}m"
    return f"{total}s"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


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
    OverviewScreen .lab-wide { width: 30; color: $text-muted; }
    OverviewScreen .val { width: 11; text-style: bold; }
    OverviewScreen .cap { color: $text-muted; }
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
        kind = current.get("kind") or (record.kind if record is not None else None)
        if kind and kind != "code":
            profile = current.get("profile")
            text.append(f" · {kind}" + (f" ({profile})" if profile else ""), style="dim")
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
            "spread": self._spread,
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
    def _ranked(
        rows: list[tuple[str, float, str]], colour: str, *, wide: bool = False
    ) -> list[Any]:
        """A short ranked list, each row a bar against the biggest.

        ``wide`` gives the label room for a sentence rather than an id: a
        failure reason is prose, and reads as nothing clipped to the width
        of a run id."""
        if not rows:
            return [TextPanel(Text("nothing to rank", style="dim"))]
        peak = max(value for _label, value, _note in rows) or 1.0
        label_class = "lab-wide" if wide else "lab"
        return [
            Horizontal(
                TextPanel(f"{label} ", classes=label_class),
                TextPanel(note, classes="val"),
                Band([Segment("v", value, colour), Segment("rest", peak - value, TRACK_COLOUR)]),
                classes="r",
            )
            for label, value, note in rows
        ]

    @staticmethod
    def _delta(value: float | None, *, lower_is_better: bool = False) -> tuple[str, str]:
        """A change against the previous window, and how to colour it."""
        if value is None:
            return "", "dim"
        arrow = "▲" if value > 0 else ("▼" if value < 0 else "=")
        good = (value < 0) if lower_is_better else (value > 0)
        style = "dim" if abs(value) < 0.05 else (OK_COLOUR if good else WAIT_COLOUR)
        return f" {arrow} {abs(value):.0%}", style

    def _compare(self, data: Analytics, rows: list[tuple[str, str, str, bool]]) -> list[Any]:
        """A small "against last week" block: label, value, and the change."""
        out: list[Any] = [TextPanel("against the week before", classes="h")]
        for label, metric, shown, lower_better in rows:
            change, style = self._delta(data.delta(metric), lower_is_better=lower_better)
            out.append(
                TextPanel(
                    Text.assemble(
                        (f"{label:<13}", "dim"),
                        (f"{shown:<12}", "bold"),
                        (change, style),
                    )
                )
            )
        return out

    @staticmethod
    def _cols(widths: tuple[int, ...], *rows: tuple[str, ...], head: bool = False) -> list[Any]:
        """A plain aligned table. Cheaper to read than a bar when the point
        is the numbers rather than the proportion."""
        out: list[Any] = []
        for row in rows:
            text = Text()
            for cell, width in zip(row, widths, strict=False):
                text.append(f"{cell:<{width}}", style="dim" if head else "")
            out.append(TextPanel(text))
        return out

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
        out: list[Any] = [
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
        ]
        out.extend(
            self._plotted(
                "outcome", f"{rate:.0%} ok" if rate is not None else "—", self._outcome(total)
            )
        )
        out.append(TextPanel("", classes="gap"))
        out.extend(
            self._plotted(
                "time",
                hm(total.elapsed),
                [
                    Segment("active", total.active, PALETTE[0]),
                    Segment("parked", total.parked, PARKED_COLOUR),
                ],
                hm,
            )
        )
        out.append(TextPanel("", classes="gap"))
        out.extend(self._plotted("phases", hm(data.active_seconds), phases, hm))
        out.append(TextPanel("", classes="gap"))
        out.extend(
            self._compare(
                data,
                [
                    ("runs", "runs", str(total.runs), False),
                    ("turns", "turns", f"{total.turns:,}", True),
                    ("active", "active", hm(total.active), True),
                    ("parked", "parked", hm(total.parked), True),
                ],
            )
        )
        out.append(TextPanel("", classes="gap"))
        out.append(TextPanel("day by day, by outcome", classes="h"))
        out.extend(self._day_chart(data))
        lever = self._lever(data)
        if lever is not None:
            out.append(TextPanel("", classes="gap"))
            out.append(lever)
        return out

    def _lever(self, data: Analytics) -> Any:
        """The one sentence worth acting on: whichever of the week's costs
        is furthest out of proportion. None when nothing stands out."""
        total = data.total
        if total.parked > total.active * 2 and total.parked > 3600:
            return self._say(
                ("Biggest lever: ", "dim"),
                (f"{total.parked_share:.0%} of elapsed was waiting on you", f"bold {WAIT_COLOUR}"),
                (f" — {hm(total.parked)} against {hm(total.active)} of work.", "dim"),
            )
        top = data.phase_turns[0] if data.phase_turns else None
        if top is not None and total.turns and top.turns > total.turns * 0.5:
            return self._say(
                ("Biggest lever: ", "dim"),
                (f"{top.phase} is {top.turns / total.turns:.0%} of every turn", "bold"),
                (f" — {top.turns:,} of {total.turns:,}.", "dim"),
            )
        if data.costliest and total.turns and data.costliest[0].turns > total.turns * 0.25:
            run = data.costliest[0]
            return self._say(
                ("Biggest lever: ", "dim"),
                (f"{run.run_id} is {run.turns / total.turns:.0%} of the week", "bold"),
                (" — o opens it.", "dim"),
            )
        return None

    @staticmethod
    def _bucket_label(data: Analytics, offset: int, fmt: str) -> str:
        """Which day a bucket is, taken from the middle of the bucket so
        rounding cannot push it into the neighbouring one."""
        when = time.localtime(
            data.since + (offset + 0.5) * (data.until - data.since) / max(len(data.days), 1)
        )
        return time.strftime(fmt, when)

    def _day_chart(self, data: Analytics) -> list[Any]:
        """The week as one stacked plot rather than a bar per day."""
        labels = [self._bucket_label(data, i, "%a") for i in range(len(data.days))]
        if not labels:
            return [TextPanel(Text("no days in the window", style="dim"))]
        series = [
            ("landed", [float(d.landed) for d in data.days], OK_COLOUR),
            ("failed", [float(d.failed) for d in data.days], BAD_COLOUR),
            ("cancelled", [float(d.cancelled) for d in data.days], IDLE_COLOUR),
        ]
        drawn = chart.stacked(labels, series)
        # The week's totals as the key — the same swatch-name-share line a
        # band carries, so the two kinds of stack read the same way.
        key = legend([Segment(name, sum(values), colour) for name, values, colour in series])
        return [drawn, TextPanel(key, classes="cap")]

    def _days(self, data: Analytics) -> list[Any]:
        """The trend as a stack: each bucket split by how its runs ended."""
        peak = max((d.runs for d in data.days), default=0) or 1
        out: list[Any] = []
        for offset, day in enumerate(data.days):
            label = self._bucket_label(data, offset, "%a %d")
            segments = [
                Segment("landed", day.landed, OK_COLOUR),
                Segment("failed", day.failed, BAD_COLOUR),
                Segment("cancelled", day.cancelled, IDLE_COLOUR),
                Segment("rest", peak - day.runs, TRACK_COLOUR),
            ]
            out.append(
                Horizontal(
                    TextPanel(label, classes="lab"),
                    TextPanel(f"{day.runs or '—'}", classes="val"),
                    Band(segments),
                    classes="r",
                )
            )
        return out

    def _flow(self, data: Analytics) -> list[Any]:
        out: list[Any] = [
            self._say(
                (f"{data.total.runs} runs", "bold"),
                (" over the week — ", "dim"),
                (f"{max((d.runs for d in data.days), default=0)} on the busiest day", "bold"),
                (f", {plural(sum(1 for d in data.days if not d.runs), 'day')} with none.", "dim"),
            ),
            TextPanel("", classes="gap"),
            TextPanel("runs per day, by outcome", classes="h"),
        ]
        out.extend(self._days(data))
        out.append(TextPanel("", classes="gap"))
        out.append(TextPanel("by kind", classes="h"))
        out.extend(
            self._cols(
                (11, 8, 9, 11, 12, 12),
                ("kind", "runs", "ok", "turns/run", "active/run", "parked/run"),
                head=True,
            )
        )
        for kind in sorted(data.lanes):
            lane = data.lane(kind)
            rate = lane.ok_rate
            out.extend(
                self._cols(
                    (11, 8, 9, 11, 12, 12),
                    (
                        kind,
                        str(lane.runs),
                        f"{rate:.0%}" if rate is not None else "—",
                        f"{lane.turns_per_run:.0f}",
                        hm(lane.active_per_run),
                        hm(lane.parked_per_run),
                    ),
                )
            )
        out.append(TextPanel("", classes="gap"))
        median, p90 = data.cycle_spread
        landed = data.landed_runs
        if landed:
            out.append(TextPanel("how long work took to land", classes="h"))
            out.append(
                TextPanel(
                    Text.assemble(
                        (f"{len(landed)} landed", "bold"),
                        ("   ·   median ", "dim"),
                        (hm(median), "bold"),
                        ("   ·   p90 ", "dim"),
                        (hm(p90), "bold"),
                    )
                )
            )
            out.extend(
                self._ranked(
                    [
                        (r.run_id, r.active + r.parked, hm(r.active + r.parked))
                        for r in data.slowest_to_land[:6]
                    ],
                    PALETTE[3],
                )
            )
            out.append(TextPanel("", classes="gap"))
        out.extend(self._compare(data, [("runs", "runs", str(data.total.runs), False)]))
        return out

    def _cost(self, data: Analytics) -> list[Any]:
        total = data.total
        median, p90 = data.turns_spread
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
        ]
        # A sparkline drew this shape but named no value on it: the peak
        # and the floor looked the same on a quiet week as on a heavy one.
        out.extend(self._trend(data, "turns", PALETTE[1]))
        out.append(TextPanel("", classes="gap"))
        # Which phase burns the turns — the total says how much, this says
        # where, and only the second one is actionable.
        by_turns = data.phase_turns
        if by_turns:
            out.append(TextPanel("turns by phase", classes="h"))
            out.append(
                Band(
                    [
                        Segment(p.phase, float(p.turns), PALETTE[i % len(PALETTE)])
                        for i, p in enumerate(by_turns[:6])
                    ]
                )
            )
            out.append(
                TextPanel(
                    legend(
                        [
                            Segment(p.phase, float(p.turns), PALETTE[i % len(PALETTE)])
                            for i, p in enumerate(by_turns[:6])
                        ]
                    )
                )
            )
            out.append(TextPanel("", classes="gap"))
            out.append(TextPanel("context re-sent per phase", classes="h"))
            out.extend(
                self._cols(
                    (11, 10, 10, 10, 9),
                    ("phase", "turns", "fresh", "cache", "ratio"),
                    head=True,
                )
            )
            for phase in by_turns[:6]:
                out.extend(
                    self._cols(
                        (11, 10, 10, 10, 9),
                        (
                            phase.phase,
                            f"{phase.turns:,}",
                            count(phase.tokens),
                            count(phase.cache),
                            f"{phase.cache_ratio:.1f}x" if phase.tokens else "—",
                        ),
                    )
                )
            out.append(TextPanel("", classes="gap"))
        out.append(TextPanel("costliest runs", classes="h"))
        out.extend(
            self._ranked(
                [(r.run_id, float(r.turns), f"{r.turns} turns") for r in data.costliest], PALETTE[1]
            )
        )
        out.append(TextPanel("", classes="gap"))
        out.append(
            TextPanel(
                Text.assemble(
                    ("median run ", "dim"),
                    (f"{median:.0f} turns", "bold"),
                    ("   ·   p90 ", "dim"),
                    (f"{p90:.0f} turns", "bold"),
                )
            )
        )
        if data.costliest and total.turns:
            top = data.costliest[0]
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
        out.append(TextPanel("", classes="gap"))
        out.extend(
            self._compare(
                data,
                [
                    ("turns", "turns", f"{total.turns:,}", True),
                    ("tokens", "tokens", count(total.tokens), True),
                    ("cache", "cache", count(total.cache), True),
                ],
            )
        )
        return out

    def _time(self, data: Analytics) -> list[Any]:
        total = data.total
        median, p90 = data.active_spread
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
            TextPanel("where the working time went", classes="h"),
            Band(phases),
            TextPanel(legend(phases)),
            TextPanel("", classes="gap"),
        ]
        if data.phases:
            out.extend(
                self._cols(
                    (11, 10, 11, 11, 9),
                    ("phase", "attempts", "total", "average", "turns"),
                    head=True,
                )
            )
            for phase in data.phases[:7]:
                out.extend(
                    self._cols(
                        (11, 10, 11, 11, 9),
                        (
                            phase.phase,
                            str(phase.attempts),
                            hm(phase.seconds),
                            hm(phase.seconds_per_attempt),
                            f"{phase.turns:,}" if phase.turns else "—",
                        ),
                    )
                )
            out.append(TextPanel("", classes="gap"))
        out.append(TextPanel("longest parked", classes="h"))
        out.extend(
            self._ranked(
                [(r.run_id, r.parked, hm(r.parked)) for r in data.longest_parked], WAIT_COLOUR
            )
        )
        out.append(TextPanel("", classes="gap"))
        out.append(
            TextPanel(
                Text.assemble(
                    ("median run works ", "dim"),
                    (hm(median), "bold"),
                    ("   ·   p90 ", "dim"),
                    (hm(p90), "bold"),
                )
            )
        )
        out.extend(
            self._compare(
                data,
                [
                    ("active", "active", hm(total.active), True),
                    ("parked", "parked", hm(total.parked), True),
                ],
            )
        )
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
                    [(reason[:28], float(n), plural(n, "run")) for reason, n in failures],
                    BAD_COLOUR,
                    wide=True,
                )
            )
            out.append(TextPanel("", classes="gap"))
        # Where the loop went round again. A phase that retries constantly
        # is costing turns nobody asked for.
        retried = data.retried_phases
        if retried:
            out.append(TextPanel("where the loop went round again", classes="h"))
            out.extend(
                self._ranked(
                    [
                        (p.phase, float(p.retries), f"{p.retries} of {p.attempts}")
                        for p in retried[:6]
                    ],
                    WAIT_COLOUR,
                )
            )
            out.append(TextPanel("", classes="gap"))
        out.append(TextPanel("phases, by how often they ran", classes="h"))
        out.extend(
            self._cols((11, 10, 10, 11), ("phase", "attempts", "retries", "turns"), head=True)
        )
        for phase in data.phases[:6]:
            out.extend(
                self._cols(
                    (11, 10, 10, 11),
                    (
                        phase.phase,
                        str(phase.attempts),
                        str(phase.retries) if phase.retries else "—",
                        f"{phase.turns:,}" if phase.turns else "—",
                    ),
                )
            )
        out.append(TextPanel("", classes="gap"))
        rework = data.rework
        out.append(TextPanel("rework", classes="h"))
        out.append(
            TextPanel(
                Text.assemble(
                    (f"{rework.tasks}", "bold"),
                    (" tasks   ", "dim"),
                    (f"{rework.revisions}", "bold"),
                    (" revisions   ", "dim"),
                    (f"{rework.replans}", "bold"),
                    (" replans   ", "dim"),
                    (f"{rework.suspect}", "bold"),
                    (" suspect verifies", "dim"),
                )
            )
        )
        out.append(
            TextPanel(
                Text.assemble(
                    (f"{data.review_rounds}", "bold"),
                    (" review rounds   ", "dim"),
                    (f"{data.ci_rounds}", "bold"),
                    (" CI rounds   ", "dim"),
                    (f"{total.cancelled}", "bold"),
                    (" cancelled by you", "dim"),
                )
            )
        )
        return out

    def _plotted(
        self,
        label: str,
        value: str,
        segments: list[Segment],
        fmt: Callable[[float], str] | None = None,
    ) -> list[Any]:
        """`_row`'s charted counterpart: the same heading and the same key,
        with a scale between them instead of a single painted row."""
        if not any(s.value > 0 for s in segments):
            return [self._row(label, value, segments)]
        drawn = chart.proportion([(s.label, s.value, s.colour) for s in segments], fmt)
        return [
            TextPanel(
                Text.assemble((f"{label:<13}", "dim"), (value, "bold")),
            ),
            drawn,
            TextPanel(legend(segments), classes="cap"),
        ]

    @staticmethod
    def _captioned(drawn: chart.Chart) -> list[Any]:
        """A plot and the sentence saying what is in it. `render` paints a
        chart, which leaves nothing for a screen reader — or a test — to
        read; the caption is the chart in words."""
        return [drawn, TextPanel(Text(drawn.caption, style="dim"), classes="cap")]

    def _trend(self, data: Analytics, metric: str, colour: str) -> list[Any]:
        """A daily series against a labelled scale."""
        series = [float(x) for x in data.daily.get(metric, ())]
        if not series:
            return [TextPanel(Text("no daily series", style="dim"))]
        labels = [self._bucket_label(data, i, "%a") for i in range(len(series))]
        return self._captioned(chart.bars(labels, series, colour))

    def _distribution(
        self,
        values: list[float],
        colour: str,
        spread: Text,
        fmt: Callable[[float], str] | None = None,
    ) -> list[Any]:
        """A histogram, under the median-and-p90 line either way.

        Median and p90 say where the middle is; they say nothing about
        whether the runs cluster there or sit in two camps either side of
        it, which is the whole reason to draw the shape. Under
        `chart.MIN_POINTS` runs there is no shape to see — every bin holds
        one run — and the two numbers are the better answer alone."""
        if not chart.enough(values):
            return [
                TextPanel(spread),
                TextPanel(
                    Text(
                        f"{plural(len(values), 'run')} — too few to show a shape "
                        f"(needs {chart.MIN_POINTS}).",
                        style="dim",
                    ),
                    classes="cap",
                ),
            ]
        # One bin per two runs, held between 6 and 12: fewer and the shape
        # is a block, more and every bin holds one run and the histogram
        # is just the scatter drawn worse.
        bins = max(6, min(12, len(values) // 2))
        return [*self._captioned(chart.histogram(values, bins, colour, fmt)), TextPanel(spread)]

    def _spread(self, data: Analytics) -> list[Any]:
        """How the week's runs are distributed, rather than what they
        totalled. The totals are on Cost and Time; this page is the one
        that shows an average hiding two populations."""
        runs = list(data.runs_seen)
        turns = [float(r.turns) for r in runs]
        active = [r.active for r in runs]
        t_median, t_p90 = data.turns_spread
        a_median, a_p90 = data.active_spread
        tail = t_p90 / t_median if t_median else 0.0
        out: list[Any] = [
            self._say(
                (f"{len(runs)} runs", "bold"),
                (" in the window. The middle one cost ", "dim"),
                (f"{t_median:.0f} turns", "bold"),
                (", the ninetieth ", "dim"),
                (f"{t_p90:.0f}", f"bold {WAIT_COLOUR}" if tail >= 2 else "bold"),
                (
                    f" — a {tail:.1f}x tail." if tail >= 2 else " — no long tail.",
                    "dim",
                ),
            ),
            TextPanel("", classes="gap"),
            TextPanel("turns per run", classes="h"),
        ]
        out.extend(
            self._distribution(
                turns,
                PALETTE[1],
                Text.assemble(
                    ("median ", "dim"),
                    (f"{t_median:.0f} turns", "bold"),
                    ("   ·   p90 ", "dim"),
                    (f"{t_p90:.0f} turns", "bold"),
                ),
            )
        )
        out.append(TextPanel("", classes="gap"))
        out.append(TextPanel("working time per run", classes="h"))
        out.extend(
            self._distribution(
                active,
                PALETTE[0],
                Text.assemble(
                    ("median ", "dim"),
                    (hm(a_median), "bold"),
                    ("   ·   p90 ", "dim"),
                    (hm(a_p90), "bold"),
                ),
                hm,
            )
        )
        out.append(TextPanel("", classes="gap"))
        out.append(TextPanel("turns against elapsed, one dot per run", classes="h"))
        out.extend(self._cost_against_time(runs))
        out.append(TextPanel("", classes="gap"))
        out.extend(self._compare(data, [("turns", "turns", f"{data.total.turns:,}", True)]))
        return out

    def _cost_against_time(self, runs: list[RunRow]) -> list[Any]:
        """Turns against elapsed. A run far off the crowd cost turns its
        wall-clock does not explain — the shape a ranked list cannot show,
        because a list is sorted by one axis and the outlier is the run
        that disagrees with both."""
        xs = [r.active + r.parked for r in runs]
        ys = [float(r.turns) for r in runs]
        if not chart.enough(xs):
            return [
                TextPanel(
                    Text(
                        f"{plural(len(xs), 'run')} — a scatter of that needs "
                        f"{chart.MIN_POINTS}; the ranked lists on Cost and Time "
                        "say more at this size.",
                        style="dim",
                    )
                )
            ]
        out = self._captioned(chart.scatter(xs, ys, PALETTE[4], hm, whole_y=True))
        out.append(
            TextPanel(
                Text.assemble(
                    ("x: elapsed", "dim"),
                    ("   ·   ", "dim"),
                    ("y: turns", "dim"),
                    ("   ·   o opens the costliest", "dim"),
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


__all__ = ["PAGES", "OverviewScreen", "PageRail", "count", "hm", "plural"]
