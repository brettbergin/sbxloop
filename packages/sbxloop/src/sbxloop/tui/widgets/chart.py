"""Plots for the shapes a :class:`~sbxloop.tui.widgets.band.Band` cannot draw.

A Band answers *what share* — one row, solid colour, no axis, and better
at that question than any library. It cannot answer *what shape*: where a
distribution piles up, whether two runs at the same cost took the same
time, what a series did between its endpoints. Those need a scale, and a
scale needs ticks.

So this module is deliberately small and deliberately additive. Nothing
here replaces a band; these are the three plots the overview had no way to
draw — a labelled series, a histogram, and a scatter — behind a house
wrapper that keeps them looking like the rest of the console.

The drawing is `plotext`, wrapped by `textual-plotext`. Its ``"auto"``
theme derives a palette from ``app.theme_variables`` and re-registers on
``theme_changed_signal``, so a plot follows the console's theme without
being told to.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from textual_plotext import PlotextPlot

#: A plot is worth drawing only once there is a shape to see. Below this
#: many points a histogram is a bar chart of ones and a scatter is a
#: handful of dots — the median-and-p90 sentence says more, and the caller
#: falls back to it.
MIN_POINTS = 8

#: Tall enough for ticks to be readable, short enough that two fit on a
#: page without scrolling on a normal terminal.
CHART_HEIGHT = 13

#: A single horizontal bar carries no vertical information, so it gets the
#: shortest frame that still leaves room for the axis and its ticks.
BAR_HEIGHT = 7

#: Rows a ranked bar needs per entry, plus the frame and axis. Plotext
#: gives a horizontal bar two rows before it labels one, so an entry that
#: gets fewer than two loses its name.
ROWS_PER_ENTRY = 2
RANKED_CHROME = 4

#: How many ticks a relabelled axis gets. Five spans the range without the
#: labels running into each other at the widths these plots get.
TICKS = 5

#: Plotext's fallback when it does not recognise a colour. It does not
#: raise on one it cannot read — `color="not-a-colour"` draws in this,
#: exactly like a hex string does — so a palette handed straight to
#: plotext fails silently and every series comes out the same blue.
_PLOTEXT_DEFAULT = 12


def rgb(colour: str) -> tuple[int, int, int]:
    """A ``#RRGGBB`` band colour as the triple plotext understands.

    `band.py` keeps every colour with a fixed meaning, and those are the
    colours these plots must use: a landed bar that is not `OK_COLOUR` is
    a different green from the one four inches up the same screen. Plotext
    accepts a name or an ``(r, g, b)`` triple and quietly ignores anything
    else, so the hex has to be unpacked here."""
    text = colour.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"not a #RRGGBB colour: {colour!r}")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def _spread(lo: float, hi: float, count: int = TICKS) -> list[float]:
    """`count` positions from `lo` to `hi` inclusive."""
    if count < 2 or hi <= lo:
        return [lo]
    step = (hi - lo) / (count - 1)
    return [lo + step * i for i in range(count)]


def whole_ticks(peak: float, count: int = TICKS, floor: float = 0.0) -> list[int]:
    """Round whole numbers from `floor` to at least `peak`.

    Every y axis on these plots counts things — runs in a bin, turns in a
    day, turns in a run — and plotext rules them from the data, which
    gives ``211.9`` and ``137.0``. Neither is a count. The step is the
    first of 1, 2 or 5 times a power of ten that covers the range in
    `count` steps, so the labels land on numbers a person would have
    chosen. `floor` lets a scatter start at its lowest point rather than
    at zero, which would spend half the plot on empty space."""
    low = min(math.floor(floor), math.floor(peak))
    span = max(peak - low, 0.0)
    if span <= 0:
        return [low, low + 1]
    rough = span / max(count - 1, 1)
    magnitude = 10 ** math.floor(math.log10(rough))
    step = magnitude
    for multiple in (1, 2, 5, 10):
        step = multiple * magnitude
        if step >= rough:
            break
    whole = max(int(step), 1)
    start = (low // whole) * whole
    return list(range(start, int(peak) + whole, whole))


class Chart(PlotextPlot):
    """A plot in the console's house style.

    The section header above a chart already names it — every other block
    on these pages is introduced by a ``classes="h"`` panel — so a chart
    never draws a title of its own and repeats it.
    """

    DEFAULT_CSS = f"""
    Chart {{ height: {CHART_HEIGHT}; }}
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        #: What the plot is of, in words. `render` paints the plot, which
        #: leaves no text for a test or a reader to assert on; this is the
        #: chart saying what it drew.
        self.caption = ""

    def label_x(self, lo: float, hi: float, fmt: Callable[[float], str]) -> None:
        """Relabel the x axis in the units the page speaks.

        Plotext numbers an axis from the data, so a duration axis reads
        ``620.3`` where every other line on the page would say ``10m``.
        The positions stay where plotext put them; only the labels change.
        """
        positions = _spread(lo, hi)
        self.plt.xticks(positions, [fmt(v) for v in positions])

    def count_x(self, peak: float, floor: float = 0.0) -> None:
        """Whole numbers along an x axis that counts things — the value
        axis of a horizontal bar, where `count_y` rules a vertical one."""
        ticks = whole_ticks(peak, floor=floor)
        self.plt.xticks([float(t) for t in ticks], [f"{t:,}" for t in ticks])

    def count_y(self, peak: float, floor: float = 0.0) -> None:
        """Whole numbers up a y axis that counts things.

        A histogram's axis counts runs and plotext will happily rule it at
        1.5, which is not a number of runs that can exist."""
        ticks = whole_ticks(peak, floor=floor)
        self.plt.yticks([float(t) for t in ticks], [f"{t:,}" for t in ticks])


