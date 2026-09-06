"""The Config screen: the resolved keys with their sources, the policy,
and the editor — a draft validated by the real loader, saved with a
backup, the restart offered."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Select, TabbedContent

from sbxloop.cli.policyview import policy_view
from sbxloop.config import Config
from sbxloop.paths import SbxloopHome
from sbxloop.tui import configkeys, configtoml
from sbxloop.tui.configedit import config_path, read_text, save_text, validate_text
from sbxloop.tui.screens.config import ConfigEditor, ConfigScreen, flatten_config
from sbxloop.tui.screens.configvalue import ValueScreen
from sbxloop.tui.screens.modals import ConfirmScreen, TextPromptScreen, TypedConfirmScreen
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


def test_flatten_walks_arrays_of_tables_and_sources_follow(hermetic: None) -> None:
    """A `[[github.repos]]` entry used to print as one blob. Every field in
    it is its own key now, and a leaf inherits the layer that supplied the
    container the loader attributed."""
    config = Config.model_validate(
        {
            "home": "/tmp/x",
            "github": {
                "repos": [
                    {"repo": "o/r", "deliver_base": "main"},
                    {"repo": "o/s", "reviewers": ["ada"]},
                ]
            },
            "policy": {"allow": ["*.example.com"]},
            "sandbox": {"env": {"RAILS_ENV": "test"}},
        }
    )
    flat = flatten_config(config)
    assert flat["github.repos[1].repo"] == "o/s"
    assert flat["github.repos[0].deliver_base"] == "main"
    assert flat["sandbox.env.RAILS_ENV"] == "test"
    # A list of scalars stays one row: the useful edit is the whole list.
    assert flat["policy.allow"] == ["*.example.com"]
    assert flat["github.repos[1].reviewers"] == ["ada"]
    assert "github.repos" not in flat, "the array itself is walked, not printed"
    # The loader attributes `github.repos` as a whole; its leaves say so.
    sources = {"github.repos": "sbxloop.toml", "daemon.poll_interval_s": "env"}
    assert configkeys.source_for("github.repos[1].repo", sources) == "sbxloop.toml"
    assert configkeys.source_for("daemon.poll_interval_s", sources) == "env"
    assert configkeys.source_for("landing.merge_method", sources) == "default"


def test_paths_and_specs_come_from_the_model() -> None:
    assert configkeys.parse_path("github.repos[1].repo") == ("github", "repos", 1, "repo")
    assert configkeys.format_path(("github", "repos", 1, "repo")) == "github.repos[1].repo"
    with pytest.raises(configkeys.PathError):
        configkeys.parse_path("github.repos[x]")
    with pytest.raises(configkeys.PathError):
        configkeys.parse_path("  ")

    boolean = configkeys.describe("keep_sandboxes")
    assert boolean.kind == "choice" and boolean.boolean and not boolean.optional
    assert configkeys.parse_value("true", boolean) is True

    literal = configkeys.describe("landing.merge_method")
    assert literal.kind == "choice" and "squash" in literal.choices
    with pytest.raises(ValueError, match="one of"):
        configkeys.parse_value("nope", literal)

    number = configkeys.describe("daemon.poll_interval_s")
    assert number.kind == "float" and "> 0" in number.summary
    with pytest.raises(ValueError, match="is a number"):
        configkeys.parse_value("soon", number)

    listed = configkeys.describe("policy.allow")
    assert listed.kind == "list"
    assert configkeys.parse_value("*.a.com\n\n *.b.com ", listed) == ["*.a.com", "*.b.com"]
    assert configkeys.render_value(["*.a.com", "*.b.com"], listed) == "*.a.com\n*.b.com"

    optional = configkeys.describe("sandbox.workspace")
    assert optional.optional and optional.kind == "str"
    assert configkeys.render_value(None, optional) == ""

    # A path the model does not describe is edited as a TOML value, and the
    # loader — not this table — decides whether it is a setting at all.
    unknown = configkeys.describe("nowhere.at.all")
    assert unknown.kind == "raw" and unknown.optional
    assert configkeys.parse_value("{a = 1}", unknown) == {"a": 1}


def test_writing_one_key_keeps_every_other_line() -> None:
    source = (
        "# the operator's file\n"
        "[daemon]\n"
        "# how often the loop looks for work\n"
        "poll_interval_s = 7.0  # tuned down\n"
        "\n"
        "[[github.repos]]\n"
        'repo = "o/r"\n'
    )
    parts = configkeys.parse_path("daemon.poll_interval_s")
    out = configtoml.set_value(source, parts, 9.0)
    assert "# how often the loop looks for work" in out
    assert "poll_interval_s = 9.0" in out and "# tuned down" in out
    assert out.replace("9.0", "7.0") == source, "one assignment changed, nothing else"

    # A table the file has never had is created; an index one past the end
    # appends an entry.
    made = configtoml.set_value(source, configkeys.parse_path("landing.merge_method"), "squash")
    assert '[landing]\nmerge_method = "squash"' in made
    grown = configtoml.set_value(source, configkeys.parse_path("github.repos[1].repo"), "o/s")
    assert grown.count("[[github.repos]]") == 2 and '"o/s"' in grown
    assert configtoml.file_value(grown, configkeys.parse_path("github.repos[1].repo")) == (
        "o/s",
        True,
    )
    assert configtoml.file_value(source, configkeys.parse_path("landing.merge_method")) == (
        None,
        False,
    )

    # Unsetting takes the line out; the layer beneath answers instead.
    gone = configtoml.unset_value(source, parts)
    assert "poll_interval_s" not in gone and "# the operator's file" in gone
    assert configtoml.unset_value(source, configkeys.parse_path("landing.merge_method")) == source

    with pytest.raises(configtoml.ConfigWriteError, match="past the end"):
        configtoml.set_value(source, configkeys.parse_path("github.repos[4].repo"), "o/t")
    with pytest.raises(configtoml.ConfigWriteError, match="key of a"):
        configtoml.set_value(source, configkeys.parse_path("daemon.poll_interval_s.deeper"), 1)
    with pytest.raises(configtoml.ConfigWriteError, match="invalid TOML"):
        configtoml.set_value("[daemon\n", parts, 1.0)


def _open_key(screen: ConfigScreen, key: str) -> None:
    table = screen.query_one("#resolved", ConsoleTable)
    table.focus()
    table.move_cursor(row=table.get_row_index(key))


def test_a_key_is_edited_from_the_resolved_view(seeded: SbxloopHome, hermetic: None) -> None:
    """The point of the tab: pick the row, type the value, done — the file
    keeps every comment it had, and the restart is offered as always."""
    (seeded.root / "sbxloop.toml").write_text(
        "# why the loop looks so often\n[daemon]\npoll_interval_s = 7.0\n"
    )

    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(1.5)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)

            # A number: Enter opens the key, Enter applies it.
            _open_key(screen, "daemon.poll_interval_s")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ValueScreen)
            assert app.screen.spec.kind == "float"
            app.screen.query_one("#value-text", Input).value = "9.0"
            await pilot.press("enter")
            await pilot.pause(2.0)
            saved = (seeded.root / "sbxloop.toml").read_text()
            assert "# why the loop looks so often" in saved, "the comments survive"
            assert "poll_interval_s = 9.0" in saved
            assert isinstance(app.screen, ConfirmScreen), "the restart is offered"
            await pilot.press("n")
            await pilot.pause(1.0)
            assert isinstance(app.screen, ConfigScreen)
            table = app.screen.query_one("#resolved", ConsoleTable)
            assert table.get_row_at(table.get_row_index("daemon.poll_interval_s"))[1] == "9.0"
            assert "9.0" in app.screen.query_one("#editor", ConfigEditor).text

            # A value the loader refuses is named and never written.
            _open_key(app.screen, "daemon.poll_interval_s")
            await pilot.press("e")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ValueScreen)
            app.screen.query_one("#value-text", Input).value = "-1"
            await pilot.press("enter")
            await pilot.pause(2.0)
            assert isinstance(app.screen, ConfigScreen)
            assert app.screen.last_verdict.startswith("daemon.poll_interval_s: draft refused")
            assert "poll_interval_s = 9.0" in (seeded.root / "sbxloop.toml").read_text()

            # A bool is two choices, applied as soon as one is picked.
            _open_key(app.screen, "keep_sandboxes")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ValueScreen)
            assert app.screen.spec.choices == ("true", "false")
            app.screen.query_one("#value-choice", Select).value = "true"
            await pilot.pause(2.0)
            assert "keep_sandboxes = true" in (seeded.root / "sbxloop.toml").read_text()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause(1.0)

    drive(scenario)


def test_a_repo_entry_is_addressable_key_by_key(seeded: SbxloopHome, hermetic: None) -> None:
    """`[[github.repos]]` used to print as one blob. Enter on a repository
    narrows the view to its keys, and each one edits on its own."""
    (seeded.root / "sbxloop.toml").write_text(
        '[[github.repos]]\nrepo = "o/r"\n\n[[github.repos]]\nrepo = "o/s"\n'
    )

    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(1.5)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            assert screen.flat["github.repos[1].repo"] == "o/s"
            table = screen.query_one("#resolved", ConsoleTable)
            row = table.get_row_at(table.get_row_index("github.repos[1].repo"))
            assert str(row[2]) == "sbxloop.toml", "a leaf inherits the array's layer"

            # The Repos tab is a way in: Enter narrows to that entry.
            repos = screen.query_one("#repos", ConsoleTable)
            repos.focus()
            repos.move_cursor(row=1)
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert screen.filter_text == "github.repos[1]"
            assert screen.query_one("#tabs", TabbedContent).active == "resolved-pane"
            assert 0 < table.row_count < len(screen.flat)

            _open_key(screen, "github.repos[1].deliver_base")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ValueScreen)
            app.screen.query_one("#value-text", Input).value = "develop"
            await pilot.press("enter")
            await pilot.pause(2.0)
            saved = (seeded.root / "sbxloop.toml").read_text()
            assert saved.count("[[github.repos]]") == 2
            assert 'repo = "o/s"\ndeliver_base = "develop"' in saved
            assert 'repo = "o/r"\n' in saved, "the other entry is untouched"

    drive(scenario)


def test_unsetting_a_key_says_what_answers_instead(seeded: SbxloopHome, hermetic: None) -> None:
    (seeded.root / "sbxloop.toml").write_text('[sandbox]\ntemplate = "custom"\n')

    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(1.5)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            _open_key(screen, "sandbox.template")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ValueScreen) and app.screen.can_unset
            await pilot.press("ctrl+u")
            await pilot.pause(2.0)
            assert "template" not in (seeded.root / "sbxloop.toml").read_text()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause(1.0)
            assert isinstance(app.screen, ConfigScreen)
            assert "unset here; it now comes from its default" in app.screen.last_verdict

    drive(scenario)


def test_a_key_the_environment_also_sets_is_written_and_flagged(
    seeded: SbxloopHome, hermetic: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is written — the operator asked — but a value the
    environment overrides must not look applied when it is not."""
    monkeypatch.setenv("SBXLOOP_DAEMON__POLL_INTERVAL_S", "5.0")
    (seeded.root / "sbxloop.toml").write_text("[daemon]\npoll_interval_s = 7.0\n")

    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(1.5)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            _open_key(screen, "daemon.poll_interval_s")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ValueScreen)
            app.screen.query_one("#value-text", Input).value = "9.0"
            await pilot.press("enter")
            await pilot.pause(2.0)
            assert "poll_interval_s = 9.0" in (seeded.root / "sbxloop.toml").read_text()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause(1.0)
            assert isinstance(app.screen, ConfigScreen)
            assert "env sets it too and wins" in app.screen.last_verdict
            assert "5.0" in app.screen.last_verdict

    drive(scenario)


