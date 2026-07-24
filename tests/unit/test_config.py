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
    (tmp_path / "sbxloop.toml").write_text('[deliver]\nrepo = "file/repo"\ndraft = true\n')
    config = load_config(cwd=tmp_path, env={})
    assert config.deliver.repo == "file/repo"
    assert config.deliver.draft is True
    assert config.deliver.base is None

    config = load_config(cwd=tmp_path, env={"SBXLOOP_DELIVER__REPO": "env/repo"})
    assert config.deliver.repo == "env/repo"

    assert Config().deliver.repo is None  # delivery is opt-in
