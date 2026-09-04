from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from sbxloop.config import Config, load_config, load_config_with_sources
from sbxloop.errors import ConfigError


def test_defaults(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env={})
    assert config == Config()
    assert config.model == "auto"
    assert config.worker_transport == "stream"
    assert config.secret_strategy == "proxy"
    assert config.budgets.max_revisions_per_task == 2


def test_artifacts_exclude_default_and_override(tmp_path: Path) -> None:
    default = load_config(cwd=tmp_path, env={}).artifacts.exclude
    assert default[:2] == [".git", ".sbxloop"]
    assert {"node_modules", "__pycache__", ".venv", "target", "obj"} <= set(default)
    # An override replaces the default wholesale — it does not extend it.
    (tmp_path / "sbxloop.toml").write_text('[artifacts]\nexclude = [".git", "node_modules"]\n')
    config = load_config(cwd=tmp_path, env={})
    assert config.artifacts.exclude == [".git", "node_modules"]


def test_init_template_exclude_matches_the_default(tmp_path: Path) -> None:
    """`sbxloop init` writes the exclude list out literally; a starter file
    that silently differs from the built-in default would be a trap."""
    from sbxloop.cli.app import DEFAULT_CONFIG_TOML

    (tmp_path / "sbxloop.toml").write_text(DEFAULT_CONFIG_TOML)
    written = load_config(cwd=tmp_path, env={}).artifacts.exclude
    assert set(written) == set(Config().artifacts.exclude)


def test_artifacts_harvest_mode_default_and_override(tmp_path: Path) -> None:
    assert load_config(cwd=tmp_path, env={}).artifacts.harvest_mode == "per-task"
    (tmp_path / "sbxloop.toml").write_text('[artifacts]\nharvest_mode = "final"\n')
    assert load_config(cwd=tmp_path, env={}).artifacts.harvest_mode == "final"


