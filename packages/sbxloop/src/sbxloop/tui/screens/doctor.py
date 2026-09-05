"""Doctor: ``sbxloop doctor`` in a worker with its progress lines, the host
checks and the sbx conformance verdicts, cached with their age; ``S``
opens the secrets registrations."""

from __future__ import annotations

import os
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Static
from textual.worker import get_current_worker

from sbxloop.cli.doctor import DoctorReport, _clean, doctor_report
from sbxloop.errors import SbxloopError
from sbxloop.tui.data import ConsoleState
from sbxloop.tui.format import age
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.screens.modals import ConfirmScreen
from sbxloop.tui.screens.secrets import SecretsScreen
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable


class DoctorScreen(ConsoleScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("d", "run_plain", "Run doctor"),
        Binding("D", "run_deep", "Deep (boots a sandbox)", show=False),
        Binding("p", "run_probe", "Probe GitHub", show=False),
        Binding("S", "secrets", "Secrets"),
    ]
    DEFAULT_CSS = """
    DoctorScreen #summary { height: auto; max-height: 4; padding: 0 1; }
    DoctorScreen #checks { height: 2fr; }
    DoctorScreen #conformance { height: 1fr; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.report: DoctorReport | None = None
        self.running: str | None = None
        self.progress: str = ""
        self.error: str | None = None

    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with Vertical(id="body"):
            yield TextPanel(Text("d runs the host checks", style="dim"), id="summary")
            yield Static("host checks", classes="title")
            yield ConsoleTable("check", "status", "detail", id="checks")
            yield Static("sbx conformance", classes="title")
            yield ConsoleTable("probe", "verdict", "status", "detail", id="conformance")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#checks", ConsoleTable).focus()
        if self.report is None and self.running is None:
            self.run_report(deep=False, probe=False)

    # -- data --------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="doctor")
    def run_report(self, *, deep: bool, probe: bool) -> None:
        self.running = "deep" if deep else ("probe" if probe else "plain")
        self.app.call_from_thread(self._summary)

        def progress(message: str) -> None:
            if not get_current_worker().is_cancelled:
                self.app.call_from_thread(self._progress, message)

        try:
            report = doctor_report(dict(os.environ), deep=deep, probe=probe, progress=progress)
        except (SbxloopError, OSError) as exc:
            if not get_current_worker().is_cancelled:
                self.app.call_from_thread(self._failed, str(exc))
            return
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._apply, report)

    def _progress(self, message: str) -> None:
        self.progress = message
        self._summary()

    def _failed(self, error: str) -> None:
        self.running = None
        self.error = error
        self._summary()

    def _apply(self, report: DoctorReport) -> None:
        self.running = None
        self.error = None
        self.progress = ""
        self.report = report
        self.render_report()

    def refresh_data(self, state: ConsoleState) -> None:
        super().refresh_data(state)
        self._summary()

    def _summary(self) -> None:
        text = Text()
        if self.running:
            text.append(f"running doctor ({self.running})… ", style="bold yellow")
            text.append(self.progress, style="dim")
        elif self.error:
            text.append(f"doctor failed: {self.error}", style="red")
        elif self.report is None:
            text.append("d runs the host checks", style="dim")
        else:
            r = self.report
            hard = [c for c in r.checks if not c.ok and c.hard]
            soft = [c for c in r.checks if not c.ok and not c.hard]
            verdict = "ready" if r.ready else f"NOT READY: {len(hard)} failing check(s)"
            text.append(verdict, style="bold green" if r.ready else "bold red")
            if soft:
                text.append(f" · {len(soft)} warning(s)", style="yellow")
            flags = " deep" if r.deep else (" probe" if r.probe else "")
            text.append(f" · checked {age(r.checked_at)}{flags}", style="dim")
            if r.conformance is not None and r.conformance.unverified:
                text.append(
                    f" · sbx drift: {len(r.conformance.unverified)} probe(s) unverified",
                    style="bold red",
                )
            if r.conformance_note:
                text.append(f"\n{r.conformance_note}", style="yellow")
            if r.conformance is not None and r.conformance.deep_run_hint:
                text.append(f"\n{r.conformance.deep_run_hint}", style="yellow")
        text.append("\nd plain · D deep · p probe GitHub · S secrets", style="dim")
        self.query_one("#summary", TextPanel).update(text)

    def render_report(self) -> None:
        self._summary()
        report = self.report
        if report is None:
            return
        rows = []
        for check in report.checks:
            if check.ok:
                status = Text("ok", style="green")
            elif check.hard:
                status = Text("FAIL", style="bold red")
            else:
                status = Text("warn", style="yellow")
            rows.append((check.name, (check.name, status, _clean(check.detail, 160))))
        self.query_one("#checks", ConsoleTable).replace_rows(rows)
        conf_rows = []
        if report.conformance is not None:
            for outcome in report.conformance.outcomes:
                if outcome.source == "unprobed":
                    status, detail = Text("unprobed", style="dim"), "needs a live sandbox (D)"
                elif outcome.is_error:
                    status, detail = Text("error", style="yellow"), outcome.detail
                elif outcome.drifts:
                    status, detail = Text("DRIFT", style="bold red"), outcome.detail
                else:
                    status, detail = Text("ok", style="green"), outcome.detail
                if outcome.source == "cache":
                    status.append(" (cached)", style="dim")
                elif outcome.source == "provision":
                    status.append(" (field)", style="dim")
                conf_rows.append(
                    (
                        outcome.probe.id,
                        (
                            outcome.probe.id,
                            _clean(outcome.verdict, 60),
                            status,
                            _clean(detail, 160),
                        ),
                    )
                )
        self.query_one("#conformance", ConsoleTable).replace_rows(conf_rows)

    # -- actions -----------------------------------------------------------------

    def action_run_plain(self) -> None:
        if self.running:
            self.app.notify("doctor is already running", severity="warning")
            return
        self.run_report(deep=False, probe=False)

    def _confirmed_run(self, *, deep: bool, probe: bool, what: str) -> None:
        if self.running:
            self.app.notify("doctor is already running", severity="warning")
            return

        def decided(ok: bool | None) -> None:
            if ok:
                self.run_report(deep=deep, probe=probe)

        self.app.push_screen(ConfirmScreen(what, f"{what}? It boots a sandbox."), decided)

    def action_run_deep(self) -> None:
        self._confirmed_run(deep=True, probe=False, what="run the live sbx conformance probes")

    def action_run_probe(self) -> None:
        self._confirmed_run(
            deep=False, probe=True, what="ask GitHub about each configured repository"
        )

    def action_secrets(self) -> None:
        self.app.push_screen(SecretsScreen())


__all__ = ["DoctorScreen"]
