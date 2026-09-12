"""Queue: grouped by what you would do about it.

The screen answers two questions — what is stuck, and what runs next — so
the rules under test are about not lying about either: a row states its own
reason and the daemon's block is stated once, a verb is offered only where
it would work, and a done item is not queue.
"""

from __future__ import annotations

import time
from pathlib import Path

from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop.paths import SbxloopHome
from sbxloop.tui.screens.items import SECTIONS, VERB_STATES, ItemsScreen, median, waited
from sbxloop.tui.widgets.tables import ConsoleTable
from tests.unit.tui.conftest import FakeCtl, drive, live_status, make_app, until

DAY = 86400.0


def footer_keys(app: object) -> set[str]:
    """The keys the footer is actually offering for the selected row."""
    screen = app.screen  # type: ignore[attr-defined]
    return {binding.key for _n, binding, _e, _t in screen.active_bindings.values()}


def queue_home(tmp_path: Path) -> SbxloopHome:
    """A home holding one of everything the four sections are meant to
    show, plus a parked item old enough that the window has to hide it.

    Built bare rather than on the shared `seeded` fixture: that one brings
    its own runs and an orphan, and a count this screen is asserting on
    should be a count of what the test put there."""
    home = SbxloopHome(tmp_path / "state")
    home.ensure_tree()
    seed_queue(home)
    return home


def seed_queue(home: SbxloopHome) -> None:
    now = time.time()
    store = StateStore(home.state_db)
    daemon = DaemonStore(home.state_db)

    def add(key: int, title: str) -> str:
        item = WorkItem(
            item_id=f"gh:issue:{key}",
            source_key=str(key),
            title=title,
            url=f"https://x/issues/{key}",
            repo="o/r",
        )
        daemon.upsert_new(item, now=now - 2 * DAY)
        return item.item_id

    gated = add(40, "Bump the CI image")
    store.create_run("r_gate", "Bump the CI image")
    store.set_run_state("r_gate", "merged")
    daemon.mark_running(gated, "r_gate", now=now - 4 * 3600)
    daemon.create_merge_gate(
        "r_gate", gated, "o/r", 170, "https://x/pull/170", None, ["brett"], "tok", now - 4 * 3600
    )
    daemon.mark_gated(gated, now - 4 * 3600)

    review = add(82, "Retry fetch on 5xx")
    store.create_run("r_rev", "Retry fetch on 5xx")
    store.set_run_state("r_rev", "building")
    daemon.mark_running(review, "r_rev", now=now - DAY)
    daemon.mark_awaiting_review(review, now=now - DAY)

    live = add(41, "Add retries to the fetch client")
    store.create_run("r_live", "Add retries")
    store.set_run_state("r_live", "building")
    daemon.mark_running(live, "r_live", now=now - 120)

    add(44, "Cache the registry probe")
    daemon.mark_blocked(add(29, "Wire the client"), "waits on #33", now=now - 2 * DAY)
    daemon.mark_failed(add(33, "Rename the thing"), "github 502", now=now - 2 * DAY, requeue=False)
    daemon.mark_failed(add(12, "Ancient failure"), "forgotten", now=now - 30 * DAY, requeue=False)
    daemon.mark_done(add(9, "Long since shipped"), now=now - DAY)

    daemon.set_local_heartbeat(now)
    daemon.close()
    store.close()


def test_a_span_of_waiting_is_not_an_age() -> None:
    """`age` reads a timestamp and says "2d ago", which is the wrong tense
    for "the oldest has waited 2d"."""
    assert waited(0) == "0s" and waited(45) == "45s"
    assert waited(600) == "10m" and waited(7200) == "2h" and waited(3 * DAY) == "3d"
    assert waited(-5) == "0s", "a clock skew is not a negative wait"
    assert median([]) == 0.0 and median([1.0, 5.0, 9.0]) == 5.0


def test_the_sections_partition_the_states_and_drop_done() -> None:
    """Every state a work item can be in is either placed or deliberately
    absent — a state that falls through appears on no screen at all."""
    placed = [state for section in SECTIONS for state in section.states]
    assert len(placed) == len(set(placed)), "a state landed in two sections"
    from sbxloop.daemon.discord_format import ITEM_STATE_MARKER

    assert set(placed) | {"done"} == set(ITEM_STATE_MARKER)
    assert "done" not in placed, "done is finished work; Runs lists it"


def test_every_verb_is_reachable_from_some_state() -> None:
    """A verb no state allows is a key that can never be pressed."""
    states = {state for section in SECTIONS for state in section.states}
    for verb, allowed in VERB_STATES.items():
        assert set(allowed) & states, f"{verb} is offered in no section"


def test_the_queue_groups_by_what_you_would_do(tmp_path: Path) -> None:
    home = queue_home(tmp_path)

    async def scenario() -> None:
        app = make_app(home, emoji=False)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("3")
            await until(pilot, lambda: isinstance(app.screen, ItemsScreen))
            screen = app.screen
            assert isinstance(screen, ItemsScreen)
            counts = {
                s.key: screen.query_one(f"#{s.key}", ConsoleTable).row_count for s in SECTIONS
            }
            assert counts["waiting"] == 2, "the gate and the review hold"
            assert counts["running"] == 1
            assert counts["queued"] == 1
            # 29 blocked and 33 failed are inside the window; 12 is not.
            assert counts["parked"] == 2
            ids = [i.item_id for i in screen.shown["parked"]]
            assert "gh:issue:12" not in ids, "a 30-day-old failure is outside the window"
            assert "gh:issue:9" not in [
                i.item_id for rows in screen.shown.values() for i in rows
            ], "a done item is not queue"

    drive(scenario)


