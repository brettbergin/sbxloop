"""The operator chooses discovered slugs rather than retyping them."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest
from textual.widgets import Input, OptionList, Static

from sbxloop import modelcatalog
from sbxloop.backends import backend_named
from sbxloop.configedit.keys import describe
from sbxloop.paths import SbxloopHome
from sbxloop.tui.screens.config import ConfigScreen
from sbxloop.tui.screens.configvalue import ValueEdit, ValueScreen
from sbxloop.tui.screens.modelvalue import ModelScreen
from tests.unit.test_modelcatalog import row
from tests.unit.tui.conftest import drive, make_app


def picker(home: SbxloopHome, path="agent.models.build", value=None) -> ModelScreen:
    return ModelScreen(
        describe(path),
        value,
        source="default",
        target=str(home.config_toml),
        in_file=value is not None,
        home=home,
        backend=backend_named("claude"),
    )


@pytest.mark.parametrize(
    "key", ["model", "agent.models.build", "concierge.model", "github.repos[0].agent_models.review"]
)
def test_discovered_model_can_be_selected_by_name_without_typing_slug(seeded, key):
    modelcatalog.save_catalog(
        seeded, backend_named("claude"), [row("precise-slug-20260909", "Friendly model")]
    )
    edits = []

    async def scenario():
        app = make_app(seeded)
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(picker(seeded, key), edits.append)
            # Not `until` on the screen: the page's worker fills it after mount,
            # and every assertion below reads what the worker wrote.
            await pilot.pause(0.2)
            assert isinstance(app.screen, ModelScreen)
            app.screen.query_one("#model-filter", Input).value = "friendly"
            await pilot.press("enter")
            await pilot.pause(0.2)

    drive(scenario)
    assert edits == [ValueEdit(key, "precise-slug-20260909")]


@pytest.mark.parametrize(
    "key", ["agent.models.build", "concierge.model", "github.repos[0].agent_models.review"]
)
def test_picker_restores_inheritance(seeded, key):
    modelcatalog.save_catalog(seeded, backend_named("claude"), [row()])
    edits = []

    async def scenario():
        app = make_app(seeded)
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(picker(seeded, key, "selected"), edits.append)
            await pilot.pause(0.1)
            await pilot.press("ctrl+u")

    drive(scenario)
    assert edits == [ValueEdit(key, unset=True)]


def test_missing_catalog_preserves_custom_current_and_auto_without_accepting_search_text(seeded):
    modelcatalog.save_catalog(seeded, backend_named("copilot"), [row("wrong-backend")])
    edits = []

    async def scenario():
        app = make_app(seeded)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = picker(seeded, value="custom-alias")
            app.push_screen(screen, edits.append)
            await pilot.pause(0.3)
            values = [choice.value for choice in screen.choices]
            assert "auto" in values and "custom-alias" in values and "wrong-backend" not in values
            assert "Refresh failed" in str(screen.query_one("#model-status", Static).render())
            screen.query_one("#model-filter", Input).value = "typo-model"
            await pilot.press("enter")
            assert app.screen is screen and not edits
            await pilot.press("ctrl+t")
            await pilot.pause(0.1)
            assert isinstance(app.screen, ValueScreen) and not isinstance(app.screen, ModelScreen)
            app.screen.query_one("#value-text", Input).value = "intentional-alias"
            await pilot.press("enter")
            await pilot.pause(0.2)

    drive(scenario)
    assert edits == [ValueEdit("agent.models.build", "intentional-alias")]


def test_stale_catalog_stays_usable_on_refresh_failure_and_manual_refresh_updates_it(
    seeded, monkeypatch
):
    catalog = modelcatalog.save_catalog(seeded, backend_named("claude"), [row("old")])
    path = seeded.model_catalogs / "claude.json"
    path.write_text(
        catalog.model_copy(
            update={"fetched_at": time.time() - modelcatalog.REFRESH_AFTER_S - 1}
        ).model_dump_json()
    )

    async def scenario():
        app = make_app(seeded)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = picker(seeded)
            app.push_screen(screen)
            await pilot.pause(0.3)
            assert any(choice.value == "old" for choice in screen.choices)
            assert "stale" in str(screen.query_one("#model-status", Static).render())
            monkeypatch.setattr(
                modelcatalog, "fetch_backend_rows", lambda *args, **kwargs: [row("fresh")]
            )
            await pilot.press("ctrl+r")
            await pilot.pause(0.3)
            assert any(choice.value == "fresh" for choice in screen.choices)
            assert not any(choice.value == "old" for choice in screen.choices)
            await pilot.press("escape")

    drive(scenario)


def test_provider_disabled_model_cannot_be_selected(seeded):
    modelcatalog.save_catalog(
        seeded, backend_named("claude"), [replace(row("blocked"), policy_state="disabled")]
    )

    async def scenario():
        app = make_app(seeded)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = picker(seeded)
            app.push_screen(screen)
            await pilot.pause(0.1)
            screen.query_one("#model-filter", Input).value = "blocked"
            await pilot.press("enter")
            assert app.screen is screen
            assert screen.query_one("#model-options", OptionList).get_option_at_index(0).disabled
            await pilot.press("escape")

    drive(scenario)


def test_reopening_after_backend_switch_reads_that_backends_catalog(seeded):
    seeded.config_toml.parent.mkdir(parents=True, exist_ok=True)
    seeded.config_toml.write_text('[agent]\nbackend = "claude"\n')
    for backend in ("claude", "codex"):
        modelcatalog.save_catalog(seeded, backend_named(backend), [row(f"{backend}-model")])

    async def scenario():
        app = make_app(seeded)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("7")
            await pilot.pause(0.5)
            config = app.screen
            assert isinstance(config, ConfigScreen)
            config.edit_key("agent.models.build")
            await pilot.pause(0.1)
            assert isinstance(app.screen, ModelScreen) and app.screen.backend.name == "claude"
            await pilot.press("escape")
            seeded.config_toml.write_text('[agent]\nbackend = "codex"\n')
            config.load()
            await pilot.pause(0.5)
            config.edit_key("agent.models.build")
            await pilot.pause(0.1)
            assert isinstance(app.screen, ModelScreen) and app.screen.backend.name == "codex"
            assert any(choice.value == "codex-model" for choice in app.screen.choices)
            assert not any(choice.value == "claude-model" for choice in app.screen.choices)
            await pilot.press("escape")

    drive(scenario)


def test_picker_keeps_controls_visible_in_small_terminal(seeded):
    modelcatalog.save_catalog(seeded, backend_named("claude"), [row()])

    async def scenario():
        app = make_app(seeded)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = picker(seeded, value="selected")
            app.push_screen(screen)
            await pilot.pause(0.2)
            assert (
                screen.query_one(".hint").region.bottom
                <= screen.query_one("#dialog").content_region.bottom
            )
            assert screen.query_one("#model-options").region.height >= 3
            await pilot.press("escape")

    drive(scenario)


def test_refresh_preserves_the_highlighted_model(seeded, monkeypatch):
    modelcatalog.save_catalog(seeded, backend_named("claude"), [row("first"), row("second")])
    monkeypatch.setattr(
        modelcatalog,
        "fetch_backend_rows",
        lambda *args, **kwargs: [row("first"), row("second"), row("third")],
    )

    async def scenario():
        app = make_app(seeded)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = picker(seeded, value="first")
            app.push_screen(screen)
            await pilot.pause(0.1)
            await pilot.press("down")
            options = screen.query_one("#model-options", OptionList)
            assert screen.choices[options.highlighted].value == "second"
            await pilot.press("ctrl+r")
            await pilot.pause(0.3)
            assert screen.choices[options.highlighted].value == "second"
            await pilot.press("escape")

    drive(scenario)
