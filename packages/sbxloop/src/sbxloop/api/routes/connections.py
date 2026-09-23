"""Owner-managed host connections. Secrets are write-only and stay in secrets.env.

The daemon consumes configuration at startup. A successful save therefore reports
``restart_required``; a provider check proves credentials and channel access but
does not claim that a bridge has reloaded or is running.
"""

from __future__ import annotations

import hashlib
import os
import threading
from datetime import UTC, datetime
from io import StringIO
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values
from fastapi import APIRouter, Depends

from sbxloop.api.auth.deps import Authenticated, get_ctx, ready_daemon, require, require_role
from sbxloop.api.collaboration_schemas import (
    ConnectionConfigure,
    ConnectionMutation,
    ConnectionOut,
    ConnectionTestOut,
    ServiceDefinitionOut,
)
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.chatservices import (
    CHAT_SERVICES,
    DISCORD_TOKEN_ENV,
    MATTERMOST_TOKEN_ENV,
    SLACK_APP_TOKEN_ENV,
    SLACK_BOT_TOKEN_ENV,
)
from sbxloop.config import FORGE_TOKEN_ENVS, Config, load_config_with_sources
from sbxloop.configedit import toml as configtoml
from sbxloop.configedit.edit import read_text, save_text as save_config_text, validate_text
from sbxloop.configedit.secrets import save_text as save_secrets_text, upsert_text
from sbxloop.vcs.backends import BACKENDS, SANDBOX_TOKEN_ENVS

router = APIRouter(prefix="/v1/connections", tags=["connections"])
_edit_lock = threading.Lock()

CHAT_NAMES = frozenset(service.name for service in CHAT_SERVICES)
SECRET_FIELDS: dict[str, dict[str, str]] = {
    "github": {"personal_access_token": SANDBOX_TOKEN_ENVS["github"][0]},
    "gitlab": {"personal_access_token": FORGE_TOKEN_ENVS["gitlab"]},
    "gitea": {"personal_access_token": FORGE_TOKEN_ENVS["gitea"]},
    "slack": {"bot_token": SLACK_BOT_TOKEN_ENV, "app_token": SLACK_APP_TOKEN_ENV},
    "discord": {"bot_token": DISCORD_TOKEN_ENV},
    "mattermost": {"bot_token": MATTERMOST_TOKEN_ENV},
}
SETTING_FIELDS: dict[str, frozenset[str]] = {
    "github": frozenset({"api_url"}),
    "gitlab": frozenset({"api_url", "token_env"}),
    "gitea": frozenset(),
    "slack": frozenset({"channel_id"}),
    "discord": frozenset({"channel_id"}),
    "mattermost": frozenset({"url", "channel_id"}),
}

SERVICE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "gitlab",
        "name": "GitLab",
        "description": "GitLab repositories, merge requests, and issues",
        "auth_type": "api_key",
        "color": "#FC6D26",
        "agent_slug": None,
        "fields": [
            {"key": "api_url", "label": "GitLab API URL", "type": "url"},
            {"key": "personal_access_token", "label": "Personal Access Token", "type": "password"},
        ],
    },
    {
        "key": "gitea",
        "name": "Gitea",
        "description": "Self-hosted Gitea repositories, pull requests, and issues",
        "auth_type": "api_key",
        "color": "#609926",
        "agent_slug": None,
        "fields": [],
        "available": False,
        "unavailable_reason": "This sbxloop version has no Gitea execution backend.",
    },
    {
        "key": "github",
        "name": "GitHub",
        "description": "Repository management through GitHub",
        "auth_type": "api_key",
        "color": "#333333",
        "agent_slug": "github",
        "fields": [
            {"key": "api_url", "label": "GitHub API URL", "type": "url"},
            {"key": "personal_access_token", "label": "Personal Access Token", "type": "password"},
        ],
    },
    {
        "key": "slack",
        "name": "Slack",
        "description": "Team messaging through sbxloop's Slack bridge",
        "auth_type": "token",
        "color": "#4A154B",
        "agent_slug": None,
        "fields": [
            {"key": "channel_id", "label": "Control channel ID", "type": "text"},
            {"key": "bot_token", "label": "Bot Token (xoxb-…)", "type": "password"},
            {"key": "app_token", "label": "App Token (xapp-…)", "type": "password"},
        ],
    },
    {
        "key": "discord",
        "name": "Discord",
        "description": "Community messaging through sbxloop's Discord bridge",
        "auth_type": "token",
        "color": "#5865F2",
        "agent_slug": None,
        "fields": [
            {"key": "channel_id", "label": "Control channel ID", "type": "text"},
            {"key": "bot_token", "label": "Bot Token", "type": "password"},
        ],
    },
    {
        "key": "mattermost",
        "name": "Mattermost",
        "description": "Self-hosted messaging through sbxloop's Mattermost bridge",
        "auth_type": "token",
        "color": "#0058CC",
        "agent_slug": None,
        "fields": [
            {"key": "url", "label": "Instance URL", "type": "url"},
            {"key": "channel_id", "label": "Control channel ID", "type": "text"},
            {"key": "bot_token", "label": "Bot Token", "type": "password"},
        ],
    },
)


