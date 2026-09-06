"""What the Daemon screen tails: the unit's journal, else the home's log
file, else the log of a daemon the console spawned."""

from __future__ import annotations

from pathlib import Path

from sbxloop.tui.system import JOURNAL_LINES, UnitState, journal_argv, journal_source


def unit(loaded: bool) -> UnitState:
    return UnitState("sbxloop-daemon", True, loaded, "active", "running", 1, 0, "", "")


def test_unit_journal_wins(tmp_path: Path) -> None:
    daemon_log = tmp_path / "daemon.log"
    daemon_log.write_text("x\n")
    source = journal_source(
        unit(True),
        "sbxloop-daemon",
        daemon_log=daemon_log,
        console_log=tmp_path / "c.log",
        spawned=False,
    )
    assert source == journal_argv("sbxloop-daemon")


def test_the_homes_log_file_when_there_is_no_unit(tmp_path: Path) -> None:
    daemon_log = tmp_path / "logs" / "daemon.log"
    daemon_log.parent.mkdir()
    daemon_log.write_text("x\n")
    for state in (None, unit(False)):
        source = journal_source(
            state,
            "sbxloop-daemon",
            daemon_log=daemon_log,
            console_log=tmp_path / "c.log",
            spawned=False,
        )
        assert source == ("tail", "-n", str(JOURNAL_LINES), "-F", str(daemon_log))


def test_the_consoles_own_spawn_log_last(tmp_path: Path) -> None:
    console_log = tmp_path / "console" / "daemon.log"
    console_log.parent.mkdir()
    console_log.write_text("x\n")
    missing = tmp_path / "logs" / "daemon.log"
    assert journal_source(None, "u", daemon_log=missing, console_log=console_log, spawned=True) == (
        "tail",
        "-n",
        str(JOURNAL_LINES),
        "-F",
        str(console_log),
    )
    assert (
        journal_source(None, "u", daemon_log=missing, console_log=console_log, spawned=False)
        is None
    )
