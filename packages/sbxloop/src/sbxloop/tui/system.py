"""What the console knows about the host: the daemon's systemd user unit
and its journal. ``systemctl --user`` and ``journalctl --user`` are the
only commands; a host without systemd reads as "no unit", never as an
error, and the console falls back to spawning the daemon itself."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import get_args

from sbxloop.log import LogLevel
from sbxloop.tui.runner import CommandRunner, RunOutcome

#: `systemctl show` properties the Daemon screen reads.
_SHOW_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "NRestarts",
    "ExecMainStartTimestamp",
    "Result",
)
#: How many journal lines the tail opens with.
JOURNAL_LINES = 200
#: The daemon's own levels (``[daemon] log_level``), as its console
#: renderer prints them in brackets. Under ``log_format = "json"`` no line
#: carries a bracketed level and the floor lets every line through.
LEVELS: tuple[str, ...] = tuple(level.lower() for level in get_args(LogLevel))
_LEVEL_RE = re.compile(r"\[(" + "|".join(LEVELS) + r")\s*\]", re.IGNORECASE)


@dataclass(frozen=True)
class UnitState:
    """One ``systemctl --user show`` reading."""

    unit: str
    available: bool  # systemd answered at all
    loaded: bool  # the unit file exists
    active: str  # active | inactive | failed | activating | deactivating | ''
    sub: str
    pid: int | None
    restarts: int
    started: str
    result: str
    error: str = ""

    @property
    def summary(self) -> str:
        if not self.available:
            return f"no systemd here ({self.error or 'systemctl not found'})"
        if not self.loaded:
            return f"no unit {self.unit} (contrib/systemd/ has one to install)"
        text = f"{self.active}"
        if self.sub and self.sub != self.active:
            text += f" ({self.sub})"
        if self.pid:
            text += f" · pid {self.pid}"
        if self.restarts:
            text += f" · restarted {self.restarts} time(s)"
        if self.result and self.result != "success":
            text += f" · last result {self.result}"
        return text

    @property
    def running(self) -> bool:
        return self.available and self.loaded and self.active == "active"


def unit_argv(verb: str, unit: str) -> tuple[str, ...]:
    """``systemctl --user <verb> <unit>``; ``show`` asks for the properties
    the console reads."""
    if verb == "show":
        return ("systemctl", "--user", "show", unit, "-p", ",".join(_SHOW_PROPERTIES))
    return ("systemctl", "--user", verb, unit)


def journal_argv(unit: str) -> tuple[str, ...]:
    return ("journalctl", "--user", "-u", unit, "-n", str(JOURNAL_LINES), "-f", "-o", "short-iso")


def parse_show(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def unit_state(unit: str, outcome: RunOutcome) -> UnitState:
    if outcome.returncode == 127:
        return UnitState(unit, False, False, "", "", None, 0, "", "", error=outcome.text)
    if not outcome.ok and not outcome.stdout.strip():
        # `systemctl --user` with no user bus (a bare ssh session without
        # DBUS_SESSION_BUS_ADDRESS, docs/deploy.md) fails this way.
        return UnitState(unit, False, False, "", "", None, 0, "", "", error=outcome.text[:200])
    props = parse_show(outcome.stdout)
    loaded = props.get("LoadState", "") == "loaded"
    pid_text = props.get("MainPID", "0")
    pid = int(pid_text) if pid_text.isdigit() and pid_text != "0" else None
    restarts_text = props.get("NRestarts", "0")
    return UnitState(
        unit,
        True,
        loaded,
        props.get("ActiveState", ""),
        props.get("SubState", ""),
        pid,
        int(restarts_text) if restarts_text.isdigit() else 0,
        props.get("ExecMainStartTimestamp", ""),
        props.get("Result", ""),
    )


def probe_unit(runner: CommandRunner, unit: str) -> UnitState:
    return unit_state(unit, runner.run(unit_argv("show", unit), timeout_s=10.0))


def level_of(line: str) -> str | None:
    """The level a journal line carries in the daemon's console rendering,
    or None for a line without one (a traceback line, systemd's own)."""
    m = _LEVEL_RE.search(line)
    return m.group(1).lower() if m else None


def passes(line: str, *, min_level: str, grep: str) -> bool:
    """The journal pane's filter: a level floor (lines without a level
    always pass, so a traceback stays with its error) and a substring."""
    if grep and grep.lower() not in line.lower():
        return False
    level = level_of(line)
    if level is None or min_level == "debug":
        return True
    return LEVELS.index(level) >= LEVELS.index(min_level)


__all__ = [
    "JOURNAL_LINES",
    "LEVELS",
    "UnitState",
    "journal_argv",
    "level_of",
    "parse_show",
    "passes",
    "probe_unit",
    "unit_argv",
    "unit_state",
]
