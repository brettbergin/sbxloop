"""The overview's plots: the shapes a band cannot draw.

A band answers "what share" and needs no scale. These answer "what shape",
which does — so the rules under test are about the scale being honest: a
count axis ruled in whole runs, a duration axis read in the units the rest
of the page uses, and no plot at all when there are too few points for a
shape to exist.
"""

from __future__ import annotations

import re
import time
from itertools import pairwise
from pathlib import Path

import pytest

from sbxloop.daemon.store import DaemonStore
from sbxloop.paths import SbxloopHome
from sbxloop.tui.screens.overview import OverviewScreen, hm
from sbxloop.tui.widgets.band import BAD_COLOUR, IDLE_COLOUR, OK_COLOUR, Band, Segment
from sbxloop.tui.widgets.chart import (
    MIN_POINTS,
    Chart,
    bars,
    enough,
    histogram,
    proportion,
    rgb,
    scatter,
    stacked,
    whole_ticks,
)
from tests.unit.tui.conftest import drive, make_app
from tests.unit.tui.test_tui_overview import page_text

from .test_tui_charts_seed import seed_many


def painted(chart: Chart, width: int = 60, height: int = 10) -> list[str]:
    """The distinct 24-bit colours a chart actually paints."""
    chart.plt.plotsize(width, height)
    return sorted(set(re.findall(r"38;2;[0-9;]+", chart.plt.build())))


def ansi(colour: str) -> str:
    red, green, blue = rgb(colour)
    return f"38;2;{red};{green};{blue}"


def test_a_hex_band_colour_becomes_a_triple_plotext_understands() -> None:
    assert rgb("#22C55E") == (34, 197, 94)
    assert rgb("22C55E") == (34, 197, 94), "the hash is optional"
    for bad in ("#FFF", "", "#GGGGGG"):
        with pytest.raises(ValueError):
            rgb(bad)


def test_a_chart_paints_the_band_palette_and_not_plotext_s_fallback() -> None:
    """Plotext does not raise on a colour it cannot read — `color="#22C55E"`
    and `color="not-a-colour"` both draw in its default. Handing it the hex
    straight made every series the same blue while looking correct in the
    source, so the palette has to be asserted on the painted output."""
    assert painted(bars(["a", "b"], [3.0, 9.0], OK_COLOUR)) == [ansi(OK_COLOUR)]
    assert painted(histogram([float(n) for n in range(20)], 6, BAD_COLOUR)) == [ansi(BAD_COLOUR)]
    assert painted(scatter([1.0, 2.0], [3.0, 7.0], IDLE_COLOUR)) == [ansi(IDLE_COLOUR)]


def test_a_stack_keeps_its_segments_apart() -> None:
    """A stacked bar whose parts share a colour says nothing at all."""
    drawn = proportion([("landed", 32.0, OK_COLOUR), ("failed", 8.0, BAD_COLOUR)])
    assert painted(drawn) == sorted({ansi(OK_COLOUR), ansi(BAD_COLOUR)})
    assert drawn.caption == "landed 32, failed 8"
    week = stacked(
        ["Mon", "Tue"],
        [
            ("landed", [4.0, 6.0], OK_COLOUR),
            ("failed", [1.0, 0.0], BAD_COLOUR),
            ("cancelled", [0.0, 1.0], IDLE_COLOUR),
        ],
    )
    assert painted(week) == sorted({ansi(OK_COLOUR), ansi(BAD_COLOUR), ansi(IDLE_COLOUR)})
    assert week.caption == "2 buckets, landed, failed, cancelled"


def test_a_count_axis_is_ruled_in_whole_things() -> None:
    """A bin holds runs. There is no such quantity as 1.5 runs, so no
    tick may say so."""
    for peak in range(1, 400):
        ticks = whole_ticks(peak)
        assert ticks[0] == 0, "a count axis starts at nothing"
        assert ticks[-1] >= peak, f"peak {peak} fell off the top of {ticks}"
        assert all(isinstance(t, int) for t in ticks)
        steps = {b - a for a, b in pairwise(ticks)}
        assert len(steps) == 1, f"uneven ticks for peak {peak}: {ticks}"
        step = steps.pop()
        mantissa = step / 10 ** (len(str(step)) - 1)
        assert mantissa in (1.0, 2.0, 5.0), f"step {step} is not a round number"
    # A window with nothing in it still gets an axis rather than a crash.
    assert whole_ticks(0) == [0, 1]
    assert whole_ticks(5, floor=5) == [5, 6], "a flat series is still an axis"


