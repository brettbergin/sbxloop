"""The Config screen: the resolved keys with their sources, the policy,
the repositories — and editing any of it one key at a time, validated by
the real loader, saved with a backup, the restart offered. There is no
file editor here, on purpose."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Input, Select, TabbedContent, TabPane, TextArea

from sbxloop.cli.policyview import policy_view
from sbxloop.config import Config
from sbxloop.paths import SbxloopHome
from sbxloop.tui import configkeys, configtoml
from sbxloop.tui.configedit import (
    FILE_LAYER,
    config_path,
    read_text,
    save_text,
    validate_text,
)
from sbxloop.tui.screens.config import ConfigScreen, flatten_config
from sbxloop.tui.screens.configvalue import ValueScreen
from sbxloop.tui.screens.modals import ConfirmScreen, TextPromptScreen
from sbxloop.tui.widgets.panel import TextPanel
from sbxloop.tui.widgets.tables import ConsoleTable
from tests.unit.tui.conftest import FakeCtl, FakeRunner, drive, live_status, make_app

REFRESH: dict[str, Any] = {"refresh_s": 3.0}


def _seed_config(home: SbxloopHome, text: str) -> None:
    """Write the operator config where the loader reads it: the home's
    ``config/sbxloop.toml``, not a file in some working directory."""
    home.config_toml.parent.mkdir(parents=True, exist_ok=True)
    home.config_toml.write_text(text)


def _config_text(home: SbxloopHome) -> str:
    return home.config_toml.read_text()


@pytest.fixture
def hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No user config layer from the developer's own ~/.config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("SBXLOOP_DAEMON__POLL_INTERVAL_S", raising=False)


def test_configedit_edits_the_homes_operator_config(tmp_path: Path, hermetic: None) -> None:
    """The file is the home's `config/sbxloop.toml` — what init writes and
    the loader reads from any directory — never a `sbxloop.toml` in
    whatever directory something happened to be started in."""
    home = SbxloopHome(tmp_path / "home")
    path = config_path(home)
    assert path == home.root / "config" / "sbxloop.toml"
    assert "[daemon]" in read_text(path)[0], "no file yet: the commented example"
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg-config"), "HOME": str(tmp_path)}
    assert validate_text("[daemon]\npoll_interval_s = 7.0\n", home=home, env=env).ok
    broken = validate_text("[daemon\n", home=home, env=env)
    assert not broken.ok and "draft refused" in broken.text
    unknown = validate_text("[daemon]\nno_such_key = 1\n", home=home, env=env)
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


def test_a_draft_is_validated_as_the_homes_own_layer(tmp_path: Path, hermetic: None) -> None:
    """The draft stands in for the home config layer, so what it says is
    what the verdict resolves — and the home it is validated against is the
    console's, never whichever one the ambient environment names."""
    home = SbxloopHome(tmp_path / "home")
    home.config_toml.parent.mkdir(parents=True)
    home.config_toml.write_text('model = "on-disk"\n')
    other = tmp_path / "elsewhere"
    (other / "config").mkdir(parents=True)
    (other / "config" / "sbxloop.toml").write_text('model = "the-wrong-home"\n')
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg-config"), "HOME": str(tmp_path)}

    verdict = validate_text(
        'model = "drafted"\n', home=home, env={**env, "SBXLOOP_HOME": str(other)}
    )
    assert verdict.ok and verdict.config is not None
    assert verdict.config.model == "drafted", "the draft replaces the file, unread"
    assert verdict.sources["model"] == FILE_LAYER
    assert home.config_toml.read_text() == 'model = "on-disk"\n', "nothing is written"


def test_flatten_and_policy_view_are_the_cli_folds() -> None:
    config = Config.model_validate(
        {"home": "/tmp/x", "github": {"repo": "o/r"}, "policy": {"allow": ["*.example.com"]}}
    )
    flat = flatten_config(config)
    assert flat["github.repo"] == "o/r" and "policy.allow" in flat
    view = policy_view(config)
    assert view.allow == "*.example.com" and view.github is not None
    assert "github.com" in view.baseline and view.phases[0][0] == "decompose"


def test_config_screen_resolves_filters_and_shows_the_policy(
    seeded: SbxloopHome, hermetic: None
) -> None:
    _seed_config(seeded, "[daemon]\npoll_interval_s = 7.0\n")

    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(1.5)
            assert isinstance(app.screen, ConfigScreen)
            table = app.screen.query_one("#resolved", ConsoleTable)
            row = table.get_row_at(table.get_row_index("daemon.poll_interval_s"))
            assert row[1] == "7.0" and str(row[2]) == FILE_LAYER
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
            status = app.screen.query_one("#file-status", TextPanel).content_text
            assert str(seeded.config_toml) in status

    drive(scenario)


