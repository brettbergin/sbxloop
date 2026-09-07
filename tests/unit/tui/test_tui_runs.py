"""The Runs screen: ordered by the clock its own column shows, a state
that cannot eat the table, and what a run cost beside what it did."""

from __future__ import annotations

import time
from pathlib import Path

from sbxloop.engine.store import StateStore
from sbxloop.paths import SbxloopHome
from sbxloop.tui.screens.runs import COLUMNS, RunsScreen, fit, took
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable
from sbxloop_worker.protocol import Usage
from tests.unit.tui.conftest import FakeCtl, drive, live_status, make_app

LONG_REASON = (
    "github op raw.api failed: GithubOpError: gh api POST "
    "/repos/owner/name/git/trees failed (rc=1): HTTP 502 Bad Gateway while "
    "creating the tree for the delivery commit; the request was retried "
    "three times and the last attempt also returned 502 from the API"
)


def test_took_reads_at_a_glance() -> None:
    assert took(0) == "—" and took(45) == "45s" and took(600) == "10m"
    assert took(3600) == "1h00" and took(8400) == "2h20"
    assert took(-5) == "—", "a clock skew is not a negative duration"


def test_columns_drop_by_priority_never_overflow() -> None:
    """One long value used to size a column and push eight others off the
    screen. Columns are dropped deliberately now, weakest first."""
    everything = fit(1000)
    assert everything == COLUMNS, "given room, everything shows"
    for width in range(30, 200):
        chosen = fit(width)
        assert sum(c.width for c in chosen) <= max(width, 40) or len(chosen) == 1
        keys = [c.key for c in chosen]
        assert keys == [c.key for c in COLUMNS if c.key in keys], "display order is kept"
    # The things you cannot reconstruct from an id survive longest; the
    # repository is near-noise on a single-repo host and goes first.
    narrow = [c.key for c in fit(70)]
    assert "run" in narrow and "state" in narrow and "title" in narrow
    assert "repo" not in narrow and "item" not in narrow


