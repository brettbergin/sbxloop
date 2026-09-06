"""``secrets.env`` loading: one file in the home, hermeticity, and the
committed example."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from sbxloop.config import Config, load_config, load_secrets_env
from sbxloop.paths import HOME_ENV, SbxloopHome

REPO_ROOT = Path(__file__).resolve().parents[2]

SENTINEL = "SBXLOOP_TEST_DOTENV_SENTINEL"


@pytest.fixture(autouse=True)
def _clean_sentinels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure vars our secrets files set never leak across tests."""
    for name in (SENTINEL, "SBXLOOP_MODEL", "COPILOT_GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    yield  # type: ignore[misc]
    os.environ.pop(SENTINEL, None)
    os.environ.pop("SBXLOOP_MODEL", None)


def secrets_file(root: Path, text: str) -> Path:
    home = SbxloopHome(root)
    home.config.mkdir(parents=True, exist_ok=True)
    home.secrets_env.write_text(text)
    return home.secrets_env


class TestLoadSecretsEnv:
    def test_loads_the_homes_file_into_environ(self, tmp_path: Path) -> None:
        # HOME is tmp_path (autouse fixture), so the home is tmp_path/.sbxloop.
        path = secrets_file(tmp_path / ".sbxloop", f"{SENTINEL}=from-secrets\n")
        assert load_secrets_env() == path
        assert os.environ[SENTINEL] == "from-secrets"

    def test_real_environment_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SENTINEL, "from-real-env")
        secrets_file(tmp_path / ".sbxloop", f"{SENTINEL}=from-secrets\n")
        load_secrets_env()
        assert os.environ[SENTINEL] == "from-real-env"

    def test_missing_file_is_noop(self) -> None:
        assert load_secrets_env() is None

    def test_sbxloop_home_relocates_the_file(self, tmp_path: Path) -> None:
        path = secrets_file(tmp_path / "elsewhere", f"{SENTINEL}=moved\n")
        assert load_secrets_env({HOME_ENV: str(tmp_path / "elsewhere")}) == path
        assert os.environ[SENTINEL] == "moved"

    def test_hermetic_mapping_reads_nothing(self, tmp_path: Path) -> None:
        secrets_file(tmp_path / ".sbxloop", f"{SENTINEL}=from-secrets\n")
        assert load_secrets_env({}) is None
        assert SENTINEL not in os.environ


class TestConfigIntegration:
    def test_load_config_reads_secrets_settings(self, tmp_path: Path) -> None:
        secrets_file(tmp_path / ".sbxloop", "SBXLOOP_MODEL=secrets-model\n")
        config = load_config(cwd=tmp_path)  # env=None -> real environ + secrets.env
        assert config.model == "secrets-model"

    def test_explicit_env_mapping_stays_hermetic(self, tmp_path: Path) -> None:
        secrets_file(tmp_path / ".sbxloop", "SBXLOOP_MODEL=secrets-model\n")
        config = load_config(cwd=tmp_path, env={})
        assert config.model == "auto"  # secrets.env not consulted
        assert "SBXLOOP_MODEL" not in os.environ  # and not loaded at all


class TestTrustBoundary:
    """A working-directory ``.env`` is never the loop's: a checkout's belongs
    to the application in it, and there is no runner directory any more."""

    def test_dotenv_in_the_working_directory_is_never_read(self, tmp_path: Path) -> None:
        work = tmp_path / "work"
        work.mkdir()
        (work / ".env").write_text(f"{SENTINEL}=the-applications-secret\n")
        assert load_secrets_env() is None
        assert SENTINEL not in os.environ
        config = load_config(cwd=work)
        assert config.model == "auto"

    def test_dotenv_inside_a_checkout_is_never_read(self, tmp_path: Path) -> None:
        from tests.unit.test_hostgit import make_repo

        root = make_repo(tmp_path)
        (root / ".env").write_text("SBXLOOP_MODEL=from-the-app\n")
        config = load_config(cwd=root)
        assert config.model == "auto"
        assert "SBXLOOP_MODEL" not in os.environ


class TestEnvExample:
    def test_example_exists_and_env_is_ignored(self) -> None:
        assert (REPO_ROOT / ".env.example").is_file()
        gitignore = (REPO_ROOT / ".gitignore").read_text().splitlines()
        assert ".env" in gitignore

    def test_example_documents_required_tokens(self) -> None:
        values = dotenv_values(REPO_ROOT / ".env.example")
        assert set(values) == {"COPILOT_GITHUB_TOKEN", "GH_TOKEN"}
        # The optional credentials are documented, commented out.
        text = (REPO_ROOT / ".env.example").read_text()
        for name in (
            "GITHUB_TOKEN",
            "DISCORD_BOT_TOKEN",
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN",
            "GH_TOKEN_TWO",
            "token_env",
        ):
            assert name in text
        assert all(not v for v in values.values())  # placeholders ship empty

    def test_example_documents_where_it_lives(self) -> None:
        text = (REPO_ROOT / ".env.example").read_text()
        assert "~/.sbxloop/config/secrets.env" in text
        assert "0600" in text
        assert "~/.config/sbxloop" not in text

    def test_example_names_every_credential_env_var_the_code_reads(self) -> None:
        from sbxloop.daemon.discord import TOKEN_ENV as DISCORD_TOKEN_ENV
        from sbxloop.daemon.slack import APP_TOKEN_ENV, BOT_TOKEN_ENV
        from sbxloop.sbx.provision import GH_TOKEN_ENVS
        from sbxloop.sbx.secretstate import COPILOT_TOKEN_ENV

        text = (REPO_ROOT / ".env.example").read_text()
        for name in (
            COPILOT_TOKEN_ENV,
            DISCORD_TOKEN_ENV,
            BOT_TOKEN_ENV,
            APP_TOKEN_ENV,
            *GH_TOKEN_ENVS,
        ):
            assert name in text, f"{name} missing from .env.example"

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
        assert config.github.repo == "you/your-repo"
