"""Config: the resolved configuration with the layer each key came from,
the effective egress policy, the repositories, and an editor for
``sbxloop.toml`` that validates a draft with the real loader before it
is saved (atomically, with a backup) and offers the restart."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Input, TabbedContent, TabPane, TextArea
from textual.worker import get_current_worker

from sbxloop.cli.policyview import PolicyView, policy_view
from sbxloop.config import Config, load_config_with_sources
from sbxloop.tui import actions
from sbxloop.tui.configedit import config_path, read_text, validate_text
from sbxloop.tui.data import ConsoleState
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.screens.modals import ConfirmScreen
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable


def flatten_config(config: Config) -> dict[str, Any]:
    flat: dict[str, Any] = {}

    def walk(prefix: str, data: dict[str, Any]) -> None:
        for key, value in data.items():
            dotted = f"{prefix}{key}"
            if isinstance(value, dict):
                walk(f"{dotted}.", value)
            else:
                flat[dotted] = value

    walk("", config.model_dump(mode="json"))
    return flat


class ConfigEditor(TextArea):
    """The draft; ``Esc`` hands focus back so the screen's keys act."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "leave", "Leave the editor", show=False),
    ]

    def action_leave(self) -> None:
        self.screen.set_focus(None)


class ConfigScreen(ConsoleScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("slash", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("i", "edit", "Edit"),
        Binding("V", "validate", "Validate draft"),
        Binding("W", "save", "Save draft"),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
        Binding("E", "editor", "$EDITOR", show=False),
        Binding("L", "reload", "Reload from disk", show=False),
    ]
    DEFAULT_CSS = """
    ConfigScreen TabbedContent { height: 1fr; }
    ConfigScreen #resolved { height: 1fr; }
    ConfigScreen #editor { height: 1fr; }
    ConfigScreen #edit-status { height: auto; max-height: 6; padding: 0 1; }
    ConfigScreen #filter { display: none; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.filter_text = ""
        self.flat: dict[str, Any] = {}
        self.sources: dict[str, str] = {}
        self.config: Config | None = None
        self.error: str | None = None
        self.path: Path | None = None
        self.last_verdict: str = "not validated yet"

    def compose(self) -> ComposeResult:
        yield from self.compose_frame()
        with Vertical(id="body"), TabbedContent(id="tabs"):
            with TabPane("Resolved", id="resolved-pane"):
                yield Input(placeholder="filter keys, values, sources", id="filter")
                yield ConsoleTable("key", "value", "source", id="resolved")
            with TabPane("Policy", id="policy-pane"), VerticalScroll():
                yield TextPanel(Text("loading…", style="dim"), id="policy")
            with TabPane("Repos", id="repos-pane"):
                yield ConsoleTable(
                    "repo", "enabled", "base", "token env", "trigger label", id="repos"
                )
            with TabPane("Edit", id="edit-pane"):
                yield TextPanel(Text("loading…", style="dim"), id="edit-status")
                yield ConfigEditor(show_line_numbers=True, soft_wrap=False, id="editor")
        yield from self.compose_footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#filter", Input).display = False
        self.query_one("#resolved", ConsoleTable).focus()
        deps = self.console_app.deps
        self.path = config_path(deps.cwd)
        self.query_one("#editor", ConfigEditor).load_text(read_text(self.path))
        if deps.read_only:
            self.query_one("#editor", ConfigEditor).read_only = True
        self._edit_status()
        self.load()

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        self.load()

    # -- data --------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="config")
    def load(self) -> None:
        deps = self.console_app.deps
        try:
            config, sources = load_config_with_sources(cwd=deps.cwd, env=dict(os.environ))
        except Exception as exc:
            if not get_current_worker().is_cancelled:
                self.app.call_from_thread(self._apply, None, {}, {}, None, str(exc))
            return
        flat = flatten_config(config)
        view = policy_view(config)
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._apply, config, flat, sources, view, None)

    def _apply(
        self,
        config: Config | None,
        flat: dict[str, Any],
        sources: dict[str, str],
        view: PolicyView | None,
        error: str | None,
    ) -> None:
        self.config, self.flat, self.sources, self.error = config, flat, sources, error
        self.render_resolved()
        if view is not None:
            self.query_one("#policy", TextPanel).update(self._policy(view))
        elif error:
            self.query_one("#policy", TextPanel).update(Text(error, style="red"))
        self._repos()

    def refresh_data(self, state: ConsoleState) -> None:
        super().refresh_data(state)

    def render_resolved(self) -> None:
        needle = self.filter_text.lower()
        rows: list[tuple[str, tuple[Any, ...]]] = []
        if self.error:
            rows.append(("error", ("configuration", Text(self.error, style="red"), "")))
        for dotted in sorted(self.flat):
            value = repr(self.flat[dotted])
            source = self.sources.get(dotted, "default")
            if needle and needle not in f"{dotted} {value} {source}".lower():
                continue
            style = "" if source == "default" else "bold"
            rows.append((dotted, (dotted, value[:120], Text(source, style=style))))
        self.query_one("#resolved", ConsoleTable).replace_rows(rows)

    @staticmethod
    def _policy(view: PolicyView) -> Any:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold", no_wrap=True)
        table.add_column()
        for phase, policy in view.phases:
            table.add_row(phase, policy)
        table.add_row("", "")
        table.add_row("baseline", view.baseline)
        table.add_row("registries", view.registries)
        table.add_row("mirrors", view.mirrors)
        table.add_row("well-known", view.well_known)
        table.add_row("[policy] allow", view.allow)
        table.add_row("[policy] deny", view.deny)
        if view.github is not None:
            table.add_row("github sandbox", view.github)
        if view.service is not None:
            table.add_row("service sandbox", view.service)
        table.add_row("audit trail", view.audit)
        return table

    def _repos(self) -> None:
        config = self.config
        if config is None:
            return
        rows = []
        for entry in config.github.repo_list():
            effective = config.github.effective_repo(entry.repo) or entry
            rows.append(
                (
                    entry.repo,
                    (
                        entry.repo,
                        "yes" if entry.enabled else "no",
                        effective.deliver_base or "(repo default)",
                        entry.token_env or "GH_TOKEN",
                        config.labels_for(entry.repo).trigger,
                    ),
                )
            )
        self.query_one("#repos", ConsoleTable).replace_rows(rows)

    def _edit_status(self) -> None:
        text = Text()
        path = self.path
        if path is not None:
            text.append(str(path), style="bold")
            text.append("  (new file)" if not path.is_file() else "", style="dim")
        text.append(f"\n{self.last_verdict}")
        text.append(
            "\ni edits · Esc leaves the editor · V validates · W saves (typed) · "
            "E opens $EDITOR · L reloads",
            style="dim",
        )
        self.query_one("#edit-status", TextPanel).update(text)

    # -- actions -----------------------------------------------------------------

    def action_filter(self) -> None:
        self.query_one("#tabs", TabbedContent).active = "resolved-pane"
        box = self.query_one("#filter", Input)
        box.display = True
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.query_one("#filter", Input)
        if box.display or self.filter_text:
            box.value = ""
            box.display = False
            self.filter_text = ""
            self.render_resolved()
            self.query_one("#resolved", ConsoleTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self.filter_text = event.value
            self.render_resolved()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            self.query_one("#resolved", ConsoleTable).focus()

    def action_edit(self) -> None:
        self.query_one("#tabs", TabbedContent).active = "edit-pane"
        self.query_one("#editor", ConfigEditor).focus()

    def draft(self) -> str:
        return self.query_one("#editor", ConfigEditor).text

    def action_validate(self) -> None:
        self.validate_draft(self.draft())

    @work(thread=True, exclusive=True, group="validate")
    def validate_draft(self, text: str, then_save: bool = False) -> None:
        deps = self.console_app.deps
        verdict = validate_text(text, scratch=deps.console_dir / "validate", env=os.environ)
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._validated, verdict, text, then_save)

    def _validated(self, verdict: str | None, text: str, then_save: bool) -> None:
        if verdict is None:
            self.last_verdict = "draft loads: the loader accepted it"
            self._edit_status()
            if then_save and self.path is not None:
                self.console_app.perform(
                    actions.save_config(self.console_app.deps, self.path, text), then=self._saved
                )
            else:
                self.app.notify("the draft loads", title="validate")
            return
        self.last_verdict = f"draft refused: {verdict}"
        self._edit_status()
        self.app.notify(verdict[:300], title="validate", severity="error", timeout=15)

    def action_save(self) -> None:
        if self.console_app.read_only:
            self.app.notify("read-only console: save refused", severity="warning")
            return
        # Never write a draft the loader would refuse: the daemon's next
        # start would fail on it.
        self.validate_draft(self.draft(), then_save=True)

    def _saved(self) -> None:
        self._edit_status()
        self.load()

        def decided(restart: bool | None) -> None:
            if restart:
                self.console_app.perform(actions.unit_verb(self.console_app.deps, "restart"))

        self.app.push_screen(
            ConfirmScreen(
                "restart the daemon?",
                "The daemon reads sbxloop.toml at start. Restart the unit now to apply "
                "the saved file? (The Daemon screen's B does the same later.)",
            ),
            decided,
        )

    def action_editor(self) -> None:
        if self.path is None:
            return
        self.console_app.perform(
            actions.open_editor(self.console_app.deps, self.path), then=self.action_reload
        )

    def action_reload(self) -> None:
        if self.path is None:
            return
        self.query_one("#editor", ConfigEditor).load_text(read_text(self.path))
        self.last_verdict = "reloaded from disk"
        self._edit_status()
        self.load()


__all__ = ["ConfigEditor", "ConfigScreen", "flatten_config"]