def bars(labels: Sequence[str], values: Sequence[float], colour: str) -> Chart:
    """A labelled series: the trend a sparkline shows without a scale."""
    chart = Chart()
    plt = chart.plt
    peak = max([*values, 0.0])
    plt.bar(list(labels), [float(v) for v in values], color=rgb(colour))
    chart.count_y(peak)
    chart.caption = f"{len(values)} buckets, peak {peak:,.0f}"
    return chart


def stacked(
    labels: Sequence[str],
    series: Sequence[tuple[str, Sequence[float], str]],
    peak: float | None = None,
) -> Chart:
    """A bucket per label, each split by what it is made of.

    The day-by-day strip did this as one `Band` per day, which shows each
    day's *mix* but makes the week's shape something you read down a
    column of bars. Stacked against a shared scale, the mix and the trend
    are the same picture."""
    chart = Chart()
    names = [name for name, _values, _colour in series]
    # No `labels=`: plotext draws its key *inside* the axes, over the top
    # of the bars it is describing. The caller puts `band.legend` under the
    # plot instead — the same key every other stack on these pages carries.
    # A per-series colour list is real despite the bundled stub declaring
    # one colour; a list of triples does paint distinct series.
    chart.plt.stacked_bar(
        list(labels),
        [[float(v) for v in values] for _name, values, _colour in series],
        color=[rgb(colour) for _name, _values, colour in series],  # type: ignore[arg-type]
    )
    totals = [sum(parts) for parts in zip(*(values for _n, values, _c in series), strict=True)]
    chart.count_y(peak if peak is not None else max([*totals, 0.0]))
    chart.caption = f"{len(labels)} buckets, {', '.join(names)}"
    return chart


def proportion(
    segments: Sequence[tuple[str, float, str]],
    fmt: Callable[[float], str] | None = None,
) -> Chart:
    """One horizontal bar split into its parts, against a value axis.

    A `Band` says the same thing in a single row and says it well; this
    trades that row count for a scale, so the share can be read off as a
    quantity rather than only as a proportion."""
    chart = Chart()
    chart.styles.height = BAR_HEIGHT
    values = [max(value, 0.0) for _name, value, _colour in segments]
    chart.plt.stacked_bar(
        [""],
        [[value] for value in values],
        color=[rgb(colour) for _name, _value, colour in segments],  # type: ignore[arg-type]
        orientation="horizontal",
    )
    total = sum(values)
    if fmt is not None:
        chart.label_x(0.0, total, fmt)
    else:
        chart.count_x(total)
    chart.caption = ", ".join(f"{name} {value:,.0f}" for name, value, _c in segments)
    return chart


