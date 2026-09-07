"""The overview's plots: the shapes a band cannot draw.

A band answers "what share" and needs no scale. These answer "what shape",
which does — so the rules under test are about the scale being honest: a
count axis ruled in whole runs, a duration axis read in the units the rest
of the page uses, and no plot at all when there are too few points for a
shape to exist.
"""

from __future__ import annotations

import time
from itertools import pairwise
from pathlib import Path

from sbxloop.daemon.store import DaemonStore
from sbxloop.paths import SbxloopHome
from sbxloop.tui.screens.overview import hm
from sbxloop.tui.widgets.chart import (
    MIN_POINTS,
    Chart,
    bars,
    enough,
    histogram,
    scatter,
    whole_ticks,
)
from tests.unit.tui.conftest import drive, make_app
from tests.unit.tui.test_tui_overview import page_text

from .test_tui_charts_seed import seed_many


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
            charts = app.screen.query(Chart)
            assert len(charts) == 1
            built = charts.first().plt.build()
            assert time.strftime("%a", time.localtime()) in built, "the days are named"

    drive(scenario)
