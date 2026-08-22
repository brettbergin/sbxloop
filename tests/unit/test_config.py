from pathlib import Path

import pytest

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
    assert config.daemon.backlog == "off"
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
            "SBXLOOP_DAEMON__BACKLOG": "github",
            "SBXLOOP_DAEMON__WORKSPACE_ISOLATION": "in-place",
            "SBXLOOP_DAEMON__STATE_DIR": "/var/lib/sbxloop",
            "SBXLOOP_DISCORD__CHANNEL_ID": "123456789",
        },
    )
    assert over.daemon.max_runs_per_day == 3
    assert over.daemon.backlog == "github"
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
    assert config.daemon.close_on_success is True and config.daemon.tracking_issue is True
    assert config.daemon.delivered_label == "sbxloop:delivered"
    (tmp_path / "sbxloop.toml").write_text(
        '[daemon]\ntrigger_label = "x"\nin_progress_label = "x"\n'
    )
    with pytest.raises(ConfigError, match="distinct"):
        load_config(cwd=tmp_path, env={})
    # delivered_label takes part in the lifecycle-label distinctness check
    (tmp_path / "sbxloop.toml").write_text('[daemon]\ndelivered_label = "sbxloop:failed"\n')
    with pytest.raises(ConfigError, match="distinct"):
        load_config(cwd=tmp_path, env={})
    # tool_repo must be owner/name
    (tmp_path / "sbxloop.toml").write_text('[daemon]\ntool_repo = "not a repo"\n')
    with pytest.raises(ConfigError, match="owner/name"):
        load_config(cwd=tmp_path, env={})
    # so does the discovery lane's audit_label
    (tmp_path / "sbxloop.toml").write_text('[daemon]\naudit_label = "sbxloop:run"\n')
    with pytest.raises(ConfigError, match="distinct"):
        load_config(cwd=tmp_path, env={})
    # GitHub labels are case-insensitive: differing only by case is a collision
    (tmp_path / "sbxloop.toml").write_text(
        '[daemon]\ntrigger_label = "sbxloop:run"\nfailed_label = "SBXLOOP:RUN"\n'
    )
    with pytest.raises(ConfigError, match="case-insensitively"):
        load_config(cwd=tmp_path, env={})
    (tmp_path / "sbxloop.toml").write_text('[daemon]\nbacklog = "yolo"\n')
    with pytest.raises(ConfigError):
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


def test_github_repo_must_be_owner_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="owner/name"):
        load_config(cwd=tmp_path, env={"SBXLOOP_GITHUB__REPO": "https://github.com/o/r"})


def test_github_disabled_by_default(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path, env={})
    assert config.github.repo is None
    assert not config.github.enabled
    assert not config.github.report


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


def test_deliver_config_layers(tmp_path: Path) -> None:
    (tmp_path / "sbxloop.toml").write_text(
        '[github]\nrepo = "file/repo"\ndeliver = true\ndeliver_draft = true\n'
    )
    config = load_config(cwd=tmp_path, env={})
    assert config.github.repo == "file/repo"
    assert config.github.deliver is True
    assert config.github.deliver_draft is True
    assert config.github.deliver_base is None

    config = load_config(cwd=tmp_path, env={"SBXLOOP_GITHUB__DELIVER_BASE": "develop"})
    assert config.github.deliver_base == "develop"

    assert Config().github.deliver is False  # delivery is opt-in


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
