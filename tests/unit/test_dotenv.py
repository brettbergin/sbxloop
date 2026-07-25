"""`.env` loading tests: precedence, hermeticity, and the committed example."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from sbxloop.config import Config, load_config, load_dotenv_file

REPO_ROOT = Path(__file__).resolve().parents[2]

SENTINEL = "SBXLOOP_TEST_DOTENV_SENTINEL"


@pytest.fixture(autouse=True)
def _clean_sentinels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure vars our .env files set never leak across tests."""
    for name in (SENTINEL, "SBXLOOP_MODEL", "COPILOT_GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    yield  # type: ignore[misc]
    os.environ.pop(SENTINEL, None)
    os.environ.pop("SBXLOOP_MODEL", None)


class TestLoadDotenvFile:
    def test_loads_values_into_environ(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(f"{SENTINEL}=from-dotenv\n")
        loaded = load_dotenv_file(tmp_path)
        assert loaded == tmp_path / ".env"
        assert os.environ[SENTINEL] == "from-dotenv"

    def test_real_environment_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SENTINEL, "from-real-env")
        (tmp_path / ".env").write_text(f"{SENTINEL}=from-dotenv\n")
        load_dotenv_file(tmp_path)
        assert os.environ[SENTINEL] == "from-real-env"

    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        assert load_dotenv_file(tmp_path) is None


class TestConfigIntegration:
    def test_load_config_reads_dotenv_settings(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("SBXLOOP_MODEL=dotenv-model\n")
        config = load_config(cwd=tmp_path)  # env=None -> real environ + .env
        assert config.model == "dotenv-model"

    def test_explicit_env_mapping_stays_hermetic(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("SBXLOOP_MODEL=dotenv-model\n")
        config = load_config(cwd=tmp_path, env={})
        assert config.model == "auto"  # .env not consulted
        assert "SBXLOOP_MODEL" not in os.environ  # and not loaded at all


class TestEnvExample:
    def test_example_exists_and_env_is_ignored(self) -> None:
        assert (REPO_ROOT / ".env.example").is_file()
        gitignore = (REPO_ROOT / ".gitignore").read_text().splitlines()
        assert ".env" in gitignore

    def test_example_documents_required_tokens(self) -> None:
        values = dotenv_values(REPO_ROOT / ".env.example")
        assert set(values) == {"COPILOT_GITHUB_TOKEN", "GH_TOKEN"}
        assert all(not v for v in values.values())  # placeholders ship empty

    def test_example_commented_settings_are_valid_config(self, tmp_path: Path) -> None:
        # Every commented-out SBXLOOP_* line must round-trip through the
        # config loader once uncommented, so the example can't rot.
        lines = (REPO_ROOT / ".env.example").read_text().splitlines()
        env: dict[str, str] = {}
        for line in lines:
            stripped = line.lstrip("#").strip()
            if stripped.startswith("SBXLOOP_") and "=" in stripped:
                key, _, value = stripped.partition("=")
                env[key] = value
        assert env, "expected commented SBXLOOP_ examples"
        config = load_config(cwd=tmp_path, env=env)
        assert isinstance(config, Config)
        assert config.github.repo == "owner/repo"
