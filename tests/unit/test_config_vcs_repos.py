"""``[[vcs.repos]]``: the repositories sbxloop works with are declared under
the forge section, whatever the forge (#2255).

The contract: ``[[vcs.repos]]`` is the one place a repository is declared;
``[[github.repos]]`` and the single ``[github] repo`` are the legacy spelling
and keep loading, folded into the same list with one notice; a file that
declares repositories under both, differently, is refused by name; the
GitHub section's own view of the list stays in step with it, including
across a run's narrowing and its persisted round trip, so nothing
downstream can read two different repository lists off one config."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from sbxloop.config import DEFAULT_CONFIG_LOCKED, Config, GithubConfig, load_config
from sbxloop.configedit.docs import doc_for
from sbxloop.configedit.keys import is_model_key
from sbxloop.daemon.configpolicy import locked_by
from sbxloop.errors import ConfigError

TWO = (
    "[vcs]\n"
    'kind = "github"\n'
    "\n"
    "[[vcs.repos]]\n"
    'repo = "o/one"\n'
    'deliver_base = "main"\n'
    "\n"
    "[[vcs.repos]]\n"
    'repo = "o/two"\n'
    "enabled = false\n"
    'token_env = "GH_TOKEN_TWO"\n'
)


def _load(tmp_path: Path, body: str) -> Config:
    (tmp_path / "sbxloop.toml").write_text(body)
    return load_config(cwd=tmp_path, env={})


class TestDeclaredUnderVcs:
    def test_the_entries_load_and_every_surface_reads_the_same_list(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path, TWO)
        assert [r.repo for r in cfg.vcs.repos] == ["o/one", "o/two"]
        assert [r.repo for r in cfg.repo_list()] == ["o/one", "o/two"]
        assert [r.repo for r in cfg.enabled_repos()] == ["o/one"]
        assert cfg.vcs.repos[1].token_env == "GH_TOKEN_TWO"
        assert cfg.vcs.repos[0].deliver_base == "main"
        # The GitHub section is a view of the same list, not a second one.
        assert [r.repo for r in cfg.github.repo_list()] == ["o/one", "o/two"]
        assert cfg.github.enabled
        assert cfg.github.repo == "o/one"
        assert cfg.primary_repo == "o/one"
        default = cfg.default_repo()
        assert default is not None and default.repo == "o/one"
        assert cfg.github.default_repo() == default
        assert cfg.find_repo("two") is not None and cfg.find_repo("two").repo == "o/two"  # type: ignore[union-attr]
        assert cfg.github.find_repo("two") == cfg.find_repo("two")
        effective = cfg.effective_repo("o/two")
        assert effective is not None and effective.branch_prefix == cfg.github.branch_prefix
        assert cfg.multi_repo is False

    def test_the_forge_helpers_resolve_off_the_declared_list(self, tmp_path: Path) -> None:
        cfg = _load(
            tmp_path,
            '[vcs]\nkind = "gitlab"\napi_url = "https://gitlab.example.com/api/v4"\n\n'
            '[[vcs.repos]]\nrepo = "group/sub/project"\n\n'
            '[[vcs.repos]]\nrepo = "o/hub"\nkind = "github"\n',
        )
        assert cfg.vcs_kind_for("group/sub/project") == "gitlab"
        assert cfg.vcs_kind_for("hub") == "github"
        assert cfg.vcs_kinds() == ("gitlab", "github")
        assert cfg.vcs_token_env_for("group/sub/project") == "GITLAB_TOKEN"
        assert cfg.clone_url_for_repo("group/sub/project") == (
            "https://gitlab.example.com/group/sub/project"
        )
        assert cfg.clone_url_for_repo("o/hub") == "https://github.com/o/hub"

    def test_no_repository_anywhere_leaves_the_forge_off(self) -> None:
        cfg = Config()
        assert cfg.vcs.repos == [] and cfg.repo_list() == []
        assert not cfg.github.enabled and cfg.primary_repo is None
        assert cfg.default_repo() is None
        assert cfg.vcs.enabled is False

    def test_the_forge_is_on_once_a_repository_is_declared(self, tmp_path: Path) -> None:
        # `vcs.enabled` is the forge-neutral "a repository is configured"
        # every consumer reads (#2255); the GitHub section's answers the same.
        cfg = _load(tmp_path, TWO)
        assert cfg.vcs.enabled is True and cfg.github.enabled is True
        legacy = _load(tmp_path, '[github]\nrepo = "o/r"\n')
        assert legacy.vcs.enabled is True


class TestLegacySpelling:
    def test_github_repos_folds_into_the_vcs_list_with_one_notice(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            cfg = _load(
                tmp_path, '[[github.repos]]\nrepo = "o/one"\n\n[[github.repos]]\nrepo = "o/two"\n'
            )
        assert [r.repo for r in cfg.vcs.repos] == ["o/one", "o/two"]
        assert [r.repo for r in cfg.github.repo_list()] == ["o/one", "o/two"]
        notices = [r for r in caplog.records if "config.repos_legacy" in r.getMessage()]
        assert len(notices) == 1, caplog.text
        assert "[[vcs.repos]]" in notices[0].getMessage()

    def test_the_single_github_repo_folds_too(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path, '[github]\nrepo = "o/r"\ndeliver_base = "develop"\n')
        assert [r.repo for r in cfg.vcs.repos] == ["o/r"]
        assert cfg.vcs.repos[0].deliver_base == "develop"
        assert cfg.github.repo == "o/r"

    def test_the_new_spelling_raises_no_notice(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            _load(tmp_path, TWO)
        assert "config.repos_legacy" not in caplog.text

    def test_both_spellings_naming_different_repositories_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"\[\[vcs\.repos\]\]"):
            _load(tmp_path, TWO + '\n[[github.repos]]\nrepo = "o/three"\n')

    def test_a_model_built_from_the_legacy_section_reads_the_same(self) -> None:
        cfg = Config(github=GithubConfig(repos=[{"repo": "o/r"}]))  # type: ignore[list-item]
        assert [r.repo for r in cfg.vcs.repos] == ["o/r"]
        assert cfg.github.repo == "o/r"


class TestNarrowingKeepsTheTwoInStep:
    def test_for_repo_narrows_both_lists_and_round_trips(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path, TWO.replace("enabled = false\n", ""))
        assert cfg.multi_repo
        narrowed = cfg.for_repo("o/two", workspace=None)
        assert [r.repo for r in narrowed.vcs.repos] == ["o/two"]
        assert [r.repo for r in narrowed.github.repo_list()] == ["o/two"]
        assert narrowed.github.repo == "o/two" and narrowed.primary_repo == "o/two"
        assert narrowed.vcs.repos[0].workspace is None
        assert narrowed.multi_repo, "the deployment's shape survives narrowing"
        again = Config.model_validate(narrowed.model_dump())
        assert [r.repo for r in again.vcs.repos] == ["o/two"]
        assert [r.repo for r in again.github.repo_list()] == ["o/two"]
        assert again.multi_repo

    def test_a_replaced_github_section_updates_the_declared_list(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path, TWO)
        # How `sbxloop run --repo` and `sbxloop daemon --repo` rebuild the section.
        github = GithubConfig.model_validate(
            {**cfg.github.model_dump(), "repo": "x/y", "repos": []}
        )
        replaced = cfg.with_github(github)
        assert [r.repo for r in replaced.vcs.repos] == ["x/y"]
        assert replaced.github.repo == "x/y"
        assert Config.model_validate(replaced.model_dump()).primary_repo == "x/y"

    def test_a_config_persisted_before_the_change_still_loads(self) -> None:
        # A run's stored config from a release that only knew [[github.repos]].
        stored = Config(github=GithubConfig(repos=[{"repo": "o/r"}])).model_dump()  # type: ignore[list-item]
        stored["vcs"].pop("repos", None)
        again = Config.model_validate(stored)
        assert [r.repo for r in again.vcs.repos] == ["o/r"]


class TestValidationSpeaksTheNewName:
    def test_kind(self) -> None:
        with pytest.raises(ValueError, match=r"vcs\.repos\[\]\.kind must be one of"):
            Config.model_validate({"vcs": {"repos": [{"repo": "o/r", "kind": "sourcehut"}]}})

    def test_shape(self) -> None:
        with pytest.raises(ValueError, match=r"vcs\.repos\[\]\.repo must be owner/name"):
            Config.model_validate({"vcs": {"repos": [{"repo": "no-slash"}]}})

    def test_duplicates(self) -> None:
        with pytest.raises(ValueError, match=r"vcs\.repos contains duplicate repository"):
            Config.model_validate({"vcs": {"repos": [{"repo": "o/r"}, {"repo": "O/R"}]}})

    def test_the_forge_rule_on_paths(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            Config.model_validate({"vcs": {"repos": [{"repo": "a/b/c"}]}})
        Config.model_validate({"vcs": {"kind": "gitlab", "repos": [{"repo": "a/b/c"}]}})


class TestChatLocks:
    def test_the_forge_and_every_credential_name_stay_locked(self) -> None:
        for key in (
            "vcs.kind",
            "vcs.api_url",
            "vcs.token_env",
            "vcs.repos[1].token_env",
            "vcs.repos[0].kind",
        ):
            assert locked_by(key, DEFAULT_CONFIG_LOCKED) is not None, key
        assert locked_by("github.repos[1].token_env", DEFAULT_CONFIG_LOCKED) is not None

    def test_a_repositorys_delivery_settings_are_not(self) -> None:
        for key in ("vcs.repos[0].deliver_base", "vcs.repos[0].enabled", "vcs.repos[2].reviewers"):
            assert locked_by(key, DEFAULT_CONFIG_LOCKED) is None, key


class TestEditorAndDocs:
    def test_a_repositorys_models_are_live_keys(self) -> None:
        assert is_model_key("vcs.repos[1].agent_models.build")
        assert is_model_key("github.repos[1].agent_models.build"), "the legacy path still edits"

    def test_the_example_documents_the_new_spelling(self) -> None:
        assert doc_for("vcs.repos[1].enabled") == doc_for("vcs.repos.enabled")
        assert doc_for("vcs.repos.enabled") is not None
        assert doc_for("vcs.repos.agent_models.build") is not None
        assert doc_for("vcs.repos.openai.base_url") is not None
