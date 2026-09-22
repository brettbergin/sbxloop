"""The local collaboration API over the real daemon store."""

from __future__ import annotations

import time
from concurrent.futures import Future
from typing import Any

import pytest

from sbxloop.daemon.concierge import ConciergeReply


class FakeConcierge:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.resets: list[str | None] = []

    def reset_session(self, session_key: str | None = None) -> None:
        self.resets.append(session_key)

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


def test_a_username_may_not_spell_a_user_id(api: Any) -> None:
    """User ids read ``usr_...`` and are public, so no username may look
    like one: a selector can never resolve to somebody else's account."""
    refused = api.client.post(
        "/v1/auth/local/register",
        json={
            "email": "mallory@example.test",
            "username": "usr_abcdef123456",
            "password": "correct horse battery staple",
        },
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "invalid_request"


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


def test_chat_messages_carry_turn_status_and_user_feedback_reactions(api: Any) -> None:
    api.ctx.concierge = FakeConcierge()
    headers = bearer(register(api))
    channel_id = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]

    accepted = api.client.post(
        f"/v1/channels/{channel_id}/turns",
        headers=headers,
        json={"content": "hello"},
    ).json()
    input_message = accepted["message"]
    assert input_message["reactions"] == ["⏳"]

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        turn = api.client.get(
            f"/v1/channels/{channel_id}/turns/{accepted['turn']['id']}", headers=headers
        ).json()
        if turn["status"] not in {"accepted", "running"}:
            break
        time.sleep(0.01)
    assert turn["status"] == "completed"
    messages = api.client.get(f"/v1/channels/{channel_id}/messages", headers=headers).json()
    assert messages[0]["reactions"] == ["✅"]

    reply = messages[1]
    route = f"/v1/channels/{channel_id}/messages/{reply['id']}/reaction"
    reacted = api.client.put(route, headers=headers, json={"emoji": "👍", "active": True})
    assert reacted.status_code == 200
    assert reacted.json()["reactions"] == ["👍"]
    repeated = api.client.put(route, headers=headers, json={"emoji": "👍", "active": True})
    assert repeated.json()["reactions"] == ["👍"]
    removed = api.client.put(route, headers=headers, json={"emoji": "👍", "active": False})
    assert removed.json()["reactions"] == []
    assert api.client.put(route, headers=headers, json={"emoji": "not-emoji"}).status_code == 422


def test_failed_and_cancelled_turns_receive_warning_reactions(api: Any) -> None:
    class FailingConcierge(FakeConcierge):
        def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
            self.calls.append({"text": text, **kwargs})
            future: Future[ConciergeReply] = Future()
            future.set_result(ConciergeReply("", ok=False, error="provider unavailable"))
            return future

    api.ctx.concierge = FailingConcierge()
    headers = bearer(register(api))
    channel_id = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    failed = api.client.post(
        f"/v1/channels/{channel_id}/turns",
        headers=headers,
        json={"content": "fail"},
    ).json()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        turn = api.client.get(
            f"/v1/channels/{channel_id}/turns/{failed['turn']['id']}", headers=headers
        ).json()
        if turn["status"] == "failed":
            break
        time.sleep(0.01)
    assert turn["status"] == "failed"
    messages = api.client.get(f"/v1/channels/{channel_id}/messages", headers=headers).json()
    assert messages[0]["reactions"] == ["⚠"]

    user = api.ctx.collaboration.user_by_username("owner")
    assert user is not None
    cancelled, _, _ = api.ctx.collaboration.accept_turn(
        user.id,
        channel_id,
        content="cancel",
        targets=(),
        client_turn_id="cancel-reaction",
        client_message_id=None,
        actor=None,
        now=api.clock(),
    )
    result = api.ctx.collaboration.cancel_turn(user.id, channel_id, cancelled.id, api.clock())
    assert result is not None and result.status == "cancelled"
    messages = api.client.get(f"/v1/channels/{channel_id}/messages", headers=headers).json()
    cancelled_input = next(
        message for message in messages if message["id"] == cancelled.input_message_id
    )
    assert cancelled_input["reactions"] == ["⚠"]


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


@pytest.mark.parametrize(
    ("intent", "contract"),
    [
        ("code", "selected sbxloop's Code runner"),
        ("workload", "selected sbxloop's Workload runner"),
    ],
)
def test_explicit_runner_selection_uses_angie_and_existing_pipeline(
    api: Any, intent: str, contract: str
) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel_id = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]

    response = api.client.post(
        f"/v1/channels/{channel_id}/turns",
        headers=headers,
        json={"content": "@critic prepare this result", "intent": intent},
    )
    assert response.status_code == 202, response.text
    assert response.json()["turn"]["targets"] == []

    deadline = time.monotonic() + 2
    while not concierge.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert concierge.calls[0]["agent_role"] == "concierge"
    assert concierge.calls[0]["allow_actions"] is True
    assert contract in concierge.calls[0]["persona"]
    assert "Do not simulate" in concierge.calls[0]["persona"]