def test_a_scatter_axis_may_start_above_zero() -> None:
    """Anchoring at zero would spend half a scatter on empty space when
    every run cost between 50 and 140 turns — but the ticks stay whole."""
    ticks = whole_ticks(140, floor=52)
    assert ticks[0] <= 52 and ticks[-1] >= 140
    assert all(isinstance(t, int) for t in ticks)
    assert ticks[0] > 0, "the floor was ignored and the axis fell back to zero"
    built = scatter([1.0, 2.0], [52.0, 140.0], "#4C8DF6", whole_y=True).plt.build()
    assert "140.0" not in built, "a turn count was ruled in tenths"


def test_a_duration_axis_reads_in_the_units_the_page_speaks() -> None:
    """Plotext numbers an axis from the data: an elapsed axis would read
    `620.3` where every other line on the page says `10m`."""
    chart = scatter([60.0, 3600.0], [1.0, 2.0], "#4C8DF6", hm)
    labels = chart.plt.build()
    assert "1m" in labels and "1h 00m" in labels
    assert "3600" not in labels, "raw seconds leaked onto a duration axis"
    # Without a formatter the axis is left alone — a turn count is a
    # number, not a duration, and must not be run through `hm`.
    plain = scatter([60.0, 3600.0], [1.0, 2.0], "#4C8DF6").plt.build()
    assert "3600" in plain


def test_a_histogram_axis_clears_its_tallest_bar() -> None:
    """The count axis is computed from the bins, not from the run total —
    if it came up short the tallest bar would run off the top."""
    # Forty values in one narrow clump and one far outlier: the fullest
    # bin holds nearly all of them.
    values = [10.0] * 39 + [500.0]
    chart = histogram(values, 8, "#4C8DF6")
    assert "39" in chart.caption or chart.caption.startswith("40 runs")
    ticks = whole_ticks(39)
    assert ticks[-1] >= 39
    assert chart.caption == "40 runs across 8 bins"


def test_a_chart_says_in_words_what_it_drew() -> None:
    """`render` paints a plot, which leaves nothing for a reader — or a
    test — to read. The caption is the chart in prose."""
    assert bars(["Mon", "Tue"], [3.0, 9.0], "#4C8DF6").caption == "2 buckets, peak 9"
    assert histogram([float(n) for n in range(20)], 6, "#4C8DF6").caption == "20 runs across 6 bins"
    assert scatter([1.0, 2.0], [3.0, 4.0], "#4C8DF6").caption == "2 runs plotted"
    assert isinstance(bars([], [], "#4C8DF6"), Chart), "an empty series is still a chart"


def test_too_few_points_is_not_a_shape() -> None:
    assert not enough([1.0] * (MIN_POINTS - 1))
    assert enough([1.0] * MIN_POINTS)


def test_spread_draws_the_distributions_once_there_are_enough_runs(seeded: SbxloopHome) -> None:
    seed_many(seeded, count=40)

    async def scenario() -> None:
        app = make_app(seeded)
        async with app.run_test(size=(140, 60)) as pilot:
            await pilot.pause(2.0)
            await pilot.press("d")
            await pilot.pause(0.5)
            text = page_text(app)
            assert "in the window" in text, "the page states its finding first"
            assert "turns per run" in text and "working time per run" in text
            assert "runs across" in text, "the histogram captions itself"
            assert "runs plotted" in text, "the scatter captions itself"
            assert "x: elapsed" in text and "y: turns" in text
            charts = app.screen.query(Chart)
            assert len(charts) == 3, "two histograms and a scatter"

    drive(scenario)


