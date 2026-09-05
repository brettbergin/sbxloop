"""The Config screen: the resolved keys with their sources, the policy,
and the editor — a draft validated by the real loader, saved with a
backup, the restart offered."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, TabbedContent

from sbxloop.cli.policyview import policy_view
from sbxloop.config import Config
from sbxloop.tui.configedit import config_path, read_text, save_text, validate_text
from sbxloop.tui.screens.config import ConfigEditor, ConfigScreen, flatten_config
from sbxloop.tui.screens.modals import ConfirmScreen, TypedConfirmScreen
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable
from tests.unit.tui.conftest import drive, make_app

REFRESH: dict[str, Any] = {"refresh_s": 3.0}


@pytest.fixture
def hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No user config layer from the developer's own ~/.config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("SBXLOOP_DAEMON__POLL_INTERVAL_S", raising=False)


def test_configedit_helpers(tmp_path: Path, hermetic: None) -> None:
    path = config_path(tmp_path)
    assert path == tmp_path / "sbxloop.toml"
    assert "[daemon]" in read_text(path), "no file yet: the commented example"
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg-config"), "HOME": str(tmp_path)}
    assert (
        validate_text("[daemon]\npoll_interval_s = 7.0\n", scratch=tmp_path / "v", env=env) is None
    )
    broken = validate_text("[daemon\n", scratch=tmp_path / "v", env=env)
    assert broken is not None
    unknown = validate_text("[daemon]\nno_such_key = 1\n", scratch=tmp_path / "v", env=env)
    assert unknown is not None and "no_such_key" in unknown
    assert not path.exists()
    assert save_text(path, "[tui]\nemoji = false\n", now=1_700_000_000.0) is None
    backup = save_text(path, "[tui]\nemoji = true\n", now=1_700_000_000.0)
    assert backup is not None and backup.name.startswith("sbxloop.toml.bak-")
    assert backup.read_text() == "[tui]\nemoji = false\n"
    assert path.read_text() == "[tui]\nemoji = true\n"


def test_flatten_and_policy_view_are_the_cli_folds() -> None:
    config = Config.model_validate(
        {"state_dir": "/tmp/x", "github": {"repo": "o/r"}, "policy": {"allow": ["*.example.com"]}}
    )
    flat = flatten_config(config)
    assert flat["github.repo"] == "o/r" and "policy.allow" in flat
    view = policy_view(config)
    assert view.allow == "*.example.com" and view.github is not None
    assert "github.com" in view.baseline and view.phases[0][0] == "decompose"


def test_config_screen_resolves_filters_validates_and_saves(seeded: Path, hermetic: None) -> None:
    (seeded / "sbxloop.toml").write_text("[daemon]\npoll_interval_s = 7.0\n")

    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(1.5)
            assert isinstance(app.screen, ConfigScreen)
            table = app.screen.query_one("#resolved", ConsoleTable)
            row = table.get_row_at(table.get_row_index("daemon.poll_interval_s"))
            assert row[1] == "7.0" and str(row[2]) == "sbxloop.toml"
            total = table.row_count
            await pilot.press("slash")
            await pilot.press(*"poll_interval")
            await pilot.pause(0.3)
            assert 0 < table.row_count < total
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert table.row_count == total
            policy = app.screen.query_one("#policy", TextPanel).content_text
            assert "decompose" in policy and "audit trail" in policy
            # A draft the loader refuses is named, never written.
            editor = app.screen.query_one("#editor", ConfigEditor)
            editor.load_text("[daemon]\nno_such_key = 1\n")
            await pilot.press("V")
            await pilot.pause(1.5)
            assert app.screen.last_verdict.startswith("draft refused")
            assert "no_such_key" in app.screen.last_verdict
            await pilot.press("W")
            await pilot.pause(1.5)
            assert isinstance(app.screen, ConfigScreen), "save validates first: no dialog"
            assert "7.0" in (seeded / "sbxloop.toml").read_text()
            # A draft that loads is saved under the typed word, with a
            # backup, and the restart is offered.
            editor.load_text("[daemon]\npoll_interval_s = 9.0\n")
            await pilot.press("W")
            await pilot.pause(1.5)
            assert isinstance(app.screen, TypedConfirmScreen)
            app.screen.query_one("#typed", Input).value = "save"
            await pilot.press("enter")
            await pilot.pause(1.5)
            assert (seeded / "sbxloop.toml").read_text() == "[daemon]\npoll_interval_s = 9.0\n"
            backups = list(seeded.glob("sbxloop.toml.bak-*"))
            assert len(backups) == 1 and "7.0" in backups[0].read_text()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause(1.0)
            assert isinstance(app.screen, ConfigScreen)
            table = app.screen.query_one("#resolved", ConsoleTable)
            row = table.get_row_at(table.get_row_index("daemon.poll_interval_s"))
            assert row[1] == "9.0"
            # i focuses the editor, Esc hands focus back to the screen.
            await pilot.press("i")
            await pilot.pause(0.3)
            assert app.screen.query_one("#tabs", TabbedContent).active == "edit-pane"
            assert app.focused is editor
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert app.focused is not editor

    drive(scenario)


def test_read_only_console_cannot_save(seeded: Path, hermetic: None) -> None:
    async def scenario() -> None:
        app = make_app(seeded, read_only=True, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(1.0)
            editor = app.screen.query_one("#editor", ConfigEditor)
            assert editor.read_only
            await pilot.press("W")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ConfigScreen)
            assert not (seeded / "sbxloop.toml").exists()

    drive(scenario)
