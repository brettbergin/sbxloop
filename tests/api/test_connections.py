"""Connection checks must contact providers and never report presence as success."""

from __future__ import annotations

from typing import Any

import httpx

from sbxloop.api.routes import connections
from sbxloop.config import Config


def test_discord_probe_checks_bot_and_channel(monkeypatch: Any) -> None:
    seen: list[str] = []
    allowed = True

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["Authorization"] == "Bot private-token"
        if request.url.path.endswith("/channels/123456") and not allowed:
            return httpx.Response(403)
        return httpx.Response(200)

    real_client = httpx.Client
    monkeypatch.setattr(
        connections.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(answer), **kwargs),
    )
    config = Config.model_validate({"discord": {"channel_id": 123456}})
    secrets = {"DISCORD_BOT_TOKEN": "private-token"}
    good = connections._probe("discord", config, secrets)
    assert good.success is True
    assert good.status == "connected"
    assert len(seen) == 2
    assert "private-token" not in good.message

    allowed = False
    denied = connections._probe("discord", config, secrets)
    assert denied.success is False
    assert denied.status == "error"
    assert "private-token" not in denied.message


def test_list_becomes_verified_only_after_provider_check(api: Any, monkeypatch: Any) -> None:
    home = api.ctx.config.paths
    home.config_toml.parent.mkdir(parents=True, exist_ok=True)
    home.config_toml.write_text(
        '[discord]\nchannel_id = 123456\n[chat]\nbackend = "discord"\n', encoding="utf-8"
    )
    home.secrets_env.write_text("DISCORD_BOT_TOKEN=private-token\n", encoding="utf-8")
    headers = api.bearer()
    before = api.client.get("/v1/connections", headers=headers).json()
    assert before[0]["status"] == "disconnected"

    monkeypatch.setattr(
        connections,
        "_probe",
        lambda name, config, secrets: connections.ConnectionTestOut(
            success=True, status="connected", message="Provider verified"
        ),
    )
    checked = api.client.post("/v1/connections/discord/test", headers=headers)
    assert checked.status_code == 200
    assert checked.json()["success"] is True
    after = api.client.get("/v1/connections", headers=headers).json()
    assert after[0]["status"] == "connected"
    assert after[0]["last_tested_at"]

    monkeypatch.setattr(
        connections,
        "_probe",
        lambda name, config, secrets: connections.ConnectionTestOut(
            success=False, status="error", message="Provider refused credentials"
        ),
    )
    api.client.post("/v1/connections/discord/test", headers=headers)
    failed = api.client.get("/v1/connections", headers=headers).json()
    assert failed[0]["status"] == "error"


def test_github_app_is_reported_without_claiming_a_personal_token(api: Any) -> None:
    home = api.ctx.config.paths
    home.config_toml.parent.mkdir(parents=True, exist_ok=True)
    home.config_toml.write_text('[vcs]\nkind = "github"\n', encoding="utf-8")
    home.secrets_env.write_text(
        "GITHUB_APP_ID=123\nGITHUB_APP_INSTALLATION_ID=456\nGITHUB_APP_PRIVATE_KEY=key\n",
        encoding="utf-8",
    )
    response = api.client.get("/v1/connections", headers=api.bearer())
    assert response.status_code == 200
    github = next(value for value in response.json() if value["id"] == "github")
    assert github["configured"] is True
    assert github["masked_credentials"]["github_app"] == "configured"
    assert github["masked_credentials"]["personal_access_token"] == "missing"
    assert github["status"] == "disconnected"


def test_bridge_catalog_requires_active_backend_and_credentials(api: Any, monkeypatch: Any) -> None:
    api.ctx.config = Config.model_validate(
        {
            "home": str(api.ctx.config.paths.root),
            "github": {"repo": "o/r"},
            "chat": {"backend": "discord"},
            "discord": {"channel_id": 123456},
        }
    )
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    response = api.client.get("/v1/bridges", headers=api.bearer())
    assert response.status_code == 200
    assert {item["backend"]: item["configured"] for item in response.json()["data"]}[
        "discord"
    ] is False
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "private-token")
    response = api.client.get("/v1/bridges", headers=api.bearer())
    assert {item["backend"]: item["configured"] for item in response.json()["data"]}[
        "discord"
    ] is True