def _known(name: str) -> None:
    if name not in SETTING_FIELDS:
        raise Problem(404, "connection_not_found", "connection not found")


def credential_snapshot(ctx: ApiContext) -> tuple[Config, dict[str, str]]:
    """The host's configuration and secrets as a check would see them now:
    what the connection routes and repository discovery read a credential
    from."""
    return _snapshot(ctx)


def _snapshot(ctx: ApiContext) -> tuple[Config, dict[str, str]]:
    home = ctx.config.paths
    if home.config_toml.is_file():
        config, _ = load_config_with_sources(
            cwd=home.root, env={**os.environ, "SBXLOOP_HOME": str(home.root)}
        )
    else:
        config = ctx.config
    saved = dotenv_values(home.secrets_env, interpolate=False) if home.secrets_env.is_file() else {}
    # A process export wins at startup. Preview this process's pending edits
    # from the file while reporting that a restart is still required.
    secrets = {**{key: value for key, value in saved.items() if value is not None}, **os.environ}
    pending_names = {
        env
        for service in ctx.connection_pending_restart
        for env in _credential_names(service, config).values()
    }
    for key in pending_names:
        value = saved.get(key)
        if value:
            secrets[key] = value
        else:
            secrets.pop(key, None)
    return config, secrets


def _credential_names(name: str, config: Config) -> dict[str, str]:
    names = dict(SECRET_FIELDS[name])
    if name in {"gitlab", "gitea"}:
        names["personal_access_token"] = config.vcs.token_env or FORGE_TOKEN_ENVS[name]
    return names


def _github_app_present(secrets: dict[str, str]) -> bool:
    return bool(
        secrets.get("GITHUB_APP_ID")
        and secrets.get("GITHUB_APP_INSTALLATION_ID")
        and (secrets.get("GITHUB_APP_PRIVATE_KEY") or secrets.get("GITHUB_APP_PRIVATE_KEY_PATH"))
    )


def _settings(name: str, config: Config) -> dict[str, str]:
    if name == "github":
        return {"api_url": config.github.api_url}
    if name in {"gitlab", "gitea"}:
        return {
            "api_url": config.vcs.api_url or "",
            "token_env": config.vcs.token_env or FORGE_TOKEN_ENVS[name],
        }
    section = getattr(config, name)
    result = {"channel_id": section.channel_ref}
    if name == "mattermost":
        result["url"] = section.url or ""
    return result


