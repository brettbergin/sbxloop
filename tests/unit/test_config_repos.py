"""Multi-repository GitHub configuration: parsing, back-compat, validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.config import GithubConfig, load_config
from sbxloop.errors import ConfigError


def _write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "sbxloop.toml").write_text(body)
    return tmp_path


def test_legacy_single_repo_normalises_to_one_entry(tmp_path: Path) -> None:
    _write(tmp_path, '[github]\nrepo = "o/r"\ndeliver_base = "develop"\n')
    cfg = load_config(cwd=tmp_path, env={})
    repos = cfg.github.repo_list()
    assert [r.repo for r in repos] == ["o/r"]
    assert repos[0].deliver_base == "develop"
    assert repos[0].enabled is True
    assert repos[0].token_env is None
    # Unchanged single-repo surface.
    assert cfg.github.repo == "o/r"
    assert cfg.github.deliver_base == "develop"
    assert cfg.github.enabled
    assert cfg.github.default_repo() is repos[0]


def test_multi_repo_parses_with_per_repo_settings(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "[[github.repos]]\n"
        'repo = "o/one"\n'
        'deliver_base = "main"\n'
        "\n"
        "[[github.repos]]\n"
        'repo = "o/two"\n'
        'deliver_base = "develop"\n'
        "enabled = false\n"
        'token_env = "GH_TOKEN_TWO"\n'
        'trigger_label = "sbxloop:go"\n'
        'labels = ["team:core"]\n',
    )
    cfg = load_config(cwd=tmp_path, env={})
    repos = cfg.github.repo_list()
    assert [r.repo for r in repos] == ["o/one", "o/two"]
    assert repos[1].enabled is False
    assert repos[1].token_env == "GH_TOKEN_TWO"
    assert repos[1].trigger_label == "sbxloop:go"
    assert repos[1].labels == ["team:core"]
    assert [r.repo for r in cfg.github.enabled_repos()] == ["o/one"]
    assert cfg.github.enabled
    # The sole *enabled* repo is the default.
    assert cfg.github.default_repo() is repos[0]


def test_find_repo_by_full_name_and_bare_name(tmp_path: Path) -> None:
    _write(
        tmp_path,
        '[[github.repos]]\nrepo = "o/one"\n\n[[github.repos]]\nrepo = "p/two"\n',
    )
    gh = load_config(cwd=tmp_path, env={}).github
    assert gh.find_repo("o/one").repo == "o/one"
    assert gh.find_repo("two").repo == "p/two"
    assert gh.find_repo("nope") is None
    # Two enabled repos → no unambiguous default.
    assert gh.default_repo() is None
    assert gh.find_repo(None) is None


def test_find_repo_bare_name_ambiguous(tmp_path: Path) -> None:
    _write(
        tmp_path,
        '[[github.repos]]\nrepo = "o/dup"\n\n[[github.repos]]\nrepo = "p/dup"\n',
    )
    gh = load_config(cwd=tmp_path, env={}).github
    assert gh.find_repo("dup") is None
    assert gh.find_repo("p/dup").repo == "p/dup"


def test_duplicate_repos_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        '[[github.repos]]\nrepo = "o/one"\n\n[[github.repos]]\nrepo = "O/One"\n',
    )
    with pytest.raises(ConfigError, match="duplicate repository"):
        load_config(cwd=tmp_path, env={})


def test_malformed_repo_entry_rejected(tmp_path: Path) -> None:
    _write(tmp_path, '[[github.repos]]\nrepo = "https://github.com/o/r"\n')
    with pytest.raises(ConfigError, match=r"github\.repos"):
        load_config(cwd=tmp_path, env={})


def test_mixing_legacy_repo_and_repos_rejected(tmp_path: Path) -> None:
    _write(tmp_path, '[github]\nrepo = "o/r"\n\n[[github.repos]]\nrepo = "o/two"\n')
    with pytest.raises(ConfigError, match="use one or the other"):
        load_config(cwd=tmp_path, env={})


def test_configured_github_without_any_repo_rejected(tmp_path: Path) -> None:
    _write(tmp_path, '[github]\ndeliver_base = "main"\nrepos = []\n')
    with pytest.raises(ConfigError, match="no repository is set"):
        load_config(cwd=tmp_path, env={})


def test_empty_github_section_still_disables_integration(tmp_path: Path) -> None:
    _write(tmp_path, "[github]\n")
    cfg = load_config(cwd=tmp_path, env={})
    assert cfg.github.repo is None
    assert not cfg.github.enabled
    assert cfg.github.repo_list() == []
    assert cfg.github.default_repo() is None


def test_repo_config_owner_and_name() -> None:
    gh = GithubConfig(repos=[{"repo": "owner/name"}])
    entry = gh.repo_list()[0]
    assert (entry.owner, entry.name) == ("owner", "name")
