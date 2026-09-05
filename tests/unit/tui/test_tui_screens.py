"""The console's screens, driven headless against a seeded store."""

from __future__ import annotations

from pathlib import Path

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


def test_overview_shows_the_live_run_queue_and_waits(seeded: Path) -> None:
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


def test_daemon_down_and_starting_read_in_the_bar(seeded: Path) -> None:
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


def test_runs_screen_lists_filters_and_opens_a_run(seeded: Path) -> None:
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


def test_run_screen_tabs_render_the_store(seeded: Path) -> None:
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


def test_queue_screen_and_help(seeded: Path) -> None:
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