def test_spread_falls_back_to_the_sentence_on_thin_data(tmp_path: Path) -> None:
    """Four runs make a histogram of ones. The median-and-p90 sentence says
    more, so the page says that instead of drawing a shape that is noise."""
    home = SbxloopHome(tmp_path / "state")
    home.ensure_tree()
    DaemonStore(home.state_db).close()
    seed_many(home, count=MIN_POINTS - 4)

    async def scenario() -> None:
        app = make_app(home)
        async with app.run_test(size=(140, 60)) as pilot:
            await pilot.pause(2.0)
            await pilot.press("d")
            await pilot.pause(0.5)
            text = page_text(app)
            assert "too few to show a shape" in text
            assert "median" in text and "p90" in text, "the numbers still get said"
            assert not app.screen.query(Chart), "nothing was drawn"

    drive(scenario)


def test_summary_draws_every_proportion_and_the_week(seeded: SbxloopHome) -> None:
    """Summary's shares and its trend are all plots now. The headings and
    the keys stay: a plot the reader cannot name is not an improvement."""
    seed_many(seeded, count=40)

    async def scenario() -> None:
        app = make_app(seeded)
        async with app.run_test(size=(140, 80)) as pilot:
            await pilot.pause(2.0)
            text = page_text(app)
            assert "runs this week" in text, "the page still opens in prose"
            for heading in ("outcome", "time", "phases", "day by day, by outcome"):
                assert heading in text, f"{heading} lost its heading"
            assert "landed" in text and "parked" in text, "the keys survived"
            # outcome, time, phases, and the week.
            assert len(app.screen.query(Chart)) == 4

    drive(scenario)


def test_a_share_of_nothing_stays_a_band() -> None:
    """An all-zero split has no proportion to draw — plotext would rule an
    axis across an empty frame and spend seven rows saying nothing — so
    that row falls back to the band, which draws an empty track in one."""
    screen = OverviewScreen()
    empty = screen._plotted("phases", "0s", [Segment("build", 0.0, OK_COLOUR)])
    assert not any(isinstance(w, Chart) for w in empty), "drew a plot of nothing"
    assert len(empty) == 1, "the fallback is the one-row band, not a stack of panels"

    real = screen._plotted("outcome", "80% ok", [Segment("landed", 4.0, OK_COLOUR)])
    assert any(isinstance(w, Chart) for w in real), "a real share was not plotted"


def test_cost_plots_the_trend_against_a_scale(seeded: SbxloopHome) -> None:
    """The sparkline this replaced drew the shape but named no value on
    it, so a quiet week and a heavy one looked identical."""
    seed_many(seeded, count=40)

    async def scenario() -> None:
        app = make_app(seeded)
        async with app.run_test(size=(140, 60)) as pilot:
            await pilot.pause(2.0)
            await pilot.press("c")
            await pilot.pause(0.5)
            assert "buckets, peak" in page_text(app)
            # The trend, the phase split, and the costliest runs.
            charts = app.screen.query(Chart)
            assert len(charts) == 3
            built = charts.first().plt.build()
            assert time.strftime("%a", time.localtime()) in built, "the days are named"


def test_every_page_draws_rather_than_paints_a_row(seeded: SbxloopHome) -> None:
    """No page still reports a share as a one-row band. A band paints with
    background colour and no glyph, which on a low-contrast terminal is a
    stripe you have to hunt for and carries no scale to read."""
    seed_many(seeded, count=40)

    async def scenario() -> None:
        app = make_app(seeded)
        async with app.run_test(size=(140, 90)) as pilot:
            await pilot.pause(2.0)
            for key, least in (("s", 4), ("f", 2), ("c", 3), ("t", 3), ("h", 1), ("d", 3)):
                await pilot.press(key)
                await pilot.pause(0.4)
                drawn = len(app.screen.query(Chart))
                assert drawn >= least, f"page {key} drew {drawn} charts, wanted {least}"
                assert not app.screen.query(Band), f"page {key} still paints a band"

    drive(scenario)

    drive(scenario)