def test_a_key_can_be_added_by_path(seeded: SbxloopHome, hermetic: None) -> None:
    """Nothing to select for a key the config has never had: `a` takes the
    dotted path, and the same dialog takes the value."""
    (seeded.root / "sbxloop.toml").write_text("[daemon]\npoll_interval_s = 7.0\n")

    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(1.5)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            screen.query_one("#resolved", ConsoleTable).focus()
            await pilot.press("a")
            await pilot.pause(0.5)
            assert isinstance(app.screen, TextPromptScreen)
            app.screen.query_one("#text", Input).value = "sandbox.env.RAILS_ENV"
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ValueScreen)
            assert not app.screen.in_file
            app.screen.query_one("#value-text", Input).value = "test"
            await pilot.press("enter")
            await pilot.pause(2.0)
            assert 'RAILS_ENV = "test"' in (seeded.root / "sbxloop.toml").read_text()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause(1.0)
            assert isinstance(app.screen, ConfigScreen)
            assert app.screen.flat["sandbox.env.RAILS_ENV"] == "test"

    drive(scenario)


def test_read_only_console_cannot_edit_a_key(seeded: SbxloopHome, hermetic: None) -> None:
    (seeded.root / "sbxloop.toml").write_text("[daemon]\npoll_interval_s = 7.0\n")

    async def scenario() -> None:
        app = make_app(seeded, read_only=True, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(1.5)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            _open_key(screen, "daemon.poll_interval_s")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ConfigScreen), "no dialog for a console that cannot write"
            await pilot.press("a")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ConfigScreen)
            assert (seeded.root / "sbxloop.toml").read_text() == "[daemon]\npoll_interval_s = 7.0\n"

    drive(scenario)