def test_artifacts_exclude_rejects_path_separators(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text('[artifacts]\nexclude = [".git/objects"]\n')
    with pytest.raises(ConfigError, match=r"artifacts\.exclude"):
        load_config(cwd=tmp_path, env={})


def test_sandbox_languages_default_is_python(tmp_path: Path) -> None:
    # Unset means "what Python has had since 0.4.0" — #140 must not change
    # provisioning for a run that never sets the key.
    config = load_config(cwd=tmp_path, env={})
    assert config.sandbox.languages == []
    assert config.sandbox.effective_languages == ("python",)


def test_sandbox_languages_normalizes_and_dedupes(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text(
        '[sandbox]\nlanguages = ["Python", "py", "  python3 "]\n'
    )
    config = load_config(cwd=tmp_path, env={})
    assert config.sandbox.languages == ["python"]
    assert config.sandbox.effective_languages == ("python",)


def test_sandbox_languages_rejects_unknown(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text('[sandbox]\nlanguages = ["cobol"]\n')
    with pytest.raises(ConfigError, match=r"unsupported sandbox\.languages"):
        load_config(cwd=tmp_path, env={})


def test_pyproject_layer(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.sbxloop]\nmodel = "gpt-5"\n[tool.sbxloop.budgets]\nmax_tasks = 5\n'
    )
    config = load_config(cwd=tmp_path, env={})
    assert config.model == "gpt-5"
    assert config.budgets.max_tasks == 5


def test_sbxloop_toml_overrides_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.sbxloop]\nmodel = "gpt-5"\napp_name = "a"\n')
    (tmp_path / "sbxloop.toml").write_text('model = "auto"\n')
    config = load_config(cwd=tmp_path, env={})
    assert config.model == "auto"  # sbxloop.toml wins
    assert config.app_name == "a"  # untouched keys survive from lower layers


def test_env_overrides_everything(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text('model = "file-model"\nkeep_sandboxes = false\n')
    env = {
        "SBXLOOP_MODEL": "env-model",
        "SBXLOOP_KEEP_SANDBOXES": "true",
        "SBXLOOP_BUDGETS__MAX_TASKS": "3",
        "SBXLOOP_BUDGETS__MAX_WALL_CLOCK_S": "60.5",
        "SBXLOOP_GITHUB__REPO": "brettbergin/sbxloop",
        "UNRELATED": "ignored",
    }
    config = load_config(cwd=tmp_path, env=env)
    assert config.model == "env-model"
    assert config.keep_sandboxes is True
    assert config.budgets.max_tasks == 3
    assert config.budgets.max_wall_clock_s == 60.5
    assert config.github.repo == "brettbergin/sbxloop"
    assert config.github.enabled


def test_workspace_isolation_default_and_validation(tmp_path: Path) -> None:
    assert load_config(cwd=tmp_path, env={}).sandbox.workspace_isolation == "auto"
    config = load_config(cwd=tmp_path, env={"SBXLOOP_SANDBOX__WORKSPACE_ISOLATION": "clone"})
    assert config.sandbox.workspace_isolation == "clone"
    with pytest.raises(ConfigError):
        load_config(cwd=tmp_path, env={"SBXLOOP_SANDBOX__WORKSPACE_ISOLATION": "yolo"})


def test_daemon_and_discord_sections(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env={})
    assert config.daemon.trigger_label == "sbxloop:run"
    assert config.daemon.max_runs_per_day == 12
    # #255: unattended posture — clone isolation, fetch refresh, no state
    # dir pin (resolved by daemon.paths at startup).
    assert config.daemon.workspace_isolation == "clone"
    assert config.daemon.refresh_workspace is True
    assert config.daemon.state_dir is None
    assert config.discord.enabled is False
    over = load_config(
        cwd=tmp_path,
        env={
            "SBXLOOP_DAEMON__MAX_RUNS_PER_DAY": "3",
            "SBXLOOP_DAEMON__WORKSPACE_ISOLATION": "in-place",
            "SBXLOOP_DAEMON__STATE_DIR": "/var/lib/sbxloop",
            "SBXLOOP_DISCORD__CHANNEL_ID": "123456789",
        },
    )
    assert over.daemon.max_runs_per_day == 3
    assert over.daemon.workspace_isolation == "in-place"
    assert over.daemon.state_dir == Path("/var/lib/sbxloop")
    assert over.discord.enabled is True and over.discord.channel_id == 123456789
    # [concierge]: on by default (effective only with [discord]), model falls
    # back to the top-level model, env overrides reach it.
    assert config.concierge.enabled is True and config.concierge.model is None
    assert config.concierge.timeout_s == 180.0 and config.concierge.session_turns == 40
    assert config.concierge.github_tools is True and config.concierge.create_issues is True
    over2 = load_config(
        cwd=tmp_path,
        env={"SBXLOOP_CONCIERGE__MODEL": "gpt-5", "SBXLOOP_CONCIERGE__TIMEOUT_S": "300"},
    )
    assert over2.concierge.model == "gpt-5" and over2.concierge.timeout_s == 300.0
    (tmp_path / "sbxloop.toml").write_text("[concierge]\ntimeout_s = 5\n")
    with pytest.raises(ConfigError):
        load_config(cwd=tmp_path, env={})
    assert config.daemon.completed_label == "sbxloop:completed"
    assert config.daemon.blocked_label == "sbxloop:blocked"
    assert config.github.deliver_closes is None
    (tmp_path / "sbxloop.toml").write_text(
        '[daemon]\ntrigger_label = "x"\nin_progress_label = "x"\n'
    )
    with pytest.raises(ConfigError, match="distinct"):
        load_config(cwd=tmp_path, env={})
    # blocked_label takes part in the lifecycle-label distinctness check
    (tmp_path / "sbxloop.toml").write_text('[daemon]\nblocked_label = "sbxloop:failed"\n')
    with pytest.raises(ConfigError, match="distinct"):
        load_config(cwd=tmp_path, env={})
    # completed_label does too, case-insensitively
    (tmp_path / "sbxloop.toml").write_text('[daemon]\ncompleted_label = "SBXLOOP:BLOCKED"\n')
    with pytest.raises(ConfigError, match="case-insensitively"):
        load_config(cwd=tmp_path, env={})
    (tmp_path / "sbxloop.toml").write_text('[daemon]\nblocked_label = " "\n')
    with pytest.raises(ConfigError, match="non-empty"):
        load_config(cwd=tmp_path, env={})
    # deliver_closes must be a positive issue number
    (tmp_path / "sbxloop.toml").write_text('[github]\nrepo = "o/r"\ndeliver_closes = 0\n')
    with pytest.raises(ConfigError):
        load_config(cwd=tmp_path, env={})
    # GitHub labels are case-insensitive: differing only by case is a collision
    (tmp_path / "sbxloop.toml").write_text(
        '[daemon]\ntrigger_label = "sbxloop:run"\nfailed_label = "SBXLOOP:RUN"\n'
    )
    with pytest.raises(ConfigError, match="case-insensitively"):
        load_config(cwd=tmp_path, env={})
    (tmp_path / "sbxloop.toml").write_text("[daemon]\nmax_runs_per_day = 0\n")
    with pytest.raises(ConfigError, match="max_runs_per_day"):
        load_config(cwd=tmp_path, env={})
    (tmp_path / "sbxloop.toml").write_text("[daemon]\npoll_interval_s = 0\n")
    with pytest.raises(ConfigError, match="poll_interval_s"):
        load_config(cwd=tmp_path, env={})


def test_sources_tracking(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.sbxloop]\nmodel = "gpt-5"\n')
    (tmp_path / "sbxloop.toml").write_text("keep_sandboxes = true\n")
    config, sources = load_config_with_sources(
        cwd=tmp_path, env={"SBXLOOP_BUDGETS__MAX_TASKS": "9"}
    )
    assert config.budgets.max_tasks == 9
    assert sources["model"] == "pyproject.toml"
    assert sources["keep_sandboxes"] == "sbxloop.toml"
    assert sources["budgets.max_tasks"] == "env"
    assert sources["budgets.max_replans_per_task"] == "default"


def test_unknown_key_is_config_error(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text("no_such_option = 1\n")
    with pytest.raises(ConfigError, match="invalid sbxloop configuration"):
        load_config(cwd=tmp_path, env={})
    # Any unknown key in those
    # sections is still a hard error.
    for section in ("daemon", "github", "landing"):
        (tmp_path / "sbxloop.toml").write_text(f"[{section}]\nno_such_option = 1\n")
        with pytest.raises(ConfigError, match="invalid sbxloop configuration"):
            load_config(cwd=tmp_path, env={})


def test_github_repo_must_be_owner_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="owner/name"):
        load_config(cwd=tmp_path, env={"SBXLOOP_GITHUB__REPO": "https://github.com/o/r"})


def test_github_disabled_by_default(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env={})
    assert config.github.repo is None
    assert not config.github.enabled
    assert not hasattr(config.github, "report")
    assert not hasattr(config.github, "deliver")
    assert not hasattr(config.github, "deliver_draft")


def test_invalid_toml_is_config_error(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text("not [valid\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(cwd=tmp_path, env={})


def test_invalid_literal_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(cwd=tmp_path, env={"SBXLOOP_WORKER_TRANSPORT": "carrier-pigeon"})


def test_env_string_fallback(tmp_path: Path) -> None:
    # Bare strings are not valid TOML scalars; they fall back to raw strings.
    config = load_config(cwd=tmp_path, env={"SBXLOOP_MODEL": "claude-sonnet"})
    assert config.model == "claude-sonnet"


def test_github_delivery_layers(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text('[github]\nrepo = "file/repo"\ncreate_repo = true\n')
    config = load_config(cwd=tmp_path, env={})
    assert config.github.repo == "file/repo"
    assert config.github.create_repo is True
    assert config.github.deliver_base is None
    assert config.landing.deliver_draft is True

    config = load_config(cwd=tmp_path, env={"SBXLOOP_GITHUB__DELIVER_BASE": "develop"})
    assert config.github.deliver_base == "develop"


class TestLandingSection:
    """[landing]: what happens to a run's work after its tasks are built.
    Merging is not optional and every knob has a bounded default."""

    def test_defaults(self, tmp_path: Path) -> None:
        landing = load_config(cwd=tmp_path, env={}).landing
        assert landing.deliver_draft is True
        assert landing.max_review_rounds == 3
        assert landing.max_ci_rounds == 2
        assert landing.ci_poll_interval_s == 60.0
        assert landing.ci_settle_s == 90.0
        assert landing.ci_timeout_s == 3600.0
        assert landing.merge_method == "auto"
        assert landing.delete_branch_on_merge is True
        assert landing.merge_update_attempts == 3
        assert landing.review_diff_max_chars == 150_000

    def test_layers_and_validation(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            "[landing]\nmax_review_rounds = 1\nci_settle_s = 0\ndeliver_draft = false\n"
        )
        config = load_config(
            cwd=tmp_path,
            env={
                "SBXLOOP_LANDING__MERGE_METHOD": "rebase",
                "SBXLOOP_LANDING__MERGE_UPDATE_ATTEMPTS": "0",
                "SBXLOOP_LANDING__CI_POLL_INTERVAL_S": "5",
            },
        )
        assert config.landing.max_review_rounds == 1
        assert config.landing.ci_settle_s == 0.0
        assert config.landing.deliver_draft is False
        assert config.landing.merge_method == "rebase"
        # 0 is a real setting, not a mistake: it disables branch updating.
        assert config.landing.merge_update_attempts == 0
        assert config.landing.ci_poll_interval_s == 5.0
        for bad in (
            {"SBXLOOP_LANDING__MERGE_METHOD": "yolo"},
            {"SBXLOOP_LANDING__MERGE_UPDATE_ATTEMPTS": "-1"},
            {"SBXLOOP_LANDING__MAX_REVIEW_ROUNDS": "-1"},
            {"SBXLOOP_LANDING__CI_POLL_INTERVAL_S": "0"},
            {"SBXLOOP_LANDING__CI_TIMEOUT_S": "0"},
            {"SBXLOOP_LANDING__REVIEW_DIFF_MAX_CHARS": "100"},
        ):
            with pytest.raises(ConfigError):
                load_config(cwd=tmp_path, env=bad)


class TestRetiredKeysAreErrors:
    """The 1.0 pipeline retired the daemon's self-filing lanes and moved the
    landing knobs under [landing]. Until 1.0.0 a config still carrying them
    loaded with a warning (the daemon host deploys unattended); now they are
    unknown keys like any other, and `extra="forbid"` names them."""

    @pytest.mark.parametrize(
        "toml",
        [
            "[daemon]\nauto_merge = true\n",
            '[daemon]\nmerge_method = "rebase"\n',
            '[daemon]\nbacklog = "github"\n',
            "[daemon]\npostmortems = true\n",
            '[daemon]\ninbox_dir = "in"\n',
            "[daemon]\ntracking_issue = false\n",
            '[github]\nrepo = "o/r"\ndeliver = true\n',
            '[github]\nrepo = "o/r"\nreport = false\n',
            '[github]\nrepo = "o/r"\ndeliver_draft = false\n',
        ],
    )
    def test_retired_keys_fail_to_load(self, tmp_path: Path, toml: str) -> None:
        (tmp_path / "sbxloop.toml").write_text(toml)
        with pytest.raises(ConfigError, match="Extra inputs are not permitted"):
            load_config(cwd=tmp_path, env={})

    def test_retired_keys_from_the_environment_fail_too(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            load_config(cwd=tmp_path, env={"SBXLOOP_DAEMON__AUTO_MERGE": "true"})

    def test_landing_knobs_live_under_landing(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            '[landing]\nmerge_method = "rebase"\ndeliver_draft = false\n'
        )
        config = load_config(cwd=tmp_path, env={})
        assert config.landing.merge_method == "rebase"
        assert config.landing.deliver_draft is False
        assert not hasattr(config, "retired_keys")


def test_policy_defaults_empty(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env={})
    assert config.policy.allow == []
    assert config.policy.deny == []


def test_policy_patterns_parse_and_normalize(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text(
        "[policy]\n"
        'allow = ["Registry.NPMJS.org", "*.crates.io", "*"]\n'
        'deny = ["evil.example.com"]\n'
    )
    config = load_config(cwd=tmp_path, env={})
    assert config.policy.allow == ["registry.npmjs.org", "*.crates.io", "*"]
    assert config.policy.deny == ["evil.example.com"]


def test_policy_invalid_pattern_is_config_error(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text('[policy]\nallow = ["https://pypi.org"]\n')
    with pytest.raises(ConfigError, match="invalid egress pattern"):
        load_config(cwd=tmp_path, env={})


def test_limits_defaults(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env={})
    assert config.limits.disk_warn == 85.0
    assert config.limits.disk_abort == 95.0
    assert config.limits.mem_warn == 90.0
    # #253: memory abort is opt-in — transient spikes are normal under a
    # parallel test run.
    assert config.limits.mem_abort == 0.0


def test_limits_mem_abort_must_exceed_mem_warn(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text("[limits]\nmem_warn = 90.0\nmem_abort = 85.0\n")
    with pytest.raises(ConfigError, match="mem_abort"):
        load_config(cwd=tmp_path, env={})
    (tmp_path / "sbxloop.toml").write_text("[limits]\nmem_warn = 90.0\nmem_abort = 97.0\n")
    assert load_config(cwd=tmp_path, env={}).limits.mem_abort == 97.0


def test_limits_layers_and_env(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text("[limits]\ndisk_warn = 70.0\ndisk_abort = 80.0\n")
    config = load_config(cwd=tmp_path, env={})
    assert config.limits.disk_warn == 70.0
    assert config.limits.disk_abort == 80.0

    config = load_config(cwd=tmp_path, env={"SBXLOOP_LIMITS__DISK_ABORT": "90.0"})
    assert config.limits.disk_abort == 90.0


def test_limits_zero_disables_without_error(tmp_path: Path) -> None:
    # warn disabled + abort enabled is a valid (abort-only) configuration.
    (tmp_path / "sbxloop.toml").write_text(
        "[limits]\ndisk_warn = 0\ndisk_abort = 95.0\nmem_warn = 0\n"
    )
    config = load_config(cwd=tmp_path, env={})
    assert config.limits.disk_warn == 0.0
    assert config.limits.disk_abort == 95.0


def test_limits_abort_must_exceed_warn(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text("[limits]\ndisk_warn = 90.0\ndisk_abort = 80.0\n")
    with pytest.raises(ConfigError, match="disk_abort"):
        load_config(cwd=tmp_path, env={})


def test_limits_must_be_percentages(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"0\.\.100"):
        load_config(cwd=tmp_path, env={"SBXLOOP_LIMITS__DISK_WARN": "150"})


class TestStateDirDefault:
    """#224: the relative ``.sbxloop`` default meant "wherever the shell
    stood" — state scattered into checkouts and an empty ``status`` from any
    other directory. The default is now per-user; relative stays opt-in."""

    def test_default_is_per_user_not_cwd(self, tmp_path: Path) -> None:
        # HOME is tmp_path (autouse fixture); cwd is a different directory.
        project = tmp_path / "proj"
        project.mkdir()
        config = load_config(cwd=project, env={})
        assert config.state_dir == tmp_path / ".sbxloop"
        assert config.state_dir.is_absolute()

    def test_relative_opt_in_is_anchored_at_the_config_root(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        (project / "sbxloop.toml").write_text('state_dir = ".sbxloop"\n')
        config, sources = load_config_with_sources(cwd=project, env={})
        assert config.state_dir == project / ".sbxloop"
        assert sources["state_dir"] == "sbxloop.toml"

    def test_tilde_expands(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text('state_dir = "~/elsewhere"\n')
        config = load_config(cwd=tmp_path, env={})
        assert config.state_dir == tmp_path / "elsewhere"

    def test_env_override_wins(self, tmp_path: Path) -> None:
        env = {"SBXLOOP_STATE_DIR": str(tmp_path / "explicit")}
        assert load_config(cwd=tmp_path, env=env).state_dir == tmp_path / "explicit"

    def test_mapped_home_governs_default_and_tilde(self, tmp_path: Path) -> None:
        # A hermetic caller's env HOME must decide state_dir the same way it
        # decides the user-config path; the process HOME (tmp_path here)
        # must not leak in.
        home = tmp_path / "mapped"
        project = tmp_path / "proj"
        project.mkdir()
        env = {"HOME": str(home)}
        assert load_config(cwd=project, env=env).state_dir == home / ".sbxloop"
        (project / "sbxloop.toml").write_text('state_dir = "~/elsewhere"\n')
        assert load_config(cwd=project, env=env).state_dir == home / "elsewhere"
        assert load_config(cwd=project, env={**env, "SBXLOOP_STATE_DIR": "~"}).state_dir == home


class TestUserConfigLayer:
    """``~/.config/sbxloop/sbxloop.toml`` is the lowest layer: it carries
    operator-level defaults and every project-level source overrides it."""

    def _write_user(self, home: Path, body: str, xdg: Path | None = None) -> None:
        root = (xdg or home / ".config") / "sbxloop"
        root.mkdir(parents=True)
        (root / "sbxloop.toml").write_text(body)

    def test_read_from_home_config(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        project = tmp_path / "proj"
        project.mkdir()
        self._write_user(home, 'model = "gpt-5"\napp_name = "mine"\n')
        config, sources = load_config_with_sources(cwd=project, env={"HOME": str(home)})
        assert config.model == "gpt-5"
        assert config.app_name == "mine"
        assert sources["model"] == "user config"

    def test_xdg_config_home_takes_priority_over_home(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        xdg = tmp_path / "xdg"
        self._write_user(home, 'model = "from-home"\n')
        self._write_user(home, 'model = "from-xdg"\n', xdg=xdg)
        env = {"HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}
        assert load_config(cwd=tmp_path, env=env).model == "from-xdg"

    def test_project_layers_override_user_config(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        project = tmp_path / "proj"
        project.mkdir()
        self._write_user(home, 'model = "user"\n[budgets]\nmax_tasks = 3\n')
        (project / "pyproject.toml").write_text('[tool.sbxloop]\nmodel = "pyproject"\n')
        config, sources = load_config_with_sources(cwd=project, env={"HOME": str(home)})
        assert config.model == "pyproject"
        assert sources["model"] == "pyproject.toml"
        # untouched keys still flow up from the user layer
        assert config.budgets.max_tasks == 3
        assert sources["budgets.max_tasks"] == "user config"

    def test_hermetic_env_never_reads_the_real_home(self, tmp_path: Path) -> None:
        # env={} names no HOME/XDG_CONFIG_HOME → no user layer at all, even
        # though Path.home() (the autouse tmp HOME) has a file.
        self._write_user(tmp_path, 'model = "leak"\n')
        assert load_config(cwd=tmp_path, env={}).model == "auto"


def test_daemon_log_level_and_format(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env={})
    assert config.daemon.log_level == "INFO"
    assert config.daemon.log_format == "console"
    over = load_config(
        cwd=tmp_path,
        env={"SBXLOOP_DAEMON__LOG_LEVEL": "debug", "SBXLOOP_DAEMON__LOG_FORMAT": "JSON"},
    )
    assert over.daemon.log_level == "DEBUG"  # case-insensitive
    assert over.daemon.log_format == "json"
    with pytest.raises(ConfigError, match="log_level"):
        load_config(cwd=tmp_path, env={"SBXLOOP_DAEMON__LOG_LEVEL": "LOUD"})
    with pytest.raises(ConfigError, match="log_format"):
        load_config(cwd=tmp_path, env={"SBXLOOP_DAEMON__LOG_FORMAT": "xml"})


def test_daemon_version_check_and_upgrade_command(tmp_path: Path) -> None:
    """#641: the release check is on by default and switchable off; #638: the
    upgrade advice comes from `upgrade_command` when the operator sets one."""
    config = load_config(cwd=tmp_path, env={})
    assert config.daemon.version_check is True
    assert config.daemon.upgrade_command is None
    (tmp_path / "sbxloop.toml").write_text(
        '[daemon]\nversion_check = false\nupgrade_command = "  pipx upgrade sbxloop "\n'
    )
    config = load_config(cwd=tmp_path, env={})
    assert config.daemon.version_check is False
    assert config.daemon.upgrade_command == "pipx upgrade sbxloop"
    over = load_config(cwd=tmp_path, env={"SBXLOOP_DAEMON__VERSION_CHECK": "true"})
    assert over.daemon.version_check is True
    (tmp_path / "sbxloop.toml").write_text('[daemon]\nupgrade_command = "   "\n')
    with pytest.raises(ConfigError, match="upgrade_command"):
        load_config(cwd=tmp_path, env={})


def test_run_cap_timezone_defaults_to_utc(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env={})
    assert config.daemon.run_cap_timezone == "UTC"
    assert config.daemon.max_runs_per_day == 12


def test_run_cap_timezone_explicit_zone(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text('[daemon]\nrun_cap_timezone = "America/New_York"\n')
    config = load_config(cwd=tmp_path, env={})
    assert config.daemon.run_cap_timezone == "America/New_York"
    assert ZoneInfo(config.daemon.run_cap_timezone) is not None
    # the cap itself keeps its name and default (backward compatibility)
    assert config.daemon.max_runs_per_day == 12
    over = load_config(cwd=tmp_path, env={"SBXLOOP_DAEMON__RUN_CAP_TIMEZONE": "Europe/Berlin"})
    assert over.daemon.run_cap_timezone == "Europe/Berlin"


def test_run_cap_timezone_rejects_bogus_zone(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text('[daemon]\nrun_cap_timezone = "Mars/Olympus"\n')
    with pytest.raises(ConfigError, match="run_cap_timezone"):
        load_config(cwd=tmp_path, env={})


def test_run_stale_after_s_default_override_and_validation(tmp_path: Path) -> None:
    """#374 liveness safety net: conservative 6h default, 0 disables."""
    assert load_config(cwd=tmp_path, env={}).daemon.run_stale_after_s == 21600.0
    (tmp_path / "sbxloop.toml").write_text("[daemon]\nrun_stale_after_s = 300\n")
    assert load_config(cwd=tmp_path, env={}).daemon.run_stale_after_s == 300.0
    (tmp_path / "sbxloop.toml").write_text("[daemon]\nrun_stale_after_s = 0\n")
    assert load_config(cwd=tmp_path, env={}).daemon.run_stale_after_s == 0.0
    (tmp_path / "sbxloop.toml").write_text("[daemon]\nrun_stale_after_s = -1\n")
    with pytest.raises(ConfigError, match=r"run_stale_after_s"):
        load_config(cwd=tmp_path, env={})


def test_merging_is_not_optional(tmp_path: Path) -> None:
    """The old `auto_merge` opt-in is gone: a run that reaches landing merges
    its own PR or ends blocked, never `done` with an unmerged PR."""
    config = load_config(cwd=tmp_path, env={})
    assert not hasattr(config.daemon, "auto_merge")
    assert not hasattr(config.landing, "auto_merge")
    assert config.landing.merge_method == "auto"


class TestMergeGateConfig:
    """[landing] merge_gate — the one opt-in human touchpoint."""

    def test_defaults_off(self) -> None:
        config = Config.model_validate({})
        assert config.landing.merge_gate == "off"
        assert config.daemon.gated_label == "sbxloop:awaiting-merge"

    def test_chat_is_the_only_other_value(self) -> None:
        assert Config.model_validate({"landing": {"merge_gate": "chat"}}).landing.merge_gate == (
            "chat"
        )
        with pytest.raises(ValidationError):
            Config.model_validate({"landing": {"merge_gate": "github"}})

    def test_gated_label_joins_the_distinctness_check(self) -> None:
        with pytest.raises(ValidationError, match="distinct"):
            Config.model_validate({"daemon": {"gated_label": "sbxloop:blocked"}})


def test_concierge_clarify_ttl_default_and_env_override(tmp_path: Path) -> None:
    """Ask, never block: one knob times the clickable choices and the
    auto-file sweep together."""
    assert load_config(cwd=tmp_path, env={}).concierge.clarify_ttl_s == 900.0
    config = load_config(cwd=tmp_path, env={"SBXLOOP_CONCIERGE__CLARIFY_TTL_S": "120"})
    assert config.concierge.clarify_ttl_s == 120.0


def test_concierge_clarify_ttl_bounds() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config.model_validate({"concierge": {"clarify_ttl_s": 10}})
    with pytest.raises(ValidationError):
        Config.model_validate({"concierge": {"clarify_ttl_s": 100_000}})


class TestGithubApiUrl:
    """One source of truth for *which* GitHub (#623): every host-shaped value
    derives from `[github] api_url`, and a GH_HOST that names another site
    fails at load rather than letting gh and the REST transport disagree."""

    def test_default_is_dotcom(self, tmp_path: Path) -> None:
        github = load_config(cwd=tmp_path, env={}).github
        assert github.api_url == "https://api.github.com"
        assert github.is_dotcom
        assert github.api_host == "api.github.com"
        assert github.web_host == "github.com"
        assert github.web_url == "https://github.com"
        assert github.allow_domains == ("api.github.com", "github.com")

    def test_enterprise_server_derives_one_host(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            '[github]\napi_url = "https://ghe.example.com/api/v3/"\n'
        )
        github = load_config(cwd=tmp_path, env={}).github
        assert github.api_url == "https://ghe.example.com/api/v3"  # trailing slash dropped
        assert not github.is_dotcom
        assert github.api_host == "ghe.example.com"
        assert github.web_host == "ghe.example.com"
        assert github.web_url == "https://ghe.example.com"
        assert github.allow_domains == ("ghe.example.com",)

    @pytest.mark.parametrize(
        "bad",
        ["http://ghe.example.com/api/v3", "ghe.example.com", "https://u:p@ghe.example.com", ""],
    )
    def test_refuses_anything_but_a_plain_https_url(self, tmp_path: Path, bad: str) -> None:
        (tmp_path / "sbxloop.toml").write_text(f'[github]\napi_url = "{bad}"\n')
        with pytest.raises(ConfigError, match="api_url must be a plain https URL"):
            load_config(cwd=tmp_path, env={})

    def test_gh_host_disagreeing_with_api_url_fails_at_load(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"GH_HOST='ghe\.example\.com'.*disagrees"):
            load_config(cwd=tmp_path, env={"GH_HOST": "ghe.example.com"})
        (tmp_path / "sbxloop.toml").write_text(
            '[github]\napi_url = "https://ghe.example.com/api/v3"\n'
        )
        with pytest.raises(ConfigError, match=r"GH_HOST='github\.com'.*disagrees"):
            load_config(cwd=tmp_path, env={"GH_HOST": "github.com"})

    def test_gh_host_naming_the_same_site_is_fine(self, tmp_path: Path) -> None:
        assert load_config(cwd=tmp_path, env={"GH_HOST": "GitHub.com"}).github.is_dotcom
        (tmp_path / "sbxloop.toml").write_text(
            '[github]\napi_url = "https://ghe.example.com/api/v3"\n'
        )
        config = load_config(cwd=tmp_path, env={"GH_HOST": "ghe.example.com"})
        assert config.github.web_host == "ghe.example.com"


class TestCloneFilter:
    def test_off_by_default(self, tmp_path: Path) -> None:
        assert load_config(cwd=tmp_path, env={}).sandbox.clone_filter is None

    def test_opt_in_spec(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text('[sandbox]\nclone_filter = "blob:none"\n')
        assert load_config(cwd=tmp_path, env={}).sandbox.clone_filter == "blob:none"

    def test_blank_means_off(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text('[sandbox]\nclone_filter = "  "\n')
        assert load_config(cwd=tmp_path, env={}).sandbox.clone_filter is None

    @pytest.mark.parametrize("bad", ["blob:none --depth 1", "--filter=blob:none"])
    def test_refuses_anything_but_one_filter_spec(self, tmp_path: Path, bad: str) -> None:
        (tmp_path / "sbxloop.toml").write_text(f'[sandbox]\nclone_filter = "{bad}"\n')
        with pytest.raises(ConfigError, match="clone_filter must be a git filter spec"):
            load_config(cwd=tmp_path, env={})


class TestConfigDiscovery:
    """Where the file layers come from, and how far each is trusted (#671).

    Discovery walks from the cwd up to the enclosing checkout's top level.
    A config file the repository *carries* (tracked in git) is project
    config and may set only ``PROJECT_LAYER_KEYS``; a file the operator put
    there (untracked, or outside any checkout) is honoured in full.
    """

    @staticmethod
    def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == "sbxloop.config" and f"'event': '{name}'" in r.getMessage()
        ]

    def test_subdirectory_of_a_checkout_finds_the_root_config(self, tmp_path: Path) -> None:
        from tests.unit.test_hostgit import git, make_repo

        root = make_repo(tmp_path)
        (root / "sbxloop.toml").write_text('model = "from-root"\n[sandbox]\nlanguages = ["go"]\n')
        # Untracked: the operator's file, not the repository's.
        nested = root / "packages" / "foo"
        nested.mkdir(parents=True)
        git("status", cwd=root)  # keep the index fresh
        config, sources = load_config_with_sources(cwd=nested, env={})
        assert config.model == "from-root"
        assert config.sandbox.languages == ["go"]
        assert sources["model"] == "sbxloop.toml"

    def test_nearest_config_wins_and_the_walk_stops_at_the_checkout(self, tmp_path: Path) -> None:
        from tests.unit.test_hostgit import make_repo

        (tmp_path / "sbxloop.toml").write_text('model = "above-the-checkout"\n')
        root = make_repo(tmp_path)
        (root / "sbxloop.toml").write_text('model = "root"\n')
        pkg = root / "pkg"
        pkg.mkdir()
        (pkg / "sbxloop.toml").write_text('model = "pkg"\n')
        assert load_config(cwd=pkg, env={}).model == "pkg"
        (root / "other").mkdir()
        assert load_config(cwd=root / "other", env={}).model == "root"
        # Outside a checkout nothing walks: the parent's file is never seen.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        assert load_config(cwd=elsewhere, env={}).model == "auto"

    def test_tracked_config_may_only_set_project_keys(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from tests.unit.test_hostgit import git, make_repo

        root = make_repo(tmp_path)
        (root / "sbxloop.toml").write_text(
            'state_dir = "/tmp/elsewhere"\n'
            "[sandbox]\n"
            'languages = ["javascript"]\n'
            'gate_command = "npm test"\n'
            'extra_allow_domains = ["evil.example"]\n'
            "[policy]\n"
            'allow = ["*"]\n'
            "[landing]\n"
            'merge_gate = "chat"\n'
        )
        git("add", "sbxloop.toml", cwd=root)
        git("commit", "-m", "carry config", cwd=root)
        with caplog.at_level(logging.WARNING):
            config, sources = load_config_with_sources(cwd=root, env={})
        assert config.sandbox.languages == ["javascript"]
        assert config.sandbox.gate_command == "npm test"
        assert sources["sandbox.languages"] == "sbxloop.toml (project)"
        assert config.sandbox.extra_allow_domains == []
        assert config.policy.allow == Config().policy.allow
        assert config.landing.merge_gate == Config().landing.merge_gate
        assert config.state_dir != Path("/tmp/elsewhere")
        (ignored,) = self._events(caplog, "config.project_layer.ignored")
        for key in (
            "'landing.merge_gate'",
            "'policy.allow'",
            "'sandbox.extra_allow_domains'",
            "'state_dir'",
        ):
            assert key in ignored
        assert "sandbox.languages" not in ignored

    def test_tracked_pyproject_section_is_project_config_too(self, tmp_path: Path) -> None:
        from tests.unit.test_hostgit import git, make_repo

        root = make_repo(tmp_path)
        (root / "pyproject.toml").write_text(
            '[tool.sbxloop]\nmodel = "not-yours"\n[tool.sbxloop.github]\nbranch_prefix = "bot/"\n'
        )
        git("add", "pyproject.toml", cwd=root)
        git("commit", "-m", "pyproject", cwd=root)
        config, sources = load_config_with_sources(cwd=root, env={})
        assert config.model == "auto"
        assert config.github.branch_prefix == "bot/"
        assert sources["github.branch_prefix"] == "pyproject.toml (project)"

    def test_untracked_config_in_a_checkout_is_the_operators(self, tmp_path: Path) -> None:
        """`sbxloop init` in a checkout writes an untracked file: that is the
        operator's, and every key it sets is honoured (the daemon's runner
        directory is the common shape of this)."""
        from tests.unit.test_hostgit import make_repo

        root = make_repo(tmp_path)
        (root / "sbxloop.toml").write_text(
            '[policy]\nallow = ["pypi.org"]\n[landing]\nmerge_gate = "chat"\n'
        )
        config, sources = load_config_with_sources(cwd=root, env={})
        assert config.landing.merge_gate == "chat"
        assert "pypi.org" in config.policy.allow
        assert sources["landing.merge_gate"] == "sbxloop.toml"

    def test_config_outside_any_checkout_is_the_operators(self, tmp_path: Path) -> None:
        work = tmp_path / "runner"
        work.mkdir()
        (work / "sbxloop.toml").write_text(
            '[landing]\nmerge_gate = "chat"\n[budgets]\nmax_tasks = 2\n'
        )
        config, sources = load_config_with_sources(cwd=work, env={})
        assert config.landing.merge_gate == "chat"
        assert config.budgets.max_tasks == 2
        assert sources["budgets.max_tasks"] == "sbxloop.toml"

    def test_relative_state_dir_anchors_at_the_discovered_root(self, tmp_path: Path) -> None:
        from tests.unit.test_hostgit import make_repo

        root = make_repo(tmp_path)
        (root / "sbxloop.toml").write_text('state_dir = ".sbxloop"\n')
        nested = root / "src" / "deep"
        nested.mkdir(parents=True)
        config = load_config(cwd=nested, env={})
        assert config.state_dir == root.resolve() / ".sbxloop"

    def test_unknown_tracking_reads_as_the_repositorys_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed: when git cannot say whether the checkout carries the
        file, it is treated as project config."""
        from sbxloop import hostgit
        from tests.unit.test_hostgit import make_repo

        root = make_repo(tmp_path)
        (root / "sbxloop.toml").write_text(
            '[landing]\nmerge_gate = "chat"\n[sandbox]\nlanguages = ["go"]\n'
        )
        monkeypatch.setattr(hostgit, "is_tracked", lambda repo_root, path: None)
        config = load_config(cwd=root, env={})
        assert config.landing.merge_gate == "off"
        assert config.sandbox.languages == ["go"]


class TestSandboxEnv:
    """`[sandbox] env` / `secret_env` (#679): plain values and names whose
    values the daemon's environment holds, per repository."""

    def test_unset_by_default(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, env={})
        assert config.sandbox.env == {}
        assert config.sandbox.secret_env == []
        assert config.sandbox_env_for(None) == {}
        assert config.secret_env_for(None) == []

    def test_plain_and_secret_names_parse(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            "[sandbox]\n"
            'env = { RAILS_ENV = "test", DATABASE_URL = "postgres://localhost/app_test" }\n'
            'secret_env = ["NPM_TOKEN", "PIP_INDEX_URL", "NPM_TOKEN"]\n'
        )
        config = load_config(cwd=tmp_path, env={})
        assert config.sandbox.env == {
            "RAILS_ENV": "test",
            "DATABASE_URL": "postgres://localhost/app_test",
        }
        # duplicates collapse, order kept
        assert config.sandbox.secret_env == ["NPM_TOKEN", "PIP_INDEX_URL"]

    def test_a_repository_override_replaces_the_global_setting(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            "[sandbox]\n"
            'env = { RAILS_ENV = "test" }\n'
            'secret_env = ["NPM_TOKEN"]\n'
            "\n"
            "[[github.repos]]\n"
            'repo = "o/rails"\n'
            "\n"
            "[[github.repos]]\n"
            'repo = "o/go"\n'
            'env = { GOFLAGS = "-mod=vendor" }\n'
            "secret_env = []\n"
        )
        config = load_config(cwd=tmp_path, env={})
        assert config.sandbox_env_for("o/rails") == {"RAILS_ENV": "test"}
        assert config.secret_env_for("o/rails") == ["NPM_TOKEN"]
        # The override REPLACES: the Go repository gets neither RAILS_ENV
        # nor NPM_TOKEN — an empty list is a real "this repo needs no secret".
        assert config.sandbox_env_for("o/go") == {"GOFLAGS": "-mod=vendor"}
        assert config.secret_env_for("o/go") == []
        # A repository without an entry falls back to the global setting.
        assert config.sandbox_env_for("o/unknown") == {"RAILS_ENV": "test"}

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            ('[sandbox]\nenv = { "1BAD" = "x" }\n', "not an environment variable name"),
            ('[sandbox]\nsecret_env = ["NO-DASH"]\n', "not an environment variable name"),
            ('[sandbox]\nenv = { GH_TOKEN = "x" }\n', "delivered by sbxloop itself"),
            ('[sandbox]\nsecret_env = ["COPILOT_GITHUB_TOKEN"]\n', "delivered by sbxloop itself"),
            ('[sandbox]\nenv = { SBXLOOP_WORKER_BACKEND = "echo" }\n', "delivered by sbxloop"),
            ('[sandbox]\nenv = { A = "1" }\nsecret_env = ["A"]\n', "both name"),
            (
                '[sandbox]\nsecret_env = ["A"]\n\n'
                '[[github.repos]]\nrepo = "o/r"\nenv = { A = "1" }\n',
                r"github.repos\[o/r\]: env and secret_env both name",
            ),
            ('[[github.repos]]\nrepo = "o/r"\nenv = { GITHUB_TOKEN = "x" }\n', "github.repos"),
        ],
    )
    def test_refuses_names_the_loop_owns_or_that_are_not_names(
        self, tmp_path: Path, body: str, match: str
    ) -> None:
        (tmp_path / "sbxloop.toml").write_text(body)
        with pytest.raises(ConfigError, match=match):
            load_config(cwd=tmp_path, env={})
