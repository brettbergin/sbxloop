"""The Config screen: the resolved keys with their sources, the policy,
and the editor — a draft validated by the real loader, saved with a
backup, the restart offered."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, TabbedContent

from sbxloop.cli.policyview import policy_view
from sbxloop.config import Config
from sbxloop.paths import SbxloopHome
from sbxloop.tui.configedit import config_path, read_text, save_text, validate_text
from sbxloop.tui.screens.config import ConfigEditor, ConfigScreen, flatten_config
from sbxloop.tui.screens.modals import ConfirmScreen, TypedConfirmScreen
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable
from tests.unit.tui.conftest import FakeCtl, FakeRunner, drive, live_status, make_app

REFRESH: dict[str, Any] = {"refresh_s": 3.0}


@pytest.fixture
def hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No user config layer from the developer's own ~/.config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("SBXLOOP_DAEMON__POLL_INTERVAL_S", raising=False)


def test_configedit_helpers(tmp_path: Path, hermetic: None) -> None:
    path = config_path(tmp_path)
    assert path == tmp_path / "sbxloop.toml"
    assert "[daemon]" in read_text(path)[0], "no file yet: the commented example"
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg-config"), "HOME": str(tmp_path)}
    assert validate_text("[daemon]\npoll_interval_s = 7.0\n", cwd=tmp_path, env=env).ok
    broken = validate_text("[daemon\n", cwd=tmp_path, env=env)
    assert not broken.ok and "draft refused" in broken.text
    unknown = validate_text("[daemon]\nno_such_key = 1\n", cwd=tmp_path, env=env)
    assert not unknown.ok and "no_such_key" in unknown.text
    assert not path.exists(), "validation writes nothing"
    (tmp_path / "unreadable.toml").write_bytes(b"\xff\xfe not utf-8")
    text, note = read_text(tmp_path / "unreadable.toml")
    assert "[daemon]" in text and note is not None and "could not read" in note
    assert save_text(path, "[tui]\nemoji = false\n", now=1_700_000_000.0) is None
    backup = save_text(path, "[tui]\nemoji = true\n", now=1_700_000_000.0)
    assert backup is not None and backup.name.startswith("sbxloop.toml.bak-")
    assert backup.read_text() == "[tui]\nemoji = false\n"
    assert path.read_text() == "[tui]\nemoji = true\n"


def test_validate_sees_the_repositorys_cut_down(tmp_path: Path, hermetic: None) -> None:
    """A tracked sbxloop.toml is the repository's: the loader keeps only
    project keys from it. The verdict says so instead of "draft loads"."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "sbxloop.toml").write_text("[daemon]\npoll_interval_s = 7.0\n")
    subprocess.run(["git", "-C", str(repo), "add", "sbxloop.toml"], check=True)
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg-config"), "HOME": str(tmp_path)}
    verdict = validate_text("[daemon]\npoll_interval_s = 9.0\n", cwd=repo, env=env)
    assert verdict.ok and verdict.dropped == ("daemon.poll_interval_s",)
    assert "repository's" in verdict.text and "ignores daemon.poll_interval_s" in verdict.text


def test_flatten_and_policy_view_are_the_cli_folds() -> None:
    config = Config.model_validate(
        {"home": "/tmp/x", "github": {"repo": "o/r"}, "policy": {"allow": ["*.example.com"]}}
    )
    flat = flatten_config(config)
    assert flat["github.repo"] == "o/r" and "policy.allow" in flat
    view = policy_view(config)
    assert view.allow == "*.example.com" and view.github is not None
    assert "github.com" in view.baseline and view.phases[0][0] == "decompose"


def test_config_screen_resolves_filters_validates_and_saves(
    seeded: SbxloopHome, hermetic: None
) -> None:
    (seeded.root / "sbxloop.toml").write_text("[daemon]\npoll_interval_s = 7.0\n")

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
            assert "7.0" in (seeded.root / "sbxloop.toml").read_text()
            # A save that fails offers no restart for a file never written.
            editor.load_text("[daemon]\npoll_interval_s = 8.0\n")

            def refuse(*_a: object, **_k: object) -> None:
                raise OSError("read-only file system")

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("sbxloop.tui.actions.save_text", refuse)
                await pilot.press("W")
                await pilot.pause(1.5)
                assert isinstance(app.screen, TypedConfirmScreen)
                app.screen.query_one("#typed", Input).value = "save"
                await pilot.press("enter")
                await pilot.pause(1.5)
            assert isinstance(app.screen, ConfigScreen), "no restart prompt after a failed save"
            assert "7.0" in (seeded.root / "sbxloop.toml").read_text()
            # A draft that loads is saved under the typed word, with a
            # backup, and the restart is offered.
            editor.load_text("[daemon]\npoll_interval_s = 9.0\n")
            await pilot.press("W")
            await pilot.pause(1.5)
            assert isinstance(app.screen, TypedConfirmScreen)
            app.screen.query_one("#typed", Input).value = "save"
            await pilot.press("enter")
            await pilot.pause(1.5)
            assert (seeded.root / "sbxloop.toml").read_text() == "[daemon]\npoll_interval_s = 9.0\n"
            backups = list(seeded.root.glob("sbxloop.toml.bak-*"))
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
            # E hands the file to $EDITOR; what it wrote is reloaded after.
            runner = app.deps.runner
            assert isinstance(runner, FakeRunner)

            def external_edit(argv: tuple[str, ...]) -> int:
                Path(argv[-1]).write_text("[daemon]\npoll_interval_s = 11.0\n")
                return 0

            runner.on_interactive = external_edit
            app.suspend = contextlib.nullcontext  # type: ignore[assignment, method-assign]
            await pilot.press("E")
            await pilot.pause(1.0)
            assert runner.interactive_calls and runner.interactive_calls[-1][-1].endswith(
                "sbxloop.toml"
            )
            assert "11.0" in editor.text, "the draft is the file the editor wrote"

    drive(scenario)


def test_the_editor_follows_the_daemons_directory(
    seeded: SbxloopHome, hermetic: None, tmp_path: Path
) -> None:
    """The daemon says where it loaded its config: that file is edited,
    not one in whatever directory the console was started from."""
    daemon_dir = tmp_path / "runner"
    daemon_dir.mkdir()
    (daemon_dir / "sbxloop.toml").write_text("[daemon]\npoll_interval_s = 3.0\n")

    async def scenario() -> None:
        app = make_app(seeded, ctl=FakeCtl(live_status(cwd=str(daemon_dir))), **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(2.0)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            assert screen.path == daemon_dir / "sbxloop.toml"
            assert "3.0" in screen.query_one("#editor", ConfigEditor).text
            assert "daemon's directory" in screen.query_one("#edit-status", TextPanel).content_text

    drive(scenario)


def test_read_only_console_cannot_save(seeded: SbxloopHome, hermetic: None) -> None:
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
            assert not (seeded.root / "sbxloop.toml").exists()

    drive(scenario)
