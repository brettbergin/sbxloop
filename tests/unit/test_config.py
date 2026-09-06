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
    from sbxloop.data import DEFAULT_CONFIG_TOML

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
    # #255: unattended posture — clone isolation, fetch refresh.
    assert config.daemon.workspace_isolation == "clone"
    assert config.daemon.refresh_workspace is True
    assert config.discord.enabled is False
    over = load_config(
        cwd=tmp_path,
        env={
            "SBXLOOP_DAEMON__MAX_RUNS_PER_DAY": "3",
            "SBXLOOP_DAEMON__WORKSPACE_ISOLATION": "in-place",
            "SBXLOOP_DISCORD__CHANNEL_ID": "123456789",
        },
    )
    assert over.daemon.max_runs_per_day == 3
    assert over.daemon.workspace_isolation == "in-place"
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
        assert config.home != Path("/tmp/elsewhere")
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


class TestCredentials:
    """`[[credentials]]` (#765): the catalogue a run's service sandbox may
    hold — names the operator grants per run, never secrets themselves."""

    def test_unset_by_default(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, env={})
        assert config.credentials == []
        assert config.credential("weather") is None
        assert config.credentials_named([]) == []

    def test_entries_parse_with_defaults_and_overrides(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            "[[credentials]]\n"
            'name = "weather"\n'
            'env = "WEATHER_API_KEY"\n'
            'host = "API.Weather.Example.com"\n'
            'description = "forecasts"\n'
            "\n"
            "[[credentials]]\n"
            'name = "keyed"\n'
            'env = "KEYED_TOKEN"\n'
            'host = "keyed.example.com"\n'
            'header = "X-Api-Key"\n'
            'scheme = ""\n'
        )
        config = load_config(cwd=tmp_path, env={})
        weather, keyed = config.credentials
        assert (weather.host, weather.header, weather.scheme) == (
            "api.weather.example.com",
            "Authorization",
            "Bearer",
        )
        assert weather.description == "forecasts"
        assert (keyed.header, keyed.scheme) == ("X-Api-Key", "")
        assert weather.catalogue_entry() == {
            "name": "weather",
            "env": "WEATHER_API_KEY",
            "host": "api.weather.example.com",
            "header": "Authorization",
            "scheme": "Bearer",
        }
        assert config.credential("keyed") is keyed

    def test_credentials_named_keeps_order_and_dedupes(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            '[[credentials]]\nname = "a"\nenv = "A"\nhost = "a.example.com"\n\n'
            '[[credentials]]\nname = "b"\nenv = "B"\nhost = "b.example.com"\n'
        )
        config = load_config(cwd=tmp_path, env={})
        assert [c.name for c in config.credentials_named(["b", "a", "b"])] == ["b", "a"]

    def test_credentials_named_refuses_an_undeclared_name(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            '[[credentials]]\nname = "a"\nenv = "A"\nhost = "a.example.com"\n'
        )
        config = load_config(cwd=tmp_path, env={})
        with pytest.raises(
            ConfigError, match=r"'zed' is not declared under \[\[credentials\]\].*a"
        ):
            config.credentials_named(["zed"])

    @pytest.mark.parametrize(
        ("field", "value", "problem"),
        [
            ("name", "Weather", "name"),
            ("name", "", "name"),
            ("env", "weather-key", r"credentials\[\]\.env"),
            ("host", "https://api.example.com", "host"),
            ("host", "api.example.com/v1", "host"),
            ("header", "X Api Key", "header"),
            ("scheme", "Bearer token", "scheme"),
        ],
    )
    def test_malformed_entries_are_refused(
        self, tmp_path: Path, field: str, value: str, problem: str
    ) -> None:
        entry = {"name": "weather", "env": "WEATHER_API_KEY", "host": "api.example.com"}
        entry[field] = value
        body = "[[credentials]]\n" + "".join(f'{k} = "{v}"\n' for k, v in entry.items())
        (tmp_path / "sbxloop.toml").write_text(body)
        with pytest.raises(ConfigError, match=problem):
            load_config(cwd=tmp_path, env={})

    def test_duplicate_names_are_refused(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            '[[credentials]]\nname = "a"\nenv = "A"\nhost = "a.example.com"\n\n'
            '[[credentials]]\nname = "a"\nenv = "B"\nhost = "b.example.com"\n'
        )
        with pytest.raises(ConfigError, match="'a' is declared twice"):
            load_config(cwd=tmp_path, env={})


