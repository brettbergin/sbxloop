"""The admin screens and verbs driven headless: Sandboxes, Daemon, the
run and item verbs, the confirmation tiers, read-only, the palette."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from textual.widgets import Input, RichLog

from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop.ids import new_run_id
from sbxloop.sbx.models import SandboxInfo
from sbxloop.tui.screens.daemon import DaemonScreen
from sbxloop.tui.screens.modals import ConfirmScreen, TextPromptScreen, TypedConfirmScreen
from sbxloop.tui.screens.run_detail import RunDetailScreen
from sbxloop.tui.screens.sandboxes import SandboxesScreen
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable
from sbxloop_worker.protocol import Event
from tests.unit.tui.conftest import (
    FakeCtl,
    FakeRunner,
    RecordingSbx,
    backdate,
    drive,
    live_status,
    make_app,
)

REFRESH: dict[str, Any] = {"refresh_s": 3.0}


def test_sandboxes_screen_classifies_and_prunes_behind_a_typed_word(seeded: Path) -> None:
    # A run id the sandbox naming scheme recognises (the seeded ids are
    # readable, not real), failed two days ago: its sandbox is an orphan.
    old = new_run_id()
    store = StateStore(seeded / "state.db")
    try:
        store.create_run(old, "an old one")
        store.set_run_state(old, "failed")
    finally:
        store.close()
    backdate(seeded, old, 2.0)
    sbx = RecordingSbx(
        [
            SandboxInfo(name="sbxloop-r_live-agent", status="running"),
            SandboxInfo(name=f"sbxloop-{old}-agent", status="stopped"),
            SandboxInfo(name="sbxloop-daemon-github-abcd1234", status="running"),
            SandboxInfo(name="somebody-elses", status="running"),
        ]
    )

    async def scenario() -> None:
        app = make_app(seeded, sbx=sbx, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("5")
            await pilot.pause(1.0)
            assert isinstance(app.screen, SandboxesScreen)
            table = app.screen.query_one("#sandboxes", ConsoleTable)
            assert table.row_count == 3, "non-sbxloop sandboxes are never listed"
            daemon_row = table.get_row_at(table.get_row_index("sbxloop-daemon-github-abcd1234"))
            assert daemon_row[1] == "daemon"
            failed_row = table.get_row_at(table.get_row_index(f"sbxloop-{old}-agent"))
            assert "orphan" in str(failed_row[6])
            summary = app.screen.query_one("#summary", TextPanel).content_text
            assert "1 orphan(s)" in summary
            # P asks for the word; a wrong word is refused, the right one prunes.
            await pilot.press("P")
            await pilot.pause(0.3)
            assert isinstance(app.screen, TypedConfirmScreen)
            box = app.screen.query_one("#typed", Input)
            box.value = "nope"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, TypedConfirmScreen)
            box.value = "prune"
            await pilot.press("enter")
            await pilot.pause(1.5)
            assert ("rm", "--force", f"sbxloop-{old}-agent") in sbx.calls
            assert not any(c[-1] == "sbxloop-r_live-agent" for c in sbx.calls if c[0] == "rm")
            # x removes the selected sandbox under its typed name.
            table = app.screen.query_one("#sandboxes", ConsoleTable)
            table.move_cursor(row=table.get_row_index("sbxloop-r_live-agent"))
            await pilot.press("x")
            await pilot.pause(0.3)
            assert isinstance(app.screen, TypedConfirmScreen)
            app.screen.query_one("#typed", Input).value = "sbxloop-r_live-agent"
            await pilot.press("enter")
            await pilot.pause(1.5)
            assert ("rm", "--force", "sbxloop-r_live-agent") in sbx.calls

    drive(scenario)


def test_daemon_screen_shows_the_unit_streams_the_journal_and_drives_the_verbs(
    seeded: Path,
) -> None:
    runner = FakeRunner()
    runner.script(
        "systemctl",
        "--user",
        "show",
        stdout="LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=4242\nNRestarts=0\n",
    )
    runner.script("systemctl", "--user", "restart")
    runner.stream_lines = [
        "2026-09-05T10:00:00+0000 db sbxloop[4242]: [info     ] daemon.tick queued=1",
        "2026-09-05T10:00:01+0000 db sbxloop[4242]: [warning  ] github.poll_failed "
        "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    ]
    ctl = FakeCtl(live_status(holds=["deploy-1"], paused=True))

    async def scenario() -> None:
        app = make_app(seeded, ctl=ctl, runner=runner, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("6")
            await pilot.pause(1.5)
            assert isinstance(app.screen, DaemonScreen)
            process = app.screen.query_one("#process", TextPanel).content_text
            assert "active (running)" in process and "pid 4242" in process
            assert "deploy-1" in process and "4/12 runs today" in process
            log = app.screen.query_one("#journal", RichLog)
            assert len(log.lines) == 2
            journal_text = "\n".join(str(s) for s in log.lines)
            assert "ghp_" not in journal_text, "credentials are redacted before rendering"
            # l raises the level floor: only the warning stays.
            await pilot.press("l")
            await pilot.pause(0.3)
            assert len(log.lines) == 1
            assert app.screen.min_level == "warning"
            # p pauses after a y/n; the daemon hears the ctl verb.
            await pilot.press("p")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause(1.0)
            assert "pause" in ctl.commands
            # B restarts the unit only under its typed name.
            await pilot.press("B")
            await pilot.pause(0.3)
            assert isinstance(app.screen, TypedConfirmScreen)
            app.screen.query_one("#typed", Input).value = "sbxloop-daemon"
            await pilot.press("enter")
            await pilot.pause(1.0)
            assert ("systemctl", "--user", "restart", "sbxloop-daemon") in runner.calls
            # D is refused while a daemon answers.
            await pilot.press("D")
            await pilot.pause(0.3)
            assert isinstance(app.screen, DaemonScreen)
            assert runner.spawned == []

    drive(scenario)


def test_daemon_screen_spawns_a_daemon_when_there_is_no_unit_and_asks_on_quit(
    seeded: Path,
) -> None:
    runner = FakeRunner()
    runner.script(
        "systemctl", "--user", "show", stdout="LoadState=not-found\nActiveState=inactive\n"
    )
    runner.stream_lines = ["console child line"]

    async def scenario() -> None:
        app = make_app(seeded, ctl=FakeCtl(down=True), runner=runner, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("6")
            await pilot.pause(1.5)
            process = app.screen.query_one("#process", TextPanel).content_text
            assert "no unit sbxloop-daemon" in process and "D spawns one here" in process
            await pilot.press("S")
            await pilot.pause(0.3)
            assert runner.calls[-1][:3] == ("systemctl", "--user", "show"), "S is refused"
            await pilot.press("D")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause(1.5)
            assert runner.spawned and runner.spawned[0][0][-1] == "daemon"
            assert "daemon" in app.deps.children.alive()
            # The journal pane now tails the child's log.
            await pilot.pause(1.0)
            assert isinstance(app.screen, DaemonScreen)
            assert app.screen._stream_argv is not None and app.screen._stream_argv[0] == "tail"
            await pilot.press("q")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause(0.5)
        assert runner.children[0].terminated

    drive(scenario)


def test_run_verbs_go_through_confirmations_and_ctl(seeded: Path) -> None:
    ctl = FakeCtl(live_status())

    async def scenario() -> None:
        app = make_app(seeded, ctl=ctl, run="r_live", **REFRESH)
        # Headless Textual cannot hand the terminal over; the fake runner
        # records what would have run.
        app.suspend = contextlib.nullcontext  # type: ignore[assignment, method-assign]
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause(1.5)
            assert isinstance(app.screen, RunDetailScreen)
            header = app.screen.query_one("#header", TextPanel).content_text
            assert "c cancel" in header and "C cancel+retry" in header and "s shell" in header
            await pilot.press("c")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause(0.3)
            assert ctl.commands == ["status"] * ctl.commands.count("status")
            await pilot.press("C")
            await pilot.pause(0.3)
            await pilot.press("y")
            await pilot.pause(1.0)
            assert "cancel --retry" in ctl.commands
            await pilot.press("plus")
            await pilot.pause(0.3)
            assert isinstance(app.screen, TextPromptScreen)
            app.screen.query_one("#text", Input).value = "2"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause(1.0)
            assert "grant-rounds r_live 2" in ctl.commands
            # s hands the terminal to a sandbox shell and comes back.
            await pilot.press("s")
            await pilot.pause(0.5)
            runner = app.deps.runner
            assert isinstance(runner, FakeRunner)
            assert runner.interactive_calls and runner.interactive_calls[0][:3] == (
                "sbx",
                "exec",
                "sbxloop-r_live-agent",
            )

    drive(scenario)


def test_read_only_refuses_every_verb(seeded: Path) -> None:
    ctl = FakeCtl(live_status())

    async def scenario() -> None:
        app = make_app(seeded, ctl=ctl, run="r_live", read_only=True, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause(1.5)
            header = app.screen.query_one("#header", TextPanel).content_text
            assert "c cancel" not in header
            await pilot.press("c")
            await pilot.pause(0.5)
            assert isinstance(app.screen, RunDetailScreen), "no dialog opens"
            assert "cancel" not in ctl.commands

    drive(scenario)


def test_queue_verbs_use_ctl_when_live_and_the_row_when_down(seeded: Path) -> None:
    ctl = FakeCtl(live_status())

    async def scenario() -> None:
        app = make_app(seeded, ctl=ctl, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("3")
            await pilot.pause(1.0)
            table = app.screen.query_one("#items", ConsoleTable)
            table.move_cursor(row=table.get_row_index("gh:issue:44"))
            await pilot.press("t")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await pilot.pause(1.0)
            assert "retry gh:issue:44" in ctl.commands
            await pilot.press("n")
            await pilot.pause(0.3)
            assert isinstance(app.screen, TextPromptScreen)
            app.screen.query_one("#text", Input).value = "the export spinner never stops"
            await pilot.press("enter")
            await pilot.pause(1.0)
            rows = app.mailbox.messages("control")
            assert rows and "@sbx please file this as an issue" in rows[-1].text
        app = make_app(seeded, ctl=FakeCtl(down=True), **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("3")
            await pilot.pause(1.5)
            table = app.screen.query_one("#items", ConsoleTable)
            table.move_cursor(row=table.get_row_index("gh:issue:41"))
            await pilot.press("u")
            await pilot.pause(0.3)
            await pilot.press("y")
            await pilot.pause(1.5)
        store = DaemonStore(seeded / "state.db")
        try:
            item = store.get("gh:issue:41")
            assert item is not None and item.state == "queued" and item.run_id is None
        finally:
            store.close()

    drive(scenario)


def test_phases_tab_folds_usage_per_persona(seeded: Path) -> None:
    store = StateStore(seeded / "state.db")
    try:
        for who in ("builder", "builder", "critic"):
            store.append_event(
                Event.now(
                    "agent.usage",
                    "r_live",
                    agent=who,
                    model="claude-sonnet-5",
                    backend="claude",
                    input_tokens=1000,
                    output_tokens=50,
                )
            )
    finally:
        store.close()

    async def scenario() -> None:
        app = make_app(seeded, run="r_live", **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause(1.5)
            usage = app.screen.query_one("#usage", TextPanel).content_text
            assert "builder" in usage and "2 turns" in usage and "critic" in usage
            assert "claude" in usage and "spend: not reported" in usage

    drive(scenario)