def test_the_console_has_no_file_editor(seeded: SbxloopHome, hermetic: None) -> None:
    """Handing the operator a file to find a line in is the thing this
    screen replaced: there is no draft buffer, no `$EDITOR` hand-off and no
    whole-file save, and the keys that drove them do nothing."""
    _seed_config(seeded, "[daemon]\npoll_interval_s = 7.0\n")

    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(1.5)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            assert [pane.id for pane in screen.query(TabPane)] == [
                "resolved-pane",
                "policy-pane",
                "repos-pane",
            ], "no Edit tab"
            assert not screen.query(TextArea), "no draft buffer anywhere on the screen"
            runner = app.deps.runner
            assert isinstance(runner, FakeRunner)
            for key in ("i", "V", "W", "E", "L", "ctrl+s"):
                await pilot.press(key)
                await pilot.pause(0.2)
            assert isinstance(app.screen, ConfigScreen), "none of them opens anything"
            assert not runner.interactive_calls, "nothing is handed to $EDITOR"
            assert _config_text(seeded) == "[daemon]\npoll_interval_s = 7.0\n"

    drive(scenario)


def test_a_save_that_cannot_be_written_offers_no_restart(
    seeded: SbxloopHome, hermetic: None
) -> None:
    _seed_config(seeded, "[daemon]\npoll_interval_s = 7.0\n")

    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(1.5)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)

            def refuse(*_a: object, **_k: object) -> None:
                raise OSError("read-only file system")

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("sbxloop.tui.actions.save_text", refuse)
                _open_key(screen, "daemon.poll_interval_s")
                await pilot.press("enter")
                await pilot.pause(0.5)
                assert isinstance(app.screen, ValueScreen)
                app.screen.query_one("#value-text", Input).value = "8.0"
                await pilot.press("enter")
                await pilot.pause(2.0)
            assert isinstance(app.screen, ConfigScreen), "no restart prompt after a failed save"
            assert _config_text(seeded) == "[daemon]\npoll_interval_s = 7.0\n"

    drive(scenario)


def test_a_saved_key_keeps_a_backup(seeded: SbxloopHome, hermetic: None) -> None:
    _seed_config(seeded, "[daemon]\npoll_interval_s = 7.0\n")

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
            assert _config_text(seeded) == "[daemon]\npoll_interval_s = 9.0\n"
            backups = list(seeded.config.glob("sbxloop.toml.bak-*"))
            assert len(backups) == 1 and "7.0" in backups[0].read_text()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause(1.0)
            assert isinstance(app.screen, ConfigScreen)
            table = app.screen.query_one("#resolved", ConsoleTable)
            assert table.get_row_at(table.get_row_index("daemon.poll_interval_s"))[1] == "9.0"

    drive(scenario)


def test_the_editor_is_the_homes_config_wherever_the_console_ran(
    seeded: SbxloopHome, hermetic: None, tmp_path: Path
) -> None:
    """A console started in a checkout that carries its own sbxloop.toml
    still edits — and still resolves — the home's operator config. The two
    used to disagree: the editor followed the daemon's directory while the
    resolved view followed the console's, so a save landed in a file this
    screen never read (#818)."""
    elsewhere = tmp_path / "a-checkout"
    elsewhere.mkdir()
    (elsewhere / "sbxloop.toml").write_text('[sandbox]\ngate_command = "make check"\n')
    _seed_config(seeded, "[daemon]\npoll_interval_s = 3.0\n")

    async def scenario() -> None:
        app = make_app(
            seeded, cwd=elsewhere, ctl=FakeCtl(live_status(cwd=str(elsewhere))), **REFRESH
        )
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(2.0)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            assert screen.path == seeded.config_toml
            status = screen.query_one("#file-status", TextPanel).content_text
            assert str(seeded.config_toml) in status
            assert screen.flat["daemon.poll_interval_s"] == 3.0
            assert screen.flat["sandbox.gate_command"] is None, (
                "the checkout's own sbxloop.toml is project config, not the operator's"
            )

    drive(scenario)


def test_a_sbxloop_toml_in_the_home_is_named_as_shadowing(
    seeded: SbxloopHome, hermetic: None
) -> None:
    """Consoles before this fix wrote edits into `<home>/sbxloop.toml`,
    which the loader applies *over* the operator config. The screen names
    that file so the split brain is visible instead of silent."""
    _seed_config(seeded, "[daemon]\npoll_interval_s = 3.0\n")
    (seeded.root / "sbxloop.toml").write_text("[daemon]\npoll_interval_s = 99.0\n")

    async def scenario() -> None:
        app = make_app(seeded, **REFRESH)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(2.0)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            status = screen.query_one("#file-status", TextPanel).content_text
            assert "sbxloop.toml sits in the home" in status
            # And an edit to a key it holds is written, then flagged.
            _open_key(screen, "daemon.poll_interval_s")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ValueScreen)
            app.screen.query_one("#value-text", Input).value = "5.0"
            await pilot.press("enter")
            await pilot.pause(2.0)
            assert "poll_interval_s = 5.0" in _config_text(seeded)
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause(1.0)
            assert isinstance(app.screen, ConfigScreen)
            assert "sets it too and wins" in app.screen.last_verdict
            assert "99.0" in app.screen.last_verdict

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
    _seed_config(seeded, "# why the loop looks so often\n[daemon]\npoll_interval_s = 7.0\n")

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
            saved = _config_text(seeded)
            assert "# why the loop looks so often" in saved, "the comments survive"
            assert "poll_interval_s = 9.0" in saved
            assert isinstance(app.screen, ConfirmScreen), "the restart is offered"
            await pilot.press("n")
            await pilot.pause(1.0)
            assert isinstance(app.screen, ConfigScreen)
            table = app.screen.query_one("#resolved", ConsoleTable)
            assert table.get_row_at(table.get_row_index("daemon.poll_interval_s"))[1] == "9.0"

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
            assert "poll_interval_s = 9.0" in _config_text(seeded)

            # A bool is two choices, applied as soon as one is picked.
            _open_key(app.screen, "keep_sandboxes")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ValueScreen)
            assert app.screen.spec.choices == ("true", "false")
            app.screen.query_one("#value-choice", Select).value = "true"
            await pilot.pause(2.0)
            assert "keep_sandboxes = true" in _config_text(seeded)
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause(1.0)

    drive(scenario)