class TestWorkloads:
    """`[[workloads]]` profiles and `[workload] default` (#758): what a
    workload run's plan may ask for, validated whole at load time."""

    CATALOGUE = (
        '[[credentials]]\nname = "weather"\nenv = "WEATHER_API_KEY"\n'
        'host = "api.weather.example.com"\n\n'
    )

    def test_unset_by_default(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, env={})
        assert config.workloads == []
        assert config.workload.default is None
        assert config.workload_profile() is None
        assert config.for_workload_profile(None) is config

    def test_profile_parses_with_defaults_and_overrides(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            self.CATALOGUE + "[[workloads]]\n"
            'name = "research"\n'
            'description = "reads the web"\n'
            'egress = ["*.Example.com", " data.example.org "]\n'
            'credentials = ["weather", "weather"]\n'
            'sinks = ["chat", "artifact", "chat"]\n'
            "repo = true\n"
            "budgets = { max_tasks = 3, max_wall_clock_s = 60.0 }\n"
            "\n"
            "[[workloads]]\n"
            'name = "bare"\n'
            'publish = "hold"\n'
            "\n"
            "[workload]\n"
            'default = "research"\n'
        )
        config = load_config(cwd=tmp_path, env={})
        research, bare = config.workloads
        assert research.egress == ["*.example.com", "data.example.org"]
        assert research.credentials == ["weather"]
        assert research.sinks == ["chat", "artifact"]
        assert research.repo is True
        assert research.publish == "auto"
        assert research.budgets.set_keys == ["max_tasks", "max_wall_clock_s"]
        assert research.covers_host("api.example.com")
        assert research.covers_host("data.example.org")
        assert not research.covers_host("other.example.org")
        assert (bare.egress, bare.credentials, bare.sinks, bare.repo) == ([], [], [], False)
        assert bare.budgets.set_keys == []
        assert bare.publish == "hold"  # a held profile parks at publishing (#760)
        assert config.workload_profile() is research
        assert config.workload_profile("bare") is bare

    def test_for_workload_profile_pins_the_choice_and_applies_budgets(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            "[budgets]\nmax_tasks = 12\nmax_revisions_per_task = 4\n\n"
            "[[workloads]]\n"
            'name = "research"\n'
            "budgets = { max_tasks = 3 }\n"
            "\n"
            "[[workloads]]\n"
            'name = "bare"\n'
        )
        config = load_config(cwd=tmp_path, env={})
        narrowed = config.for_workload_profile("research")
        assert narrowed.workload.default == "research"
        assert narrowed.budgets.max_tasks == 3
        assert narrowed.budgets.max_revisions_per_task == 4
        # the profile list itself is untouched, so the persisted config still
        # dumps what the operator wrote and a resume sees no drift
        assert narrowed.workloads == config.workloads
        assert config.workload.default is None
        assert config.budgets.max_tasks == 12
        # a profile without overrides pins the name and keeps the budgets
        bare = config.for_workload_profile("bare")
        assert bare.workload.default == "bare"
        assert bare.budgets == config.budgets

    def test_unknown_profile_name_is_a_config_error(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text('[[workloads]]\nname = "research"\n')
        config = load_config(cwd=tmp_path, env={})
        with pytest.raises(ConfigError, match=r"'nope' is not declared.*declared: research"):
            config.workload_profile("nope")
        with pytest.raises(ConfigError, match="'nope'"):
            config.for_workload_profile("nope")

    @pytest.mark.parametrize(
        ("body", "problem"),
        [
            (
                '[[workloads]]\nname = "research"\ncredentials = ["weather"]\n',
                "workloads.research.credentials names 'weather', which is not declared "
                r"under \[\[credentials\]\] \(declared: none\)",
            ),
            (
                '[[workloads]]\nname = "research"\n\n[workload]\ndefault = "nope"\n',
                r"\[workload\] default = 'nope' names no \[\[workloads\]\] profile "
                r"\(declared: research\)",
            ),
            (
                '[[workloads]]\nname = "research"\n\n[[workloads]]\nname = "research"\n',
                "profile 'research' is declared twice",
            ),
            (
                '[[workloads]]\nname = "Research Team"\n',
                "workloads\\[\\].name must be lowercase",
            ),
            (
                '[[workloads]]\nname = "research"\negress = ["https://example.com/path"]\n',
                "workloads\\[\\].egress patterns must be domains",
            ),
            (
                '[[workloads]]\nname = "research"\nsinks = ["email"]\n',
                "sinks",
            ),
            (
                '[[workloads]]\nname = "research"\npublish = "later"\n',
                "publish",
            ),
            (
                '[[workloads]]\nname = "research"\nbudgets = { max_tasks = 0 }\n',
                "max_tasks",
            ),
            (
                '[[workloads]]\nname = "research"\nbudgets = { max_turns = 3 }\n',
                "max_turns",
            ),
        ],
    )
    def test_unsound_profiles_are_refused(self, tmp_path: Path, body: str, problem: str) -> None:
        (tmp_path / "sbxloop.toml").write_text(body)
        with pytest.raises(ConfigError, match=problem):
            load_config(cwd=tmp_path, env={})

    def test_unknown_default_without_profiles_lists_none(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text('[workload]\ndefault = "research"\n')
        with pytest.raises(
            ConfigError, match=r"names no \[\[workloads\]\] profile \(declared: none\)"
        ):
            load_config(cwd=tmp_path, env={})


class TestRegistries:
    """`[[registries]]` (#680): private package registries, per repository."""

    def test_unset_by_default(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, env={})
        assert config.registries == []
        assert config.registries_for(None) == []
        assert config.registry_auth_envs_for(None) == []

    def test_entries_parse_and_auth_envs_are_collected(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            "[[registries]]\n"
            'kind = "npm"\n'
            'host = "artifactory.example.com"\n'
            'url = "https://artifactory.example.com/api/npm/npm-virtual/"\n'
            'auth_env = "NPM_TOKEN"\n'
            'scope = "@example"\n'
            "\n"
            "[[registries]]\n"
            'kind = "go"\n'
            'host = "github.example.com"\n'
        )
        config = load_config(cwd=tmp_path, env={})
        assert [r.kind for r in config.registries] == ["npm", "go"]
        assert config.registries[0].scope == "@example"
        assert config.registry_auth_envs_for(None) == ["NPM_TOKEN"]

    def test_a_repository_override_replaces_the_global_list(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            "[[registries]]\n"
            'kind = "go"\n'
            'host = "github.example.com"\n'
            "\n"
            "[[github.repos]]\n"
            'repo = "owner/inherits"\n'
            "\n"
            "[[github.repos]]\n"
            'repo = "owner/own"\n'
            "[[github.repos.registries]]\n"
            'kind = "pypi"\n'
            'host = "pypi.example.com"\n'
            'url = "https://pypi.example.com/simple"\n'
            'auth_env = "PYPI_TOKEN"\n'
            'auth_user = "svc"\n'
            "\n"
            "[[github.repos]]\n"
            'repo = "owner/none"\n'
            "registries = []\n"
        )
        config = load_config(cwd=tmp_path, env={})
        assert [r.host for r in config.registries_for("owner/inherits")] == ["github.example.com"]
        assert [r.host for r in config.registries_for("owner/own")] == ["pypi.example.com"]
        assert config.registry_auth_envs_for("owner/own") == ["PYPI_TOKEN"]
        assert config.registries_for("owner/none") == []

    def test_one_default_per_ecosystem(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            "[[registries]]\n"
            'kind = "pypi"\n'
            'host = "a.example.com"\n'
            'url = "https://a.example.com/simple"\n'
            "[[registries]]\n"
            'kind = "pypi"\n'
            'host = "b.example.com"\n'
            'url = "https://b.example.com/simple"\n'
        )
        with pytest.raises(ConfigError, match="more than one 'pypi' registry"):
            load_config(cwd=tmp_path, env={})

    def test_a_credential_name_cannot_also_be_plain_env(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            "[sandbox]\n"
            'env = { NPM_TOKEN = "literal" }\n'
            "[[registries]]\n"
            'kind = "npm"\n'
            'host = "a.example.com"\n'
            'url = "https://a.example.com/npm/"\n'
            'auth_env = "NPM_TOKEN"\n'
        )
        with pytest.raises(
            ConfigError, match=r"env and a registry's auth_env both name \['NPM_TOKEN'\]"
        ):
            load_config(cwd=tmp_path, env={})


class TestSandboxEnv:
    """`[sandbox] env` (#679): plain values, per repository. Its old
    secret counterpart `secret_env` is refused by name (#766): the only
    credential in the agent sandbox is the agent's own."""

    def test_unset_by_default(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, env={})
        assert config.sandbox.env == {}
        assert config.sandbox_env_for(None) == {}

    def test_plain_values_parse(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            "[sandbox]\n"
            'env = { RAILS_ENV = "test", DATABASE_URL = "postgres://localhost/app_test" }\n'
        )
        config = load_config(cwd=tmp_path, env={})
        assert config.sandbox.env == {
            "RAILS_ENV": "test",
            "DATABASE_URL": "postgres://localhost/app_test",
        }

    def test_a_repository_override_replaces_the_global_setting(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            "[sandbox]\n"
            'env = { RAILS_ENV = "test" }\n'
            "\n"
            "[[github.repos]]\n"
            'repo = "o/rails"\n'
            "\n"
            "[[github.repos]]\n"
            'repo = "o/go"\n'
            'env = { GOFLAGS = "-mod=vendor" }\n'
        )
        config = load_config(cwd=tmp_path, env={})
        assert config.sandbox_env_for("o/rails") == {"RAILS_ENV": "test"}
        # The override REPLACES: the Go repository does not get RAILS_ENV.
        assert config.sandbox_env_for("o/go") == {"GOFLAGS": "-mod=vendor"}
        # A repository without an entry falls back to the global setting.
        assert config.sandbox_env_for("o/unknown") == {"RAILS_ENV": "test"}

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            ('[sandbox]\nenv = { "1BAD" = "x" }\n', "not an environment variable name"),
            ('[sandbox]\nenv = { GH_TOKEN = "x" }\n', "delivered by sbxloop itself"),
            ('[sandbox]\nenv = { SBXLOOP_WORKER_BACKEND = "echo" }\n', "delivered by sbxloop"),
            (
                '[sandbox]\nenv = { NPM_TOKEN = "x" }\n\n'
                '[[registries]]\nkind = "npm"\nhost = "npm.example.com"\n'
                'url = "https://npm.example.com/"\nauth_env = "NPM_TOKEN"\n',
                "env and a registry's auth_env both name",
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

    @pytest.mark.parametrize(
        ("body", "where"),
        [
            ('[sandbox]\nsecret_env = ["NPM_TOKEN"]\n', r"\[sandbox\]"),
            (
                '[[github.repos]]\nrepo = "o/r"\nsecret_env = ["NPM_TOKEN"]\n',
                r"\[\[github.repos\]\]",
            ),
            # Even empty: the key itself is the mistake to name.
            ("[sandbox]\nsecret_env = []\n", r"\[sandbox\]"),
        ],
    )
    def test_secret_env_is_refused_by_name_with_the_way_forward(
        self, tmp_path: Path, body: str, where: str
    ) -> None:
        """`secret_env` put an operator secret in the agent's sandbox; #766
        removed it. The refusal names the key, not `extra="forbid"`'s
        generic "extra inputs", and says where each kind of secret now
        belongs."""
        (tmp_path / "sbxloop.toml").write_text(body)
        with pytest.raises(ConfigError, match=f"{where} secret_env is no longer supported") as info:
            load_config(cwd=tmp_path, env={})
        message = str(info.value)
        assert "[[registries]]" in message and "auth_env" in message
        assert "[[credentials]]" in message
        assert "ci-only" in message
        assert "Extra inputs" not in message


class TestAptPackagesAndSetupCommands:
    """`[sandbox] apt_packages` / `setup_commands` (#681): OS packages beside
    the toolchains and pre-run commands, per repository."""

    def test_unset_by_default(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, env={})
        assert config.sandbox.apt_packages == []
        assert config.sandbox.setup_commands == []
        assert config.apt_packages_for(None) == []
        assert config.setup_commands_for(None) == []

    def test_lists_parse_and_a_repository_override_replaces_them(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            "[sandbox]\n"
            'apt_packages = ["libpq-dev", "protobuf-compiler", "libpq-dev", "ffmpeg=7:6.1-1"]\n'
            'setup_commands = ["npx playwright install --with-deps chromium", '
            '"pre-commit install-hooks"]\n'
            "\n"
            "[[github.repos]]\n"
            'repo = "o/web"\n'
            "\n"
            "[[github.repos]]\n"
            'repo = "o/go"\n'
            "apt_packages = []\n"
            'setup_commands = ["go mod download"]\n'
        )
        config = load_config(cwd=tmp_path, env={})
        # duplicates collapse, order kept; an apt version pin is a package
        assert config.sandbox.apt_packages == ["libpq-dev", "protobuf-compiler", "ffmpeg=7:6.1-1"]
        assert config.setup_commands_for("o/web") == [
            "npx playwright install --with-deps chromium",
            "pre-commit install-hooks",
        ]
        assert config.apt_packages_for("o/web") == config.sandbox.apt_packages
        # The override REPLACES: an empty list is a real "nothing extra".
        assert config.apt_packages_for("o/go") == []
        assert config.setup_commands_for("o/go") == ["go mod download"]
        assert config.apt_packages_for("o/unknown") == config.sandbox.apt_packages

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            ('apt_packages = ["libpq-dev; rm -rf /"]', "is not an apt package name"),
            ('apt_packages = ["Lib PQ"]', "is not an apt package name"),
            ('apt_packages = [""]', "is not an apt package name"),
            ('setup_commands = ["  "]', "cannot be empty"),
            ('setup_commands = ["make\\nmake test"]', "one command per entry"),
        ],
    )
    def test_refusals(self, tmp_path: Path, body: str, match: str) -> None:
        (tmp_path / "sbxloop.toml").write_text(f"[sandbox]\n{body}\n")
        with pytest.raises(ConfigError, match=match):
            load_config(cwd=tmp_path, env={})

    def test_repository_entries_are_checked_the_same_way(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            '[[github.repos]]\nrepo = "o/web"\napt_packages = ["$(evil)"]\n'
        )
        with pytest.raises(ConfigError, match=r"github.repos\[\].apt_packages"):
            load_config(cwd=tmp_path, env={})


class TestVerifyMode:
    """`[sandbox] verify_mode` (#682): full by default, a `[[github.repos]]`
    entry overrides it, and the vocabulary is closed."""

    def test_full_by_default(self, tmp_path: Path) -> None:
        config = load_config(cwd=tmp_path, env={})
        assert config.sandbox.verify_mode == "full"
        assert config.verify_mode_for(None) == "full"

    def test_global_and_per_repository(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text(
            "[sandbox]\n"
            'verify_mode = "advisory"\n'
            "\n"
            "[[github.repos]]\n"
            'repo = "o/web"\n'
            "\n"
            "[[github.repos]]\n"
            'repo = "o/api"\n'
            'verify_mode = "ci-only"\n'
        )
        config = load_config(cwd=tmp_path, env={})
        assert config.verify_mode_for("o/web") == "advisory"
        assert config.verify_mode_for("o/api") == "ci-only"
        assert config.verify_mode_for(None) == "advisory"

    def test_unknown_mode_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "sbxloop.toml").write_text('[sandbox]\nverify_mode = "skip"\n')
        with pytest.raises(ConfigError, match="verify_mode"):
            load_config(cwd=tmp_path, env={})
