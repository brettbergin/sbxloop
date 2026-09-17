"""Every authenticated request knows which workspace member, if any, is
calling. A registered user is their member row; a plain API client has no
member and keeps the reach its capabilities always gave it."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import OperationalError

from sbxloop.api.auth.deps import resolve_token


def _register(api: Any, **extra: Any) -> dict[str, Any]:
    body = {
        "email": "owner@example.test",
        "username": "owner",
        "password": "correct horse battery staple",
        **extra,
    }
    response = api.client.post("/v1/auth/local/register", json=body)
    assert response.status_code == 201, response.text
    return dict(response.json())


def _bearer(token: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {token['access_token']}"}


def test_a_registered_user_calls_as_the_workspace_owner(api: Any) -> None:
    token = _register(api)

    auth = resolve_token(api.ctx, token["access_token"])

    assert auth.member is not None
    assert auth.member.role == "owner"
    assert auth.member.user.username == "owner"
    assert auth.member.workspace_id == "local"

    # The single local user sees what it saw before.
    me = api.client.get("/v1/users/me", headers=_bearer(token))
    assert me.status_code == 200
    assert me.json()["username"] == "owner"
    assert api.client.get("/v1/channels", headers=_bearer(token)).status_code == 200


def test_a_plain_client_has_no_member_and_keeps_its_reach(api: Any) -> None:
    _register(api)
    token = api.token()

    auth = resolve_token(api.ctx, token["access_token"])

    assert auth.member is None
    assert api.client.get("/v1/runs", headers=_bearer(token)).status_code == 200
    assert api.client.get("/v1/events", headers=_bearer(token)).status_code == 200


def test_a_client_without_a_profile_is_refused_with_the_same_problem(api: Any) -> None:
    headers = api.bearer()
    for method, path in (
        ("GET", "/v1/users/me"),
        ("GET", "/v1/channels"),
        ("GET", "/v1/teams"),
        ("POST", "/v1/channels"),
    ):
        response = api.client.request(method, path, headers=headers, json={})
        assert response.status_code == 403, (path, response.text)
        assert response.json()["code"] == "local_profile_required", path


def test_a_removed_member_is_no_longer_the_local_profile(api: Any) -> None:
    owner = _register(api)
    store = api.ctx.collaboration
    owner_id = store.member_for_client(owner["client_id"]).user.id
    _, raw = store.create_invite("member", None, created_by=owner_id, ttl_s=60, now=api.clock())
    guest = _register(api, email="g@example.test", username="guest", invite_token=raw)
    store.remove_member(store.member_for_client(guest["client_id"]).user.id)

    assert resolve_token(api.ctx, guest["access_token"]).member is None
    response = api.client.get("/v1/users/me", headers=_bearer(guest))
    assert response.status_code == 403
    assert response.json()["code"] == "local_profile_required"


def test_last_seen_is_recorded_at_most_once_a_minute(api: Any) -> None:
    token = _register(api)
    store = api.ctx.collaboration
    user_id = store.member_for_client(token["client_id"]).user.id
    started = api.clock()

    assert api.client.get("/v1/users/me", headers=_bearer(token)).status_code == 200
    assert store.member_for_user(user_id).user.last_seen_at == started

    api.clock.t += 30
    assert api.client.get("/v1/users/me", headers=_bearer(token)).status_code == 200
    assert store.member_for_user(user_id).user.last_seen_at == started

    api.clock.t += 31
    assert api.client.get("/v1/users/me", headers=_bearer(token)).status_code == 200
    assert store.member_for_user(user_id).user.last_seen_at == started + 61


def test_a_failed_last_seen_write_never_fails_the_request(
    api: Any, monkeypatch: Any, caplog: Any
) -> None:
    token = _register(api)
    store = api.ctx.collaboration

    def busy(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("UPDATE local_users", {}, Exception("database is locked"))

    monkeypatch.setattr(store, "touch_last_seen", busy)

    with caplog.at_level(logging.DEBUG, logger="sbxloop.api.auth.deps"):
        me = api.client.get("/v1/users/me", headers=_bearer(token))
        # A stream's periodic access re-check resolves the token the same way.
        auth = resolve_token(api.ctx, token["access_token"])

    assert me.status_code == 200, me.text
    assert me.json()["username"] == "owner"
    assert auth.member is not None
    assert auth.member.user.username == "owner"
    assert any(
        record.levelno == logging.DEBUG and "last_seen" in record.getMessage()
        for record in caplog.records
    )


def test_a_turn_reports_the_unavailable_concierge_before_the_profile_check(api: Any) -> None:
    api.ctx.concierge = None
    response = api.client.post(
        "/v1/channels/any-channel/turns",
        headers=api.bearer(),
        json={"content": "hello", "client_turn_id": "turn-1"},
    )

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "collaboration_runtime_unavailable"
