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


class TestNaming:
    """#621: what the loop calls its branches, commits and pull requests is
    the operator's, daemon-wide with per-repository overrides."""

    def test_defaults_are_what_the_loop_always_wrote(self, tmp_path: Path) -> None:
        cfg = load_config(cwd=tmp_path, env={})
        assert cfg.github.pr_title_template == "sbxloop: {title}"
        assert cfg.github.branch_prefix == "sbxloop/"
        assert cfg.github.commit_message_template.startswith("sbxloop run {run_id}")
        assert cfg.github.branch_prefix_for("o/r") == "sbxloop/"

    def test_per_repo_overrides_fold_into_the_effective_entry(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "[github]\n"
            'branch_prefix = "bot/"\n'
            'pr_title_template = "chore: {title}"\n'
            "[[github.repos]]\n"
            'repo = "o/one"\n'
            "[[github.repos]]\n"
            'repo = "o/two"\n'
            'branch_prefix = "sbx-"\n'
            'commit_message_template = "{title} ({run_id})"\n',
        )
        cfg = load_config(cwd=tmp_path, env={})
        one = cfg.github.effective_repo("o/one")
        two = cfg.github.effective_repo("o/two")
        assert one is not None and two is not None
        assert (one.branch_prefix, one.pr_title_template) == ("bot/", "chore: {title}")
        assert one.commit_message_template == cfg.github.commit_message_template
        assert two.branch_prefix == "sbx-"
        assert two.commit_message_template == "{title} ({run_id})"
        assert cfg.github.branch_prefix_for("o/two") == "sbx-"
        assert cfg.github.branch_prefix_for("o/one") == "bot/"
        assert cfg.github.branch_prefix_for(None) == "bot/"

    @pytest.mark.parametrize(
        ("key", "value", "match"),
        [
            ("pr_title_template", '"{titel}"', "unknown placeholder"),
            ("pr_title_template", '""', "empty"),
            ("commit_message_template", '"{run_id} {branch}"', "unknown placeholder"),
            ("branch_prefix", '""', "empty"),
            ("branch_prefix", '"sbx loop/"', "branch_prefix"),
            ("branch_prefix", '"/sbx/"', "branch_prefix"),
            ("branch_prefix", '"a..b/"', "branch_prefix"),
            ("branch_prefix", '"x.lock"', "branch_prefix"),
        ],
    )
    def test_bad_naming_is_rejected_at_load(
        self, tmp_path: Path, key: str, value: str, match: str
    ) -> None:
        _write(tmp_path, f'[github]\nrepo = "o/r"\n{key} = {value}\n')
        with pytest.raises(ConfigError, match=match):
            load_config(cwd=tmp_path, env={})

    def test_a_bad_per_repo_value_names_its_key(self, tmp_path: Path) -> None:
        _write(tmp_path, '[[github.repos]]\nrepo = "o/r"\npr_title_template = "{nope}"\n')
        with pytest.raises(ConfigError, match="pr_title_template"):
            load_config(cwd=tmp_path, env={})

    def test_bot_login_folds_per_repo_and_is_validated(self, tmp_path: Path) -> None:
        """#622: the operator's word on the loop's login, daemon-wide with
        per-repository overrides; unset everywhere is None, never ""."""
        _write(
            tmp_path,
            "[github]\n"
            'bot_login = "my-app[bot]"\n'
            "[[github.repos]]\n"
            'repo = "o/one"\n'
            "[[github.repos]]\n"
            'repo = "o/two"\n'
            'bot_login = "other-bot"\n',
        )
        cfg = load_config(cwd=tmp_path, env={})
        assert cfg.github.bot_login_for("o/one") == "my-app[bot]"
        assert cfg.github.bot_login_for("o/two") == "other-bot"
        assert cfg.github.bot_login_for(None) == "my-app[bot]"
        two = cfg.github.effective_repo("o/two")
        assert two is not None and two.bot_login == "other-bot"
        assert load_config(cwd=tmp_path, env={}).github.bot_login_for("o/three") == "my-app[bot]"

    def test_unset_bot_login_is_none(self, tmp_path: Path) -> None:
        _write(tmp_path, '[github]\nrepo = "o/r"\n')
        cfg = load_config(cwd=tmp_path, env={})
        assert cfg.github.bot_login is None
        assert cfg.github.bot_login_for("o/r") is None

    @pytest.mark.parametrize("value", ['""', '"-leading"', '"has space"', '"foo[bot]x"', '"a--b"'])
    def test_a_bad_bot_login_is_rejected_at_load(self, tmp_path: Path, value: str) -> None:
        _write(tmp_path, f'[github]\nrepo = "o/r"\nbot_login = {value}\n')
        with pytest.raises(ConfigError, match="bot_login"):
            load_config(cwd=tmp_path, env={})
        _write(tmp_path, f'[[github.repos]]\nrepo = "o/r"\nbot_login = {value}\n')
        with pytest.raises(ConfigError, match="bot_login"):
            load_config(cwd=tmp_path, env={})

    def test_placeholders_may_repeat_and_use_format_spec(self, tmp_path: Path) -> None:
        _write(
            tmp_path, '[github]\nrepo = "o/r"\npr_title_template = "[{repo}] {title} {title!s}"\n'
        )
        assert load_config(cwd=tmp_path, env={}).github.pr_title_template.startswith("[{repo}]")


def test_reviewers_and_review_notify_fall_back_per_repo(tmp_path: Path) -> None:
    """#675: a repo entry's own `reviewers`/`review_notify` win; an entry
    without them inherits `[github] reviewers` / `[landing] review_notify`."""
    _write(
        tmp_path,
        "[github]\n"
        'reviewers = ["alice", "o/reviewers"]\n'
        "\n"
        "[[github.repos]]\n"
        'repo = "o/one"\n'
        "\n"
        "[[github.repos]]\n"
        'repo = "o/two"\n'
        'reviewers = ["bob"]\n'
        'review_notify = ["u2"]\n'
        "\n"
        "[landing]\n"
        'review_notify = ["u1"]\n',
    )
    cfg = load_config(cwd=tmp_path, env={})
    assert cfg.github.reviewers_for("o/one") == ["alice", "o/reviewers"]
    assert cfg.github.reviewers_for("o/two") == ["bob"]
    assert cfg.review_notify_for("o/one") == ["u1"]
    assert cfg.review_notify_for("o/two") == ["u2"]
    # An empty per-repo list is a real override: request nobody, ping nobody.
    _write(
        tmp_path,
        '[github]\nreviewers = ["alice"]\n\n[[github.repos]]\nrepo = "o/one"\nreviewers = []\n'
        'review_notify = []\n\n[landing]\nreview_notify = ["u1"]\n',
    )
    cfg = load_config(cwd=tmp_path, env={})
    assert cfg.github.reviewers_for("o/one") == []
    assert cfg.review_notify_for("o/one") == []


def test_review_wait_timings_must_be_positive(tmp_path: Path) -> None:
    _write(tmp_path, '[[github.repos]]\nrepo = "o/one"\n\n[landing]\nreview_wait_s = 0\n')
    with pytest.raises(Exception, match="review_wait_s"):
        load_config(cwd=tmp_path, env={})
    _write(tmp_path, '[[github.repos]]\nrepo = "o/one"\n\n[landing]\nreview_poll_interval_s = -1\n')
    with pytest.raises(Exception, match="review_poll_interval_s"):
        load_config(cwd=tmp_path, env={})
