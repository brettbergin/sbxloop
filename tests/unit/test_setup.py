"""The interactive, repeatable setup of the three sbxloop integrations."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values
from typer.testing import CliRunner

from sbxloop.cli.app import app
from sbxloop.config import load_config
from sbxloop.paths import SbxloopHome

runner = CliRunner()


def _home(tmp_path: Path) -> SbxloopHome:
    return SbxloopHome(tmp_path / ".sbxloop")


def _config(tmp_path: Path):
    return load_config(
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "SBXLOOP_HOME": str(_home(tmp_path).root)},
    )


def test_setup_explains_and_writes_each_integration(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["setup"],
        input=(
            "codex\n"
            "agent-secret\n"
            "discord\n"
            "123456789\n"
            "discord-secret\n"
            "github\n"
            "\n"  # default public GitHub API root
            "acme/widgets\n"
            "github-secret\n"
        ),
        env={"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert "1. Agent backend (Codex, Claude, or OpenAI-compatible)" in result.output
    assert "2. Chat backend (Discord, Slack, or Mattermost)" in result.output
    assert "3. VCS (GitHub, Gitea, or GitLab)" in result.output
    assert "agent-secret" not in result.output
    assert "discord-secret" not in result.output
    assert "github-secret" not in result.output

    config = _config(tmp_path)
    assert config.agent.backend == "codex"
    assert config.chat_backend == "discord"
    assert config.discord.channel_id == 123456789
    assert config.vcs.kind == "github"
    assert config.vcs.api_url == "https://api.github.com"
    assert config.vcs.repos[0].repo == "acme/widgets"
    assert config.vcs.repos[0].kind == "github"
    # Written as the current spelling (#2255), not the legacy one.
    written = _home(tmp_path).config_toml.read_text()
    assert "[[vcs.repos]]" in written and "\n[[github.repos]]" not in written

    secrets = dotenv_values(_home(tmp_path).secrets_env)
    assert secrets["OPENAI_API_KEY"] == "agent-secret"
    assert secrets["DISCORD_BOT_TOKEN"] == "discord-secret"
    assert secrets["GH_TOKEN"] == "github-secret"
    if os.name != "nt":
        assert _home(tmp_path).secrets_env.stat().st_mode & 0o777 == 0o600


def test_setup_rerun_keeps_secrets_and_writes_nothing_twice(tmp_path: Path) -> None:
    first = runner.invoke(
        app,
        ["setup"],
        input=(
            "openai\n"
            "https://llm.example.com/v1\n"
            "LOCAL_LLM_KEY\n"
            "agent=${UNSET}#secret\n"
            "mattermost\n"
            "https://chat.example.com\n"
            "abcdefghijklmnopqrstuvwxyz\n"
            "mattermost-secret\n"
            "gitlab\n"
            "https://gitlab.example.com/api/v4\n"
            "acme/widgets\n"
            "gitlab-secret\n"
        ),
        env={"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
    )
    assert first.exit_code == 0, first.output
    home = _home(tmp_path)
    config_before = home.config_toml.read_bytes()
    secrets_before = home.secrets_env.read_bytes()

    second = runner.invoke(
        app,
        ["setup"],
        input="\n" * 12,
        env={"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
    )

    assert second.exit_code == 0, second.output
    assert home.config_toml.read_bytes() == config_before
    assert home.secrets_env.read_bytes() == secrets_before
    assert not list(home.config_toml.parent.glob("*.bak-*"))
    text = home.secrets_env.read_text()
    for name in ("LOCAL_LLM_KEY", "MATTERMOST_BOT_TOKEN", "GITLAB_TOKEN"):
        assert sum(line.startswith(f"{name}=") for line in text.splitlines()) == 1

    config = _config(tmp_path)
    assert config.agent.backend == "openai"
    assert config.agent.openai.base_url == "https://llm.example.com/v1"
    assert config.agent.openai.api_key_env == "LOCAL_LLM_KEY"
    assert config.chat_backend == "mattermost"
    assert config.mattermost.url == "https://chat.example.com"
    assert config.vcs.kind == "gitlab"
    assert config.vcs.token_env == "GITLAB_TOKEN"
    assert dotenv_values(home.secrets_env, interpolate=False)["LOCAL_LLM_KEY"] == (
        "agent=${UNSET}#secret"
    )


def test_setup_updates_existing_files_in_place(tmp_path: Path) -> None:
    home = _home(tmp_path)
    home.ensure_tree()
    home.config_toml.write_text(
        "# operator note\n"
        '[agent]\nbackend = "claude"\n'
        '[slack]\nchannel_id = "C0123ABCDE"\n'
        '[vcs]\nkind = "gitea"\napi_url = "https://old.example.com/api/v1"\n'
        '[[github.repos]]\nrepo = "old/repo"\nkind = "gitea"\n'
    )
    home.secrets_env.write_text(
        "# kept\nANTHROPIC_API_KEY=stale-agent\nANTHROPIC_API_KEY=old-agent\n"
        "SLACK_BOT_TOKEN=old-bot\n"
        "SLACK_APP_TOKEN=old-app\nGITEA_TOKEN=old-vcs\n"
    )

    result = runner.invoke(
        app,
        ["setup"],
        input=(
            "\n"  # claude
            "replacement-agent\n"
            "\n"  # slack
            "C9876ZYXWV\n"
            "replacement-bot\n"
            "replacement-app\n"
            "gitlab\n"
            "https://gitlab.example.com/api/v4\n"
            "new/repo\n"
            "replacement-vcs\n"
        ),
        env={"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert "# operator note" in home.config_toml.read_text()
    config = _config(tmp_path)
    assert config.agent.backend == "claude"
    assert config.slack.channel_id == "C9876ZYXWV"
    assert config.vcs.kind == "gitlab"
    assert config.vcs.repos[0].repo == "new/repo"
    assert config.vcs.repos[0].kind == "gitlab"
    # The legacy [[github.repos]] entry the file carried was migrated in
    # place (#2255), so the file now says [[vcs.repos]] and nothing else.
    written = home.config_toml.read_text()
    assert written.count("[[vcs.repos]]") == 1 and "\n[[github.repos]]" not in written
    secrets = dotenv_values(home.secrets_env)
    assert secrets["ANTHROPIC_API_KEY"] == "replacement-agent"
    assert secrets["SLACK_BOT_TOKEN"] == "replacement-bot"
    assert secrets["SLACK_APP_TOKEN"] == "replacement-app"
    assert secrets["GITLAB_TOKEN"] == "replacement-vcs"
    assert (
        sum(
            line.startswith("ANTHROPIC_API_KEY=")
            for line in home.secrets_env.read_text().splitlines()
        )
        == 1
    )


def test_setup_validates_the_whole_draft_before_writing(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["setup"],
        input=(
            "openai\n"
            "not-a-url\n"
            "OPENAI_API_KEY\n"
            "never-written-agent-secret\n"
            "discord\n"
            "123456789\n"
            "never-written-chat-secret\n"
            "github\n"
            "\n"
            "acme/widgets\n"
            "never-written-vcs-secret\n"
        ),
        env={"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
    )

    assert result.exit_code == 2
    assert "setup was not saved" in result.output
    assert "never-written" not in result.output
    assert not _home(tmp_path).config_toml.exists()
    assert not _home(tmp_path).secrets_env.exists()


def test_setup_offers_gitea_and_names_its_current_support_level(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["setup"],
        input=(
            "claude\n"
            "agent-secret\n"
            "discord\n"
            "123456789\n"
            "chat-secret\n"
            "gitea\n"
            "https://gitea.example.com/api/v1\n"
            "acme/widgets\n"
            "gitea-secret\n"
        ),
        env={"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert "Gitea settings will be saved" in result.output
    config = _config(tmp_path)
    assert config.vcs.kind == "gitea"
    assert config.vcs.api_url == "https://gitea.example.com/api/v1"
    assert config.vcs.token_env == "GITEA_TOKEN"
    assert dotenv_values(_home(tmp_path).secrets_env)["GITEA_TOKEN"] == "gitea-secret"
