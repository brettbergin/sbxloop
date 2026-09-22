"""Workspace membership: roles, invites and the capabilities a role grants.

The installation used to hold exactly one local user. These tests pin the
multi-user groundwork: the first user owns the workspace, a second user can
only join through an invite, an invite is stored as a hash and expires, the
last owner cannot be demoted, and a member's API client holds what the role
grants and nothing more.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

import sbxloop.api.collaboration as collaboration_module
from sbxloop.api.collaboration import CollaborationError, Member
from sbxloop.api.routes.workspace import router as workspace_router
from sbxloop.daemon.controls.principal import ALL_CAPABILITIES, ROLE_CAPABILITIES
from sbxloop.db.api_models import ApiEventRow, ClientRow, RefreshTokenRow
from sbxloop.db.collaboration_models import WorkspaceInviteRow, WorkspaceMemberRow

OWNER = {
    "email": "owner@example.test",
    "username": "owner",
    "password": "correct horse battery staple",
}


def _register(api: Any, body: dict[str, Any]) -> Any:
    return api.client.post("/v1/auth/local/register", json=body)


def _client_capabilities(api: Any, client_id: str) -> set[str]:
    with api.ctx.collaboration.dstore.read() as session:
        row = session.get(ClientRow, client_id)
        assert row is not None
        return set(json.loads(row.capabilities_json))


def _owner(api: Any) -> Member:
    response = _register(api, OWNER)
    assert response.status_code == 201, response.text
    member = api.ctx.collaboration.member_for_client(response.json()["client_id"])
    assert member is not None
    return member


def test_the_first_user_owns_the_workspace_with_every_capability(api: Any) -> None:
    owner = _owner(api)

    assert owner.role == "owner"
    assert owner.workspace_id == "local"
    assert owner.user.username == "owner"
    assert _client_capabilities(api, owner.user.client_id) == set(ALL_CAPABILITIES)
    assert [m.user.id for m in api.ctx.collaboration.list_members()] == [owner.user.id]


def test_a_second_user_without_an_invite_is_still_refused(api: Any) -> None:
    _owner(api)
    response = _register(
        api, {"email": "b@example.test", "username": "b", "password": "another password"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "local_user_exists"


def test_an_invite_admits_a_member_with_the_role_capabilities(api: Any) -> None:
    owner = _owner(api)
    store = api.ctx.collaboration
    invite, raw = store.create_invite(
        "member", "b@example.test", created_by=owner.user.id, ttl_s=3600, now=api.clock()
    )

    response = _register(
        api,
        {
            "email": "b@example.test",
            "username": "b",
            "password": "another password",
            "invite_token": raw,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    member = store.member_for_client(body["client_id"])
    assert member is not None
    assert member.role == "member"
    assert member.user.username == "b"

    granted = _client_capabilities(api, member.user.client_id)
    assert granted == {
        "runs:read",
        "runs:steer",
        "items:create",
        "collaboration:read",
        "collaboration:write",
        "collaboration:delegate",
    }
    assert "artifacts:read" not in body["scope"].split()
    assert "credentials:manage" not in body["scope"].split()

    # The invite is spent: it cannot admit anyone else.
    again = _register(
        api,
        {
            "email": "c@example.test",
            "username": "c",
            "password": "yet another password",
            "invite_token": raw,
        },
    )
    assert again.status_code == 403
    assert again.json()["code"] == "invite_invalid"
    assert invite.role == "member"


def test_an_invite_is_stored_only_as_a_hash(api: Any) -> None:
    owner = _owner(api)
    invite, raw = api.ctx.collaboration.create_invite(
        "admin", None, created_by=owner.user.id, ttl_s=60, now=api.clock()
    )
    with api.ctx.collaboration.dstore.read() as session:
        rows = session.scalars(select(WorkspaceInviteRow)).all()
    assert len(rows) == 1
    assert rows[0].id == invite.id
    assert rows[0].token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in {str(value) for value in vars(rows[0]).values()}


def test_an_expired_invite_is_refused(api: Any) -> None:
    owner = _owner(api)
    _, raw = api.ctx.collaboration.create_invite(
        "member", None, created_by=owner.user.id, ttl_s=60, now=api.clock()
    )
    api.clock.t += 61
    response = _register(
        api,
        {
            "email": "late@example.test",
            "username": "late",
            "password": "a late password",
            "invite_token": raw,
        },
    )
    assert response.status_code == 403
    assert response.json()["code"] == "invite_expired"
    assert len(api.ctx.collaboration.list_members()) == 1


def test_an_unknown_invite_is_refused(api: Any) -> None:
    _owner(api)
    response = _register(
        api,
        {
            "email": "x@example.test",
            "username": "x",
            "password": "a guessing password",
            "invite_token": "inv_not-a-real-token",
        },
    )
    assert response.status_code == 403
    assert response.json()["code"] == "invite_invalid"


def test_the_last_owner_cannot_be_demoted_or_removed(api: Any) -> None:
    owner = _owner(api)
    store = api.ctx.collaboration
    with pytest.raises(CollaborationError) as demoted:
        store.set_role(owner.user.id, "admin")
    assert demoted.value.code == "last_owner"
    with pytest.raises(CollaborationError) as removed:
        store.remove_member(owner.user.id)
    assert removed.value.code == "last_owner"
    member = store.member_for_user(owner.user.id)
    assert member is not None and member.role == "owner"


def test_a_role_change_rewrites_the_client_capabilities(api: Any) -> None:
    owner = _owner(api)
    store = api.ctx.collaboration
    _, raw = store.create_invite(
        "member", None, created_by=owner.user.id, ttl_s=60, now=api.clock()
    )
    body = _register(
        api,
        {
            "email": "b@example.test",
            "username": "b",
            "password": "another password",
            "invite_token": raw,
        },
    ).json()
    user_id = store.member_for_client(body["client_id"]).user.id

    promoted = store.set_role(user_id, "admin")
    assert promoted.role == "admin"
    assert _client_capabilities(api, body["client_id"]) == set(ROLE_CAPABILITIES["admin"])
    assert "credentials:manage" not in _client_capabilities(api, body["client_id"])

    # With a second owner, the first may step down.
    store.set_role(user_id, "owner")
    assert store.set_role(owner.user.id, "member").role == "member"
    assert _client_capabilities(api, owner.user.client_id) == set(ROLE_CAPABILITIES["member"])


def test_a_removed_member_loses_membership_and_capabilities(api: Any) -> None:
    owner = _owner(api)
    store = api.ctx.collaboration
    _, raw = store.create_invite(
        "member", None, created_by=owner.user.id, ttl_s=60, now=api.clock()
    )
    body = _register(
        api,
        {
            "email": "b@example.test",
            "username": "b",
            "password": "another password",
            "invite_token": raw,
        },
    ).json()
    user_id = store.member_for_client(body["client_id"]).user.id

    assert store.remove_member(user_id) is True
    assert store.member_for_user(user_id) is None
    assert _client_capabilities(api, body["client_id"]) == set()
    assert store.remove_member(user_id) is False


def test_accepting_an_invite_adds_an_existing_user_once(api: Any) -> None:
    owner = _owner(api)
    store = api.ctx.collaboration
    _, first = store.create_invite(
        "member", None, created_by=owner.user.id, ttl_s=60, now=api.clock()
    )
    body = _register(
        api,
        {
            "email": "b@example.test",
            "username": "b",
            "password": "another password",
            "invite_token": first,
        },
    ).json()
    user_id = store.member_for_client(body["client_id"]).user.id
    store.remove_member(user_id)

    _, second = store.create_invite(
        "admin", None, created_by=owner.user.id, ttl_s=60, now=api.clock()
    )
    member = store.accept_invite(second, user_id, now=api.clock())
    assert member.role == "admin"
    assert member.user.id == user_id
    assert _client_capabilities(api, body["client_id"]) == set(ROLE_CAPABILITIES["admin"])

    _, third = store.create_invite(
        "member", None, created_by=owner.user.id, ttl_s=60, now=api.clock()
    )
    with pytest.raises(CollaborationError) as twice:
        store.accept_invite(third, user_id, now=api.clock())
    assert twice.value.code == "already_member"


def test_role_capabilities_follow_the_documented_mapping() -> None:
    assert ROLE_CAPABILITIES["owner"] == ALL_CAPABILITIES
    assert ROLE_CAPABILITIES["admin"] == ALL_CAPABILITIES - {"credentials:manage"}
    assert "artifacts:read" not in ROLE_CAPABILITIES["member"]
    assert "daemon:manage" not in ROLE_CAPABILITIES["member"]


# -- the member, invite and directory routes ----------------------------------------
#
# Contract (plan S-P5, consumed by Angie's members page): any member reads the
# directory; admins and owners manage members and invites; only an owner
# grants, removes or acts on the owner role; nobody deactivates or removes
# themselves; the workspace keeps an owner; a plain API client counts as an
# owner only when it holds daemon:manage.

DIRECTORY_KEYS = {
    "id",
    "username",
    "email",
    "full_name",
    "avatar_url",
    "role",
    "is_active",
    "auth_source",
    "last_seen_at",
}


def _headers(token: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {token['access_token']}"}


def _owner_token(api: Any) -> dict[str, Any]:
    response = _register(api, OWNER)
    assert response.status_code == 201, response.text
    return dict(response.json())


def _join(api: Any, role: str, name: str) -> tuple[dict[str, Any], str]:
    """A new user with ``role``, admitted through an invite; their token and id."""
    store = api.ctx.collaboration
    _, raw = store.create_invite(role, None, created_by=_owner_id(api), ttl_s=3600, now=api.clock())
    response = _register(
        api,
        {
            "email": f"{name}@example.test",
            "username": name,
            "password": f"{name} has a long password",
            "invite_token": raw,
        },
    )
    assert response.status_code == 201, response.text
    token = dict(response.json())
    member = store.member_for_client(token["client_id"])
    assert member is not None
    return token, member.user.id


def _owner_id(api: Any) -> str:
    return next(m.user.id for m in api.ctx.collaboration.list_members() if m.role == "owner")


def _code(response: Any) -> str:
    return str(response.json().get("code"))


def _events(api: Any, type_: str) -> list[ApiEventRow]:
    with api.ctx.collaboration.dstore.read() as session:
        return list(
            session.scalars(
                select(ApiEventRow).where(ApiEventRow.type == type_).order_by(ApiEventRow.seq)
            )
        )


def test_any_member_reads_the_directory(api: Any) -> None:
    owner = _owner_token(api)
    api.clock.t += 60
    member, member_id = _join(api, "member", "bea")
    api.clock.t += 60
    assert api.client.get("/v1/users/me", headers=_headers(member)).status_code == 200

    response = api.client.get("/v1/users", headers=_headers(member))

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [entry["username"] for entry in data] == ["owner", "bea"]
    for entry in data:
        assert set(entry) == DIRECTORY_KEYS
        assert entry["auth_source"] == "local"
        assert entry["avatar_url"] is None
        assert entry["is_active"] is True
    bea = data[1]
    assert bea["id"] == member_id
    assert bea["role"] == "member"
    assert bea["email"] == "bea@example.test"
    # The clock starts at 1_000_000 s; bea was last seen 120 s later.
    assert bea["last_seen_at"] == "1970-01-12T13:48:40Z"
    assert data[0]["role"] == "owner"
    assert data[0]["last_seen_at"] is None
    assert api.client.get("/v1/users", headers=_headers(owner)).status_code == 200


def test_the_profile_reports_role_and_sign_in_source(api: Any) -> None:
    owner = _owner_token(api)
    member, _ = _join(api, "member", "bea")

    mine = api.client.get("/v1/users/me", headers=_headers(owner)).json()
    theirs = api.client.get("/v1/users/me", headers=_headers(member)).json()

    assert mine["role"] == "owner"
    assert theirs["role"] == "member"
    assert mine["auth_source"] == "local"
    assert mine["avatar_url"] is None


def test_a_member_cannot_manage_people_or_invites(api: Any) -> None:
    _owner_token(api)
    member, _ = _join(api, "member", "bea")
    _, other_id = _join(api, "member", "cal")
    headers = _headers(member)

    refusals = [
        api.client.patch(
            f"/v1/workspace/members/{other_id}", json={"role": "admin"}, headers=headers
        ),
        api.client.delete(f"/v1/workspace/members/{other_id}", headers=headers),
        api.client.post("/v1/workspace/invites", json={"role": "member"}, headers=headers),
        api.client.get("/v1/workspace/invites", headers=headers),
    ]

    for response in refusals:
        assert response.status_code == 403, response.text
        assert _code(response) == "forbidden_role"
    other = api.ctx.collaboration.member_for_user(other_id)
    assert other is not None and other.role == "member"


def test_a_plain_client_needs_daemon_manage(api: Any) -> None:
    _owner_token(api)
    _, member_id = _join(api, "member", "bea")
    reader = api.bearer(frozenset({"runs:read", "collaboration:read", "collaboration:write"}))

    for response in (
        api.client.get("/v1/users", headers=reader),
        api.client.get("/v1/workspace/invites", headers=reader),
        api.client.patch(
            f"/v1/workspace/members/{member_id}", json={"role": "admin"}, headers=reader
        ),
    ):
        assert response.status_code == 403, response.text
        assert _code(response) == "forbidden_role"

    operator = api.bearer(frozenset({"daemon:manage"}))
    assert api.client.get("/v1/users", headers=operator).status_code == 200
    promoted = api.client.patch(
        f"/v1/workspace/members/{member_id}", json={"role": "owner"}, headers=operator
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "owner"
    created = api.client.post("/v1/workspace/invites", json={"role": "owner"}, headers=operator)
    assert created.status_code == 201, created.text


def test_an_admin_manages_members_but_never_an_owner(api: Any) -> None:
    _owner_token(api)
    owner_id = _owner_id(api)
    admin, _ = _join(api, "admin", "ada")
    member_token, member_id = _join(api, "member", "bea")
    headers = _headers(admin)

    promoted = api.client.patch(
        f"/v1/workspace/members/{member_id}", json={"role": "admin"}, headers=headers
    )
    assert promoted.status_code == 200, promoted.text
    assert set(promoted.json()) == DIRECTORY_KEYS
    assert promoted.json()["role"] == "admin"
    assert _client_capabilities(api, member_token["client_id"]) == set(ROLE_CAPABILITIES["admin"])

    refusals = [
        api.client.patch(
            f"/v1/workspace/members/{member_id}", json={"role": "owner"}, headers=headers
        ),
        api.client.patch(
            f"/v1/workspace/members/{owner_id}", json={"role": "member"}, headers=headers
        ),
        api.client.patch(
            f"/v1/workspace/members/{owner_id}", json={"is_active": False}, headers=headers
        ),
        api.client.delete(f"/v1/workspace/members/{owner_id}", headers=headers),
        api.client.post("/v1/workspace/invites", json={"role": "owner"}, headers=headers),
    ]
    for response in refusals:
        assert response.status_code == 403, response.text
        assert _code(response) == "owner_required"
    owner = api.ctx.collaboration.member_for_user(owner_id)
    assert owner is not None and owner.role == "owner" and owner.user.active


def test_the_last_owner_and_the_caller_are_protected(api: Any) -> None:
    owner = _headers(_owner_token(api))
    owner_id = _owner_id(api)
    admin_token, admin_id = _join(api, "admin", "ada")
    admin = _headers(admin_token)
    operator = api.bearer(frozenset({"daemon:manage"}))

    demoted = api.client.patch(
        f"/v1/workspace/members/{owner_id}", json={"role": "admin"}, headers=owner
    )
    assert demoted.status_code == 409 and _code(demoted) == "last_owner"
    for response in (
        api.client.patch(
            f"/v1/workspace/members/{owner_id}", json={"is_active": False}, headers=operator
        ),
        api.client.delete(f"/v1/workspace/members/{owner_id}", headers=operator),
    ):
        assert response.status_code == 409, response.text
        assert _code(response) == "last_owner"

    for response in (
        api.client.patch(
            f"/v1/workspace/members/{owner_id}", json={"is_active": False}, headers=owner
        ),
        api.client.delete(f"/v1/workspace/members/{owner_id}", headers=owner),
        api.client.patch(
            f"/v1/workspace/members/{admin_id}", json={"is_active": False}, headers=admin
        ),
        api.client.delete(f"/v1/workspace/members/{admin_id}", headers=admin),
    ):
        assert response.status_code == 409, response.text
        assert _code(response) == "self_action"

    members = api.ctx.collaboration.list_members()
    assert {m.user.id for m in members} == {owner_id, admin_id}
    assert all(m.user.active for m in members)


def test_unknown_members_and_bad_bodies_are_refused(api: Any) -> None:
    owner = _headers(_owner_token(api))

    missing = api.client.patch(
        "/v1/workspace/members/user_nobody", json={"role": "admin"}, headers=owner
    )
    assert missing.status_code == 404 and _code(missing) == "user_not_found"
    gone = api.client.delete("/v1/workspace/members/user_nobody", headers=owner)
    assert gone.status_code == 404 and _code(gone) == "user_not_found"
    _, member_id = _join(api, "member", "bea")
    for body in ({"role": "emperor"}, {"is_active": "sometimes"}, {"nickname": "x"}):
        response = api.client.patch(f"/v1/workspace/members/{member_id}", json=body, headers=owner)
        assert response.status_code == 422, response.text
    for body in (
        {"role": "member", "ttl_hours": 721},
        {"role": "member", "ttl_hours": 0},
        {"role": "emperor"},
        {},
    ):
        response = api.client.post("/v1/workspace/invites", json=body, headers=owner)
        assert response.status_code == 422, response.text


def test_deactivating_a_member_revokes_their_tokens(api: Any) -> None:
    owner = _headers(_owner_token(api))
    member, member_id = _join(api, "member", "bea")
    assert api.client.get("/v1/users/me", headers=_headers(member)).status_code == 200

    response = api.client.patch(
        f"/v1/workspace/members/{member_id}", json={"is_active": False}, headers=owner
    )

    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False
    stale = api.client.get("/v1/users/me", headers=_headers(member))
    assert stale.status_code == 401, stale.text
    assert api.client.get("/v1/runs", headers=_headers(member)).status_code == 401
    refreshed = api.client.post(
        "/v1/auth/token",
        json={"grant_type": "refresh_token", "refresh_token": member["refresh_token"]},
    )
    assert refreshed.status_code == 401
    login = {"username": "bea", "password": "bea has a long password"}
    assert api.client.post("/v1/auth/local/login", json=login).status_code == 401
    directory = api.client.get("/v1/users", headers=owner).json()["data"]
    assert [e["is_active"] for e in directory if e["id"] == member_id] == [False]

    restored = api.client.patch(
        f"/v1/workspace/members/{member_id}", json={"is_active": True}, headers=owner
    )
    assert restored.status_code == 200 and restored.json()["is_active"] is True
    again = api.client.post("/v1/auth/local/login", json=login)
    assert again.status_code == 200, again.text
    assert api.client.get("/v1/users/me", headers=_headers(again.json())).status_code == 200
    assert _client_capabilities(api, member["client_id"]) == set(ROLE_CAPABILITIES["member"])


def test_removing_a_member_ends_their_access(api: Any) -> None:
    owner = _headers(_owner_token(api))
    member, member_id = _join(api, "member", "bea")

    response = api.client.delete(f"/v1/workspace/members/{member_id}", headers=owner)

    assert response.status_code == 204
    assert response.content == b""
    assert api.ctx.collaboration.member_for_user(member_id) is None
    assert _client_capabilities(api, member["client_id"]) == set()
    assert api.client.get("/v1/runs", headers=_headers(member)).status_code == 401
    assert api.client.get("/v1/users", headers=_headers(member)).status_code == 401
    refreshed = api.client.post(
        "/v1/auth/token",
        json={"grant_type": "refresh_token", "refresh_token": member["refresh_token"]},
    )
    assert refreshed.status_code == 401
    directory = api.client.get("/v1/users", headers=owner).json()["data"]
    assert member_id not in {entry["id"] for entry in directory}
    again = api.client.delete(f"/v1/workspace/members/{member_id}", headers=owner)
    assert again.status_code == 404 and _code(again) == "user_not_found"


def test_invites_are_created_listed_and_revoked(api: Any) -> None:
    owner = _headers(_owner_token(api))
    owner_id = _owner_id(api)

    created = api.client.post(
        "/v1/workspace/invites",
        json={"role": "admin", "email": "New@Example.test"},
        headers=owner,
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == {"id", "token", "expires_at", "role", "email"}
    assert body["role"] == "admin"
    assert body["email"] == "new@example.test"
    assert body["token"].startswith("inv_")
    # 72 hours after the clock's 1_000_000 s start, by default.
    assert body["expires_at"] == "1970-01-15T13:46:40Z"
    longest = api.client.post(
        "/v1/workspace/invites", json={"role": "member", "ttl_hours": 720}, headers=owner
    )
    assert longest.status_code == 201
    # 30 days: 3_592_000 s after the epoch.
    assert longest.json()["expires_at"] == "1970-02-11T13:46:40Z"
    assert longest.json()["email"] is None

    listed = api.client.get("/v1/workspace/invites", headers=owner)
    assert listed.status_code == 200
    entries = listed.json()["data"]
    assert {entry["id"] for entry in entries} == {body["id"], longest.json()["id"]}
    first = next(entry for entry in entries if entry["id"] == body["id"])
    assert first == {
        "id": body["id"],
        "role": "admin",
        "email": "new@example.test",
        "expires_at": body["expires_at"],
        "accepted_at": None,
        "created_by": owner_id,
    }
    assert body["token"] not in listed.text

    revoked = api.client.delete(f"/v1/workspace/invites/{body['id']}", headers=owner)
    assert revoked.status_code == 204
    remaining = api.client.get("/v1/workspace/invites", headers=owner).json()["data"]
    assert [entry["id"] for entry in remaining] == [longest.json()["id"]]
    refused = _register(
        api,
        {
            "email": "new@example.test",
            "username": "new",
            "password": "a newcomer password",
            "invite_token": body["token"],
        },
    )
    assert refused.status_code == 403 and _code(refused) == "invite_invalid"
    missing = api.client.delete(f"/v1/workspace/invites/{body['id']}", headers=owner)
    assert missing.status_code == 404 and _code(missing) == "invite_not_found"


def test_a_spent_invite_is_listed_as_accepted(api: Any) -> None:
    owner = _headers(_owner_token(api))
    body = api.client.post("/v1/workspace/invites", json={"role": "member"}, headers=owner).json()
    api.clock.t += 30
    joined = _register(
        api,
        {
            "email": "bea@example.test",
            "username": "bea",
            "password": "bea has a long password",
            "invite_token": body["token"],
        },
    )
    assert joined.status_code == 201

    entries = api.client.get("/v1/workspace/invites", headers=owner).json()["data"]

    assert [(e["id"], e["accepted_at"]) for e in entries] == [(body["id"], "1970-01-12T13:47:10Z")]


def test_an_invite_with_an_email_admits_only_that_address(api: Any) -> None:
    owner = _headers(_owner_token(api))
    token = api.client.post(
        "/v1/workspace/invites",
        json={"role": "member", "email": "Bea@Example.test"},
        headers=owner,
    ).json()["token"]

    wrong = _register(
        api,
        {
            "email": "mallory@example.test",
            "username": "mallory",
            "password": "a borrowed password",
            "invite_token": token,
        },
    )
    assert wrong.status_code == 403, wrong.text
    assert _code(wrong) == "invite_email_mismatch"
    assert len(api.ctx.collaboration.list_members()) == 1

    right = _register(
        api,
        {
            "email": "BEA@example.TEST",
            "username": "bea",
            "password": "bea has a long password",
            "invite_token": token,
        },
    )
    assert right.status_code == 201, right.text


def test_accepting_an_addressed_invite_checks_the_user_email(api: Any) -> None:
    _owner_token(api)
    store = api.ctx.collaboration
    _, member_id = _join(api, "member", "bea")
    store.remove_member(member_id)
    owner_id = _owner_id(api)
    _, other = store.create_invite(
        "member", "someone@example.test", created_by=owner_id, ttl_s=60, now=api.clock()
    )
    with pytest.raises(CollaborationError) as refused:
        store.accept_invite(other, member_id, now=api.clock())
    assert refused.value.code == "invite_email_mismatch"
    assert store.member_for_user(member_id) is None

    _, mine = store.create_invite(
        "member", "BEA@example.test", created_by=owner_id, ttl_s=60, now=api.clock()
    )
    assert store.accept_invite(mine, member_id, now=api.clock()).role == "member"


def test_every_admin_change_is_audited_without_tokens(api: Any) -> None:
    owner_token = _owner_token(api)
    owner = _headers(owner_token)
    owner_id = _owner_id(api)
    member, member_id = _join(api, "member", "bea")

    api.client.patch(
        f"/v1/workspace/members/{member_id}",
        json={"role": "admin", "is_active": False},
        headers=owner,
    )
    api.client.delete(f"/v1/workspace/members/{member_id}", headers=owner)
    invite = api.client.post("/v1/workspace/invites", json={"role": "member"}, headers=owner)
    api.client.delete(f"/v1/workspace/invites/{invite.json()['id']}", headers=owner)

    updated = _events(api, "workspace.member.updated")
    removed = _events(api, "workspace.member.removed")
    created = _events(api, "workspace.invite.created")
    revoked = _events(api, "workspace.invite.revoked")
    assert len(updated) == 1 and len(removed) == 1 and len(revoked) == 1
    assert len(created) == 2
    assert json.loads(updated[0].data_json) == {
        "user_id": member_id,
        "role": "admin",
        "is_active": False,
    }
    assert json.loads(removed[0].data_json)["user_id"] == member_id
    assert json.loads(revoked[0].data_json)["invite_id"] == invite.json()["id"]
    for row in (updated[0], removed[0], created[-1], revoked[0]):
        assert json.loads(row.actor_json)["id"] == owner_id
    secrets = {
        invite.json()["token"],
        owner_token["access_token"],
        owner_token["refresh_token"],
        member["access_token"],
        member["refresh_token"],
    }
    for row in [*updated, *removed, *created, *revoked]:
        text = f"{row.data_json} {row.actor_json}"
        assert not any(secret in text for secret in secrets)


def test_capabilities_advertise_the_workspace_people_features(api: Any) -> None:
    response = api.client.get("/v1/capabilities", headers=api.bearer())

    assert response.status_code == 200
    features = response.json()["features"]
    assert "workspace.members" in features
    assert "users.directory" in features


def test_an_invite_email_is_trimmed_and_a_blank_one_is_absent(api: Any) -> None:
    owner = _headers(_owner_token(api))

    padded = api.client.post(
        "/v1/workspace/invites",
        json={"role": "member", "email": "  Bea@Example.test \t"},
        headers=owner,
    )
    assert padded.status_code == 201, padded.text
    assert padded.json()["email"] == "bea@example.test"

    for blank in ("", " ", "  ", "     ", "\t\n "):
        response = api.client.post(
            "/v1/workspace/invites",
            json={"role": "member", "email": blank},
            headers=owner,
        )
        assert response.status_code == 201, (blank, response.text)
        assert response.json()["email"] is None

    # Still too short once trimmed: not an email and not blank.
    short = api.client.post(
        "/v1/workspace/invites", json={"role": "member", "email": " ab "}, headers=owner
    )
    assert short.status_code == 422, short.text


def _login_owner(api: Any) -> dict[str, Any]:
    response = api.client.post(
        "/v1/auth/local/login",
        json={"username": OWNER["username"], "password": OWNER["password"]},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_an_operator_invite_names_its_client_as_the_inviter(api: Any) -> None:
    _owner_token(api)
    client, secret = api.register("operator", frozenset({"daemon:manage"}))
    token = api.client.post(
        "/v1/auth/token",
        json={"grant_type": "client_credentials", "client_id": client.id, "client_secret": secret},
    ).json()
    operator = _headers(token)

    created = api.client.post("/v1/workspace/invites", json={"role": "member"}, headers=operator)
    assert created.status_code == 201, created.text
    listed = api.client.get("/v1/workspace/invites", headers=operator).json()["data"]
    assert [entry["created_by"] for entry in listed] == [f"client:{client.id}"]
    event = _events(api, "workspace.invite.created")[-1]
    assert json.loads(event.data_json)["created_by"] == f"client:{client.id}"
    assert json.loads(event.actor_json)["id"] == client.id

    joined = _register(
        api,
        {
            "email": "bea@example.test",
            "username": "bea",
            "password": "bea has a long password",
            "invite_token": created.json()["token"],
        },
    )
    assert joined.status_code == 201, joined.text
    member = api.ctx.collaboration.member_for_client(joined.json()["client_id"])
    assert member is not None
    with api.ctx.collaboration.dstore.read() as session:
        row = session.scalars(
            select(WorkspaceMemberRow).where(WorkspaceMemberRow.user_id == member.user.id)
        ).one()
        assert row.invited_by == f"client:{client.id}"

    # A member's invite still names the user who made it.
    owner = _headers(_login_owner(api))
    mine = api.client.post("/v1/workspace/invites", json={"role": "member"}, headers=owner)
    assert mine.status_code == 201, mine.text
    listed = api.client.get("/v1/workspace/invites", headers=owner).json()["data"]
    by_id = {entry["id"]: entry["created_by"] for entry in listed}
    assert by_id[mine.json()["id"]] == _owner_id(api)


def _live_refresh_tokens(api: Any, client_id: str) -> int:
    with api.ctx.collaboration.dstore.read() as session:
        rows = session.scalars(
            select(RefreshTokenRow).where(
                RefreshTokenRow.client_id == client_id, RefreshTokenRow.revoked_at.is_(None)
            )
        ).all()
        return len(rows)


def test_deactivation_and_removal_revoke_refresh_tokens_in_the_same_step(api: Any) -> None:
    _owner_token(api)
    store = api.ctx.collaboration
    bea, bea_id = _join(api, "member", "bea")
    cal, cal_id = _join(api, "member", "cal")
    assert _live_refresh_tokens(api, bea["client_id"]) == 1
    assert _live_refresh_tokens(api, cal["client_id"]) == 1

    # The membership change itself revokes: no separate call follows it.
    store.update_member(bea_id, active=False, now=api.clock())
    assert _live_refresh_tokens(api, bea["client_id"]) == 0
    assert store.remove_member(cal_id, now=api.clock())
    assert _live_refresh_tokens(api, cal["client_id"]) == 0


def test_a_failed_membership_change_keeps_refresh_tokens(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_token(api)
    store = api.ctx.collaboration
    bea, bea_id = _join(api, "member", "bea")
    cal, cal_id = _join(api, "member", "cal")

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the audit write failed")

    monkeypatch.setattr(collaboration_module, "_event", broken)
    with pytest.raises(RuntimeError):
        store.update_member(bea_id, active=False, now=api.clock())
    with pytest.raises(RuntimeError):
        store.remove_member(cal_id, now=api.clock())
    monkeypatch.undo()

    # Both transactions rolled back whole: the members and their tokens stand.
    assert _live_refresh_tokens(api, bea["client_id"]) == 1
    assert _live_refresh_tokens(api, cal["client_id"]) == 1
    member = store.member_for_user(bea_id)
    assert member is not None and member.user.active
    assert store.member_for_user(cal_id) is not None
    refreshed = api.client.post(
        "/v1/auth/token",
        json={"grant_type": "refresh_token", "refresh_token": bea["refresh_token"]},
    )
    assert refreshed.status_code == 200, refreshed.text


def _pending_invite_ids(api: Any, headers: dict[str, str]) -> set[str]:
    listed = api.client.get("/v1/workspace/invites", headers=headers).json()["data"]
    return {entry["id"] for entry in listed if entry["accepted_at"] is None}


def _registration(api: Any, name: str, token: str) -> Any:
    return _register(
        api,
        {
            "email": f"{name}@example.test",
            "username": name,
            "password": f"{name} has a long password",
            "invite_token": token,
        },
    )


def test_losing_workspace_standing_revokes_the_invites_one_created(api: Any) -> None:
    """An invite stands only while its creator does: removing or
    deactivating an admin withdraws every invite they had not yet spent,
    in the same step and on the record."""
    owner = _headers(_owner_token(api))
    owner_id = _owner_id(api)
    ada, ada_id = _join(api, "admin", "ada")
    bob, bob_id = _join(api, "admin", "bob")
    store = api.ctx.collaboration
    by_ada = [
        api.client.post("/v1/workspace/invites", json={"role": role}, headers=_headers(ada)).json()
        for role in ("admin", "member")
    ]
    by_bob = api.client.post(
        "/v1/workspace/invites", json={"role": "member"}, headers=_headers(bob)
    ).json()
    _, spent = store.create_invite("member", None, created_by=ada_id, ttl_s=3600, now=api.clock())
    assert _registration(api, "cal", spent).status_code == 201
    mine, by_owner = store.create_invite(
        "member", None, created_by=owner_id, ttl_s=3600, now=api.clock()
    )

    assert api.client.delete(f"/v1/workspace/members/{ada_id}", headers=owner).status_code == 204
    paused = api.client.patch(
        f"/v1/workspace/members/{bob_id}", json={"is_active": False}, headers=owner
    )
    assert paused.status_code == 200, paused.text

    for invite in (*by_ada, by_bob):
        refused = _registration(api, "dan", invite["token"])
        assert refused.status_code == 403, refused.text
        assert _code(refused) == "invite_invalid"
    assert _pending_invite_ids(api, owner) == {mine.id}
    revoked = _events(api, "workspace.invite.revoked")
    assert {json.loads(row.data_json)["invite_id"] for row in revoked} == {
        invite["id"] for invite in (*by_ada, by_bob)
    }
    assert {json.loads(row.actor_json)["id"] for row in revoked} == {owner_id}
    # The spent invite stays on record, and the owner's own invite still admits.
    assert [
        entry["accepted_at"] is not None
        for entry in api.client.get("/v1/workspace/invites", headers=owner).json()["data"]
        if entry["created_by"] == ada_id
    ] == [True]
    assert _registration(api, "dan", by_owner).status_code == 201


def test_an_invite_whose_creator_is_no_longer_a_member_admits_nobody(api: Any) -> None:
    _owner_token(api)
    store = api.ctx.collaboration
    _, ada_id = _join(api, "admin", "ada")
    store.remove_member(ada_id, now=api.clock())
    # An invite still naming a creator who is gone (a row from before the
    # creator left) is refused at acceptance too.
    _, raw = store.create_invite("member", None, created_by=ada_id, ttl_s=3600, now=api.clock())

    refused = _registration(api, "dan", raw)
    assert refused.status_code == 403, refused.text
    assert _code(refused) == "invite_invalid"
    with pytest.raises(CollaborationError) as again:
        store.accept_invite(raw, ada_id, now=api.clock())
    assert again.value.code == "invite_invalid"
    assert store.member_for_user(ada_id) is None


def test_an_invite_grants_at_most_its_creators_current_role(api: Any) -> None:
    owner = _headers(_owner_token(api))
    owner_id = _owner_id(api)
    store = api.ctx.collaboration
    ada, ada_id = _join(api, "admin", "ada")
    above = api.client.post(
        "/v1/workspace/invites", json={"role": "admin"}, headers=_headers(ada)
    ).json()
    within = api.client.post(
        "/v1/workspace/invites", json={"role": "member"}, headers=_headers(ada)
    ).json()

    # Demoting ada withdraws the invite above the new role and keeps the other.
    demoted = api.client.patch(
        f"/v1/workspace/members/{ada_id}", json={"role": "member"}, headers=owner
    )
    assert demoted.status_code == 200, demoted.text
    refused = _registration(api, "cal", above["token"])
    assert refused.status_code == 403 and _code(refused) == "invite_invalid"
    assert above["id"] not in _pending_invite_ids(api, owner)
    assert within["id"] in _pending_invite_ids(api, owner)
    assert [
        json.loads(row.data_json)["invite_id"] for row in _events(api, "workspace.invite.revoked")
    ] == [above["id"]]
    assert _registration(api, "cal", within["token"]).status_code == 201

    # An owner invite whose creator is an admin by the time it is accepted
    # admits an admin, with an admin's capabilities.
    _, raw = store.create_invite("owner", None, created_by=owner_id, ttl_s=3600, now=api.clock())
    store.set_role(ada_id, "owner")
    assert store.set_role(owner_id, "admin").role == "admin"
    joined = _registration(api, "dan", raw)
    assert joined.status_code == 201, joined.text
    dan = store.member_for_client(joined.json()["client_id"])
    assert dan is not None and dan.role == "admin"
    assert _client_capabilities(api, joined.json()["client_id"]) == set(ROLE_CAPABILITIES["admin"])
    accepted = _events(api, "workspace.invite.accepted")
    assert json.loads(accepted[-1].data_json)["role"] == "admin"


def test_the_endpoint_catalog_lists_only_real_workspace_methods() -> None:
    docs = Path(__file__).resolve().parents[2] / "docs" / "api.md"
    section = docs.read_text(encoding="utf-8").split("## Endpoint catalog", 1)[1]
    section = section.split("\n## ", 1)[0]
    documented: set[tuple[str, str]] = set()
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        paths = re.findall(r"`([^`]+)`", cells[1])
        if not any(p.startswith("/v1/workspace/") or p == "/v1/users" for p in paths):
            continue
        methods = re.findall(r"`([A-Z]+)`", cells[0])
        assert methods, line
        documented.update((method, path) for method in methods for path in paths)

    served = {
        (method, route.path.replace("{invite_id}", "{id}"))
        for route in workspace_router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert documented == served
