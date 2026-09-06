"""The console's screens, driven headless against a seeded store."""

from __future__ import annotations

from sbxloop.paths import SbxloopHome
from sbxloop.tui.screens.run_detail import RunDetailScreen
from sbxloop.tui.widgets.chronology import ChronologyLog
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.statusbar import StatusBar
from sbxloop.tui.widgets.tables import ConsoleTable
from tests.unit.tui.conftest import FakeCtl, drive, live_status, make_app


def bar_text(app: object) -> str:
    from sbxloop.tui.app import SbxloopTui

    assert isinstance(app, SbxloopTui)
    bar = app.screen.query_one("#statusbar", StatusBar)
    return bar.last.plain


def test_overview_shows_the_live_run_queue_and_waits(seeded: SbxloopHome) -> None:
    async def scenario() -> None:
        app = make_app(seeded)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(1.0)
            bar = bar_text(app)
            assert "running" in bar and "r_live" in bar and "runs 4/12" in bar
            assert "bridge ✓" in bar and "9.9.9" in bar
            current = app.screen.query_one("#current", TextPanel).content_text
            assert "r_live" in current and "Add retries" in current
            queue = app.screen.query_one("#queue", ConsoleTable)
            assert queue.row_count == 1
            recent = app.screen.query_one("#recent", ConsoleTable)
            assert recent.row_count == 3
            waits = app.screen.query_one("#notices", TextPanel).content_text
            assert "gh:issue:40" in waits and "ready to merge" in waits

    drive(scenario)


def test_daemon_down_and_starting_read_in_the_bar(seeded: SbxloopHome) -> None:
    async def scenario() -> None:
        app = make_app(seeded, ctl=FakeCtl(down=True))
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(1.0)
            assert "daemon down" in bar_text(app)
            assert app.screen.query_one("#recent", ConsoleTable).row_count == 3, "history stays"
        app = make_app(seeded, ctl=FakeCtl(stale=True))
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(1.0)
            assert "starting" in bar_text(app)
        app = make_app(
            seeded,
            ctl=FakeCtl(live_status(paused=True, holds=["operator", "deploy-1"], current=None)),
        )
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(1.0)
            assert "paused (operator, deploy-1)" in bar_text(app)

    drive(scenario)


def test_runs_screen_lists_filters_and_opens_a_run(seeded: SbxloopHome) -> None:
    async def scenario() -> None:
        app = make_app(seeded)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("2")
            await pilot.pause(0.5)
            table = app.screen.query_one("#runs", ConsoleTable)
            assert table.row_count == 3
            row = table.get_row_at(table.get_row_index("r_failed"))
            assert "orphaned" in str(row[1])
            await pilot.press("slash")
            await pilot.press(*"merged")
            await pilot.pause(0.3)
            assert table.row_count == 1
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert table.row_count == 3
            table.move_cursor(row=table.get_row_index("r_live"))
            await pilot.press("enter")
            await pilot.pause(1.0)
            assert isinstance(app.screen, RunDetailScreen)
            assert app.screen.run_id == "r_live"

    drive(scenario)


def test_run_screen_tabs_render_the_store(seeded: SbxloopHome) -> None:
    async def scenario() -> None:
        app = make_app(seeded, run="r_live")
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(1.5)
            screen = app.screen
            assert isinstance(screen, RunDetailScreen)
            header = screen.query_one("#header", TextPanel).content_text
            assert "r_live" in header and "gh:issue:41" in header and "building" in header
            assert "tasks 1/2" in header
            thread = screen.query_one("#thread-log", ChronologyLog)
            assert thread.count >= 2, "agent message and tool lines were rendered"
            tasks = screen.query_one("#tasks-table", ConsoleTable)
            assert tasks.row_count == 2
            phases = screen.query_one("#phases-table", ConsoleTable)
            assert phases.row_count == 1
            usage = screen.query_one("#usage", TextPanel).content_text
            assert "7 turn(s)" in usage and "not reported" in usage
            events = screen.query_one("#events-log", ChronologyLog)
            assert events.count == 5
            await pilot.press("slash")
            await pilot.press(*"policy.")
            await pilot.press("enter")
            await pilot.pause(1.0)
            assert events.count == 1
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, RunDetailScreen)

    drive(scenario)


def test_queue_screen_and_help(seeded: SbxloopHome) -> None:
    async def scenario() -> None:
        app = make_app(seeded, emoji=False)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("3")
            await pilot.pause(0.5)
            assert app.screen.query_one("#queued", ConsoleTable).row_count == 1
            assert app.screen.query_one("#items", ConsoleTable).row_count == 3
            assert "●" not in bar_text(app)
            await pilot.press("question_mark")
            await pilot.pause(0.3)
            assert app.screen.__class__.__name__ == "HelpScreen"

    drive(scenario)


