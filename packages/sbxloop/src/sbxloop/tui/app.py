"""The console application: modes for the shared screens, a pushed screen
per run, two pollers (the store every ``[tui] refresh_s``, the daemon's
``status`` every few seconds), the bar every screen carries, and the one
place an admin verb is confirmed, run and reported."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from textual import work
from textual.app import App, ScreenStackError
from textual.binding import Binding, BindingType
from textual.worker import get_current_worker

from sbxloop import __version__
from sbxloop.config import Config
from sbxloop.daemon.control import ControlClient
from sbxloop.daemon.mailbox import MailboxClient
from sbxloop.sbx.cli import SbxCLI
from sbxloop.tui.actions import Action, Deps, Outcome, stop_spawned_daemon
from sbxloop.tui.chat import ChatSession
from sbxloop.tui.commands import ConsoleCommands
from sbxloop.tui.data import ConsoleState, CtlClient, build_state, probe_daemon
from sbxloop.tui.runner import CommandRunner, SubprocessRunner
from sbxloop.tui.screens.chat import ChatScreen
from sbxloop.tui.screens.config import ConfigScreen
from sbxloop.tui.screens.daemon import DaemonScreen
from sbxloop.tui.screens.doctor import DoctorScreen
from sbxloop.tui.screens.help import HelpScreen
from sbxloop.tui.screens.items import ItemsScreen
from sbxloop.tui.screens.modals import (
    ConfirmScreen,
    OutcomeScreen,
    TypedConfirmScreen,
)
from sbxloop.tui.screens.overview import OverviewScreen
from sbxloop.tui.screens.run_detail import RunDetailScreen
from sbxloop.tui.screens.runs import RunsScreen
from sbxloop.tui.screens.sandboxes import SandboxesScreen

#: How often the daemon is asked ``status`` (a read-only verb, but a
#: ctl round trip; the store poll is the fast one).
PROBE_INTERVAL_S = 5.0


class SbxloopTui(App[None]):
    TITLE = "sbxloop"
    CSS = """
    Screen { background: $background; }
    .title { color: $text-muted; padding: 0 1; }
    """
    MODES: ClassVar[dict[str, str | Callable[[], Any]]] = {
        "overview": OverviewScreen,
        "runs": RunsScreen,
        "items": ItemsScreen,
        "chat": ChatScreen,
        "sandboxes": SandboxesScreen,
        "daemon": DaemonScreen,
        "config": ConfigScreen,
        "doctor": DoctorScreen,
        "help": HelpScreen,
    }
    COMMANDS: ClassVar[set[Any]] = App.COMMANDS | {ConsoleCommands}
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "mode('overview')", "Overview"),
        Binding("2", "mode('runs')", "Runs"),
        Binding("3", "mode('items')", "Queue"),
        Binding("4", "mode('chat')", "Chat"),
        Binding("5", "mode('sandboxes')", "Sandboxes"),
        Binding("6", "mode('daemon')", "Daemon"),
        Binding("7", "mode('config')", "Config"),
        Binding("8", "mode('doctor')", "Doctor"),
        Binding("question_mark", "mode('help')", "Help"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config: Config,
        state_dir: Path,
        *,
        mailbox: MailboxClient,
        ctl: CtlClient | None = None,
        read_only: bool = False,
        initial_run: str | None = None,
        clock: Callable[[], float] = time.time,
        runner: CommandRunner | None = None,
        unit: str | None = None,
        sbx_factory: Callable[[], SbxCLI] | None = None,
        cwd: Path | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.state_dir = state_dir
        self.mailbox = mailbox
        operator = mailbox.operator_name
        self.ctl: CtlClient = (
            ctl if ctl is not None else ControlClient(state_dir, by=f"{operator} via sbxloop tui")
        )
        self.read_only = read_only
        self.initial_run = initial_run
        self.clock = clock
        self.emoji = bool(config.tui.emoji)
        self.state = ConsoleState(version=__version__, read_only=read_only)
        self.chat = ChatSession(
            mailbox, read_only=read_only, prefix=config.tui.command_prefix, clock=clock
        )
        self.deps = Deps(
            ctl=self.ctl,
            runner=runner if runner is not None else SubprocessRunner(),
            mailbox=mailbox,
            config=config,
            state_dir=state_dir,
            unit=unit or config.tui.daemon_unit,
            operator=operator,
            sbx=sbx_factory or (lambda: SbxCLI(app_name=config.app_name or None)),
            daemon=lambda: self.state.daemon,
            read_only=read_only,
            clock=clock,
            cwd=cwd or Path.cwd(),
        )
        # The newest control-channel row the Chat screen has shown, for the
        # unread count in the bar.
        self._control_seen = 0

    # -- lifecycle ---------------------------------------------------------------

    def on_mount(self) -> None:
        self.switch_mode("overview")
        self.set_interval(float(self.config.tui.refresh_s), self.refresh_state)
        self.set_interval(PROBE_INTERVAL_S, self.probe)
        self.refresh_state()
        self.probe()
        if self.initial_run:
            self.open_run(self.initial_run)

    # -- pollers -----------------------------------------------------------------

    @work(thread=True, exclusive=True, group="refresh")
    def refresh_state(self) -> None:
        state = build_state(
            self.mailbox,
            self.state,
            now=self.clock(),
            retry_backoff_s=self.config.daemon.retry_backoff_s,
            control_seen=self._control_seen,
        )
        if get_current_worker().is_cancelled:
            return  # superseded, or the app is shutting down
        self.call_from_thread(self._apply_state, state)

    @work(thread=True, exclusive=True, group="probe")
    def probe(self) -> None:
        snapshot = probe_daemon(self.ctl, now=self.clock())
        if get_current_worker().is_cancelled:
            return
        self.call_from_thread(self._apply_daemon, snapshot)

    def _apply_state(self, state: ConsoleState) -> None:
        if state.refreshed_at < self.state.refreshed_at:
            return  # an older snapshot landing after a newer one
        # The snapshot was built off-thread from an older state: the daemon
        # probe applied here since then is the fresher fact, keep it.
        state.daemon = self.state.daemon
        self.state = state
        self._repaint()

    def _apply_daemon(self, snapshot: Any) -> None:
        self.state.daemon = snapshot
        self._repaint()

    def _repaint(self) -> None:
        try:
            screen = self.screen
        except ScreenStackError:  # a snapshot landing while the app shuts down
            return
        repaint = getattr(screen, "repaint", None)
        if repaint is not None:
            repaint()

    # -- navigation --------------------------------------------------------------

    def action_mode(self, name: str) -> None:
        self.switch_mode(name)

    def action_refresh(self) -> None:
        self.refresh_state()
        self.probe()

    def open_run(self, run_id: str) -> None:
        self.push_screen(RunDetailScreen(run_id))

    def chat_seen(self, row_id: int) -> None:
        self._control_seen = max(self._control_seen, row_id)

    def unread(self) -> int:
        """Control-channel rows newer than the last one the Chat screen showed."""
        return self.state.control_unread

    # -- admin verbs -------------------------------------------------------------

    def perform(
        self,
        action: Action,
        *,
        then: Callable[[], Any] | None = None,
        on_success: Callable[[], Any] | None = None,
    ) -> None:
        """Run one admin verb the way every verb runs: refused read-only,
        refused without a daemon when it needs one, confirmed by its tier,
        executed off the UI thread, reported as a toast or a screen, then
        ``then`` (a screen's own re-poll, whatever the outcome),
        ``on_success`` (only when it worked) and a refresh."""
        if self.read_only and action.mutating:
            self.notify(f"read-only console: {action.title} refused", severity="warning")
            return
        if action.needs_live and not self.deps.daemon_live():
            why = "starting" if self.deps.daemon_starting() else "not running"
            self.notify(
                f"the daemon is {why}: {action.title} needs a live daemon", severity="warning"
            )
            return

        def go(confirmed: bool | None) -> None:
            if not confirmed:
                return
            if action.interactive is not None:
                self._interactive(action, then, on_success)
                return
            self.execute(action, then, on_success)

        if action.confirm == "none":
            go(True)
        elif action.confirm == "typed":
            self.push_screen(TypedConfirmScreen(action.title, action.prompt, action.typed), go)
        else:
            self.push_screen(ConfirmScreen(action.title, action.prompt), go)

    @work(thread=True, group="action")
    def execute(
        self,
        action: Action,
        then: Callable[[], Any] | None,
        on_success: Callable[[], Any] | None = None,
    ) -> None:
        try:
            outcome = action.run()
        except Exception as exc:
            outcome = Outcome(False, f"{action.title} failed: {exc}")
        self.call_from_thread(self.show_outcome, action, outcome, then, on_success)

    def show_outcome(
        self,
        action: Action,
        outcome: Outcome,
        then: Callable[[], Any] | None = None,
        on_success: Callable[[], Any] | None = None,
    ) -> None:
        if outcome.long:
            self.push_screen(OutcomeScreen(action.title, outcome.text, ok=outcome.ok))
        else:
            self.notify(
                outcome.text or "done",
                title=action.title,
                severity="information" if outcome.ok else "error",
                timeout=8 if outcome.ok else 15,
            )
        if then is not None:
            then()
        if outcome.ok and on_success is not None:
            on_success()
        self.action_refresh()

    def _interactive(
        self,
        action: Action,
        then: Callable[[], Any] | None = None,
        on_success: Callable[[], Any] | None = None,
    ) -> None:
        """Hand the terminal to a process (a sandbox shell, an editor) and
        take it back — then the same follow-up every verb gets, since the
        process may have changed what the screen shows."""
        argv = action.interactive or ()
        with self.suspend():
            code = self.deps.runner.interactive(argv)
        self.notify(
            f"{action.title}: exit {code}", severity="information" if code == 0 else "warning"
        )
        if then is not None:
            then()
        if code == 0 and on_success is not None:
            on_success()
        self.action_refresh()

    async def action_quit(self) -> None:
        """Quit — asking first about a daemon spawned from this console."""
        alive = self.deps.children.alive()
        if "daemon" not in alive:
            self.exit()
            return
        action = stop_spawned_daemon(self.deps)

        def decided(stop: bool | None) -> None:
            if not stop:
                self.exit()
                return
            self.notify("stopping the spawned daemon…", title=action.title)
            self.execute(action, self.exit)

        self.push_screen(
            ConfirmScreen(
                "a daemon is still running",
                f"The daemon spawned from this console is running (pid {alive['daemon'].pid}). "
                f"{action.prompt} y stops it, then quits; n quits and leaves it running.",
            ),
            decided,
        )


def build_app(
    config: Config,
    state_dir: Path,
    *,
    operator_id: str,
    read_only: bool = False,
    initial_run: str | None = None,
    unit: str | None = None,
) -> SbxloopTui:
    mailbox = MailboxClient(state_dir / "state.db", operator_id=operator_id)
    return SbxloopTui(
        config,
        state_dir,
        mailbox=mailbox,
        read_only=read_only,
        initial_run=initial_run,
        unit=unit,
    )


__all__ = ["PROBE_INTERVAL_S", "SbxloopTui", "build_app"]