def test_a_filter_reaches_past_the_window(tmp_path: Path) -> None:
    """The parked section shows a week. A search that cannot reach the item
    you typed is not a search, so filtering lifts the window."""
    home = queue_home(tmp_path)

    async def scenario() -> None:
        app = make_app(home, emoji=False)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("3")
            await until(pilot, lambda: isinstance(app.screen, ItemsScreen))
            screen = app.screen
            assert isinstance(screen, ItemsScreen)
            assert "gh:issue:12" not in [i.item_id for i in screen.shown["parked"]]
            await pilot.press("slash")
            for key in "ancient":
                await pilot.press(key)
            await pilot.pause(0.6)
            assert [i.item_id for i in screen.shown["parked"]] == ["gh:issue:12"]

    drive(scenario)


def test_the_footer_offers_only_what_the_row_allows(tmp_path: Path) -> None:
    """`m` used to be advertised on every row and worked on the few with an
    open gate. A key that cannot work is not offered.

    This asserts the footer rather than `check_action` on purpose: the
    screen must return ``False`` to hide a binding — ``None`` renders it
    greyed but present, and the difference is invisible in a text dump."""
    home = queue_home(tmp_path)

    async def scenario() -> None:
        app = make_app(home, emoji=False)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("3")
            await until(pilot, lambda: isinstance(app.screen, ItemsScreen))
            screen = app.screen
            assert isinstance(screen, ItemsScreen)
            screen.query_one("#waiting", ConsoleTable).focus()
            screen.query_one("#waiting", ConsoleTable).move_cursor(row=0)
            await pilot.pause(0.4)
            item = screen._selected()
            assert item is not None and item.state == "gated"
            keys = footer_keys(app)
            assert "m" in keys, "the gated row is where merge works"
            assert "t" not in keys and "u" not in keys, "retry cannot apply to a gated item"

            screen.query_one("#parked", ConsoleTable).focus()
            screen.query_one("#parked", ConsoleTable).move_cursor(row=0)
            await pilot.pause(0.4)
            keys = footer_keys(app)
            assert "t" in keys and "u" in keys, "a parked item is what retry is for"
            assert "m" not in keys, "no gate here"

    drive(scenario)


def test_the_daemon_s_block_is_stated_once_and_the_rows_keep_their_reason(
    tmp_path: Path,
) -> None:
    """When the breaker is open nothing dispatches whatever a row says. The
    block is the banner's job; the row keeps its own state, and the queued
    table is dimmed so neither reads as the whole truth."""
    home = queue_home(tmp_path)

    async def scenario() -> None:
        app = make_app(
            home,
            emoji=False,
            ctl=FakeCtl(live_status(breaker_open=True, consecutive_failures=2)),
        )
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("3")
            await until(pilot, lambda: isinstance(app.screen, ItemsScreen))
            screen = app.screen
            assert isinstance(screen, ItemsScreen)
            assert "breaker open" in screen.blocked and "nothing dispatches" in screen.blocked
            assert screen.query_one("#banner").display
            assert screen.query_one("#queued", ConsoleTable).has_class("held")
            # The row still says why *it* is waiting, not why the daemon is.
            for item in screen.shown["queued"]:
                assert "breaker" not in screen._why(item, app.state.items, time.time())

    drive(scenario)


def test_a_healthy_daemon_draws_no_banner(tmp_path: Path) -> None:
    home = queue_home(tmp_path)

    async def scenario() -> None:
        app = make_app(home, emoji=False)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("3")
            await until(pilot, lambda: isinstance(app.screen, ItemsScreen))
            screen = app.screen
            assert isinstance(screen, ItemsScreen)
            assert screen.blocked == ""
            assert not screen.query_one("#banner").display
            assert not screen.query_one("#queued", ConsoleTable).has_class("held")

    drive(scenario)


def test_j_and_k_cross_a_section_boundary(tmp_path: Path) -> None:
    """The cursor is not trapped in whichever table it landed in."""
    home = queue_home(tmp_path)

    async def scenario() -> None:
        app = make_app(home, emoji=False)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("3")
            await until(pilot, lambda: isinstance(app.screen, ItemsScreen))
            screen = app.screen
            assert isinstance(screen, ItemsScreen)
            waiting = screen.query_one("#waiting", ConsoleTable)
            waiting.focus()
            waiting.move_cursor(row=waiting.row_count - 1)
            await pilot.pause(0.3)
            await pilot.press("j")
            await pilot.pause(0.4)
            assert screen.query_one("#running", ConsoleTable).has_focus, "j stopped at the edge"
            await pilot.press("k")
            await pilot.pause(0.4)
            assert waiting.has_focus, "k did not come back"

    drive(scenario)