def test_a_busy_daemon_reads_as_alive_not_down(seeded: SbxloopHome) -> None:
    """A `pending` reply is a daemon that took the request but was too busy
    to answer in time — the misread the ctl queue warns about."""
    from sbxloop.daemon.control import CommandReply
    from sbxloop.tui.data import probe_daemon

    class Busy:
        def submit(self, cmd: str, *, timeout_s: float = 30.0) -> CommandReply | None:
            return CommandReply("pending", ok=False, pending=True)

    snap = probe_daemon(Busy(), now=1.0)
    assert snap.live and not snap.starting and snap.status is None
    assert snap.error and "busy" in snap.error

    async def scenario() -> None:
        app = make_app(seeded, ctl=Busy())  # type: ignore[arg-type]
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(1.0)
            current = app.screen.query_one("#current", TextPanel).content_text
            assert "busy" in current and "down" not in current

    drive(scenario)


def test_queue_lists_the_daemons_dispatch_order_and_eligibility(seeded: SbxloopHome) -> None:
    """A resume-pending run goes first, a failed attempt waits its backoff:
    the daemon's own rule (`dispatch_eligible_at`), not a re-derivation."""
    import time

    from sbxloop.daemon.model import WorkItem
    from sbxloop.daemon.store import DaemonStore, dispatch_eligible_at

    dstore = DaemonStore(seeded.state_db)
    now = time.time()
    resume = WorkItem(item_id="gh:issue:50", source_key="50", title="Resume me", repo="o/r")
    dstore.upsert_new(resume, now=now - 100)
    dstore.mark_running("gh:issue:50", "r_resume", now=now - 90)
    dstore.mark_resume_pending("gh:issue:50", now=now - 80)
    failed = WorkItem(item_id="gh:issue:51", source_key="51", title="Backoff", repo="o/r")
    dstore.upsert_new(failed, now=now - 200)
    dstore.mark_running("gh:issue:51", "r_b", now=now - 150)
    dstore.mark_failed("gh:issue:51", "boom", now=now - 10, requeue=True)
    ordered = [i.item_id for i in dstore.queued_in_order()]
    assert ordered[0] == "gh:issue:50", "the interrupted run resumes first"
    backoff = dstore.get("gh:issue:51")
    assert backoff is not None
    assert dispatch_eligible_at(backoff, 900.0) > now, "a failed attempt waits its backoff"
    assert dispatch_eligible_at(dstore.get("gh:issue:50"), 900.0) == 0.0  # type: ignore[arg-type]
    dstore.close()

    async def scenario() -> None:
        app = make_app(seeded)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.8)
            await pilot.press("3")
            await pilot.pause(0.8)
            table = app.screen.query_one("#queued", ConsoleTable)
            first = table.get_row_at(0)
            assert str(first[0]) == "gh:issue:50" and "resume" in str(first[4])
            row = table.get_row_at(table.get_row_index("gh:issue:51"))
            assert str(row[4]) != "now"

    drive(scenario)


def test_tables_repaint_in_place_and_keep_the_cursor(seeded: SbxloopHome) -> None:
    async def scenario() -> None:
        app = make_app(seeded)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.5)
            await pilot.press("2")
            await pilot.pause(0.5)
            table = app.screen.query_one("#runs", ConsoleTable)
            table.move_cursor(row=2)
            before = table.selected_key()
            app.action_refresh()
            await pilot.pause(1.0)
            assert table.selected_key() == before, "a tick does not move the cursor"

    drive(scenario)


def test_escape_clears_the_event_filter_before_leaving_the_run(seeded: SbxloopHome) -> None:
    async def scenario() -> None:
        app = make_app(seeded, run="r_live")
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(1.5)
            assert isinstance(app.screen, RunDetailScreen)
            await pilot.press("slash")
            await pilot.press(*"pol")
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert isinstance(app.screen, RunDetailScreen), "Esc cleared the filter, not the run"
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, RunDetailScreen)

    drive(scenario)


def test_events_wait_while_follow_is_off(seeded: SbxloopHome) -> None:
    from sbxloop.engine.store import StateStore
    from sbxloop_worker.protocol import Event

    async def scenario() -> None:
        app = make_app(seeded, run="r_live")
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(1.5)
            screen = app.screen
            assert isinstance(screen, RunDetailScreen)
            log = screen.query_one("#events-log", ChronologyLog)
            before = log.count
            await pilot.press("f")  # follow off
            store = StateStore(seeded.state_db)
            store.append_event(Event.now("policy.deny", "r_live", domain="evil.example"))
            store.close()
            screen.load()
            await pilot.pause(1.0)
            assert log.count == before, "held while follow is off"
            await pilot.press("f")  # follow on: catch up
            await pilot.pause(0.3)
            assert log.count == before + 1

    drive(scenario)
