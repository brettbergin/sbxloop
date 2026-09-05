"""Sandboxes: ``sbx ls`` classified against the store the way
``sbxloop sandbox prune`` classifies it, and the run directories the way
``sbxloop gc`` does — with the removal verbs behind typed confirmations."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Static
from textual.worker import get_current_worker

from sbxloop.errors import SbxloopError
from sbxloop.gc import RunDirVerdict, classify_run_dirs, format_bytes
from sbxloop.sbx.models import SandboxInfo
from sbxloop.sbx.prune import (
    DAEMON_OWNED_PREFIXES,
    SandboxVerdict,
    classify_sandboxes,
    format_age,
)
from sbxloop.tui import actions
from sbxloop.tui.data import ConsoleState
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable

#: `sbx ls` forks a process; the store poll is the fast one.
SANDBOX_POLL_S = 10.0


class SandboxesScreen(ConsoleScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("s", "shell", "Shell"),
        Binding("x", "remove", "Remove"),
        Binding("X", "stop", "Stop", show=False),
        Binding("P", "prune", "Prune orphans"),
        Binding("G", "gc", "gc run dirs"),
        Binding("k", "toggle_kept", "Include kept", show=False),
    ]
    DEFAULT_CSS = """
    SandboxesScreen #summary { height: auto; max-height: 4; padding: 0 1; }
    SandboxesScreen #sandboxes { height: 2fr; }
    SandboxesScreen #rundirs { height: 1fr; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.verdicts: list[SandboxVerdict] = []
        self.infos: list[SandboxInfo] = []
        self.rundirs: list[RunDirVerdict] = []
        self.include_kept = False
        self.error: str | None = None
        self._poll_next = True

    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with Vertical(id="body"):
            yield TextPanel(Text("listing sandboxes…", style="dim"), id="summary")
            yield Static("sandboxes (sbx ls, classified against this store)", classes="title")
            yield ConsoleTable(
                "sandbox", "role", "run", "run state", "age", "status", "verdict", id="sandboxes"
            )
            yield Static(
                "run directories (the daemon's daily sweep, as a dry run)", classes="title"
            )
            yield ConsoleTable("run", "state", "age", "size", "verdict", id="rundirs")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.set_interval(SANDBOX_POLL_S, self.poll)
        self.query_one("#sandboxes", ConsoleTable).focus()
        self.poll()

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        self.poll()

    # -- data ----------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="sandboxes")
    def poll(self) -> None:
        deps = self.console_app.deps
        config = deps.config
        error: str | None = None
        infos: list[SandboxInfo] = []
        verdicts: list[SandboxVerdict] = []
        rundirs: list[RunDirVerdict] = []
        try:
            infos = deps.sbx().ls()
            with deps.mailbox.read_engine() as engine:
                verdicts = classify_sandboxes(
                    infos, engine, include_kept=self.include_kept, now=deps.clock()
                )
        except SbxloopError as exc:
            error = str(exc)
        try:
            with deps.mailbox.read_engine() as engine:
                rundirs = classify_run_dirs(
                    engine,
                    deps.state_dir,
                    older_than_s=config.daemon.prune_runs_after_days * 86400.0,
                    now=deps.clock(),
                )
        except Exception as exc:
            error = error or f"run directories: {exc}"
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._apply, infos, verdicts, rundirs, error)

    def _apply(
        self,
        infos: list[SandboxInfo],
        verdicts: list[SandboxVerdict],
        rundirs: list[RunDirVerdict],
        error: str | None,
    ) -> None:
        self.infos, self.verdicts, self.rundirs, self.error = infos, verdicts, rundirs, error
        self.render_tables()

    def refresh_data(self, state: ConsoleState) -> None:
        super().refresh_data(state)

    def render_tables(self) -> None:
        status_by_name = {i.name: i.status or "" for i in self.infos}
        rows = []
        for v in self.verdicts:
            role = "daemon" if v.name.startswith(DAEMON_OWNED_PREFIXES) else (v.role or "?")
            verdict = Text(("orphan — " if v.orphan else "keep — ") + v.reason)
            verdict.stylize("red" if v.orphan else "green", 0, 6 if v.orphan else 4)
            rows.append(
                (
                    v.name,
                    (
                        v.name,
                        role,
                        v.run_id or "—",
                        v.run_state or "unknown",
                        format_age(v.age_s),
                        status_by_name.get(v.name, ""),
                        verdict,
                    ),
                )
            )
        self.query_one("#sandboxes", ConsoleTable).replace_rows(rows)
        self.query_one("#rundirs", ConsoleTable).replace_rows(
            (
                v.run_id,
                (
                    v.run_id,
                    v.run_state or "unknown",
                    format_age(v.age_s),
                    format_bytes(v.size_bytes) if v.prunable else "",
                    Text(("prune — " if v.prunable else "keep — ") + v.reason),
                ),
            )
            for v in self.rundirs
        )
        orphans = sum(1 for v in self.verdicts if v.orphan)
        prunable = [v for v in self.rundirs if v.prunable]
        summary = Text()
        if self.error:
            summary.append(self.error, style="red")
            summary.append("\n")
        summary.append(
            f"{len(self.verdicts)} sbxloop sandbox(es) · {orphans} orphan(s)"
            f"{' (kept included)' if self.include_kept else ''} · "
            f"{len(prunable)} run dir(s) past retention "
            f"({format_bytes(sum(v.size_bytes for v in prunable))})",
            style="bold",
        )
        summary.append(
            "\nthe state DB is per working copy: 'unknown' sandboxes may belong to another "
            "checkout's runs on this host",
            style="dim",
        )
        self.query_one("#summary", TextPanel).update(summary)

    # -- actions -------------------------------------------------------------------

    def _selected(self) -> SandboxVerdict | None:
        key = self.query_one("#sandboxes", ConsoleTable).selected_key()
        return next((v for v in self.verdicts if v.name == key), None)

    def action_shell(self) -> None:
        v = self._selected()
        if v is None:
            self.app.notify("select a sandbox first", severity="warning")
            return
        self.console_app.perform(actions.shell(self.console_app.deps, v.name))

    def action_remove(self) -> None:
        v = self._selected()
        if v is None:
            return
        self.console_app.perform(
            actions.remove_one_sandbox(self.console_app.deps, v.name, v.role), then=self.poll
        )

    def action_stop(self) -> None:
        v = self._selected()
        if v is None:
            return
        self.console_app.perform(
            actions.stop_sandbox(self.console_app.deps, v.name), then=self.poll
        )

    def action_prune(self) -> None:
        if not any(v.orphan for v in self.verdicts):
            self.app.notify("nothing to prune: no orphaned sandbox", severity="information")
            return
        self.console_app.perform(
            actions.prune_sandboxes(self.console_app.deps, self.verdicts), then=self.poll
        )

    def action_gc(self) -> None:
        if not any(v.prunable for v in self.rundirs):
            self.app.notify("nothing to remove: no run directory past retention")
            return
        days = self.console_app.deps.config.daemon.prune_runs_after_days
        self.console_app.perform(actions.gc_run_dirs(self.console_app.deps, days), then=self.poll)

    def action_toggle_kept(self) -> None:
        self.include_kept = not self.include_kept
        self.app.notify(f"kept sandboxes {'included' if self.include_kept else 'excluded'}")
        self.poll()


__all__ = ["SANDBOX_POLL_S", "SandboxesScreen"]
