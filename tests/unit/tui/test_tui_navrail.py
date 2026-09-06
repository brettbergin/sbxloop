"""The navigation rail: the console's map, on every screen.

The rail is docked, so it must appear without any screen composing it, it
must follow the mode however the mode changed, and it must say when a
screen you are not on wants attention. Below the width threshold it gets
out of the way and the keys still reach everything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.daemon.model import WorkItem
from sbxloop.paths import SbxloopHome
from sbxloop.tui.app import SbxloopTui
from sbxloop.tui.data import ConsoleState, ItemsSnapshot
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.screens.overview import OverviewScreen
from sbxloop.tui.screens.runs import RunsScreen
from sbxloop.tui.widgets.navrail import (
    NAV,
    NavButton,
    NavRail,
    badges,
)
from tests.unit.tui.conftest import drive, make_app

REFRESH = {"refresh_s": 3.0}


def test_nav_is_the_single_source_of_the_consoles_shape() -> None:
    """Every screen in the rail is a mode with a binding, and every mode is
    in the rail — a screen cannot be reachable by key and missing from the
    map, or listed and unreachable."""
    modes = set(SbxloopTui.MODES)
    assert {item.mode for item in NAV} == modes
    bound = {b.key for b in SbxloopTui.BINDINGS}
    for item in NAV:
        key = "question_mark" if item.key == "?" else item.key
        assert key in bound, f"{item.mode} is in the rail with no binding"
    # The nav keys are the rail's job now; the footer keeps its row for the
    # verbs of whichever screen is up.
    shown = {b.key for b in SbxloopTui.BINDINGS if b.show}
    assert shown == {"r", "q"}


def test_badges_name_what_wants_attention() -> None:
    item = WorkItem(item_id="gh:issue:1", source_key="1", title="t", url="u", repo="o/r")
    empty = ConsoleState()
    assert badges(empty, 0) == {}
    state = ConsoleState(
        items=ItemsSnapshot(queued=(item, item), eligible_at={}, items=(), gates=(), holds=())
    )
    assert badges(state, 0) == {"items": 2}
    assert badges(state, 3) == {"items": 2, "chat": 3}


def test_the_rail_is_on_every_screen_and_follows_the_mode(
    seeded: SbxloopHome, tmp_path: Path
) -> None:
    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(1.0)
            assert isinstance(app.screen, OverviewScreen)
            rail = app.screen.query_one("#navrail", NavRail)
            assert len(rail.query(NavButton)) == len(NAV)
            active = [b.item.mode for b in rail.query(NavButton) if b.active]
            assert active == ["overview"], "the screen you are on is marked"

            # A key switches the mode and the new screen's rail follows.
            await pilot.press("2")
            await pilot.pause(1.0)
            assert isinstance(app.screen, RunsScreen)
            rail = app.screen.query_one("#navrail", NavRail)
            assert [b.item.mode for b in rail.query(NavButton) if b.active] == ["runs"]

            # Clicking a rail row is the same verb as its key.
            button = next(b for b in rail.query(NavButton) if b.item.mode == "config")
            button.post_message(NavButton.Selected("config"))
            await pilot.pause(1.0)
            assert app.current_mode == "config"
            rail = app.screen.query_one("#navrail", NavRail)
            assert [b.item.mode for b in rail.query(NavButton) if b.active] == ["config"]

    drive(scenario)


def test_a_queued_item_badges_the_queue_row(seeded: SbxloopHome) -> None:
    """The rail is how a screen you are not on says it wants you."""

    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(1.5)
            rail = app.screen.query_one("#navrail", NavRail)
            queue = next(b for b in rail.query(NavButton) if b.item.mode == "items")
            assert queue.badge > 0, "the seeded home has a queued item"
            daemon = next(b for b in rail.query(NavButton) if b.item.mode == "daemon")
            assert daemon.badge > 0, "and an open merge gate"
            runs = next(b for b in rail.query(NavButton) if b.item.mode == "runs")
            assert runs.badge == 0, "a screen with nothing to say carries no badge"

    drive(scenario)


def test_a_narrow_terminal_hides_the_rail_but_keeps_the_keys(seeded: SbxloopHome) -> None:
    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(70, 24)) as pilot:
            await pilot.pause(1.0)
            screen = app.screen
            assert isinstance(screen, ConsoleScreen)
            assert screen.has_class("-narrow")
            assert not screen.query_one("#navrail", NavRail).display
            # Every screen is still one keystroke away.
            await pilot.press("3")
            await pilot.pause(1.0)
            assert app.current_mode == "items"

    drive(scenario)


def test_a_wide_terminal_shows_the_rail(seeded: SbxloopHome) -> None:
    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(1.0)
            screen = app.screen
            assert isinstance(screen, ConsoleScreen)
            assert not screen.has_class("-narrow")
            assert screen.query_one("#navrail", NavRail).display

    drive(scenario)


@pytest.mark.parametrize("key,mode", [(i.key, i.mode) for i in NAV if i.key != "?"])
def test_every_rail_row_reaches_its_screen(seeded: SbxloopHome, key: str, mode: str) -> None:
    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(1.0)
            await pilot.press(key)
            await pilot.pause(1.0)
            assert app.current_mode == mode
            rail = app.screen.query_one("#navrail", NavRail)
            assert [b.item.mode for b in rail.query(NavButton) if b.active] == [mode]

    drive(scenario)
