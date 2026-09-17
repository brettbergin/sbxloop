"""Who may see, post to and manage a channel, and who is in it.

A private channel belongs to its channel members: anyone else is told it
does not exist. A workspace channel may be read and posted to by every
workspace member (posting makes them a channel member). Managing a channel
takes its owner or a workspace admin. A plain API client, with no member
behind it, keeps full access. Agents join a channel as participants, by
hand or by being mentioned.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from sbxloop.api.channel_access import ChannelAccess
from sbxloop.api.collaboration import CollaborationError, Member
from sbxloop.db.api_models import ApiEventRow
from sbxloop.db.collaboration_models import ChannelRow
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled


def _member(api: Any, token: dict[str, Any]) -> Member:
    member = api.ctx.collaboration.member_for_client(token["client_id"])
    assert member is not None
    return member


def _invite(api: Any, role: str, username: str) -> dict[str, Any]:
    store = api.ctx.collaboration
    owner = store.user_by_username("owner")
    assert owner is not None
    _, raw = store.create_invite(role, None, created_by=owner.id, ttl_s=3600, now=api.clock())
    response = api.client.post(
        "/v1/auth/local/register",
        json={
            "email": f"{username}@example.test",
            "username": username,
            "password": "another long password",
            "full_name": username.title(),
            "invite_token": raw,
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _people(api: Any) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """The workspace owner, a plain member and an admin, as request headers."""
    owner = bearer(register(api))
    guest = bearer(_invite(api, "member", "guest"))
    admin = bearer(_invite(api, "admin", "admin"))
    return owner, guest, admin


def _user_id(api: Any, headers: dict[str, str]) -> str:
    return str(api.client.get("/v1/users/me", headers=headers).json()["id"])


def _channel(api: Any, headers: dict[str, str], visibility: str | None = None) -> str:
    created = api.client.post("/v1/channels", json={"title": "Plans"}, headers=headers)
    assert created.status_code == 201, created.text
    channel_id = str(created.json()["id"])
    if visibility is not None:
        changed = api.client.patch(
            f"/v1/channels/{channel_id}", json={"visibility": visibility}, headers=headers
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["visibility"] == visibility
    return channel_id


def _events(api: Any, prefix: str) -> list[tuple[str, dict[str, Any]]]:
    with api.harness.dstore.read() as session:
        rows = session.scalars(
            select(ApiEventRow).where(ApiEventRow.type.like(f"{prefix}%")).order_by(ApiEventRow.seq)
        ).all()
        return [(str(row.type), json.loads(row.data_json)) for row in rows]


def test_a_private_channel_does_not_exist_for_other_members(api: Any) -> None:
    api.ctx.concierge = FakeConcierge()
    owner, guest, _ = _people(api)
    channel_id = _channel(api, owner)
    base = f"/v1/channels/{channel_id}"
    assert api.client.get(f"{base}/messages", headers=owner).json() == []

    for method, path, body in (
        ("GET", base, None),
        ("PATCH", base, {"title": "Mine now"}),
        ("DELETE", base, None),
        ("GET", f"{base}/messages", None),
        ("GET", f"{base}/work", None),
        ("GET", f"{base}/turns", None),
        ("GET", f"{base}/turns/trn_missing", None),
        ("POST", f"{base}/turns/trn_missing/cancel", None),
        ("POST", f"{base}/turns", {"content": "hello"}),
        ("GET", f"{base}/members", None),
        ("POST", f"{base}/members", {"user_id": _user_id(api, guest)}),
        ("GET", f"{base}/participants", None),
        ("PUT", f"{base}/participants/planner", {}),
        ("DELETE", f"{base}/participants/planner", None),
    ):
        response = api.client.request(method, path, headers=guest, json=body)
        assert response.status_code == 404, (method, path, response.text)

    listing = api.client.get("/v1/channels", headers=guest).json()
    assert listing["items"] == []
    assert listing["total"] == 0
    # The owner still sees and uses it as before.
    assert api.client.get(base, headers=owner).json()["my_role"] == "owner"


def test_a_workspace_channel_can_be_read_and_posted_to_by_any_member(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    owner, guest, _ = _people(api)
    guest_id = _user_id(api, guest)
    channel_id = _channel(api, owner, "workspace")
    base = f"/v1/channels/{channel_id}"

    seen = api.client.get(base, headers=guest)
    assert seen.status_code == 200, seen.text
    assert seen.json()["visibility"] == "workspace"
    assert seen.json()["my_role"] is None
    assert seen.json()["created_by"] == _user_id(api, owner)
    assert seen.json()["silenced_until"] is None
    assert api.client.get(f"{base}/messages", headers=guest).status_code == 200

    accepted = api.client.post(f"{base}/turns", headers=guest, json={"content": "hello"})
    assert accepted.status_code == 202, accepted.text
    turn = settled(api.client, guest, channel_id, accepted.json()["turn"]["id"])
    assert turn["status"] == "completed"
    assert concierge.calls[0]["author_id"] == guest_id

    # Posting made the guest a channel member.
    assert api.client.get(base, headers=guest).json()["my_role"] == "member"
    members = api.client.get(f"{base}/members", headers=guest).json()["data"]
    assert {(m["user_id"], m["role"]) for m in members} == {
        (_user_id(api, owner), "owner"),
        (guest_id, "member"),
    }
    assert ("collaboration.member.added", {"channel_id": channel_id, "user_id": guest_id}) in (
        _events(api, "collaboration.member.")
    )

    messages = api.client.get(f"{base}/messages", headers=owner).json()
    assert messages[0]["author"]["id"] == guest_id
    reply = messages[-1]
    reacted = api.client.put(
        f"{base}/messages/{reply['id']}/reaction", headers=guest, json={"emoji": "👍"}
    )
    assert reacted.status_code == 200, reacted.text


def test_listing_shows_my_channels_and_workspace_channels(api: Any) -> None:
    owner, guest, _ = _people(api)
    private = _channel(api, owner)
    shared = _channel(api, owner, "workspace")
    mine = _channel(api, guest)

    owner_view = api.client.get("/v1/channels", headers=owner).json()
    assert {c["id"]: c["my_role"] for c in owner_view["items"]} == {
        private: "owner",
        shared: "owner",
    }
    guest_view = api.client.get("/v1/channels", headers=guest).json()
    assert {c["id"]: c["my_role"] for c in guest_view["items"]} == {shared: None, mine: "owner"}
    assert guest_view["total"] == 2
    assert guest_view["has_more"] is False
    page = api.client.get("/v1/channels?limit=1", headers=guest).json()
    assert len(page["items"]) == 1
    assert page["has_more"] is True


def test_managing_a_channel_takes_its_owner_or_a_workspace_admin(api: Any) -> None:
    owner, guest, admin = _people(api)
    channel_id = _channel(api, owner, "workspace")
    base = f"/v1/channels/{channel_id}"

    for method, body in (("PATCH", {"title": "Guest's"}), ("DELETE", None)):
        refused = api.client.request(method, base, headers=guest, json=body)
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "channel_forbidden"
    refused = api.client.patch(base, headers=guest, json={"visibility": "private"})
    assert refused.status_code == 403

    renamed = api.client.patch(base, headers=admin, json={"title": "Admin's"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "Admin's"
    assert api.client.patch(base, headers=owner, json={"title": "Owner's"}).status_code == 200

    # Made private again, it disappears for everyone outside it.
    assert api.client.patch(base, headers=owner, json={"visibility": "private"}).status_code == 200
    assert api.client.get(base, headers=guest).status_code == 404
    assert api.client.get(base, headers=admin).status_code == 404

    other = _channel(api, owner, "workspace")
    assert api.client.delete(f"/v1/channels/{other}", headers=admin).status_code == 204


def test_a_plain_client_keeps_full_access(api: Any) -> None:
    owner, _, _ = _people(api)
    channel_id = _channel(api, owner)
    store = api.ctx.collaboration
    guest_member = _member(api, {"client_id": store.user_by_username("guest").client_id})
    with api.harness.dstore.transaction() as session:
        row = session.get(ChannelRow, channel_id)
        for need in ("read", "post", "manage"):
            ChannelAccess.check(session, row, None, need)  # type: ignore[arg-type]
        with pytest.raises(CollaborationError) as hidden:
            ChannelAccess.check(session, row, guest_member, "read")
        assert hidden.value.code == "channel_not_found"
    assert store.get_channel(None, channel_id) is not None
    assert [c.id for c in store.list_channels(None, limit=10, offset=0)[0]] == [channel_id]


def test_members_are_added_removed_and_may_leave(api: Any) -> None:
    owner, guest, admin = _people(api)
    owner_id, guest_id, admin_id = (_user_id(api, h) for h in (owner, guest, admin))
    channel_id = _channel(api, owner)
    members = f"/v1/channels/{channel_id}/members"

    added = api.client.post(members, headers=owner, json={"user_id": guest_id})
    assert added.status_code == 201, added.text
    assert added.json()["user_id"] == guest_id
    assert added.json()["role"] == "member"
    assert api.client.post(members, headers=owner, json={"user_id": guest_id}).status_code == 409
    assert (
        api.client.post(members, headers=owner, json={"user_id": "usr_nobody"}).status_code == 404
    )

    listed = api.client.get(members, headers=guest)
    assert listed.status_code == 200
    entry = next(m for m in listed.json()["data"] if m["user_id"] == guest_id)
    assert entry["last_read_sequence"] == 0
    assert entry["joined_at"]
    assert entry["user"] == {
        "id": guest_id,
        "username": "guest",
        "full_name": "Guest",
        "avatar_url": None,
    }

    # A plain channel member cannot add or remove anyone else.
    refused = api.client.post(members, headers=guest, json={"user_id": admin_id})
    assert refused.status_code == 403
    assert refused.json()["code"] == "channel_forbidden"
    assert api.client.delete(f"{members}/{owner_id}", headers=guest).status_code == 403

    # The last owner cannot leave while others remain.
    blocked = api.client.delete(f"{members}/{owner_id}", headers=owner)
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "last_channel_owner"

    # A member may leave; the private channel then disappears for them.
    assert api.client.delete(f"{members}/{guest_id}", headers=guest).status_code == 204
    assert api.client.get(f"/v1/channels/{channel_id}", headers=guest).status_code == 404
    assert api.client.delete(f"{members}/{guest_id}", headers=owner).status_code == 404

    # Handing over ownership lets the first owner go.
    handed = api.client.post(members, headers=owner, json={"user_id": guest_id, "role": "owner"})
    assert handed.status_code == 201
    assert api.client.delete(f"{members}/{owner_id}", headers=owner).status_code == 204
    assert api.client.get(f"/v1/channels/{channel_id}", headers=owner).status_code == 404
    assert api.client.get(f"/v1/channels/{channel_id}", headers=guest).json()["my_role"] == "owner"

    assert _events(api, "collaboration.member.") == [
        ("collaboration.member.added", {"channel_id": channel_id, "user_id": guest_id}),
        ("collaboration.member.removed", {"channel_id": channel_id, "user_id": guest_id}),
        ("collaboration.member.added", {"channel_id": channel_id, "user_id": guest_id}),
        ("collaboration.member.removed", {"channel_id": channel_id, "user_id": owner_id}),
    ]


def test_an_owner_promotes_a_current_member_and_then_leaves(api: Any) -> None:
    owner, guest, admin = _people(api)
    owner_id, guest_id, admin_id = (_user_id(api, h) for h in (owner, guest, admin))
    channel_id = _channel(api, owner)
    members = f"/v1/channels/{channel_id}/members"
    added = api.client.post(members, headers=owner, json={"user_id": guest_id})
    assert added.status_code == 201, added.text
    joined_at = added.json()["joined_at"]

    # A plain member cannot promote themselves.
    refused = api.client.post(members, headers=guest, json={"user_id": guest_id, "role": "owner"})
    assert refused.status_code == 403
    assert refused.json()["code"] == "channel_forbidden"

    # An explicit, different role for a current member changes it in place.
    promoted = api.client.post(members, headers=owner, json={"user_id": guest_id, "role": "owner"})
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "owner"
    assert promoted.json()["joined_at"] == joined_at
    listed = api.client.get(members, headers=guest).json()["data"]
    assert {m["user_id"]: m["role"] for m in listed} == {owner_id: "owner", guest_id: "owner"}

    # Nothing to change, or no role given: still already a member.
    for body in ({"user_id": guest_id, "role": "owner"}, {"user_id": guest_id}):
        again = api.client.post(members, headers=owner, json=body)
        assert again.status_code == 409
        assert again.json()["code"] == "already_channel_member"

    # With another owner in place, the first owner may leave.
    assert api.client.delete(f"{members}/{owner_id}", headers=owner).status_code == 204
    assert api.client.get(f"/v1/channels/{channel_id}", headers=guest).json()["my_role"] == "owner"

    # The last owner cannot step down, alone or with others in the channel.
    step_down = {"user_id": guest_id, "role": "member"}
    alone = api.client.post(members, headers=guest, json=step_down)
    assert alone.status_code == 409
    assert alone.json()["code"] == "last_channel_owner"
    assert api.client.post(members, headers=guest, json={"user_id": admin_id}).status_code == 201
    with_others = api.client.post(members, headers=guest, json=step_down)
    assert with_others.status_code == 409
    assert with_others.json()["code"] == "last_channel_owner"

    assert _events(api, "collaboration.member.") == [
        ("collaboration.member.added", {"channel_id": channel_id, "user_id": guest_id}),
        ("collaboration.member.updated", {"channel_id": channel_id, "user_id": guest_id}),
        ("collaboration.member.removed", {"channel_id": channel_id, "user_id": owner_id}),
        ("collaboration.member.added", {"channel_id": channel_id, "user_id": admin_id}),
    ]


def test_an_admin_manages_members_of_a_workspace_channel(api: Any) -> None:
    owner, guest, admin = _people(api)
    guest_id = _user_id(api, guest)
    channel_id = _channel(api, owner, "workspace")
    members = f"/v1/channels/{channel_id}/members"
    assert api.client.post(members, headers=admin, json={"user_id": guest_id}).status_code == 201
    assert api.client.delete(f"{members}/{guest_id}", headers=admin).status_code == 204


def test_participants_are_put_listed_and_removed(api: Any) -> None:
    owner, guest, _ = _people(api)
    owner_id = _user_id(api, owner)
    channel_id = _channel(api, owner, "workspace")
    participants = f"/v1/channels/{channel_id}/participants"

    added = api.client.put(f"{participants}/planner", headers=owner, json={})
    assert added.status_code == 200, added.text
    body = added.json()
    assert body["agent_slug"] == "planner"
    assert body["mode"] == "mention"
    assert body["added_by"]["kind"] == "human"
    assert body["added_by"]["id"] == owner_id
    assert body["muted_until"] is None
    assert body["created_at"]
    assert body["status"] == "idle"
    assert body["activity"] is None

    updated = api.client.put(
        f"{participants}/planner", headers=guest, json={"mode": "ambient", "muted_until": 99.5}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["mode"] == "ambient"
    assert updated.json()["muted_until"] == 99.5
    assert updated.json()["added_by"]["id"] == owner_id

    unknown = api.client.put(f"{participants}/nobody-here", headers=owner, json={})
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "agent_not_found"
    bad_mode = api.client.put(f"{participants}/critic", headers=owner, json={"mode": "loud"})
    assert bad_mode.status_code == 422

    listed = api.client.get(participants, headers=guest).json()["data"]
    assert [p["agent_slug"] for p in listed] == ["planner"]

    assert api.client.delete(f"{participants}/planner", headers=guest).status_code == 204
    assert api.client.delete(f"{participants}/planner", headers=guest).status_code == 404
    assert api.client.get(participants, headers=owner).json()["data"] == []

    assert _events(api, "collaboration.participant.") == [
        ("collaboration.participant.added", {"channel_id": channel_id, "agent_slug": "planner"}),
        (
            "collaboration.participant.updated",
            {"channel_id": channel_id, "agent_slug": "planner"},
        ),
        (
            "collaboration.participant.removed",
            {"channel_id": channel_id, "agent_slug": "planner"},
        ),
    ]


def test_mentioning_an_agent_adds_it_and_reports_its_activity(api: Any) -> None:
    api.ctx.concierge = FakeConcierge()
    owner = bearer(register(api))
    owner_id = _user_id(api, owner)
    channel_id = _channel(api, owner)
    accepted = api.client.post(
        f"/v1/channels/{channel_id}/turns",
        headers=owner,
        json={"content": "@planner what next?"},
    )
    assert accepted.status_code == 202, accepted.text
    assert settled(api.client, owner, channel_id, accepted.json()["turn"]["id"])["status"] == (
        "completed"
    )

    listed = api.client.get(f"/v1/channels/{channel_id}/participants", headers=owner).json()
    assert [(p["agent_slug"], p["mode"], p["added_by"]["id"]) for p in listed["data"]] == [
        ("planner", "mention", owner_id)
    ]
    assert listed["data"][0]["status"] == "idle"

    # A second mention does not add it twice.
    again = api.client.post(
        f"/v1/channels/{channel_id}/turns", headers=owner, json={"content": "@planner more"}
    )
    settled(api.client, owner, channel_id, again.json()["turn"]["id"])

    events = _events(api, "collaboration.participant.")
    added = [data for kind, data in events if kind == "collaboration.participant.added"]
    assert added == [{"channel_id": channel_id, "agent_slug": "planner"}]
    activity = [data for kind, data in events if kind == "collaboration.participant.activity"]
    assert activity[:2] == [
        {"channel_id": channel_id, "agent_slug": "planner", "status": "thinking"},
        {"channel_id": channel_id, "agent_slug": "planner", "status": "idle"},
    ]


def test_a_participant_is_thinking_while_its_turn_runs(api: Any) -> None:
    owner = bearer(register(api))
    user = api.ctx.collaboration.user_by_username("owner")
    channel_id = _channel(api, owner)
    store = api.ctx.collaboration
    turn, _, _ = store.accept_turn(
        user.id,
        channel_id,
        content="@critic look",
        targets=("critic",),
        participants=("critic",),
        client_turn_id=None,
        client_message_id=None,
        actor=None,
        now=api.clock(),
        intent="delegate",
    )
    store.start_turn(turn.id, api.clock())
    store.participant_started(turn.id, 0, api.clock())

    listed = api.client.get(f"/v1/channels/{channel_id}/participants", headers=owner).json()
    assert [(p["agent_slug"], p["status"]) for p in listed["data"]] == [("critic", "thinking")]

    store.finish_turn(turn.id, error=None, now=api.clock())
    listed = api.client.get(f"/v1/channels/{channel_id}/participants", headers=owner).json()
    assert [(p["agent_slug"], p["status"]) for p in listed["data"]] == [("critic", "idle")]
    assert _events(api, "collaboration.participant.activity")[-1] == (
        "collaboration.participant.activity",
        {"channel_id": channel_id, "agent_slug": "critic", "status": "idle"},
    )


def test_a_members_queued_turn_recovers_as_that_member(api: Any) -> None:
    owner, guest, _ = _people(api)
    guest_id = _user_id(api, guest)
    channel_id = _channel(api, owner, "workspace")
    store = api.ctx.collaboration
    turn, _, _ = store.accept_turn(
        guest_id,
        channel_id,
        content="queued",
        targets=(),
        client_turn_id=None,
        client_message_id=None,
        actor=None,
        now=api.clock(),
    )
    recovered = store.recover_turns(api.clock())
    assert [(t.id, user.id) for t, user, _ in recovered] == [(turn.id, guest_id)]


def test_capabilities_advertise_members_and_participants(api: Any) -> None:
    features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "collaboration.participants" in features
    assert "collaboration.channel_members" in features
