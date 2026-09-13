"""The local collaboration API over the real daemon store."""

from __future__ import annotations

import time
from concurrent.futures import Future
from typing import Any

from sbxloop.daemon.concierge import ConciergeReply


class FakeConcierge:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
        self.calls.append({"text": text, **kwargs})
        future: Future[ConciergeReply] = Future()
        target = kwargs["session_key"].rsplit(":", 1)[-1]
        future.set_result(ConciergeReply(f"reply from {target}"))
        return future


def register(api: Any) -> dict[str, Any]:
    response = api.client.post(
        "/v1/auth/local/register",
        json={
            "email": "owner@example.test",
            "username": "owner",
            "password": "correct horse battery staple",
            "full_name": "Local Owner",
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def bearer(token: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {token['access_token']}"}


def test_local_onboarding_uses_existing_token_contract(api: Any) -> None:
    token = register(api)

    me = api.client.get("/v1/users/me", headers=bearer(token))
    assert me.status_code == 200
    assert me.json()["username"] == "owner"
    assert me.json()["timezone"] == "UTC"

    duplicate = api.client.post(
        "/v1/auth/local/register",
        json={
            "email": "other@example.test",
            "username": "other",
            "password": "another long password",
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "local_user_exists"

    login = api.client.post(
        "/v1/auth/local/login",
        json={"username": "owner", "password": "correct horse battery staple"},
    )
    assert login.status_code == 200
    assert login.json()["refresh_token"].startswith("rt_")

    # The pre-existing machine-client grant remains available unchanged.
    assert api.token()["token_type"] == "Bearer"


def test_channels_are_durable_revisioned_and_tombstoned(api: Any) -> None:
    headers = bearer(register(api))
    created = api.client.post("/v1/channels", json={"title": "First"}, headers=headers)
    assert created.status_code == 201
    channel = created.json()

    renamed = api.client.patch(
        f"/v1/channels/{channel['id']}", json={"title": "Renamed"}, headers=headers
    )
    assert renamed.status_code == 200
    assert renamed.json()["revision"] == channel["revision"] + 1

    listing = api.client.get("/v1/channels", headers=headers)
    assert listing.json()["items"][0]["title"] == "Renamed"

    deleted = api.client.delete(f"/v1/channels/{channel['id']}", headers=headers)
    assert deleted.status_code == 204
    assert api.client.get(f"/v1/channels/{channel['id']}", headers=headers).status_code == 404


def test_turns_keep_channel_memory_and_expand_team_mentions(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel_id = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    team = api.client.post(
        "/v1/teams",
        headers=headers,
        json={
            "name": "Builders",
            "slug": "builders",
            "agent_slugs": ["software-dev", "github"],
        },
    )
    assert team.status_code == 201, team.text

    accepted = api.client.post(
        f"/v1/channels/{channel_id}/turns",
        headers=headers,
        json={"content": "@builders please inspect this", "client_turn_id": "turn-1"},
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["turn"]["targets"] == ["software-dev", "github"]

    deadline = time.monotonic() + 2
    messages: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        messages = api.client.get(f"/v1/channels/{channel_id}/messages", headers=headers).json()
        if len(messages) == 3:
            break
        time.sleep(0.01)
    assert [message["role"] for message in messages] == ["user", "assistant", "assistant"]
    assert [message["agent_slug"] for message in messages[1:]] == ["software-dev", "github"]
    assert all(call["allow_actions"] for call in concierge.calls)
    assert concierge.calls[0]["session_key"] == f"{channel_id}:software-dev"

    replay = api.client.post(
        f"/v1/channels/{channel_id}/turns",
        headers=headers,
        json={"content": "@builders please inspect this", "client_turn_id": "turn-1"},
    )
    assert replay.status_code == 202
    assert replay.json()["replayed"] is True
    assert len(concierge.calls) == 2


def test_ordinary_conversation_cannot_use_action_tools(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel_id = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]

    response = api.client.post(
        f"/v1/channels/{channel_id}/turns",
        headers=headers,
        json={"content": "hello Angie"},
    )
    assert response.status_code == 202

    deadline = time.monotonic() + 2
    while not concierge.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert concierge.calls[0]["allow_actions"] is False
    assert concierge.calls[0]["session_key"] == f"{channel_id}:angie"


def test_preferences_are_durable_and_injected_into_new_turns(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))

    definitions = api.client.get("/v1/prompts/definitions", headers=headers)
    assert definitions.status_code == 200
    assert "personality" in {value["name"] for value in definitions.json()}

    updated = api.client.put(
        "/v1/prompts/personality",
        headers=headers,
        json={"content": "Be concise."},
    )
    assert updated.status_code == 200
    assert updated.json()["content"] == "# Personality\n\nBe concise.\n"

    channel_id = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    assert (
        api.client.post(
            f"/v1/channels/{channel_id}/turns",
            headers=headers,
            json={"content": "hello"},
        ).status_code
        == 202
    )
    deadline = time.monotonic() + 2
    while not concierge.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "User preferences:" in concierge.calls[0]["persona"]
    assert "Be concise." in concierge.calls[0]["persona"]


def test_workflow_definitions_keep_angie_crud_contract(api: Any) -> None:
    headers = bearer(register(api))
    created = api.client.post(
        "/v1/workflows",
        headers=headers,
        json={
            "name": "Issue triage",
            "slug": "issue-triage",
            "description": "Classify incoming issues",
            "trigger_event": "github.issue.opened",
        },
    )
    assert created.status_code == 201, created.text
    workflow = created.json()
    assert workflow["is_enabled"] is True

    changed = api.client.patch(
        f"/v1/workflows/{workflow['id']}",
        headers=headers,
        json={"is_enabled": False},
    )
    assert changed.status_code == 200
    assert changed.json()["is_enabled"] is False
    assert api.client.get("/v1/workflows", headers=headers).json()[0]["slug"] == "issue-triage"
    assert api.client.delete(f"/v1/workflows/{workflow['id']}", headers=headers).status_code == 204


def test_connections_report_operator_configuration_without_accepting_secrets(api: Any) -> None:
    headers = bearer(register(api))
    services = api.client.get("/v1/connections/services", headers=headers)
    assert services.status_code == 200
    assert {value["key"] for value in services.json()} >= {"github", "slack", "discord"}
    configured = api.client.get("/v1/connections", headers=headers)
    assert configured.status_code == 200
    assert all(value["masked_credentials"] for value in configured.json())
    assert "do-not-store" not in configured.text

    rejected = api.client.post(
        "/v1/connections",
        headers=headers,
        json={"service_type": "github", "credentials": {"token": "do-not-store"}},
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "operator_managed_connection"
