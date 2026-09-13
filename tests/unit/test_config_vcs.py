"""``[vcs]``: which forge the repositories live on (#1009, #1013).

The section is the one config key an operator sets to point sbxloop at a
forge. It has to fail closed on a kind nobody implements by name, fold
its API root into the GitHub section every consumer reads, resolve per
repository, and read an unnamed ``[github]`` as the GitHub backend with
one notice rather than a warning."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from sbxloop.config import (
    DEFAULT_CONFIG_LOCKED,
    FORGE_TOKEN_ENVS,
    VCS_KINDS,
    Config,
    load_config,
)
from sbxloop.errors import ConfigError


def cfg(**doc: object) -> Config:
    return Config.model_validate(doc)


class TestKind:
    def test_github_is_the_default(self) -> None:
        assert cfg().vcs.kind == "github"
        assert cfg().vcs_kind_for() == "github"
        assert cfg().vcs_kinds() == ("github",)

    def test_the_three_kinds_load(self) -> None:
        for kind in VCS_KINDS:
            assert cfg(vcs={"kind": kind}).vcs.kind == kind

    def test_an_unknown_kind_fails_closed_naming_the_accepted_ones(self) -> None:
        with pytest.raises(ValueError, match=r"vcs\.kind must be one of github, gitlab, gitea"):
            cfg(vcs={"kind": "bitbucket"})
        with pytest.raises(ValueError, match=r"github\.repos\[\]\.kind must be one of"):
            cfg(github={"repos": [{"repo": "o/r", "kind": "sourcehut"}]})

    def test_a_repository_may_name_its_own_forge(self) -> None:
        config = cfg(
            vcs={"kind": "gitlab"},
            github={"repos": [{"repo": "o/a"}, {"repo": "o/b", "kind": "github"}]},
        )
        assert config.vcs_kind_for("o/a") == "gitlab"
        assert config.vcs_kind_for("o/b") == "github"
        assert config.vcs_kind_for("b") == "github", "by bare name too"
        assert config.vcs_kinds() == ("gitlab", "github")

    def test_a_disabled_repository_does_not_count(self) -> None:
        config = cfg(github={"repos": [{"repo": "o/a", "kind": "gitea", "enabled": False}]})
        assert config.vcs_kinds() == ("github",)

    def test_the_section_is_locked_from_the_concierge(self) -> None:
        assert "vcs" in DEFAULT_CONFIG_LOCKED
        assert "vcs" in cfg().concierge.config_locked


class TestApiUrl:
    def test_unset_leaves_github_alone(self) -> None:
        config = cfg(github={"repo": "o/r", "api_url": "https://ghe.example.com/api/v3"})
        assert config.vcs.api_url is None
        assert config.github.api_url == "https://ghe.example.com/api/v3"

    def test_a_vcs_root_fills_the_github_one(self) -> None:
        config = cfg(vcs={"api_url": "https://ghe.example.com/api/v3/"}, github={"repo": "o/r"})
        assert config.github.api_url == "https://ghe.example.com/api/v3"
        assert config.github.api_host == "ghe.example.com"

    def test_the_same_value_in_both_is_fine(self) -> None:
        config = cfg(
            vcs={"api_url": "https://ghe.example.com/api/v3"},
            github={"repo": "o/r", "api_url": "https://ghe.example.com/api/v3"},
        )
        assert config.github.api_url == "https://ghe.example.com/api/v3"

    def test_two_different_values_are_refused(self) -> None:
        with pytest.raises(ValueError, match="disagree"):
            cfg(
                vcs={"api_url": "https://one.example.com"},
                github={"repo": "o/r", "api_url": "https://two.example.com/api/v3"},
            )

    def test_another_forges_root_is_not_githubs(self) -> None:
        config = cfg(vcs={"kind": "gitlab", "api_url": "https://gitlab.example.com"})
        assert config.github.api_url == "https://api.github.com"

    def test_the_root_must_be_a_plain_https_url(self) -> None:
        with pytest.raises(ValueError, match=r"vcs\.api_url must be a plain https URL"):
            cfg(vcs={"api_url": "http://gitlab.example.com"})


class TestLoaderNotice:
    def test_an_unnamed_github_section_is_read_as_the_github_backend_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "sbxloop.toml").write_text('[github]\nrepo = "o/r"\n')
        with caplog.at_level(logging.INFO):
            config = load_config(tmp_path, env={"HOME": str(tmp_path)})
        assert config.vcs.kind == "github"
        notices = [r for r in caplog.records if "config.vcs_defaulted" in r.getMessage()]
        assert len(notices) == 1
        assert all(r.levelno < logging.WARNING for r in notices), "a notice, not a warning"

    def test_a_named_forge_needs_no_notice(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "sbxloop.toml").write_text('[vcs]\nkind = "github"\n[github]\nrepo = "o/r"\n')
        with caplog.at_level(logging.INFO):
            load_config(tmp_path, env={"HOME": str(tmp_path)})
        assert not [r for r in caplog.records if "config.vcs_defaulted" in r.getMessage()]

    def test_an_unknown_kind_in_a_file_is_a_config_error(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text('[vcs]\nkind = "svn"\n')
        with pytest.raises(ConfigError, match="github, gitlab, gitea"):
            load_config(tmp_path, env={"HOME": str(tmp_path)})


class TestBotLogins:
    """The automated reviewers on a forge without a bot signal (#1021):
    a `[vcs]` default and a per-repository override, empty by default."""

    def test_empty_by_default_so_every_reviewer_is_human(self) -> None:
        assert cfg().bot_logins_for() == ()
        assert cfg(github={"repos": [{"repo": "o/a"}]}).bot_logins_for("o/a") == ()

    def test_the_section_and_then_the_entry(self) -> None:
        config = cfg(
            vcs={"kind": "gitea", "api_url": "https://gt.example/api/v1", "bot_logins": ["ci-bot"]},
            github={"repos": [{"repo": "o/a"}, {"repo": "o/b", "bot_logins": ["renovate"]}]},
        )
        assert config.bot_logins_for("o/a") == ("ci-bot",)
        assert config.bot_logins_for("o/b") == ("renovate",)

    def test_an_empty_login_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"vcs\.bot_logins must not contain an empty login"):
            cfg(vcs={"kind": "gitea", "api_url": "https://gt.example/api/v1", "bot_logins": [" "]})


class TestTokenEnv:
    """The variable the forge token is read from is a name that travels
    through config; the value only ever lives in secrets.env."""

    def test_github_has_no_single_token_variable(self) -> None:
        config = cfg(github={"repos": [{"repo": "o/a"}]})
        assert config.vcs_token_env_for("o/a") is None, "GH_TOKEN/GITHUB_TOKEN or the App"
        assert cfg().vcs_token_env_for() is None

    def test_the_other_forges_default_to_their_own_variable(self) -> None:
        assert FORGE_TOKEN_ENVS == {"gitlab": "GITLAB_TOKEN", "gitea": "GITEA_TOKEN"}
        config = cfg(
            vcs={"kind": "gitlab"},
            github={"repos": [{"repo": "o/a"}, {"repo": "o/b", "kind": "gitea"}]},
        )
        assert config.vcs_token_env_for("o/a") == "GITLAB_TOKEN"
        assert config.vcs_token_env_for("o/b") == "GITEA_TOKEN"

    def test_the_section_and_then_the_entry_override_the_default(self) -> None:
        config = cfg(
            vcs={"kind": "gitlab", "token_env": "GL_MAIN"},
            github={"repos": [{"repo": "o/a"}, {"repo": "o/b", "token_env": "GL_OTHER"}]},
        )
        assert config.vcs_token_env_for("o/a") == "GL_MAIN"
        assert config.vcs_token_env_for("o/b") == "GL_OTHER"
        github_entry = cfg(github={"repos": [{"repo": "o/c", "token_env": "GH_TWO"}]})
        assert github_entry.vcs_token_env_for("o/c") == "GH_TWO"

    def test_a_name_that_is_not_a_variable_is_refused(self) -> None:
        with pytest.raises(
            ValueError, match=r"vcs\.token_env must be an environment variable name"
        ):
            cfg(vcs={"token_env": "glpat-abc123"})
