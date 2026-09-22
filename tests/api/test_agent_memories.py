"""The per-agent memory routes over the real daemon store."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from sbxloop.agents.memory import WorkspaceChannelVisibility
from sbxloop.daemon.controls.principal import ALL_CAPABILITIES
from sbxloop.db.api_models import ApiEventRow
from sbxloop.db.collaboration_models import ChannelMemberRow, ChannelRow
from tests.api.conftest import build
from tests.api.test_collaboration import bearer, register


def memories_url(slug: str = "planner") -> str:
    return f"/v1/agents/{slug}/memories"


def test_capabilities_advertise_agent_memory(api: Any) -> None:
    features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "agents.memory" in features


def test_memory_crud_round_trip(api: Any) -> None:
    token = register(api)
    headers = bearer(token)
    user_id = api.client.get("/v1/users/me", headers=headers).json()["id"]

    created = api.client.post(
        memories_url(),
        headers=headers,
        json={"content": "Standups are at nine.", "kind": "preference", "pinned": True},
    )
    assert created.status_code == 201, created.text
    memory = created.json()
    assert memory["id"].startswith("mem_")
    assert memory["agent_slug"] == "planner"
    assert memory["kind"] == "preference"
    assert memory["content"] == "Standups are at nine."
    assert memory["pinned"] is True
    assert memory["author"] == f"user:{user_id}"
    assert memory["source_channel_id"] is None
    assert memory["revision"] == 1

    listed = api.client.get(memories_url(), headers=headers)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [memory["id"]]
    assert api.client.get(memories_url("critic"), headers=headers).json() == []

    patched = api.client.patch(
        f"{memories_url()}/{memory['id']}",
        headers=headers,
        json={"content": "Standups are at ten.", "pinned": False, "expected_revision": 1},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["content"] == "Standups are at ten."
    assert patched.json()["pinned"] is False
    assert patched.json()["revision"] == 2

    stale = api.client.patch(
        f"{memories_url()}/{memory['id']}",
        headers=headers,
        json={"content": "stale", "expected_revision": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "revision_conflict"

    deleted = api.client.delete(f"{memories_url()}/{memory['id']}", headers=headers)
    assert deleted.status_code == 204
    assert api.client.get(memories_url(), headers=headers).json() == []
    again = api.client.delete(f"{memories_url()}/{memory['id']}", headers=headers)
    assert again.status_code == 404
    assert again.json()["code"] == "memory_not_found"

    with api.harness.dstore.read() as session:
        events = list(
            session.scalars(
                select(ApiEventRow)
                .where(ApiEventRow.type.like("agent.memory.%"))
                .order_by(ApiEventRow.seq)
            )
        )
    assert [event.type for event in events] == [
        "agent.memory.created",
        "agent.memory.updated",
        "agent.memory.deleted",
    ]
    for event in events:
        assert "Standups" not in event.data_json
        assert json.loads(event.data_json)["memory_id"] == memory["id"]


def test_a_memory_is_addressed_through_its_own_agent(api: Any) -> None:
    headers = bearer(register(api))
    memory = api.client.post(memories_url(), headers=headers, json={"content": "mine"}).json()
    wrong = api.client.patch(
        f"{memories_url('critic')}/{memory['id']}",
        headers=headers,
        json={"content": "theirs", "expected_revision": 1},
    )
    assert wrong.status_code == 404
    assert (
        api.client.delete(f"{memories_url('critic')}/{memory['id']}", headers=headers).status_code
        == 404
    )
    assert api.client.get(memories_url(), headers=headers).json()[0]["content"] == "mine"


def test_an_alias_names_the_same_agent(api: Any) -> None:
    headers = bearer(register(api))
    created = api.client.post(memories_url("angie"), headers=headers, json={"content": "hi"})
    assert created.status_code == 201, created.text
    assert created.json()["agent_slug"] == "concierge"
    assert len(api.client.get(memories_url("concierge"), headers=headers).json()) == 1


def test_unknown_agents_and_memories_are_not_found(api: Any) -> None:
    headers = bearer(register(api))
    for response in (
        api.client.get(memories_url("nobody"), headers=headers),
        api.client.post(memories_url("nobody"), headers=headers, json={"content": "x"}),
        api.client.patch(
            f"{memories_url('nobody')}/mem_x", headers=headers, json={"expected_revision": 1}
        ),
        api.client.delete(f"{memories_url('nobody')}/mem_x", headers=headers),
    ):
        assert response.status_code == 404
        assert response.json()["code"] == "agent_not_found"
    missing = api.client.patch(
        f"{memories_url()}/mem_missing", headers=headers, json={"expected_revision": 1}
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "memory_not_found"


def test_channel_scoping_and_include_private(api: Any) -> None:
    headers = bearer(register(api))
    here = api.client.post("/v1/channels", json={"title": "here"}, headers=headers).json()["id"]
    there = api.client.post("/v1/channels", json={"title": "there"}, headers=headers).json()["id"]
    for content, channel in (("global", None), ("from here", here), ("from there", there)):
        body: dict[str, Any] = {"content": content}
        if channel is not None:
            body["channel_id"] = channel
        created = api.client.post(memories_url(), headers=headers, json=body)
        assert created.status_code == 201, created.text

    def listed(**params: Any) -> list[str]:
        response = api.client.get(memories_url(), headers=headers, params=params)
        assert response.status_code == 200, response.text
        return sorted(item["content"] for item in response.json())

    assert listed(channel_id=here) == ["from here", "global"]
    assert listed() == ["global"]
    assert listed(channel_id=here, include_private="true") == [
        "from here",
        "from there",
        "global",
    ]
    assert listed(channel_id=here, q="there") == []
    assert listed(channel_id=here, q="HERE") == ["from here"]

    unknown = api.client.get(memories_url(), headers=headers, params={"channel_id": "chn_nope"})
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "channel_not_found"
    post_unknown = api.client.post(
        memories_url(), headers=headers, json={"content": "x", "channel_id": "chn_nope"}
    )
    assert post_unknown.status_code == 404


def test_listing_does_not_count_as_use(api: Any) -> None:
    headers = bearer(register(api))
    api.client.post(memories_url(), headers=headers, json={"content": "alpha"})
    items = api.client.get(memories_url(), headers=headers, params={"q": "alpha"}).json()
    assert items[0]["last_used_at"] is None


def test_capabilities_gate_reads_and_writes(api: Any) -> None:
    denied = api.client.get(
        memories_url(), headers=api.bearer(ALL_CAPABILITIES - {"collaboration:read"})
    )
    assert denied.status_code == 403
    read_only = api.bearer(frozenset({"collaboration:read"}))
    assert api.client.get(memories_url(), headers=read_only).status_code == 200
    for response in (
        api.client.post(memories_url(), headers=read_only, json={"content": "x"}),
        api.client.patch(
            f"{memories_url()}/mem_x", headers=read_only, json={"expected_revision": 1}
        ),
        api.client.delete(f"{memories_url()}/mem_x", headers=read_only),
    ):
        assert response.status_code == 403
    assert api.client.get(memories_url()).status_code == 401


def test_a_plain_api_client_may_manage_memories(api: Any) -> None:
    owner = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=owner).json()["id"]
    headers = api.bearer()
    created = api.client.post(
        memories_url(), headers=headers, json={"content": "machine note", "channel_id": channel}
    )
    assert created.status_code == 201, created.text
    assert created.json()["author"].startswith("user:")
    listed = api.client.get(memories_url(), headers=headers, params={"include_private": "true"})
    assert [item["content"] for item in listed.json()] == ["machine note"]


def test_disabled_memory_refuses_new_memories(tmp_path: Any) -> None:
    built = build(tmp_path, config={"memory": {"enabled": False}})
    with built.client:
        headers = built.bearer()
        refused = built.client.post(memories_url(), headers=headers, json={"content": "x"})
        assert refused.status_code == 409
        assert refused.json()["code"] == "memory_disabled"
        assert built.client.get(memories_url(), headers=headers).json() == []
    built.ctx.close()


def test_content_is_capped_by_config(tmp_path: Any) -> None:
    built = build(tmp_path, config={"memory": {"max_item_chars": 5}})
    with built.client:
        created = built.client.post(
            memories_url(), headers=built.bearer(), json={"content": "abcdefgh"}
        )
        assert created.json()["content"] == "abcde"
    built.ctx.close()


def _set_visibility(api: Any, channel_id: str, visibility: str) -> None:
    with api.harness.dstore.transaction() as session:
        row = session.get(ChannelRow, channel_id)
        assert row is not None
        row.visibility = visibility


def _invite(api: Any, role: str, username: str) -> dict[str, str]:
    store = api.ctx.collaboration
    owner = next(m for m in store.list_members() if m.role == "owner")
    _, raw = store.create_invite(role, None, created_by=owner.user.id, ttl_s=60, now=api.clock())
    response = api.client.post(
        "/v1/auth/local/register",
        json={
            "email": f"{username}@example.test",
            "username": username,
            "password": "another long password",
            "invite_token": raw,
        },
    )
    assert response.status_code == 201, response.text
    return bearer(response.json())


def _channel(api: Any, headers: dict[str, str], title: str) -> str:
    created = api.client.post("/v1/channels", json={"title": title}, headers=headers)
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def test_the_context_reads_channel_visibility(api: Any) -> None:
    headers = bearer(register(api))
    assert isinstance(api.ctx.memory.visibility, WorkspaceChannelVisibility)
    closed = _channel(api, headers, "closed")
    shared = _channel(api, headers, "shared")
    for content, channel in (("shared note", shared), ("closed note", closed)):
        created = api.client.post(
            memories_url(), headers=headers, json={"content": content, "channel_id": channel}
        )
        assert created.status_code == 201, created.text

    def listed() -> list[str]:
        response = api.client.get(memories_url(), headers=headers)
        assert response.status_code == 200, response.text
        return [item["content"] for item in response.json()]

    assert listed() == []
    _set_visibility(api, shared, "workspace")
    assert listed() == ["shared note"]


def test_a_member_never_sees_private_channels_they_cannot_read(api: Any) -> None:
    owner = bearer(register(api))
    member = _invite(api, "member", "member")
    admin = _invite(api, "admin", "admin")
    member_id = api.client.get("/v1/users/me", headers=member).json()["id"]

    owners = _channel(api, owner, "owner private")
    joined = _channel(api, owner, "joined")
    shared = _channel(api, owner, "shared")
    members = _channel(api, member, "member private")
    _set_visibility(api, shared, "workspace")
    with api.harness.dstore.transaction() as session:
        session.add(
            ChannelMemberRow(channel_id=joined, user_id=member_id, role="member", joined_at=1.0)
        )
    for content, source, headers in (
        ("global", None, owner),
        ("owner private", owners, owner),
        ("joined", joined, owner),
        ("shared", shared, owner),
        ("member private", members, member),
    ):
        body: dict[str, Any] = {"content": content}
        if source is not None:
            body["channel_id"] = source
        created = api.client.post(memories_url(), headers=headers, json=body)
        assert created.status_code == 201, created.text

    def listed(headers: dict[str, str], **params: Any) -> list[str]:
        response = api.client.get(memories_url(), headers=headers, params=params)
        assert response.status_code == 200, response.text
        return sorted(item["content"] for item in response.json())

    assert listed(member, include_private="true") == [
        "global",
        "joined",
        "member private",
        "shared",
    ]
    assert listed(member, channel_id=members) == ["global", "member private", "shared"]
    everything = ["global", "joined", "member private", "owner private", "shared"]
    assert listed(admin, include_private="true") == everything
    assert listed(owner, include_private="true") == everything
    assert listed(api.bearer(), include_private="true") == everything


def test_a_member_cannot_edit_or_delete_a_memory_they_cannot_read(api: Any) -> None:
    owner = bearer(register(api))
    outsider = _invite(api, "member", "outsider")
    insider = _invite(api, "member", "insider")
    insider_id = api.client.get("/v1/users/me", headers=insider).json()["id"]

    private = _channel(api, owner, "owner private")
    with api.harness.dstore.transaction() as session:
        session.add(
            ChannelMemberRow(channel_id=private, user_id=insider_id, role="member", joined_at=1.0)
        )
    created = api.client.post(
        memories_url(),
        headers=owner,
        json={"content": "the launch date is a secret", "channel_id": private},
    )
    assert created.status_code == 201, created.text
    memory_id = str(created.json()["id"])

    def live() -> list[tuple[str, str, bool, int]]:
        response = api.client.get(memories_url(), headers=owner, params={"include_private": "true"})
        assert response.status_code == 200, response.text
        return [
            (item["id"], item["content"], item["pinned"], item["revision"])
            for item in response.json()
        ]

    # The id leaked to a member the channel never let in; the memory did not.
    assert (
        api.client.get(memories_url(), headers=outsider, params={"include_private": "true"}).json()
        == []
    )

    patched = api.client.patch(
        f"{memories_url()}/{memory_id}",
        headers=outsider,
        json={"content": "moved to friday", "pinned": True, "expected_revision": 1},
    )
    assert patched.status_code == 404, patched.text
    assert patched.json()["code"] == "memory_not_found"
    assert "launch date" not in patched.text

    deleted = api.client.delete(f"{memories_url()}/{memory_id}", headers=outsider)
    assert deleted.status_code == 404, deleted.text
    assert deleted.json()["code"] == "memory_not_found"

    # The refusal an unknown id gets, so neither answer confirms the memory.
    unknown = api.client.delete(f"{memories_url()}/mem_0000000000000000", headers=outsider)
    assert unknown.status_code == 404
    assert unknown.json()["code"] == deleted.json()["code"]
    assert (
        api.client.patch(
            f"{memories_url()}/mem_0000000000000000",
            headers=outsider,
            json={"content": "moved to friday", "pinned": True, "expected_revision": 1},
        ).json()["code"]
        == patched.json()["code"]
    )

    assert live() == [(memory_id, "the launch date is a secret", False, 1)]

    # The channel's own members still edit and forget it.
    ok = api.client.patch(
        f"{memories_url()}/{memory_id}",
        headers=insider,
        json={"content": "the launch date moved", "expected_revision": 1},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["content"] == "the launch date moved"
    assert live() == [(memory_id, "the launch date moved", False, 2)]
    gone = api.client.delete(f"{memories_url()}/{memory_id}", headers=insider)
    assert gone.status_code == 204, gone.text
    assert live() == []
