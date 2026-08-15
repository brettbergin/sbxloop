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
    assert config.discord.enabled is False
    over = load_config(
        cwd=tmp_path,
        env={
            "SBXLOOP_DAEMON__MAX_RUNS_PER_DAY": "3",
            "SBXLOOP_DAEMON__BACKLOG": "github",
            "SBXLOOP_DISCORD__CHANNEL_ID": "123456789",
        },
    )
    assert over.daemon.max_runs_per_day == 3
    assert over.daemon.backlog == "github"
    assert over.discord.enabled is True and over.discord.channel_id == 123456789
    (tmp_path / "sbxloop.toml").write_text(
        '[daemon]\ntrigger_label = "x"\nin_progress_label = "x"\n'
    )
    with pytest.raises(ConfigError, match="distinct"):
        load_config(cwd=tmp_path, env={})
    (tmp_path / "sbxloop.toml").write_text('[daemon]\nbacklog = "yolo"\n')
    with pytest.raises(ConfigError):
        load_config(cwd=tmp_path, env={})
    (tmp_path / "sbxloop.toml").write_text("[daemon]\nmax_runs_per_day = 0\n")
    with pytest.raises(ConfigError, match="max_runs_per_day"):
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