def seed(home: SbxloopHome) -> None:
    """A run that started first but was touched last — the case the old
    ordering buried."""
    now = time.time()
    store = StateStore(home.state_db)
    plan = [
        # id,        state,    started days ago, last touched mins ago, turns, active
        ("r_old_new", "merged", 6.0, 2.0, 300, 4000.0),
        ("r_recent", "failed", 0.5, 400.0, 20, 300.0),
        ("r_middle", "merged", 2.0, 900.0, 60, 900.0),
    ]
    for run_id, state, days, mins, turns, active in plan:
        created = now - days * 86400
        store.create_run(run_id, f"outcome for {run_id}")
        store._conn.execute(
            "UPDATE runs SET state=?, created_at=?, updated_at=?, reason=?, pr_number=? "
            "WHERE run_id=?",
            (
                state,
                created,
                now - mins * 60,
                LONG_REASON if state == "failed" else None,
                7,
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
            usage=Usage(input_tokens=turns * 10, output_tokens=0, cache_read_tokens=0),
        )
        store._conn.execute(
            "UPDATE phase_attempts SET ended_at=? WHERE run_id=?", (created + active, run_id)
        )
    store._conn.commit()
    store.close()


def test_the_store_hands_back_the_runs_that_moved(tmp_path: Path) -> None:
    """The screen sorts what it is given, so it alone cannot fix this: the
    *limit* is applied in the store, and under the old `created_at` order a
    long-lived run still being worked on fell off the end of the list."""
    store = StateStore(tmp_path / "s.db")
    now = time.time()
    try:
        for index in range(5):
            run_id = f"r{index}"
            store.create_run(run_id, "outcome")
            # Started oldest-first; touched newest-first. The two orders
            # are exact reverses of each other.
            store._conn.execute(
                "UPDATE runs SET created_at=?, updated_at=? WHERE run_id=?",
                (now - (10 - index) * 86400, now - index * 60, run_id),
            )
        store._conn.commit()
        assert [r.run_id for r in store.recent_runs()] == ["r0", "r1", "r2", "r3", "r4"]
        assert [r.run_id for r in store.list_runs()] == ["r4", "r3", "r2", "r1", "r0"]
        # The limit follows the order that matters: the two runs touched
        # most recently, not the two started most recently.
        assert [r.run_id for r in store.recent_runs(limit=2)] == ["r0", "r1"]
        costs = store.run_costs(["r0", "r1"])
        assert costs == {}, "no phases recorded, no cost claimed"
    finally:
        store.close()


def test_runs_are_ordered_by_the_clock_the_column_shows(tmp_path: Path) -> None:
    """The list was newest-*started* first while the last column showed
    when each run was last *touched*, so a run that began on Tuesday and
    merged this morning sat far down a list saying "2m ago"."""
    home = SbxloopHome(tmp_path / "state")
    home.ensure_tree()
    seed(home)
    from sbxloop.daemon.store import DaemonStore

    DaemonStore(home.state_db).close()

    async def scenario() -> None:
        app = make_app(home)
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.press("2")
            await pilot.pause(1.5)
            table = app.screen.query_one("#runs", ConsoleTable)
            assert table.get_row_index("r_old_new") == 0, (
                "started first, touched last — it belongs at the top"
            )
            assert table.get_row_index("r_recent") == 1
            assert table.get_row_index("r_middle") == 2

    drive(scenario)


def test_the_live_run_is_pinned_above_the_rest(tmp_path: Path) -> None:
    home = SbxloopHome(tmp_path / "state")
    home.ensure_tree()
    seed(home)
    from sbxloop.daemon.store import DaemonStore

    DaemonStore(home.state_db).close()

    async def scenario() -> None:
        status = live_status(current={"item_id": "i", "run_id": "r_middle", "title": "t"})
        app = make_app(home, ctl=FakeCtl(status))
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.press("2")
            await pilot.pause(1.5)
            table = app.screen.query_one("#runs", ConsoleTable)
            assert table.get_row_index("r_middle") == 0, "observing wants the live run first"
            assert table.get_row_index("r_old_new") == 1

    drive(scenario)


def test_a_long_reason_cannot_eat_the_table(tmp_path: Path) -> None:
    """A 400-character failure reason sized the state column and pushed
    eight others off the screen. It lives under the table now, in full."""
    home = SbxloopHome(tmp_path / "state")
    home.ensure_tree()
    seed(home)
    from sbxloop.daemon.store import DaemonStore

    DaemonStore(home.state_db).close()

    async def scenario() -> None:
        app = make_app(home)
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.press("2")
            await pilot.pause(1.5)
            screen = app.screen
            assert isinstance(screen, RunsScreen)
            table = screen.query_one("#runs", ConsoleTable)
            table.move_cursor(row=table.get_row_index("r_recent"))
            await pilot.pause(0.5)
            row = table.get_row_at(table.get_row_index("r_recent"))
            state = str(row[1])
            assert "failed" in state
            assert len(state) < 20, f"the state is a word, not a paragraph: {state!r}"
            assert "502" not in state
            # …and the title, the cost and the age all still have room.
            titles = [c.title for c in fit(table.size.width)]
            for wanted in ("title", "turns", "took", "updated"):
                assert wanted in titles, f"{wanted} was pushed off the table"
            # The reason is under the table, uncut.
            detail = screen.query_one("#detail", TextPanel).content_text
            assert LONG_REASON in detail, "the whole reason, not a clipped one"

    drive(scenario)


def test_a_run_shows_what_it_cost(tmp_path: Path) -> None:
    home = SbxloopHome(tmp_path / "state")
    home.ensure_tree()
    seed(home)
    from sbxloop.daemon.store import DaemonStore

    DaemonStore(home.state_db).close()

    async def scenario() -> None:
        app = make_app(home)
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.press("2")
            await pilot.pause(1.5)
            table = app.screen.query_one("#runs", ConsoleTable)
            titles = [c.title for c in fit(table.size.width)]
            row = table.get_row_at(table.get_row_index("r_old_new"))
            assert str(row[titles.index("turns")]) == "300"
            assert str(row[titles.index("took")]) == "1h06"

    drive(scenario)


def test_p_needs_a_pull_request(tmp_path: Path) -> None:
    """`p` on a run with no PR says so rather than doing nothing."""
    home = SbxloopHome(tmp_path / "state")
    home.ensure_tree()
    store = StateStore(home.state_db)
    store.create_run("r_nopr", "no pull request here")
    store.close()
    from sbxloop.daemon.store import DaemonStore

    DaemonStore(home.state_db).close()

    async def scenario() -> None:
        app = make_app(home)
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.press("2")
            await pilot.pause(1.5)
            await pilot.press("p")
            await pilot.pause(0.5)
            assert isinstance(app.screen, RunsScreen), "no browser, no crash"

    drive(scenario)
