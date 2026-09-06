"""Daemon: the systemd user unit and the process behind ``status``,
versions and the upgrade command, per-repository polling health, who
waits on a human, and the journal streamed live — with the unit and
daemon verbs on keys."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, ClassVar

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid, Vertical
from textual.css.query import NoMatches
from textual.widgets import Input, RichLog
from textual.worker import get_current_worker

from sbxloop.daemon.versions import VersionProbe
from sbxloop.errors import SbxloopError
from sbxloop.log import redact_text
from sbxloop.tui import actions
from sbxloop.tui.data import ConsoleState
from sbxloop.tui.format import age, clock
from sbxloop.tui.runner import StreamHandle
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.screens.modals import TextPromptScreen
from sbxloop.tui.system import LEVELS, UnitState, journal_source, passes, probe_unit
from sbxloop.tui.widgets.panel import TextPanel

#: `systemctl show` is cheap but forks; the bar and this screen share it.
UNIT_POLL_S = 15.0
#: PyPI is asked once an hour (the probe memoises for `PYPI_TTL_S` anyway).
VERSIONS_POLL_S = 3600.0
#: Journal lines kept for re-filtering.
JOURNAL_BUFFER = 2000


class DaemonScreen(ConsoleScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("p", "pause", "Pause"),
        Binding("u", "resume", "Resume"),
        Binding("a", "resume_all", "Release every hold", show=False),
        Binding("c", "cancel", "Cancel run"),
        Binding("C", "cancel_retry", "Cancel + retry", show=False),
        Binding("g", "stop_daemon", "Graceful stop", show=False),
        Binding("S", "unit_start", "Start unit"),
        Binding("T", "unit_stop", "Stop unit", show=False),
        Binding("B", "unit_restart", "Restart unit"),
        Binding("D", "spawn", "Spawn daemon", show=False),
        Binding("e", "stop_child", "Stop spawned daemon", show=False),
        Binding("U", "upgrade", "Upgrade", show=False),
        Binding("R", "resume_repo", "Resume repo", show=False),
        Binding("slash", "grep", "Grep journal"),
        Binding("l", "level", "Level", show=False),
        Binding("f", "follow", "Follow", show=False),
        Binding("escape", "clear_grep", "Clear grep", show=False),
    ]
    DEFAULT_CSS = """
    DaemonScreen #panels { grid-size: 2 2; grid-gutter: 0 1; height: 16; }
    DaemonScreen #panels TextPanel { border: round $primary; padding: 0 1; height: 1fr; }
    DaemonScreen #journal { height: 1fr; border: round $secondary; }
    DaemonScreen #grep { display: none; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.unit: UnitState | None = None
        self.versions_text = "checking versions…"
        self.versions_at: float | None = None
        self.min_level = "info"
        self.grep = ""
        self.follow = True
        self._lines: deque[str] = deque(maxlen=JOURNAL_BUFFER)
        self._stream: StreamHandle | None = None
        self._stream_argv: tuple[str, ...] | None = None
        self._stream_generation = 0
        self._active = True
        self._pollers: tuple[Any, ...] = ()

    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with Vertical(id="body"):
            with Grid(id="panels"):
                yield TextPanel(Text("probing the unit…", style="dim"), id="process")
                yield TextPanel(Text(self.versions_text, style="dim"), id="versions")
                yield TextPanel(Text("—", style="dim"), id="repos")
                yield TextPanel(Text("—", style="dim"), id="waits")
            yield Input(placeholder="journal grep", id="grep")
            yield RichLog(id="journal", max_lines=JOURNAL_BUFFER, wrap=False, markup=False)
        yield from self.compose_footer()

    def on_mount(self) -> None:
        super().on_mount()
        for panel, title in (
            ("process", "process"),
            ("versions", "versions"),
            ("repos", "repositories"),
            ("waits", "waiting on a human"),
        ):
            self.query_one(f"#{panel}", TextPanel).border_title = title
        self._journal_title()
        # The grep box is hidden until `/`; without this the screen's
        # auto-focus lands on it and every key types instead of acting.
        self.query_one("#grep", Input).display = False
        self.query_one("#journal", RichLog).focus()
        self._pollers = (
            self.set_interval(UNIT_POLL_S, self.probe_unit),
            self.set_interval(VERSIONS_POLL_S, self.probe_versions),
        )
        self.probe_unit()
        self.probe_versions()

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        self._active = True
        for timer in self._pollers:
            timer.resume()
        self.probe_unit()

    def on_screen_suspend(self) -> None:
        # A mode screen stays mounted: its timers would keep forking
        # systemctl and re-opening the journal behind a screen nobody looks
        # at. The stream is a process: closed here, reopened on resume.
        self._active = False
        for timer in self._pollers:
            timer.pause()
        self._close_stream()

    def on_unmount(self) -> None:
        self._close_stream()

    # -- data --------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="unit")
    def probe_unit(self) -> None:
        deps = self.console_app.deps
        unit = probe_unit(deps.runner, deps.unit)
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._apply_unit, unit)

    def _apply_unit(self, unit: UnitState) -> None:
        self.unit = unit
        self.repaint()
        if self._active:
            self._ensure_stream()

    @work(thread=True, exclusive=True, group="versions")
    def probe_versions(self) -> None:
        deps = self.console_app.deps
        try:
            sbx = deps.sbx()
        except SbxloopError:
            sbx = None
        probe = VersionProbe(
            sbx=sbx,
            upgrade_command=deps.config.daemon.upgrade_command,
            check_pypi=deps.config.daemon.version_check,
        )
        try:
            text = probe.summary()
        except Exception as exc:
            text = f"reading versions failed: {exc}"
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._apply_versions, text)

    def _apply_versions(self, text: str) -> None:
        self.versions_text = text
        self.versions_at = time.time()
        self.repaint()

    def refresh_data(self, state: ConsoleState) -> None:
        super().refresh_data(state)
        self.query_one("#process", TextPanel).update(self._process(state))
        versions = Text(self.versions_text)
        if self.versions_at:
            versions.append(f"\nchecked {age(self.versions_at)}", style="dim")
        self.query_one("#versions", TextPanel).update(versions)
        self.query_one("#repos", TextPanel).update(self._repos(state))
        self.query_one("#waits", TextPanel).update(self._waits(state))

    # -- panels ------------------------------------------------------------------

    def _process(self, state: ConsoleState) -> Any:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold", no_wrap=True)
        table.add_column()
        deps = self.console_app.deps
        unit = self.unit
        table.add_row("unit", unit.summary if unit else "probing…")
        daemon = state.daemon
        status = (daemon.status if daemon else None) or {}
        if daemon is None:
            table.add_row("daemon", "probing…")
        elif daemon.starting:
            table.add_row("daemon", Text("starting (ctl refused as stale)", style="yellow"))
        elif not daemon.live:
            hint = ""
            if unit is not None and not unit.loaded:
                hint = " · D spawns one here" if not deps.read_only else ""
            elif unit is not None and unit.loaded and not unit.running:
                hint = " · S starts the unit" if not deps.read_only else ""
            table.add_row("daemon", Text(f"down{hint}", style="bold red"))
        else:
            pid = status.get("pid")
            started = status.get("started_at")
            up = f" · up {age(float(started))}" if started else ""
            table.add_row(
                "daemon",
                f"pid {pid}{up} · version {status.get('version', '?')} · "
                f"ctl {daemon.latency_ms} ms",
            )
            current = status.get("current") or {}
            if current:
                table.add_row("current", f"{current.get('run_id')} — {current.get('title', '')}")
            elif status.get("claiming"):
                table.add_row("current", f"claiming {status['claiming']}")
            else:
                table.add_row("current", "idle")
            holds = status.get("holds") or []
            table.add_row(
                "holds",
                Text(", ".join(holds), style="yellow") if holds else "none",
            )
            breaker = "open" if status.get("breaker_open") else "closed"
            table.add_row(
                "breaker",
                Text(
                    f"{breaker} · {status.get('consecutive_failures', 0)} consecutive failure(s)",
                    style="red" if status.get("breaker_open") else "",
                ),
            )
            table.add_row(
                "cap",
                f"{status.get('runs_today', 0)}/{status.get('max_runs_per_day', '?')} runs "
                f"today ({status.get('run_cap_timezone', 'UTC')}), "
                f"{status.get('resumes_today', 0)} resume(s)",
            )
            if status.get("stopping"):
                table.add_row(
                    "stopping", Text("yes — exits after the run in flight", style="yellow")
                )
        children = deps.children.alive()
        if children:
            table.add_row(
                "spawned here",
                ", ".join(f"{name} (pid {h.pid})" for name, h in children.items()),
            )
        return table

    @staticmethod
    def _repos(state: ConsoleState) -> Any:
        status = (state.daemon.status if state.daemon else None) or {}
        repos = [r for r in (status.get("repos") or []) if isinstance(r, dict)]
        if not repos:
            return Text("single repository, or no daemon answering", style="dim")
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold", no_wrap=True)
        table.add_column()
        table.add_column()
        for r in repos:
            state_text = str(r.get("state", "?"))
            style = "" if state_text == "ok" else ("red" if state_text == "suspended" else "yellow")
            detail = str(r.get("reason") or "")
            failures = r.get("failures")
            if failures:
                detail = f"{detail} · {failures} failure(s)".strip(" ·")
            table.add_row(str(r.get("repo", "?")), Text(state_text, style=style), detail)
        return table

    def _waits(self, state: ConsoleState) -> Any:
        items = state.items
        if items is None or not (items.gates or items.holds):
            return Text("nobody", style="dim")
        emoji = self.console_app.emoji
        text = Text()
        for g in items.gates:
            text.append(f"{'⏸ ' if emoji else ''}{g.item_id} ready to merge · PR #{g.pr_number} · ")
            text.append(f"{age(g.created_at)}\n", style="dim")
        for h in items.holds:
            text.append(f"{'👀 ' if emoji else ''}{h.item_id} {h.state} · next poll ")
            text.append(f"{age(h.next_poll_at)}\n", style="dim")
        return text

    # -- the journal -------------------------------------------------------------

    def _journal_source(self) -> tuple[str, ...] | None:
        deps = self.console_app.deps
        return journal_source(
            self.unit,
            deps.unit,
            daemon_log=deps.home.daemon_log,
            console_log=deps.console_dir / "daemon.log",
            spawned="daemon" in deps.children.by_name,
        )

    def _journal_title(self) -> None:
        try:
            log = self.query_one("#journal", RichLog)
        except NoMatches:
            return
        source = "no journal: no unit here and no daemon spawned from this console"
        if self._stream_argv:
            source = " ".join(self._stream_argv[:4])
        log.border_title = (
            f"{source} · level ≥ {self.min_level} · grep {self.grep!r} · "
            f"follow {'on' if self.follow else 'off'}"
        )

    def _ensure_stream(self) -> None:
        argv = self._journal_source()
        if argv == self._stream_argv and self._stream is not None:
            return
        self._close_stream()
        self._stream_argv = argv
        self._journal_title()
        if argv is None:
            return
        self._lines.clear()
        self.query_one("#journal", RichLog).clear()
        self._stream_generation += 1
        self.tail_journal(argv, self._stream_generation)

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        self._stream_argv = None
        if stream is not None:
            # terminate + wait: off the UI thread.
            threading.Thread(target=stream.close, name="journal-close", daemon=True).start()

    @work(thread=True, group="journal")
    def tail_journal(self, argv: tuple[str, ...], generation: int) -> None:
        deps = self.console_app.deps
        try:
            stream = deps.runner.stream(argv)
        except OSError as exc:
            self.app.call_from_thread(self._journal_note, f"could not start {argv[0]}: {exc}")
            return
        if generation != self._stream_generation or not self._active:
            stream.close()  # suspended or superseded while the process started
            return
        self._stream = stream
        try:
            # One line per hop: the stream blocks between lines (`-f`), so
            # a batch would hold the newest line back until the next one.
            for raw in stream.lines():
                if get_current_worker().is_cancelled or generation != self._stream_generation:
                    break
                self.app.call_from_thread(self._journal_lines, [redact_text(raw)], generation)
        finally:
            stream.close()

    def _journal_note(self, text: str) -> None:
        self.query_one("#journal", RichLog).write(Text(text, style="red"))

    def _journal_lines(self, lines: list[str], generation: int) -> None:
        if generation != self._stream_generation:
            return
        log = self.query_one("#journal", RichLog)
        for line in lines:
            self._lines.append(line)
            if self.follow and passes(line, min_level=self.min_level, grep=self.grep):
                log.write(line)

    def _refilter(self) -> None:
        log = self.query_one("#journal", RichLog)
        log.clear()
        for line in self._lines:
            if passes(line, min_level=self.min_level, grep=self.grep):
                log.write(line)
        self._journal_title()

    # -- actions -----------------------------------------------------------------

    def action_pause(self) -> None:
        self.console_app.perform(actions.pause(self.console_app.deps))

    def action_resume(self) -> None:
        self.console_app.perform(actions.resume(self.console_app.deps))

    def action_resume_all(self) -> None:
        self.console_app.perform(actions.resume(self.console_app.deps, every=True))

    def action_cancel(self) -> None:
        self.console_app.perform(actions.cancel_current(self.console_app.deps))

    def action_cancel_retry(self) -> None:
        self.console_app.perform(actions.cancel_current(self.console_app.deps, retry=True))

    def action_stop_daemon(self) -> None:
        self.console_app.perform(actions.stop_daemon(self.console_app.deps))

    def _unit(self, verb: str) -> None:
        unit = self.unit
        if unit is not None and not unit.available:
            self.app.notify(unit.summary, severity="warning")
            return
        if unit is not None and not unit.loaded:
            self.app.notify(
                f"{unit.summary}; D spawns a daemon from here instead", severity="warning"
            )
            return
        self.console_app.perform(
            actions.unit_verb(self.console_app.deps, verb), then=self.probe_unit
        )

    def action_unit_start(self) -> None:
        self._unit("start")

    def action_unit_stop(self) -> None:
        self._unit("stop")

    def action_unit_restart(self) -> None:
        self._unit("restart")

    def action_spawn(self) -> None:
        deps = self.console_app.deps
        if deps.daemon_live() or deps.daemon_starting():
            self.app.notify("a daemon is already answering on this state dir", severity="warning")
            return
        if "daemon" in deps.children.alive():
            self.app.notify("the daemon spawned from here is still running", severity="warning")
            return
        self.console_app.perform(actions.spawn_daemon(deps), then=self._after_spawn)

    def _after_spawn(self) -> None:
        self.console_app.probe()
        self._ensure_stream()

    def action_stop_child(self) -> None:
        deps = self.console_app.deps
        if "daemon" not in deps.children.alive():
            self.app.notify("no daemon was spawned from this console", severity="warning")
            return
        self.console_app.perform(
            actions.Action(
                "stop the daemon spawned here",
                lambda: actions.stop_child(deps, "daemon"),
                prompt=(
                    "Stop the daemon this console spawned (SIGTERM; it finishes the run in flight)?"
                ),
            ),
            then=self.console_app.probe,
        )

    def action_upgrade(self) -> None:
        self.console_app.perform(actions.upgrade(self.console_app.deps), then=self.probe_versions)

    def action_resume_repo(self) -> None:
        def submitted(repo: str | None) -> None:
            if repo:
                self.console_app.perform(actions.resume_repo(self.console_app.deps, repo))

        self.app.push_screen(
            TextPromptScreen(
                "resume a repository",
                "owner/name of the suspended repository",
                placeholder="owner/name",
            ),
            submitted,
        )

    def action_grep(self) -> None:
        box = self.query_one("#grep", Input)
        box.display = True
        box.focus()

    def action_clear_grep(self) -> None:
        box = self.query_one("#grep", Input)
        if box.display or self.grep:
            box.value = ""
            box.display = False
            self.grep = ""
            self._refilter()
            self.query_one("#journal", RichLog).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "grep":
            return
        self.grep = event.value.strip()
        event.input.display = False
        self._refilter()
        self.query_one("#journal", RichLog).focus()

    def action_level(self) -> None:
        index = (LEVELS.index(self.min_level) + 1) % len(LEVELS)
        self.min_level = LEVELS[index]
        self._refilter()

    def action_follow(self) -> None:
        self.follow = not self.follow
        if self.follow:
            self._refilter()
        self._journal_title()
        self.app.notify(f"journal follow {'on' if self.follow else 'off'} · {clock(time.time())}")


__all__ = ["JOURNAL_BUFFER", "UNIT_POLL_S", "VERSIONS_POLL_S", "DaemonScreen"]
