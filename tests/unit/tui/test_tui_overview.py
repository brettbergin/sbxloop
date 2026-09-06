"""The Overview's pages: a finding in a sentence, then a bar.

The screen answers "is this working well". Each page must state its own
finding in prose before it draws anything, keep code and workload runs
apart, and never call a cancelled run a failure.
"""

from __future__ import annotations

import time
from pathlib import Path

from textual.containers import VerticalScroll

from sbxloop.engine.store import StateStore
from sbxloop.paths import SbxloopHome
from sbxloop.tui.screens.overview import PAGES, OverviewScreen, PageRail, count, hm
from sbxloop.tui.screens.run_detail import RunDetailScreen
from sbxloop.tui.widgets.band import Segment, legend, paint, widths
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop_worker.protocol import Usage
from tests.unit.tui.conftest import drive, make_app

DAY = 86400.0


def page_text(app: object) -> str:
    from sbxloop.tui.app import SbxloopTui

    assert isinstance(app, SbxloopTui)
    page = app.screen.query_one("#page", VerticalScroll)
    return "\n".join(w.content_text for w in page.walk_children() if isinstance(w, TextPanel))


def seed_week(home: SbxloopHome) -> None:
    """A week the pages have something to say about: a costly run, a run
    parked most of a day, two failures sharing a cause, one cancelled."""
    now = time.time()
    store = StateStore(home.state_db)
    plan = [
        ("r_costly", "code", "merged", 5.0, 3000.0, 5000.0, 400),
        ("r_parked", "code", "merged", 4.0, 600.0, 50000.0, 40),
        ("r_bad1", "code", "failed", 3.0, 200.0, 300.0, 12),
        ("r_bad2", "code", "failed", 2.0, 200.0, 300.0, 12),
        ("r_stop", "code", "cancelled", 2.0, 100.0, 150.0, 5),
        ("r_wl", "workload", "completed", 1.0, 300.0, 400.0, 30),
    ]
    for run_id, kind, state, days, active, elapsed, turns in plan:
        created = now - days * DAY
        store.create_run(run_id, f"outcome {run_id}", kind=kind)
        store._conn.execute(
            "UPDATE runs SET state=?, created_at=?, updated_at=?, reason=? WHERE run_id=?",
            (
                state,
                created,
                created + elapsed,
                "github op raw.api failed: 502" if state == "failed" else None,
                run_id,
            ),
        )
        store.record_phase(
            run_id,
            "build",
            task_id="t1",
            attempt=1,
            status="ok",
            output_json="{}",
            started_at=created,
            turns=turns,
            usage=Usage(input_tokens=turns * 1000, output_tokens=0, cache_read_tokens=0),
        )
        store._conn.execute(
            "UPDATE phase_attempts SET ended_at=? WHERE run_id=?", (created + active, run_id)
        )
    store._conn.commit()
    store.close()


def test_hm_and_count_read_at_a_glance() -> None:
    assert hm(0) == "0s" and hm(45) == "45s" and hm(600) == "10m"
    assert hm(3600) == "1h 00m" and hm(50000) == "13h 53m"
    assert hm(-5) == "0s", "a clock skew is not a negative duration"
    assert count(950) == "950" and count(38_600) == "39k" and count(4_200_000) == "4.2M"


def test_a_band_fills_its_width_exactly() -> None:
    """Rounding must never leave a gap or overrun the row, at any width."""
    parts = [Segment("a", 1, "#111111"), Segment("b", 2, "#222222"), Segment("c", 7, "#333333")]
    for width in range(1, 120):
        cells = widths(parts, width)
        assert sum(cells) == width, f"width {width} did not fill exactly"
        assert all(c >= 0 for c in cells)
        assert len(paint(parts, width).plain) == width
    assert widths(parts, 0) == [] and widths([], 30) == []
    assert len(paint([], 12).plain) == 12, "an empty band is still a row"
    # A segment worth nothing takes no cells and is left out of the key.
    assert widths([Segment("a", 0, "#1"), Segment("b", 5, "#2")], 10) == [0, 10]
    assert legend([Segment("build", 3, "#111111"), Segment("gate", 1, "#222222")]).plain.startswith(
        "■ build 75%"
    )
    assert legend([]).plain == ""


def test_every_page_states_its_finding_then_draws(seeded: SbxloopHome) -> None:
    seed_week(seeded)

    async def scenario() -> None:
        app = make_app(seeded)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(2.0)
            assert isinstance(app.screen, OverviewScreen)

            summary = page_text(app)
            assert "runs this week" in summary and "waiting on you" in summary
            assert "outcome" in summary and "phases" in summary

            await pilot.press("c")
            await pilot.pause(0.5)
            cost = page_text(app)
            assert "turns" in cost and "per run" in cost
            assert "r_costly" in cost, "the costliest run is named, not averaged away"
            assert "of the week's turns" in cost

            await pilot.press("t")
            await pilot.pause(0.5)
            timed = page_text(app)
            assert "waited on a human" in timed
            assert "r_parked" in timed, "the run that waited longest is named"

            await pilot.press("h")
            await pilot.pause(0.5)
            health = page_text(app)
            # The seeded home carries an orphaned run of its own, so this
            # week has three failures across two causes — the page groups
            # them rather than listing three one-offs.
            assert "3 runs failed" in health and "2 causes" in health
            assert "github op raw.api failed" in health

            await pilot.press("f")
            await pilot.pause(0.5)
            flow = page_text(app)
            assert "code" in flow and "workload" in flow, "the kinds stay apart"

            await pilot.press("s")
            await pilot.pause(0.5)
            assert "runs this week" in page_text(app)

    drive(scenario)


def test_the_page_rail_marks_the_page_and_uses_letters(seeded: SbxloopHome) -> None:
    """Digits belong to the console's rail; Overview's pages take letters
    so a page cannot steal a screen's key."""
    assert {item.key for item in PAGES}.isdisjoint({str(n) for n in range(1, 9)})

    async def scenario() -> None:
        app = make_app(seeded)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(1.0)
            rail = app.screen.query_one(PageRail)
            assert "▸ Summary" in rail.render().plain
            await pilot.press("t")
            await pilot.pause(0.5)
            assert "▸ Time" in rail.render().plain
            assert "s Summary" in rail.render().plain

    drive(scenario)


def test_o_opens_the_costliest_run(seeded: SbxloopHome) -> None:
    """The spike is one keystroke from the run behind it."""
    seed_week(seeded)

    async def scenario() -> None:
        app = make_app(seeded)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(2.0)
            await pilot.press("o")
            await pilot.pause(1.5)
            assert isinstance(app.screen, RunDetailScreen)
            assert app.screen.run_id == "r_costly"

    drive(scenario)


def test_an_empty_window_says_so_rather_than_drawing_nothing(tmp_path: Path) -> None:
    home = SbxloopHome(tmp_path / "state")
    home.ensure_tree()
    StateStore(home.state_db).close()
    from sbxloop.daemon.store import DaemonStore

    DaemonStore(home.state_db).close()

    async def scenario() -> None:
        app = make_app(home)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(2.0)
            assert "No runs in the last 7 days" in page_text(app)

    drive(scenario)
