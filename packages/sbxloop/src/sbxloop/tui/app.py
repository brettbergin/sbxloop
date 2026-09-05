"""The console application: modes for the shared screens, a pushed screen
per run, two pollers (the store every ``[tui] refresh_s``, the daemon's
``status`` every few seconds), and the bar every screen carries."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from textual import work
from textual.app import App
from textual.binding import Binding, BindingType

from sbxloop import __version__
from sbxloop.config import Config
from sbxloop.daemon.control import ControlClient
from sbxloop.daemon.mailbox import MailboxClient
from sbxloop.tui.data import ConsoleState, CtlClient, build_state, probe_daemon
from sbxloop.tui.screens.help import HelpScreen
from sbxloop.tui.screens.items import ItemsScreen
from sbxloop.tui.screens.overview import OverviewScreen
from sbxloop.tui.screens.run_detail import RunDetailScreen
from sbxloop.tui.screens.runs import RunsScreen

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
        "help": HelpScreen,
    }
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "mode('overview')", "Overview"),
        Binding("2", "mode('runs')", "Runs"),
        Binding("3", "mode('items')", "Queue"),
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
    ) -> None:
        super().__init__()
        self.config = config
        self.state_dir = state_dir
        self.mailbox = mailbox
        self.ctl: CtlClient = ctl if ctl is not None else ControlClient(state_dir)
        self.read_only = read_only
        self.initial_run = initial_run
        self.clock = clock
        self.emoji = bool(config.tui.emoji)
        self.state = ConsoleState(version=__version__, read_only=read_only)

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
        state = build_state(self.mailbox, self.state, now=self.clock())
        self.call_from_thread(self._apply_state, state)

    @work(thread=True, exclusive=True, group="probe")
    def probe(self) -> None:
        snapshot = probe_daemon(self.ctl, now=self.clock())
        self.call_from_thread(self._apply_daemon, snapshot)

    def _apply_state(self, state: ConsoleState) -> None:
        # The snapshot was built off-thread from an older state: the daemon
        # probe applied here since then is the fresher fact, keep it.
        state.daemon = self.state.daemon
        self.state = state
        self._repaint()

    def _apply_daemon(self, snapshot: Any) -> None:
        self.state.daemon = snapshot
        self._repaint()

    def _repaint(self) -> None:
        repaint = getattr(self.screen, "repaint", None)
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


def build_app(
    config: Config,
    state_dir: Path,
    *,
    operator_id: str,
    read_only: bool = False,
    initial_run: str | None = None,
) -> SbxloopTui:
    mailbox = MailboxClient(state_dir / "state.db", operator_id=operator_id)
    return SbxloopTui(
        config, state_dir, mailbox=mailbox, read_only=read_only, initial_run=initial_run
    )


__all__ = ["PROBE_INTERVAL_S", "SbxloopTui", "build_app"]
