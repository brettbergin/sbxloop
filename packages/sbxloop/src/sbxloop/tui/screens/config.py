"""Config: the resolved configuration with the layer each key came from,
the effective egress policy, the repositories, and two ways to change any
of it.

The first is per key. Every setting is one row — arrays of tables walked
down to ``github.repos[1].deliver_base``, so nothing is a blob you have to
find in a file — and ``Enter`` on a row opens that key alone: what it
holds, what it accepts, which layer is currently answering, and a widget
shaped by its type. What comes back is written into the draft at that path
and nowhere else, every comment in the file kept.

The second is the file, in a text editor, for the edits no single key
describes. Both go through the same gate: the real loader gets the whole
draft first, and only a draft it accepts is saved — atomically, with a
backup, and the restart offered."""

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
from textual.notifications import SeverityLevel
from textual.widgets import Input, TabbedContent, TabPane, TextArea
from textual.worker import get_current_worker

from sbxloop.cli.policyview import PolicyView, policy_view
from sbxloop.config import Config, load_config_with_sources
from sbxloop.tui import actions, configkeys, configtoml
from sbxloop.tui.configedit import (
    FILE_LAYERS,
    Verdict,
    config_path,
    read_text,
    validate_text,
)
from sbxloop.tui.data import ConsoleState
from sbxloop.tui.screens.base import ConsoleScreen
from sbxloop.tui.screens.configvalue import ValueEdit, ValueScreen
from sbxloop.tui.screens.modals import ConfirmScreen, TextPromptScreen
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable


def flatten_config(config: Config) -> dict[str, Any]:
    """Every setting as one addressable leaf: ``daemon.poll_interval_s``,
    ``github.repos[1].deliver_base``, ``sandbox.env.RAILS_ENV``."""
    return configkeys.flatten(config.model_dump(mode="json"))


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
        Binding("e", "edit_value", "Edit value"),
        Binding("a", "add_key", "Add key"),
        Binding("i", "edit", "Edit file"),
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
        # What the editor was loaded with: a re-anchor or reload replaces
        # the draft only while it is untouched.
        self._loaded_text = ""
        self._anchor_note = ""
        #: Whether the pending keyed save is the whole change (see
        #: ``_key_edited``); a hand-edited draft is confirmed as one.
        self._keyed_alone = True

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
        self._anchor(deps.cwd, "the console's directory")
        if deps.read_only:
            self.query_one("#editor", ConfigEditor).read_only = True
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
        # The daemon says where it loaded its config from: that is the
        # file to edit, whatever directory the console was started in.
        status = (state.daemon.status if state.daemon and state.daemon.live else None) or {}
        cwd = status.get("cwd")
        if cwd and self.path is not None and config_path(Path(cwd)) != self.path:
            self._anchor(Path(cwd), "the daemon's directory")

    def _anchor(self, root: Path, origin: str) -> None:
        """Point the editor at ``root``'s sbxloop.toml, loading it when the
        draft is untouched (else the operator's edit stays)."""
        path = config_path(root)
        editor = self.query_one("#editor", ConfigEditor)
        untouched = editor.text == self._loaded_text
        self.path = path
        self._anchor_note = f"({origin})"
        if untouched:
            text, note = read_text(path)
            editor.load_text(text)
            self._loaded_text = text
            if note:
                self.last_verdict = note
        else:
            self.last_verdict = f"the file moved to {path}; the draft is yours — L reloads"
        self._edit_status()

    def render_resolved(self) -> None:
        needle = self.filter_text.lower()
        rows: list[tuple[str, tuple[Any, ...]]] = []
        if self.error:
            rows.append(("error", ("configuration", Text(self.error, style="red"), "")))
        for dotted in sorted(self.flat):
            value = repr(self.flat[dotted])
            source = configkeys.source_for(dotted, self.sources)
            if needle and needle not in f"{dotted} {value} {source}".lower():
                continue
            style = "" if source == "default" else "bold"
            # Short enough that the source column survives an 80-column
            # terminal; the whole value is one Enter away.
            shown = value if len(value) <= 60 else value[:59] + "…"
            rows.append((dotted, (dotted, shown, Text(source, style=style))))
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
        for index, entry in enumerate(config.github.repo_list()):
            effective = config.github.effective_repo(entry.repo) or entry
            rows.append(
                (
                    f"github.repos[{index}]",
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
            text.append(f"  {self._anchor_note}", style="dim")
            text.append("  (new file)" if not path.is_file() else "", style="dim")
        text.append(f"\n{self.last_verdict}")
        text.append(
            "\ni edits the file · Esc leaves the editor · V validates · W saves (typed) · "
            "E opens $EDITOR · L reloads"
            "\nResolved: Enter or e edits one key · a adds one",
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

    # -- one key at a time -------------------------------------------------------

    def on_data_table_row_selected(self, event: ConsoleTable.RowSelected) -> None:
        """Enter on a resolved key edits it; Enter on a repository narrows
        the resolved view to that entry's keys."""
        key = str(event.row_key.value or "")
        if event.data_table.id == "resolved" and key != "error":
            self.edit_key(key)
        elif event.data_table.id == "repos" and key:
            self._filter_to(key)

    def _filter_to(self, prefix: str) -> None:
        box = self.query_one("#filter", Input)
        box.display = True
        box.value = prefix
        self.query_one("#tabs", TabbedContent).active = "resolved-pane"
        self.query_one("#resolved", ConsoleTable).focus()

    def action_edit_value(self) -> None:
        self.query_one("#tabs", TabbedContent).active = "resolved-pane"
        key = self.query_one("#resolved", ConsoleTable).selected_key()
        if key is None or key == "error":
            self.app.notify("no key selected", title="config", severity="warning")
            return
        self.edit_key(key)

    def action_add_key(self) -> None:
        """A key the resolved view has no row for: a new ``[[github.repos]]``
        entry, an environment variable, a registry."""
        if self._refuse_read_only():
            return

        def typed(dotted: str | None) -> None:
            if dotted:
                self.edit_key(dotted.strip())

        self.app.push_screen(
            TextPromptScreen(
                "add a key",
                "The dotted path to set — github.repos[2].repo, sandbox.env.RAILS_ENV. "
                "An index one past the end appends an entry.",
                placeholder="section.key",
            ),
            typed,
        )

    def _refuse_read_only(self) -> bool:
        if self.console_app.read_only:
            self.app.notify("read-only console: editing refused", severity="warning")
            return True
        return False

    def edit_key(self, dotted: str) -> None:
        """Open one setting on its own; what comes back goes through the
        loader before the file is touched."""
        if self._refuse_read_only():
            return
        if self.path is None:  # pragma: no cover - anchored on mount
            return
        try:
            parts = configkeys.parse_path(dotted)
        except configkeys.PathError as exc:
            self.app.notify(str(exc)[:300], title="config", severity="error")
            return
        dotted = configkeys.format_path(parts)
        why = configkeys.ENV_ONLY_KEYS.get(dotted)
        if why is not None:
            self.app.notify(
                f"{dotted} is not a file setting: {why}", title=dotted, severity="warning"
            )
            return
        _, in_file = configtoml.file_value(self.draft(), parts)
        spec = configkeys.describe(dotted)
        value = self.flat.get(dotted)
        source = configkeys.source_for(dotted, self.sources) if dotted in self.flat else "unset"
        self.app.push_screen(
            ValueScreen(
                spec,
                value,
                source=source,
                target=str(self.path),
                in_file=in_file,
            ),
            self._key_edited,
        )

    def _key_edited(self, edit: ValueEdit | None) -> None:
        if edit is None:
            return
        draft = self.draft()
        parts = configkeys.parse_path(edit.path)
        try:
            text = (
                configtoml.unset_value(draft, parts)
                if edit.unset
                else configtoml.set_value(draft, parts, edit.value)
            )
        except ValueError as exc:
            self.last_verdict = f"{edit.path}: {exc}"
            self._edit_status()
            self.app.notify(str(exc)[:300], title=edit.path, severity="error", timeout=15)
            return
        if text == draft:
            self.app.notify(f"{edit.path} already says that", title="config")
            return
        # A one-key edit saves without a second confirmation because the
        # dialog *was* the confirmation. That only holds while the key is
        # the whole change: a draft the operator has also edited by hand
        # would ride along, so it goes back to the typed tier.
        self._keyed_alone = draft == self._loaded_text
        self.validate_draft(text, then_save=True, edit=edit)

    def draft(self) -> str:
        return self.query_one("#editor", ConfigEditor).text

    def action_validate(self) -> None:
        self.validate_draft(self.draft())

    @work(thread=True, exclusive=True, group="validate")
    def validate_draft(
        self, text: str, then_save: bool = False, edit: ValueEdit | None = None
    ) -> None:
        deps = self.console_app.deps
        root = self.path.parent if self.path is not None else deps.cwd
        verdict = validate_text(text, cwd=root, env=os.environ)
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._validated, verdict, text, then_save, edit)

    def _validated(
        self, verdict: Verdict, text: str, then_save: bool, edit: ValueEdit | None = None
    ) -> None:
        key = edit.path if edit is not None else None
        self.last_verdict = verdict.text if key is None else f"{key}: {verdict.text}"
        if not verdict.ok:
            self._edit_status()
            self.app.notify(
                verdict.text[:300], title=key or "validate", severity="error", timeout=15
            )
            return
        if edit is not None:
            note, severity = self._answered_by(edit, verdict)
            if note is not None:
                self.last_verdict = note
                self.app.notify(note[:300], title=edit.path, severity=severity, timeout=15)
        self._edit_status()
        if verdict.dropped:
            self.app.notify(verdict.text[:300], title="validate", severity="warning", timeout=15)
        if then_save and self.path is not None:
            solo = key if self._keyed_alone else None
            self.console_app.perform(
                actions.save_config(self.console_app.deps, self.path, text, key=solo),
                on_success=lambda: self._saved(text),
            )
        elif not verdict.dropped:
            self.app.notify("the draft loads", title="validate")

    def _answered_by(self, edit: ValueEdit, verdict: Verdict) -> tuple[str | None, SeverityLevel]:
        """Which layer answers for the edited key once the draft applies.
        This file is written either way — the operator asked for it — but a
        key the environment or the home config also sets would otherwise
        look applied when it is not, and an unset key should say what it
        fell back to."""
        if verdict.config is None:  # pragma: no cover - guarded by verdict.ok
            return None, "information"
        source = configkeys.source_for(edit.path, verdict.sources)
        if source in FILE_LAYERS:
            return None, "information"
        value = configkeys.flatten(verdict.config.model_dump(mode="json")).get(edit.path)
        if edit.unset:
            where = "its default" if source == "default" else source
            return f"{edit.path} unset here; it now comes from {where}: {value!r}", "information"
        return (
            f"{edit.path} is written, but {source} sets it too and wins: "
            f"the loop still sees {value!r}",
            "warning",
        )

    def action_save(self) -> None:
        if self.console_app.read_only:
            self.app.notify("read-only console: save refused", severity="warning")
            return
        # Never write a draft the loader would refuse: the daemon's next
        # start would fail on it.
        self._keyed_alone = True
        self.validate_draft(self.draft(), then_save=True)

    def _saved(self, text: str) -> None:
        self._loaded_text = text
        editor = self.query_one("#editor", ConfigEditor)
        if editor.text != text:
            # A one-key edit rewrote the file; the draft is that file.
            editor.load_text(text)
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
        self.console_app.perform(actions.open_editor(self.path), then=self.action_reload)

    def action_reload(self) -> None:
        if self.path is None:
            return
        text, note = read_text(self.path)
        self.query_one("#editor", ConfigEditor).load_text(text)
        self._loaded_text = text
        self.last_verdict = note or "reloaded from disk"
        self._edit_status()
        self.load()


__all__ = ["ConfigEditor", "ConfigScreen", "flatten_config"]
