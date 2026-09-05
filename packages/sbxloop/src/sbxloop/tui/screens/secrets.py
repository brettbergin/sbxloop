"""Secrets: the tracked custom-secret registrations as ``sbxloop secrets
list`` judges them, with clean (dry run first, then typed) and rotate
(hidden prompt, then typed)."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.worker import get_current_worker

from sbxloop.errors import SbxloopError
from sbxloop.sbx.secretstate import SecretRow, clean_secrets, secret_rows, secrets_context
from sbxloop.tui import actions
from sbxloop.tui.data import ConsoleState
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.screens.modals import OutcomeScreen, TextPromptScreen
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable

_STATUS = {"ok": ("ok", "green"), "warn": ("warn", "yellow"), "unknown": ("?", "dim")}


class SecretsScreen(ConsoleScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "Back"),
        Binding("r", "reload", "Refresh"),
        Binding("x", "clean", "Clean stale"),
        Binding("X", "clean_all", "Clean every owned", show=False),
        Binding("K", "rotate", "Rotate"),
    ]
    DEFAULT_CSS = """
    SecretsScreen #summary { height: auto; max-height: 5; padding: 0 1; }
    SecretsScreen #secrets { height: 1fr; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[SecretRow] = []
        self.error: str | None = None

    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with Vertical(id="body"):
            yield TextPanel(Text("reading registrations…", style="dim"), id="summary")
            yield ConsoleTable("env", "expected", "actual", "status", "note", id="secrets")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#secrets", ConsoleTable).focus()
        self.load()

    @work(thread=True, exclusive=True, group="secrets")
    def load(self) -> None:
        deps = self.console_app.deps
        try:
            cli, live = secrets_context(deps.config, deps.sbx())
            rows = secret_rows(deps.config, cli, live)
            error = None
        except SbxloopError as exc:
            rows, error = [], str(exc)
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._apply, rows, error)

    def _apply(self, rows: list[SecretRow], error: str | None) -> None:
        self.rows, self.error = rows, error
        self.render_rows()

    def refresh_data(self, state: ConsoleState) -> None:
        super().refresh_data(state)

    def render_rows(self) -> None:
        table_rows = []
        for row in self.rows:
            label, style = _STATUS[row.judgement.status]
            table_rows.append(
                (
                    row.env,
                    (
                        row.env,
                        row.expected,
                        row.actual,
                        Text(label, style=style),
                        row.judgement.note,
                    ),
                )
            )
        self.query_one("#secrets", ConsoleTable).replace_rows(table_rows)
        text = Text()
        if self.error:
            text.append(self.error + "\n", style="red")
        warned = sum(1 for r in self.rows if r.judgement.status == "warn")
        text.append(f"{len(self.rows)} tracked secret(s) · {warned} warning(s)", style="bold")
        text.append(
            "\nGH_TOKEN uses sbx's built-in github service secret; it is never managed here."
            "\nx cleans the stale registrations (dry run first) · X every owned one · "
            "K rotates the agent credential's registration",
            style="dim",
        )
        self.query_one("#summary", TextPanel).update(text)

    # -- actions -----------------------------------------------------------------

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        self.load()

    def _clean(self, *, every: bool) -> None:
        if self.console_app.read_only:
            self.app.notify("read-only console: clean refused", severity="warning")
            return
        self.dry_run(every=every)

    @work(thread=True, exclusive=True, group="secrets-dry")
    def dry_run(self, *, every: bool) -> None:
        deps = self.console_app.deps
        try:
            cli, live = secrets_context(deps.config, deps.sbx())
            outcomes = clean_secrets(deps.config, cli, live, apply=False, all_=every)
        except SbxloopError as exc:
            self.app.call_from_thread(self.app.notify, str(exc), severity="error")
            return
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._dry_shown, outcomes, every)

    def _dry_shown(self, outcomes: list[object], every: bool) -> None:
        from sbxloop.sbx.secretstate import CleanOutcome

        real = [o for o in outcomes if isinstance(o, CleanOutcome)]
        if not any(o.removed for o in real):
            self.app.notify(
                "\n".join(f"{o.env}: {o.message}" for o in real) or "nothing tracked",
                title="nothing to clean",
            )
            return
        text = "\n".join(f"{o.env}: {o.message}" for o in real)

        def closed(_: object) -> None:
            self.console_app.perform(
                actions.clean_secret_registrations(self.console_app.deps, every=every),
                then=self.load,
            )

        self.app.push_screen(OutcomeScreen("dry run — Esc, then confirm", text), closed)

    def action_clean(self) -> None:
        self._clean(every=False)

    def action_clean_all(self) -> None:
        self._clean(every=True)

    def action_rotate(self) -> None:
        if self.console_app.read_only:
            self.app.notify("read-only console: rotate refused", severity="warning")
            return

        def typed(token: str | None) -> None:
            if token:
                self.console_app.perform(
                    actions.rotate_secret_registrations(self.console_app.deps, token),
                    then=self.load,
                )

        self.app.push_screen(
            TextPromptScreen(
                "rotate the agent credential",
                "The new token (hidden; never an argument, never logged). The registration "
                "in sbx is replaced; update your export / .env yourself.",
                password=True,
            ),
            typed,
        )


__all__ = ["SecretsScreen"]