def ranked(
    rows: Sequence[tuple[str, float]],
    colour: str,
    fmt: Callable[[float], str] | None = None,
) -> Chart:
    """A short ranked list as bars against a scale, biggest at the top.

    The band version drew each row against the biggest, which shows the
    ranking and hides the quantity: two runs at 400 and 12 turns looked
    like a full bar and a stub with no way to tell 12 from 120. Plotext
    stacks its categories upward, so the order is reversed on the way in
    to put the costliest run at the top where the eye starts."""
    chart = Chart()
    chart.styles.height = max(len(rows) * ROWS_PER_ENTRY + RANKED_CHROME, BAR_HEIGHT)
    ordered = list(reversed(rows))
    chart.plt.bar(
        [label for label, _value in ordered],
        [float(value) for _label, value in ordered],
        orientation="horizontal",
        color=rgb(colour),
    )
    peak = max([value for _label, value in rows], default=0.0)
    if fmt is not None:
        chart.label_x(0.0, peak, fmt)
    else:
        chart.count_x(peak)
    chart.caption = f"{len(rows)} ranked, top {rows[0][0] if rows else '—'}"
    return chart


def histogram(
    values: Sequence[float],
    bins: int,
    colour: str,
    fmt: Callable[[float], str] | None = None,
) -> Chart:
    """Where a distribution piles up — the half of a spread that median
    and p90 cannot show, because two very different shapes share them."""
    chart = Chart()
    numbers = [float(v) for v in values]
    chart.plt.hist(numbers, bins=bins, color=rgb(colour))
    chart.count_y(_tallest(numbers, bins))
    if fmt is not None and numbers:
        chart.label_x(min(numbers), max(numbers), fmt)
    chart.caption = f"{len(values)} runs across {bins} bins"
    return chart


def _tallest(values: Sequence[float], bins: int) -> int:
    """How many runs land in the fullest bin — the top of the count axis.

    Counted the same way plotext bins them (equal width, last bin closed)
    so the axis cannot come up short of the tallest bar."""
    if not values:
        return 1
    lo, hi = min(values), max(values)
    if hi <= lo:
        return len(values)
    counts = [0] * bins
    for value in values:
        index = min(int((value - lo) / (hi - lo) * bins), bins - 1)
        counts[index] += 1
    return max(counts)


def scatter(
    xs: Sequence[float],
    ys: Sequence[float],
    colour: str,
    fmt: Callable[[float], str] | None = None,
    *,
    whole_y: bool = False,
) -> Chart:
    """Two costs against each other, one dot per run. The run sitting away
    from the crowd is the point; a ranked list buries it under whichever
    axis it happened to be sorted by."""
    chart = Chart()
    across = [float(x) for x in xs]
    up = [float(y) for y in ys]
    # Braille packs four dots into a cell, so a hundred runs stay
    # distinguishable in a plot thirteen rows tall.
    chart.plt.scatter(across, up, color=rgb(colour), marker="braille")
    if fmt is not None and across:
        chart.label_x(min(across), max(across), fmt)
    if whole_y and up:
        chart.count_y(max(up), floor=min(up))
    chart.caption = f"{len(xs)} runs plotted"
    return chart


def enough(values: Sequence[float]) -> bool:
    """Whether a distribution has enough points to be worth a plot."""
    return len(values) >= MIN_POINTS


__all__ = [
    "BAR_HEIGHT",
    "CHART_HEIGHT",
    "MIN_POINTS",
    "TICKS",
    "Chart",
    "bars",
    "enough",
    "histogram",
    "proportion",
    "ranked",
    "rgb",
    "scatter",
    "whole_ticks",
]
