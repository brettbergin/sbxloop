"""``sbxloop config migrate``: the legacy repository spelling rewritten in
place, every comment kept (#2255, PR 2).

The contract: ``[[github.repos]]`` entries and their ``[github.repos.*]``
sub-tables move under ``[[vcs.repos]]``; a single ``[github] repo`` with
its delivery settings becomes one entry; the rest of ``[github]`` and every
comment stay; a file already on the new spelling is left byte-identical; a
file that declares repositories under both is refused by name, never
merged; the command backs the previous file up like every other edit."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.config import load_config
from sbxloop.configedit.toml import ConfigWriteError, migrate_repos

LEGACY = """\
# operator note at the top
model = "auto"

[vcs]
kind = "gitlab"  # self-managed
api_url = "https://gitlab.example.com/api/v4"

[github]
api_url = "https://api.github.com"  # stays: the backend's own setting
bot_login = "loop[bot]"

# the first project
[[github.repos]]
repo = "group/one"
deliver_base = "main"  # trunk

[github.repos.agent_models]
build = "claude-opus-5"

[[github.repos]]
repo = "group/two"
enabled = false
"""


class TestMigrateRepos:
    def test_the_entries_move_under_vcs_with_their_comments(self, tmp_path: Path) -> None:
        text, moved = migrate_repos(LEGACY)
        assert moved == ["group/one", "group/two"]
        assert "[[github.repos]]" not in text and "[github.repos." not in text
        assert text.count("[[vcs.repos]]") == 2
        assert "[vcs.repos.agent_models]" in text
        for kept in (
            "# operator note at the top",
            "# the first project",
            'deliver_base = "main"  # trunk',
            'kind = "gitlab"  # self-managed',
            'api_url = "https://api.github.com"  # stays: the backend\'s own setting',
            'bot_login = "loop[bot]"',
        ):
            assert kept in text, kept
        (tmp_path / "sbxloop.toml").write_text(text)
        cfg = load_config(cwd=tmp_path, env={})
        assert [r.repo for r in cfg.vcs.repos] == ["group/one", "group/two"]
        assert cfg.vcs.repos[0].deliver_base == "main"
        assert cfg.vcs.repos[0].agent_models.build == "claude-opus-5"
        assert cfg.vcs.repos[1].enabled is False
        assert cfg.github.bot_login == "loop[bot]"
        assert cfg.vcs.kind == "gitlab"

    def test_a_single_github_repo_becomes_one_entry(self, tmp_path: Path) -> None:
        legacy = (
            "[github]\n"
            'repo = "acme/app"  # the one repo\n'
            'deliver_base = "develop"\n'
            "create_repo = true\n"
            'reviewers = ["lead"]\n'
        )
        text, moved = migrate_repos(legacy)
        assert moved == ["acme/app"]
        assert "[[vcs.repos]]" in text
        (tmp_path / "sbxloop.toml").write_text(text)
        cfg = load_config(cwd=tmp_path, env={})
        assert [r.repo for r in cfg.vcs.repos] == ["acme/app"]
        assert cfg.vcs.repos[0].deliver_base == "develop"
        assert cfg.vcs.repos[0].create_repo is True
        assert cfg.github.reviewers == ["lead"]
        # The section no longer declares the repository itself.
        assert 'repo = "acme/app"' in text and "[github]\nrepo" not in text

    def test_a_file_without_a_vcs_section_gets_one(self) -> None:
        text, moved = migrate_repos('[[github.repos]]\nrepo = "o/r"\n')
        assert moved == ["o/r"]
        assert "[[vcs.repos]]" in text

    def test_the_new_spelling_is_left_alone(self) -> None:
        current = '[vcs]\nkind = "github"\n\n[[vcs.repos]]\nrepo = "o/r"\n'
        assert migrate_repos(current) == (current, [])
        assert migrate_repos('model = "auto"\n') == ('model = "auto"\n', [])

    def test_migrating_twice_is_the_same_as_once(self) -> None:
        once, _ = migrate_repos(LEGACY)
        assert migrate_repos(once) == (once, [])

    def test_both_spellings_are_refused_not_merged(self) -> None:
        both = LEGACY + '\n[[vcs.repos]]\nrepo = "group/three"\n'
        with pytest.raises(ConfigWriteError, match=r"\[\[vcs\.repos\]\].*\[\[github\.repos\]\]"):
            migrate_repos(both)


class TestCommand:
    """``sbxloop config migrate`` on the home's file."""

    def test_rewrites_the_file_with_a_backup_and_says_what_moved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from sbxloop.cli.app import app
        from sbxloop.paths import SbxloopHome

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SBXLOOP_HOME", str(tmp_path / ".sbxloop"))
        home = SbxloopHome(tmp_path / ".sbxloop")
        home.ensure_tree()
        home.config_toml.write_text(LEGACY)
        result = CliRunner().invoke(app, ["config", "migrate"])
        assert result.exit_code == 0, result.output
        assert "group/one" in result.output and "group/two" in result.output
        assert "[[vcs.repos]]" in home.config_toml.read_text()
        backups = list(home.config_toml.parent.glob("sbxloop.toml.bak-*"))
        assert len(backups) == 1 and backups[0].read_text() == LEGACY
        again = CliRunner().invoke(app, ["config", "migrate"])
        assert again.exit_code == 0, again.output
        assert "nothing to migrate" in again.output
        assert len(list(home.config_toml.parent.glob("sbxloop.toml.bak-*"))) == 1

    def test_a_file_with_both_spellings_is_refused_and_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from sbxloop.cli.app import app
        from sbxloop.paths import SbxloopHome

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SBXLOOP_HOME", str(tmp_path / ".sbxloop"))
        home = SbxloopHome(tmp_path / ".sbxloop")
        home.ensure_tree()
        both = LEGACY + '\n[[vcs.repos]]\nrepo = "group/three"\n'
        home.config_toml.write_text(both)
        result = CliRunner().invoke(app, ["config", "migrate"])
        assert result.exit_code == 2, result.output
        assert home.config_toml.read_text() == both


class TestEditorWritesTheCurrentSpelling:
    """An edit addressed as ``vcs.repos[i].<key>`` on a file still on the
    legacy spelling migrates the file first, so the write lands where the
    loader reads it instead of appending an empty second list."""

    def test_a_repo_key_edit_migrates_a_legacy_file(self, tmp_path: Path) -> None:
        from sbxloop.configedit import ConfigEditor
        from sbxloop.paths import SbxloopHome

        home = SbxloopHome(tmp_path / ".sbxloop")
        home.ensure_tree()
        home.config_toml.write_text(
            '[[github.repos]]\nrepo = "o/r"\n\n[[github.repos]]\nrepo = "o/s"\n'
        )
        editor = ConfigEditor(home, {"HOME": str(tmp_path)})
        change = editor.set("vcs.repos[1].deliver_base", "develop")
        assert change.ok, change.verdict.error
        assert change.note is not None and "[[vcs.repos]]" in change.note
        editor.commit(change)
        saved = home.config_toml.read_text()
        assert saved.count("[[vcs.repos]]") == 2 and "[[github.repos]]" not in saved
        assert 'repo = "o/s"\ndeliver_base = "develop"' in saved
        assert 'repo = "o/r"\n' in saved
        assert editor.describe("vcs.repos[1].deliver_base").in_file