def test_a_repo_entry_is_addressable_key_by_key(seeded: SbxloopHome, hermetic: None) -> None:
    """`[[github.repos]]` used to print as one blob. Enter on a repository
    narrows the view to its keys, and each one edits on its own."""
    _seed_config(seeded, '[[github.repos]]\nrepo = "o/r"\n\n[[github.repos]]\nrepo = "o/s"\n')

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
            assert str(row[2]) == FILE_LAYER, "a leaf inherits the array's layer"

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
            saved = _config_text(seeded)
            assert saved.count("[[github.repos]]") == 2
            assert 'repo = "o/s"\ndeliver_base = "develop"' in saved
            assert 'repo = "o/r"\n' in saved, "the other entry is untouched"

    drive(scenario)


def test_unsetting_a_key_says_what_answers_instead(seeded: SbxloopHome, hermetic: None) -> None:
    _seed_config(seeded, '[sandbox]\ntemplate = "custom"\n')

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
            assert "template" not in _config_text(seeded)
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
    _seed_config(seeded, "[daemon]\npoll_interval_s = 7.0\n")

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
            assert "poll_interval_s = 9.0" in _config_text(seeded)
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
    _seed_config(seeded, "[daemon]\npoll_interval_s = 7.0\n")

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
            assert 'RAILS_ENV = "test"' in _config_text(seeded)
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause(1.0)
            assert isinstance(app.screen, ConfigScreen)
            assert app.screen.flat["sandbox.env.RAILS_ENV"] == "test"

    drive(scenario)


def test_read_only_console_cannot_edit_a_key(seeded: SbxloopHome, hermetic: None) -> None:
    _seed_config(seeded, "[daemon]\npoll_interval_s = 7.0\n")

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
            assert _config_text(seeded) == "[daemon]\npoll_interval_s = 7.0\n"

    drive(scenario)


def test_a_key_the_environment_owns_is_refused(seeded: SbxloopHome, hermetic: None) -> None:
    """`home` is resolved after every layer: a file that sets it changes
    nothing, so the dialog says so instead of writing a dead line."""
    _seed_config(seeded, "[daemon]\npoll_interval_s = 7.0\n")

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
            assert _config_text(seeded) == "[daemon]\npoll_interval_s = 7.0\n"

    drive(scenario)


def test_an_edit_shows_up_in_the_resolved_view_at_once(
    seeded: SbxloopHome, hermetic: None, tmp_path: Path
) -> None:
    """The bug this fixes (#818). The editor was anchored to the daemon's
    directory while the resolved view resolved from the console's, so on a
    real home install the save landed in a file the screen never read: the
    row kept the old value however often it was refreshed, and editing the
    key again answered "already says that" because the *draft* had changed
    and nothing else had."""
    _seed_config(seeded, 'model = "claude-sonnet-5"\n')
    launched_from = tmp_path / "somewhere-else"
    launched_from.mkdir()

    async def scenario() -> None:
        app = make_app(
            seeded,
            cwd=launched_from,
            ctl=FakeCtl(live_status(cwd=str(seeded.root))),
            **REFRESH,
        )
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("7")
            await pilot.pause(2.0)
            screen = app.screen
            assert isinstance(screen, ConfigScreen)
            assert screen.flat["model"] == "claude-sonnet-5"

            _open_key(screen, "model")
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, ValueScreen)
            app.screen.query_one("#value-text", Input).value = "claude-haiku-4-5-20251001"
            await pilot.press("enter")
            await pilot.pause(2.0)
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause(1.5)

            # The file the daemon reads carries it …
            assert 'model = "claude-haiku-4-5-20251001"' in _config_text(seeded)
            assert not (seeded.root / "sbxloop.toml").exists(), "nothing written beside the home"
            # … and the row says so without another keystroke.
            assert isinstance(app.screen, ConfigScreen)
            assert app.screen.flat["model"] == "claude-haiku-4-5-20251001"
            table = app.screen.query_one("#resolved", ConsoleTable)
            assert (
                table.get_row_at(table.get_row_index("model"))[1] == "'claude-haiku-4-5-20251001'"
            )

    drive(scenario)