def test_runner_selection_rejects_explicit_chat_targets(api: Any) -> None:
    api.ctx.concierge = FakeConcierge()
    headers = bearer(register(api))
    channel_id = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    response = api.client.post(
        f"/v1/channels/{channel_id}/turns",
        headers=headers,
        json={"content": "ship this", "intent": "code", "target_slugs": ["builder"]},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "runner_target_conflict"


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


def test_connections_report_operator_configuration_without_accepting_legacy_secrets(
    api: Any,
) -> None:
    headers = bearer(register(api))
    services = api.client.get("/v1/connections/services", headers=headers)
    assert services.status_code == 200
    assert {value["key"] for value in services.json()} >= {"github", "slack", "discord"}
    catalog = {value["key"]: value for value in services.json()}
    assert catalog["gitlab"]["available"] is True
    assert catalog["gitlab"]["fields"]
    assert catalog["gitea"]["available"] is False
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


def test_connections_read_actual_vcs_and_do_not_claim_unchecked_bridges(api: Any) -> None:
    headers = bearer(register(api))
    home = api.ctx.config.paths
    home.config_toml.parent.mkdir(parents=True, exist_ok=True)
    home.config_toml.write_text(
        '[vcs]\nkind = "gitlab"\napi_url = "https://gitlab.example.com/api/v4"\n'
        '[github]\nrepo = "o/r"\n[discord]\nchannel_id = 123456\n'
        '[chat]\nbackend = "discord"\n',
        encoding="utf-8",
    )
    home.secrets_env.write_text(
        "GITLAB_TOKEN=example\nDISCORD_BOT_TOKEN=example\n", encoding="utf-8"
    )
    response = api.client.get("/v1/connections", headers=headers)
    assert response.status_code == 200, response.text
    values = {entry["id"]: entry for entry in response.json()}
    assert values["gitlab"]["configured"] is True
    assert values["gitlab"]["status"] == "disconnected"
    assert values["discord"]["status"] == "disconnected"
    assert "GITLAB_TOKEN=example" not in response.text
    assert "DISCORD_BOT_TOKEN=example" not in response.text


def test_owner_can_save_and_remove_a_chat_connection(api: Any) -> None:
    headers = bearer(register(api))
    saved = api.client.put(
        "/v1/connections/mattermost",
        headers=headers,
        json={
            "settings": {
                "url": "https://chat.example.com",
                "channel_id": "abcdefghijklmnopqrstuvwxyz",
            },
            "credentials": {"bot_token": "secret-value"},
            "activate": True,
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is True
    assert saved.json()["restart_required"] is True
    assert saved.json()["status"] == "disconnected"
    assert "secret-value" not in saved.text
    home = api.ctx.config.paths
    assert "secret-value" in home.secrets_env.read_text("utf-8")
    assert 'backend = "mattermost"' in home.config_toml.read_text("utf-8")
    removed = api.client.delete("/v1/connections/mattermost", headers=headers)
    assert removed.status_code == 204, removed.text
    assert "secret-value" not in home.secrets_env.read_text("utf-8")
    after = api.client.get("/v1/connections", headers=headers).json()
    mattermost = next(entry for entry in after if entry["id"] == "mattermost")
    assert mattermost["configured"] is False
    assert mattermost["restart_required"] is True
    assert mattermost["masked_credentials"]["bot_token"] == "missing"


def test_owner_can_select_and_save_gitlab(api: Any) -> None:
    headers = bearer(register(api))
    response = api.client.put(
        "/v1/connections/gitlab",
        headers=headers,
        json={
            "settings": {"api_url": "https://gitlab.example.com/api/v4"},
            "credentials": {"personal_access_token": "private-gitlab-token"},
            "activate": True,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["configured"] is True
    assert response.json()["status"] == "disconnected"
    assert response.json()["restart_required"] is True
    assert "private-gitlab-token" not in response.text
    home = api.ctx.config.paths
    assert 'kind = "gitlab"' in home.config_toml.read_text("utf-8")
    assert 'api_url = "https://gitlab.example.com/api/v4"' in home.config_toml.read_text("utf-8")
    assert "private-gitlab-token" in home.secrets_env.read_text("utf-8")


def test_connection_write_refuses_host_overrides(api: Any, monkeypatch: Any) -> None:
    headers = bearer(register(api))
    monkeypatch.setenv("SBXLOOP_VCS__KIND", "github")
    overridden = api.client.put(
        "/v1/connections/gitlab",
        headers=headers,
        json={"settings": {"api_url": "https://gitlab.example.com/api/v4"}},
    )
    assert overridden.status_code == 409
    assert overridden.json()["code"] == "external_connection_setting"
    monkeypatch.delenv("SBXLOOP_VCS__KIND")

    monkeypatch.setenv("GITLAB_TOKEN", "host-token")
    external_secret = api.client.put(
        "/v1/connections/gitlab",
        headers=headers,
        json={"credentials": {"personal_access_token": "managed-token"}},
    )
    assert external_secret.status_code == 409
    assert external_secret.json()["code"] == "external_connection"
    assert "managed-token" not in external_secret.text


def test_connection_write_rejects_non_owner_and_unknown_fields(api: Any) -> None:
    headers = bearer(register(api))
    bad = api.client.put(
        "/v1/connections/discord",
        headers=headers,
        json={"settings": {"unknown": "value"}, "credentials": {"bot_token": "secret"}},
    )
    assert bad.status_code == 422
    assert "secret" not in bad.text

    machine = api.bearer(frozenset({"collaboration:read", "collaboration:write"}))
    forbidden = api.client.put(
        "/v1/connections/discord",
        headers=machine,
        json={"settings": {"channel_id": "123456"}, "credentials": {"bot_token": "secret"}},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "forbidden_role"
    assert "secret" not in forbidden.text
