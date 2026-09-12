"""Choose a model from the backend's cached catalogue, with explicit custom input."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from sbxloop import modelcatalog
from sbxloop.backends import AgentBackend
from sbxloop.config import Config
from sbxloop.configedit.keys import FieldSpec
from sbxloop.log import redact_text
from sbxloop.paths import SbxloopHome
from sbxloop.tui.screens.configvalue import ValueEdit, ValueScreen


class ModelScreen(ValueScreen):
    DEFAULT_CSS = """
    ModelScreen #dialog { width: 104; height: 26; max-height: 95%; }
    ModelScreen #model-options { height: 1fr; min-height: 3; }
    ModelScreen #model-status { height: auto; max-height: 2; color: $text-muted; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+r", "refresh", "Refresh models", show=False, priority=True),
        Binding("ctrl+t", "custom", "Custom model", show=False, priority=True),
        Binding("down", "next_model", "Next", show=False, priority=True),
        Binding("up", "previous_model", "Previous", show=False, priority=True),
    ]

    def __init__(
        self,
        spec: FieldSpec,
        value: Any,
        *,
        source: str,
        target: str,
        in_file: bool,
        home: SbxloopHome,
        backend: AgentBackend,
        config: Config | None = None,
    ) -> None:
        super().__init__(spec, value, source=source, target=target, in_file=in_file)
        self.home, self.backend, self.config = home, backend, config
        # The endpoint the openai backend's catalog is keyed by (#617): a
        # listing from another endpoint is never offered.
        self.endpoint = modelcatalog.catalog_endpoint(config) if config is not None else None
        self.catalog = modelcatalog.load_catalog(home, backend, endpoint=self.endpoint)
        self.choices: list[ValueEdit] = []
        self.refreshing = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(self.spec.path), classes="key")
            yield Static(self._facts(), classes="facts")
            yield Static("", id="model-status")
            yield Input(placeholder="Search model names or slugs", id="model-filter")
            yield OptionList(id="model-options")
            yield Static(
                "↑/↓ selects · Enter applies · ^R refreshes · ^T enters a custom slug\n"
                + ("^U restores inheritance · " if self.can_unset else "")
                + "Esc cancels",
                classes="hint",
            )

    def on_mount(self) -> None:
        self.render_choices()
        self.show_status()
        self.query_one("#model-filter", Input).focus()
        if self.catalog is None or self.catalog.stale():
            self.action_refresh()

    def show_status(self, error: str | None = None) -> None:
        text = f"{self.backend.label} · "
        if self.catalog is None:
            text += "no cached catalog"
        else:
            stamp = datetime.fromtimestamp(self.catalog.fetched_at, UTC).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            text += f"{len(self.catalog.models)} models · cached {stamp}"
            if self.catalog.stale():
                text += " · stale"
        if self.refreshing:
            text += " · refreshing…"
        if error:
            text += f"\nRefresh failed: {error}"
        self.query_one("#model-status", Static).update(Text(text))

    def render_choices(self) -> None:
        options = self.query_one("#model-options", OptionList)
        previous = (
            self.choices[options.highlighted]
            if options.highlighted is not None and options.highlighted < len(self.choices)
            else ValueEdit(self.spec.path, self.value)
        )
        needle = self.query_one("#model-filter", Input).value.casefold().strip()
        rows: list[tuple[str, ValueEdit, bool]] = []
        if self.can_unset:
            rows.append(
                ("Inherit — remove this override", ValueEdit(self.spec.path, unset=True), False)
            )
        rows.append(("auto — backend default selection", ValueEdit(self.spec.path, "auto"), False))
        known = {"auto"}
        for entry in self.catalog.models if self.catalog else []:
            if entry.id in known:
                continue
            known.add(entry.id)
            label = f"{entry.name}  ·  {entry.id}" if entry.name != entry.id else entry.id
            disabled = entry.policy_state == "disabled"
            if disabled:
                label += " (disabled by provider policy)"
            rows.append((label, ValueEdit(self.spec.path, entry.id), disabled))
        if self.value and self.value not in known:
            rows.append(
                (
                    f"{self.value} (current; not in catalog)",
                    ValueEdit(self.spec.path, self.value),
                    False,
                )
            )
        rows = [row for row in rows if not needle or needle in row[0].casefold()]
        self.choices = [row[1] for row in rows]
        options.clear_options()
        options.add_options([Option(Text(label), disabled=disabled) for label, _, disabled in rows])
        if rows:
            current = next(
                (
                    i
                    for i, (_, edit, disabled) in enumerate(rows)
                    if not disabled and edit == previous
                ),
                next((i for i, row in enumerate(rows) if not row[2]), None),
            )
            options.highlighted = current

    def action_apply(self) -> None:
        options = self.query_one("#model-options", OptionList)
        index = options.highlighted
        if index is not None and not options.get_option_at_index(index).disabled:
            self.dismiss(self.choices[index])
        else:
            self.notify(
                "Choose a listed model, refresh, or use ^T for a custom slug.", severity="warning"
            )

    def action_next_model(self) -> None:
        self.query_one("#model-options", OptionList).action_cursor_down()

    def action_previous_model(self) -> None:
        self.query_one("#model-options", OptionList).action_cursor_up()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_apply()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.render_choices()

    def action_custom(self) -> None:
        def selected(edit: ValueEdit | None) -> None:
            if edit is not None:
                self.dismiss(edit)

        self.app.push_screen(
            ValueScreen(
                self.spec, self.value, source=self.source, target=self.target, in_file=self.in_file
            ),
            selected,
        )

    def action_refresh(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self.show_status()
        self.refresh_models()

    @work(thread=True, exclusive=True, group="model-catalog")
    def refresh_models(self) -> None:
        try:
            catalog: modelcatalog.ModelCatalog | None = modelcatalog.refresh_catalog(
                self.home, self.backend, self.config
            )
            error = None
        except Exception as exc:
            catalog = modelcatalog.load_catalog(self.home, self.backend, endpoint=self.endpoint)
            error = redact_text(str(exc))[:500]
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.refreshed, catalog, error)

    def refreshed(self, catalog: modelcatalog.ModelCatalog | None, error: str | None) -> None:
        self.refreshing = False
        self.catalog = catalog or self.catalog
        self.render_choices()
        self.show_status(error)
        if error:
            self.notify(error, title=f"{self.backend.label} models", severity="warning", timeout=15)