def _fingerprint(
    name: str, settings: dict[str, str], secrets: dict[str, str], names: dict[str, str]
) -> str:
    payload = repr(
        (
            name,
            sorted(settings.items()),
            [(field, secrets.get(env, "")) for field, env in names.items()],
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _record(
    ctx: ApiContext, name: str, config: Config, secrets: dict[str, str]
) -> ConnectionOut | None:
    names = _credential_names(name, config)
    settings = _settings(name, config)
    provided = {field: bool(secrets.get(env)) for field, env in names.items()}
    if name == "github":
        provided["personal_access_token"] = bool(
            secrets.get("GH_TOKEN") or secrets.get("GITHUB_TOKEN")
        )
        if _github_app_present(secrets):
            provided["github_app"] = True
    if name in CHAT_NAMES:
        section = getattr(config, name)
        selected = bool(section.enabled)
        active = ctx.config.chat_backend == name and ctx.config.chat_section(name).enabled
        present = selected or any(provided.values()) or name in ctx.connection_pending_restart
        complete = selected and all(provided.values())
    else:
        selected = name in config.vcs_kinds() or config.vcs.kind == name
        active = name in ctx.config.vcs_kinds()
        present = (selected and config.vcs.enabled) or any(provided.values())
        if name in {"gitlab", "gitea"}:
            present = present or selected
        has_credentials = any(provided.values()) if name == "github" else all(provided.values())
        complete = selected and has_credentials and (name == "github" or bool(settings["api_url"]))
    if not present:
        return None
    fingerprint = _fingerprint(name, settings, secrets, names)
    checked = ctx.connection_checks.get(name)
    status: Literal["connected", "expired", "error", "disconnected"] = (
        "error"
        if not complete
        else checked[2]
        if checked is not None and checked[0] == fingerprint
        else "disconnected"
    )
    tested_at = (
        datetime.fromtimestamp(checked[1], tz=UTC).isoformat()
        if checked is not None and checked[0] == fingerprint
        else None
    )
    return ConnectionOut(
        id=name,
        service_type=name,
        display_name="sbxloop host",
        auth_type="token" if name in CHAT_NAMES else "api_key",
        status=status,
        configured=complete,
        active=active,
        restart_required=name in ctx.connection_pending_restart,
        settings=settings,
        last_tested_at=tested_at,
        masked_credentials={
            field: "configured" if present else "missing" for field, present in provided.items()
        },
    )


def _list(ctx: ApiContext) -> list[ConnectionOut]:
    config, secrets = _snapshot(ctx)
    return [record for name in SETTING_FIELDS if (record := _record(ctx, name, config, secrets))]


def _update(ctx: ApiContext, name: str, body: ConnectionConfigure) -> ConnectionOut:
    with _edit_lock:
        return _update_locked(ctx, name, body)


def _update_locked(ctx: ApiContext, name: str, body: ConnectionConfigure) -> ConnectionOut:
    _known(name)
    if name not in BACKENDS and name not in CHAT_NAMES:
        raise Problem(409, "connection_unavailable", "this service has no execution backend")
    unknown = set(body.settings) - SETTING_FIELDS[name]
    unknown_secrets = set(body.credentials) - set(SECRET_FIELDS[name])
    if unknown or unknown_secrets:
        raise Problem(422, "invalid_connection_field", "unknown connection setting or credential")
    if any(
        len(value) > 8192 or "\n" in value or "\r" in value for value in body.credentials.values()
    ):
        raise Problem(
            422, "invalid_credential", "a credential must be one line and at most 8192 characters"
        )
    if any(len(value) > 2048 for value in body.settings.values()):
        raise Problem(422, "invalid_connection_field", "a setting is too long")
    for key in ("api_url", "url"):
        value = body.settings.get(key)
        if value:
            parsed = urlsplit(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise Problem(
                    422,
                    "invalid_connection_setting",
                    "URLs may not include credentials, query, or fragment",
                )
    if name == "gitlab" and "token_env" in body.settings:
        existing_config, _ = _snapshot(ctx)
        existing = existing_config.vcs.token_env or FORGE_TOKEN_ENVS["gitlab"]
        if body.settings["token_env"] != existing:
            raise Problem(
                422, "invalid_connection_setting", "change the token environment name on the host"
            )
    if name == "github" and body.credentials.get("personal_access_token"):
        _, current_secrets = _snapshot(ctx)
        if _github_app_present(current_secrets):
            raise Problem(
                409,
                "github_auth_conflict",
                "remove the host's GitHub App credentials before saving a personal token",
            )

    home = ctx.config.paths
    current, _ = read_text(home.config_toml)
    draft = current
    managed_paths: set[str] = set()
    try:
        if name in {"github", "gitlab"} and body.activate:
            draft = configtoml.set_value(draft, ("vcs", "kind"), name)
            managed_paths.add("vcs.kind")
            if name == "github":
                draft = configtoml.unset_value(draft, ("vcs", "api_url"))
                managed_paths.add("vcs.api_url")
        if name in CHAT_NAMES and body.activate:
            draft = configtoml.set_value(draft, ("chat", "backend"), name)
            managed_paths.add("chat.backend")
        for key, value in body.settings.items():
            path = (
                ("github", "api_url")
                if name == "github"
                else ("vcs", key)
                if name == "gitlab"
                else (name, key)
            )
            managed_paths.add(".".join(path))
            if key == "channel_id" and name == "discord" and value.strip():
                draft = configtoml.set_value(draft, path, int(value))
            elif value.strip():
                draft = configtoml.set_value(draft, path, value.strip())
            else:
                draft = configtoml.unset_value(draft, path)
    except (ValueError, TypeError) as exc:
        raise Problem(422, "invalid_connection_setting", str(exc)) from None
    verdict = validate_text(draft, home=home, env=os.environ)
    if not verdict.ok or verdict.config is None:
        raise Problem(
            422, "invalid_connection_setting", verdict.error or "configuration was refused"
        )
    if any(verdict.sources.get(path) not in (None, "home config") for path in managed_paths):
        raise Problem(
            409,
            "external_connection_setting",
            "a host or repository override controls this setting; edit it at its source",
        )
    config = verdict.config
    credential_names = _credential_names(name, config)
    updates = {credential_names[field]: value for field, value in body.credentials.items() if value}
    secrets_before = home.secrets_env.read_text("utf-8") if home.secrets_env.is_file() else ""
    saved_secrets = dotenv_values(stream=StringIO(secrets_before), interpolate=False)
    if any(os.environ.get(key) and os.environ[key] != saved_secrets.get(key) for key in updates):
        raise Problem(
            409,
            "external_connection",
            "a host credential overrides this managed secret; edit it at its source",
        )
    secrets_after = upsert_text(secrets_before, updates)
    parsed_updates = dotenv_values(stream=StringIO(secrets_after), interpolate=False)
    if any(parsed_updates.get(key) != value for key, value in updates.items()):
        raise Problem(422, "invalid_credential", "credential could not be saved faithfully")
    try:
        if secrets_after != secrets_before:
            save_secrets_text(home.secrets_env, secrets_after)
        if draft != current:
            save_config_text(home.config_toml, draft)
    except OSError as exc:
        if secrets_after != secrets_before:
            save_secrets_text(home.secrets_env, secrets_before)
        raise Problem(500, "connection_save_failed", "could not save the connection") from exc
    if draft != current or secrets_after != secrets_before:
        ctx.connection_pending_restart.add(name)
        ctx.connection_checks.pop(name, None)
    saved_config, secrets = _snapshot(ctx)
    result = _record(ctx, name, saved_config, secrets)
    assert result is not None  # A successful configure creates the record.
    return result


def _remove(ctx: ApiContext, name: str) -> None:
    with _edit_lock:
        _remove_locked(ctx, name)


def _remove_locked(ctx: ApiContext, name: str) -> None:
    _known(name)
    config, secrets = _snapshot(ctx)
    if _record(ctx, name, config, secrets) is None:
        raise Problem(404, "connection_not_found", "connection not found")
    home = ctx.config.paths
    current, _ = read_text(home.config_toml)
    draft = current
    if name in CHAT_NAMES:
        draft = configtoml.unset_value(draft, (name, "channel_id"))
        if config.chat.backend == name:
            draft = configtoml.unset_value(draft, ("chat", "backend"))
    # A forge may still be named by repositories. Removing its managed token
    # must not silently retarget a repository to another forge.
    names = _credential_names(name, config)
    before = home.secrets_env.read_text("utf-8") if home.secrets_env.is_file() else ""
    saved = dotenv_values(home.secrets_env, interpolate=False) if home.secrets_env.is_file() else {}
    if any(secrets.get(env) and secrets[env] != saved.get(env) for env in names.values()):
        raise Problem(
            409,
            "external_connection",
            "this connection has a credential outside sbxloop's managed secret file",
        )
    owned = {env: "" for env in names.values() if saved.get(env)}
    if not owned and draft == current:
        raise Problem(
            409,
            "external_connection",
            "this connection is configured outside sbxloop's managed files",
        )
    after = upsert_text(before, owned)
    verdict = validate_text(draft, home=home, env=os.environ)
    if not verdict.ok:
        raise Problem(
            422, "invalid_connection_setting", verdict.error or "configuration was refused"
        )
    try:
        if after != before:
            save_secrets_text(home.secrets_env, after)
        if draft != current:
            save_config_text(home.config_toml, draft)
    except OSError as exc:
        if after != before:
            save_secrets_text(home.secrets_env, before)
        raise Problem(500, "connection_save_failed", "could not remove the connection") from exc
    ctx.connection_pending_restart.add(name)
    ctx.connection_checks.pop(name, None)


def _probe(name: str, config: Config, secrets: dict[str, str]) -> ConnectionTestOut:
    names = _credential_names(name, config)
    if name == "github" and not secrets.get("GH_TOKEN") and secrets.get("GITHUB_TOKEN"):
        secrets = {**secrets, "GH_TOKEN": secrets["GITHUB_TOKEN"]}
    if name == "github" and _github_app_present(secrets) and not secrets.get("GH_TOKEN"):
        return ConnectionTestOut(
            success=False,
            status="disconnected",
            message=(
                "GitHub App is configured on the host; validate its installation "
                "with sbxloop doctor"
            ),
        )
    if any(not secrets.get(env) for env in names.values()):
        return ConnectionTestOut(
            success=False, status="error", message="A required credential is missing"
        )
    settings = _settings(name, config)
    if name == "gitlab" and not settings["api_url"]:
        return ConnectionTestOut(
            success=False, status="error", message="The GitLab API URL is missing"
        )
    if name in CHAT_NAMES and not settings["channel_id"]:
        return ConnectionTestOut(
            success=False, status="error", message="The control channel ID is missing"
        )
    try:
        with httpx.Client(timeout=8.0, follow_redirects=False) as client:
            if name == "github":
                response = client.get(
                    f"{settings['api_url'].rstrip('/')}/user",
                    headers={
                        "Authorization": f"Bearer {secrets[names['personal_access_token']]}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                ok = response.status_code == 200
            elif name == "gitlab":
                response = client.get(
                    f"{settings['api_url'].rstrip('/')}/user",
                    headers={"PRIVATE-TOKEN": secrets[names["personal_access_token"]]},
                )
                ok = response.status_code == 200
            elif name == "discord":
                headers = {"Authorization": f"Bot {secrets[names['bot_token']]}"}
                response = client.get("https://discord.com/api/v10/users/@me", headers=headers)
                ok = (
                    response.status_code == 200
                    and client.get(
                        f"https://discord.com/api/v10/channels/{settings['channel_id']}",
                        headers=headers,
                    ).status_code
                    == 200
                )
            elif name == "mattermost":
                headers = {"Authorization": f"Bearer {secrets[names['bot_token']]}"}
                root = settings["url"].rstrip("/") + "/api/v4"
                response = client.get(f"{root}/users/me", headers=headers)
                ok = (
                    response.status_code == 200
                    and client.get(
                        f"{root}/channels/{settings['channel_id']}", headers=headers
                    ).status_code
                    == 200
                )
            else:
                bot = {"Authorization": f"Bearer {secrets[names['bot_token']]}"}
                app = {"Authorization": f"Bearer {secrets[names['app_token']]}"}
                response = client.post("https://slack.com/api/auth.test", headers=bot)
                bot_ok = response.status_code == 200 and response.json().get("ok") is True
                channel = (
                    client.get(
                        "https://slack.com/api/conversations.info",
                        headers=bot,
                        params={"channel": settings["channel_id"]},
                    )
                    if bot_ok
                    else None
                )
                app_response = (
                    client.post("https://slack.com/api/apps.connections.open", headers=app)
                    if bot_ok
                    else None
                )
                ok = bool(
                    channel
                    and channel.status_code == 200
                    and channel.json().get("ok") is True
                    and app_response
                    and app_response.status_code == 200
                    and app_response.json().get("ok") is True
                )
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return ConnectionTestOut(
            success=False, status="error", message="Could not reach or validate the provider"
        )
    return ConnectionTestOut(
        success=ok,
        status="connected" if ok else "error",
        message="Provider credentials and channel access verified"
        if ok and name in CHAT_NAMES
        else "Provider credentials verified"
        if ok
        else "The provider refused the credentials or channel access",
    )


@router.get("/services", response_model=list[ServiceDefinitionOut])
async def services(
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> list[ServiceDefinitionOut]:
    return [ServiceDefinitionOut.model_validate(item) for item in SERVICE_DEFINITIONS]


@router.get("", response_model=list[ConnectionOut])
async def list_connections(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> list[ConnectionOut]:
    return await ctx.call(_list, ctx)


@router.get("/{connection_id}", response_model=ConnectionOut)
async def get_connection(
    connection_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> ConnectionOut:
    _known(connection_id)
    values = await ctx.call(_list, ctx)
    found = next((value for value in values if value.id == connection_id), None)
    if found is None:
        raise Problem(404, "connection_not_found", "connection not found")
    return found


@router.post("", response_model=ConnectionOut)
async def legacy_create(
    _body: ConnectionMutation,
    _auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> ConnectionOut:
    raise Problem(
        409, "operator_managed_connection", "use the owner-only service configuration route"
    )


@router.patch("/{connection_id}", response_model=ConnectionOut)
async def legacy_patch(
    connection_id: str,
    _body: ConnectionMutation,
    _auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> ConnectionOut:
    _ = connection_id
    raise Problem(
        409, "operator_managed_connection", "use the owner-only service configuration route"
    )


@router.put("/{connection_id}", response_model=ConnectionOut)
async def configure_connection(
    connection_id: str,
    body: ConnectionConfigure,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    _auth: Authenticated = Depends(require_role("owner")),  # noqa: B008
) -> ConnectionOut:
    return await ctx.call(_update, ctx, connection_id, body)


@router.delete("/{connection_id}", status_code=204)
async def remove_connection(
    connection_id: str,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    _auth: Authenticated = Depends(require_role("owner")),  # noqa: B008
) -> None:
    await ctx.call(_remove, ctx, connection_id)


@router.post("/{connection_id}/test", response_model=ConnectionTestOut)
async def test_connection(
    connection_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> ConnectionTestOut:
    _known(connection_id)
    if connection_id not in BACKENDS and connection_id not in CHAT_NAMES:
        raise Problem(409, "connection_unavailable", "this service has no execution backend")
    config, secrets = await ctx.call(_snapshot, ctx)
    record = _record(ctx, connection_id, config, secrets)
    if record is None:
        raise Problem(404, "connection_not_found", "connection not found")
    result = await ctx.call(_probe, connection_id, config, secrets)
    fingerprint = _fingerprint(
        connection_id,
        _settings(connection_id, config),
        secrets,
        _credential_names(connection_id, config),
    )
    ctx.connection_checks[connection_id] = (
        fingerprint,
        ctx.clock(),
        result.status,
        result.message,
    )
    return result
