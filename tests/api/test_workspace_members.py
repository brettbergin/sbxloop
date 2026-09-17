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
from typing import Any

import pytest
from sqlalchemy import select

from sbxloop.api.collaboration import CollaborationError, Member
from sbxloop.daemon.controls.principal import ALL_CAPABILITIES, ROLE_CAPABILITIES
from sbxloop.db.api_models import ClientRow
from sbxloop.db.collaboration_models import WorkspaceInviteRow

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