def test_a_key_edited_over_a_hand_edited_draft_is_confirmed(
    seeded: SbxloopHome, hermetic: None
) -> None:
    """The value dialog is the confirmation for the key it edited — not for
    whatever else the operator had typed into the draft. That save lands
    under the typed tier, everything in the buffer named."""
    (seeded.root / "sbxloop.toml").write_text("[daemon]\npoll_interval_s = 7.0\n")

    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(1.5)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            screen.query_one("#editor", ConfigEditor).load_text(
                "[daemon]\npoll_interval_s = 7.0\nmax_runs_per_day = 3\n"
            )
            _open_key(screen, "daemon.poll_interval_s")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ValueScreen)
            app.screen.query_one("#value-text", Input).value = "9.0"
            await pilot.press("enter")
            await pilot.pause(2.0)
            assert isinstance(app.screen, TypedConfirmScreen)
            app.screen.query_one("#typed", Input).value = "save"
            await pilot.press("enter")
            await pilot.pause(2.0)
            saved = (seeded.root / "sbxloop.toml").read_text()
            assert "poll_interval_s = 9.0" in saved and "max_runs_per_day = 3" in saved
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")

    drive(scenario)


def test_a_key_the_environment_owns_is_refused(seeded: SbxloopHome, hermetic: None) -> None:
    """`home` is resolved after every layer: a file that sets it changes
    nothing, so the dialog says so instead of writing a dead line."""
    (seeded.root / "sbxloop.toml").write_text("[daemon]\npoll_interval_s = 7.0\n")

    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(1.5)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            _open_key(screen, "home")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ConfigScreen), "no dialog for a key a file cannot set"
            assert (seeded.root / "sbxloop.toml").read_text() == "[daemon]\npoll_interval_s = 7.0\n"

    drive(scenario)
