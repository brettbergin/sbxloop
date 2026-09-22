"""Interactive setup for the agent, chat and version-control backends."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import typer
from dotenv import dotenv_values
from rich.console import Console

from sbxloop.configedit import keys as configkeys, toml as configtoml
from sbxloop.configedit.edit import save_text as save_config_text, validate_text
from sbxloop.configedit.secrets import save_text as save_secrets_text, upsert_text
from sbxloop.data import render_config_template, secrets_env_template
from sbxloop.paths import SbxloopHome

AGENT_BACKENDS = ("codex", "claude", "openai")
CHAT_BACKENDS = ("discord", "slack", "mattermost")
VCS_BACKENDS = ("github", "gitea", "gitlab")
GITHUB_TOKEN_ENV = "GH_TOKEN"  # nosec B105 - env var name, not a secret
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SetupError(ValueError):
    """A setup draft that cannot safely be written."""


def _file_value[T](text: str, dotted: str, default: T) -> T:
    value, present = configtoml.file_value(text, configkeys.parse_path(dotted))
    return cast("T", value) if present else default


def _choice(label: str, choices: tuple[str, ...], current: str) -> str:
    default = current if current in choices else choices[0]
    while True:
        value = _required(f"{label} ({'/'.join(choices)})", default).lower()
        if value in choices:
            return value
        typer.echo(f"Choose one of {', '.join(choices)}.")


def _required(label: str, current: str | None = None, *, hide_input: bool = False) -> str:
    default = current or None
    return cast(
        "str",
        typer.prompt(
            label,
            default=default,
            show_default=not hide_input and default is not None,
            hide_input=hide_input,
        ),
    ).strip()


def _secret(label: str, name: str, current: str | None) -> str:
    if current:
        replacement = cast(
            "str",
            typer.prompt(
                f"{label} ({name}; leave blank to keep the saved value)",
                default="",
                show_default=False,
                hide_input=True,
            ),
        )
        return replacement or current
    return cast("str", typer.prompt(f"{label} ({name})", hide_input=True))


def _env_name(label: str, current: str) -> str:
    while True:
        value = _required(label, current)
        if _ENV_NAME.fullmatch(value):
            return value
        typer.echo("Enter an environment variable name such as OPENAI_API_KEY.")


def _repo_key(text: str) -> tuple[str, str]:
    """The first ``[[vcs.repos]]`` entry's key and what it says now; the
    legacy spelling has been migrated before this is asked (#2255)."""
    repos, has_repos = configtoml.file_value(text, ("vcs", "repos"))
    if has_repos and isinstance(repos, list) and repos:
        return "vcs.repos[0].repo", str(repos[0].get("repo", ""))
    return "vcs.repos[0].repo", ""


def _chat_default(text: str) -> str:
    explicit = _file_value(text, "chat.backend", "")
    if explicit in CHAT_BACKENDS:
        return explicit
    configured = [
        backend
        for backend in CHAT_BACKENDS
        if _file_value(text, f"{backend}.channel_id", None) is not None
    ]
    return configured[0] if len(configured) == 1 else "discord"


def _set_many(text: str, settings: dict[str, Any], unsets: tuple[str, ...] = ()) -> str:
    for dotted, value in settings.items():
        text = configtoml.set_value(text, configkeys.parse_path(dotted), value)
    for dotted in unsets:
        text = configtoml.unset_value(text, configkeys.parse_path(dotted))
    return text


def _read(path: Path, fallback: Callable[[], str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return fallback()
    except (OSError, UnicodeDecodeError) as exc:
        raise SetupError(f"could not read {path}: {exc}") from exc


def run_setup(home: SbxloopHome, *, console: Console) -> None:
    """Prompt for and atomically upsert one complete sbxloop setup."""
    home.ensure_tree()
    config_before = _read(home.config_toml, render_config_template)
    secrets_before = _read(home.secrets_env, secrets_env_template)
    existing_secrets = (
        {
            name: value
            for name, value in dotenv_values(
                dotenv_path=home.secrets_env, interpolate=False
            ).items()
            if value is not None
        }
        if home.secrets_env.is_file()
        else {}
    )

    console.print(
        "sbxloop setup will configure these three integrations:\n"
        "  1. Agent backend (Codex, Claude, or OpenAI-compatible)\n"
        "  2. Chat backend (Discord, Slack, or Mattermost)\n"
        "  3. VCS (GitHub, Gitea, or GitLab)\n"
        "Secrets are hidden while entered and stored only in config/secrets.env."
    )

    settings: dict[str, Any] = {}
    secret_updates: dict[str, str] = {}

    console.print("\n[bold]1. Agent backend[/]")
    agent = _choice(
        "Agent backend", AGENT_BACKENDS, _file_value(config_before, "agent.backend", "codex")
    )
    settings["agent.backend"] = agent
    if agent == "claude":
        secret_updates["ANTHROPIC_API_KEY"] = _secret(
            "Anthropic API key", "ANTHROPIC_API_KEY", existing_secrets.get("ANTHROPIC_API_KEY")
        )
    elif agent == "codex":
        secret_updates["OPENAI_API_KEY"] = _secret(
            "OpenAI API key", "OPENAI_API_KEY", existing_secrets.get("OPENAI_API_KEY")
        )
    else:
        base_url = _required(
            "OpenAI-compatible API root",
            _file_value(config_before, "agent.openai.base_url", ""),
        )
        key_env = _env_name(
            "API key environment variable",
            _file_value(config_before, "agent.openai.api_key_env", "OPENAI_API_KEY"),
        )
        settings["agent.openai.base_url"] = base_url
        settings["agent.openai.api_key_env"] = key_env
        insecure = urlsplit(base_url).scheme.lower() == "http"
        if insecure:
            insecure = typer.confirm(
                "This endpoint uses plain HTTP. Allow the credential to travel unencrypted?",
                default=_file_value(config_before, "agent.openai.allow_insecure_endpoint", False),
            )
        settings["agent.openai.allow_insecure_endpoint"] = insecure
        secret_updates[key_env] = _secret(
            "Endpoint API key", key_env, existing_secrets.get(key_env)
        )

    console.print("\n[bold]2. Chat backend[/]")
    chat = _choice("Chat backend", CHAT_BACKENDS, _chat_default(config_before))
    settings["chat.backend"] = chat
    if chat == "discord":
        settings["discord.channel_id"] = typer.prompt(
            "Discord channel ID",
            type=int,
            default=_file_value(config_before, "discord.channel_id", None),
        )
        secret_updates["DISCORD_BOT_TOKEN"] = _secret(
            "Discord bot token", "DISCORD_BOT_TOKEN", existing_secrets.get("DISCORD_BOT_TOKEN")
        )
    elif chat == "slack":
        settings["slack.channel_id"] = _required(
            "Slack channel ID", _file_value(config_before, "slack.channel_id", "")
        )
        secret_updates["SLACK_BOT_TOKEN"] = _secret(
            "Slack bot token", "SLACK_BOT_TOKEN", existing_secrets.get("SLACK_BOT_TOKEN")
        )
        secret_updates["SLACK_APP_TOKEN"] = _secret(
            "Slack app-level token", "SLACK_APP_TOKEN", existing_secrets.get("SLACK_APP_TOKEN")
        )
    else:
        settings["mattermost.url"] = _required(
            "Mattermost instance URL", _file_value(config_before, "mattermost.url", "")
        )
        settings["mattermost.channel_id"] = _required(
            "Mattermost channel ID", _file_value(config_before, "mattermost.channel_id", "")
        )
        secret_updates["MATTERMOST_BOT_TOKEN"] = _secret(
            "Mattermost bot token",
            "MATTERMOST_BOT_TOKEN",
            existing_secrets.get("MATTERMOST_BOT_TOKEN"),
        )

    console.print("\n[bold]3. VCS[/]")
    old_vcs = _file_value(config_before, "vcs.kind", "github")
    vcs = _choice("VCS", VCS_BACKENDS, old_vcs)
    settings["vcs.kind"] = vcs
    token_env: str
    unsets: tuple[str, ...]
    if vcs == "github":
        github_api = _file_value(config_before, "github.api_url", "https://api.github.com")
        api_default = (
            _file_value(config_before, "vcs.api_url", github_api)
            if old_vcs == "github"
            else github_api
        )
        api_url = _required("GitHub API root", api_default)
        settings["vcs.api_url"] = api_url
        settings["github.api_url"] = api_url
        token_env = GITHUB_TOKEN_ENV
        unsets = ("vcs.token_env",)
    else:
        api_default = _file_value(config_before, "vcs.api_url", "") if old_vcs == vcs else ""
        settings["vcs.api_url"] = _required(f"{vcs.title()} API root", api_default)
        token_env = "GITLAB_TOKEN" if vcs == "gitlab" else "GITEA_TOKEN"
        settings["vcs.token_env"] = token_env
        unsets = ()
        if vcs == "gitea":
            console.print(
                "[yellow]Gitea settings will be saved, but that VCS backend is not "
                "implemented yet.[/]"
            )

    # A file still on the legacy spelling is moved to [[vcs.repos]] first
    # (#2255), so the repository is written where the loader expects it.
    try:
        config_before, _moved = configtoml.migrate_repos(config_before)
    except configtoml.ConfigWriteError as exc:
        raise SetupError(f"setup was not saved: {exc}") from exc
    repo_key, repo_default = _repo_key(config_before)
    settings[repo_key] = _required("Repository (owner/name)", repo_default)
    settings[repo_key.rsplit(".", 1)[0] + ".kind"] = vcs
    secret_updates[token_env] = _secret(
        f"{vcs.title()} token", token_env, existing_secrets.get(token_env)
    )

    try:
        config_after = _set_many(config_before, settings, unsets)
    except ValueError as exc:
        raise SetupError(f"setup was not saved: {exc}") from exc
    verdict = validate_text(
        config_after,
        home=home,
        env={**os.environ, "HOME": str(home.root.parent), "SBXLOOP_HOME": str(home.root)},
    )
    if not verdict.ok:
        raise SetupError(f"setup was not saved: {verdict.error}")
    secrets_after = upsert_text(secrets_before, secret_updates)

    try:
        if secrets_after != secrets_before:
            save_secrets_text(home.secrets_env, secrets_after)
        elif home.secrets_env.is_file():
            home.secrets_env.chmod(0o600)
        if config_after != config_before:
            save_config_text(home.config_toml, config_after)
    except OSError as exc:
        raise SetupError(f"could not save setup: {exc}") from exc

    console.print(f"\nConfiguration ready in {home.config_toml}", highlight=False)
    console.print(
        f"Secrets ready in {home.secrets_env} (values hidden, mode 0600)", highlight=False
    )
    console.print("Next: run `sbxloop doctor`, then restart the daemon if it is running.")


__all__ = ["SetupError", "run_setup"]
