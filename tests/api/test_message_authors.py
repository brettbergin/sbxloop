"""Every message and turn says who wrote it; every channel knows its owner.

The store records an author on each write path, the API serializes it as an
optional ``author`` object beside the fields existing clients read, and the
capability document advertises it.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from sbxloop.api.collaboration import Author, LocalUser
from sbxloop.db.api_models import ApiEventRow
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled

OLD_MESSAGE_FIELDS = {
    "id",
    "channel_id",
    "turn_id",
    "sequence",
    "role",
    "kind",
    "content",
    "agent_slug",
    "created_at",
    "work",
    "reactions",
}


def owner(api: Any) -> LocalUser:
    user = api.ctx.collaboration.user_by_username("owner")
    assert user is not None
    return user


def accept(api: Any, user: LocalUser, channel_id: str, targets: tuple[str, ...] = ()) -> Any:
    turn, message, _ = api.ctx.collaboration.accept_turn(
        user.id,
        channel_id,
        content="hello",
        targets=targets,
        client_turn_id=None,
        client_message_id=None,
        actor=None,
        now=api.clock(),
    )
    return turn, message


def test_creating_a_channel_makes_the_creator_its_owner_member(api: Any) -> None:
    register(api)
    user = owner(api)
    from sbxloop.db.collaboration_models import ChannelMemberRow

    channel = api.ctx.collaboration.create_channel(user.id, "Plans", 5.0)
    assert channel.visibility == "private"
    assert channel.created_by == user.id
    with api.harness.dstore.read() as session:
        members = [
            (row.channel_id, row.user_id, row.role, row.added_by, row.joined_at)
            for row in session.scalars(select(ChannelMemberRow))
        ]
    assert members == [(channel.id, user.id, "owner", user.id, 5.0)]


def test_a_human_turn_and_its_message_are_authored_by_the_human(api: Any) -> None:
    register(api)
    user = owner(api)
    channel = api.ctx.collaboration.create_channel(user.id, "T", 1.0)
    turn, message = accept(api, user, channel.id)
    assert message.author == Author("human", user.id, "Local Owner")
    assert turn.author == Author("human", user.id, "Local Owner")
    assert turn.trigger == "human"
    assert turn.parent_turn_id is None
    assert turn.source_message_id is None
    assert turn.chain_depth == 0


def test_every_write_path_records_its_author(api: Any) -> None:
    register(api)
    user = owner(api)
    store = api.ctx.collaboration
    channel = store.create_channel(user.id, "T", 1.0)
    turn, _ = accept(api, user, channel.id, ("planner",))
    assert store.start_turn(turn.id, 2.0)
    assert store.participant_started(turn.id, 0, 2.0)
    planner = store.append_reply(
        turn.id, content="plan", agent_slug="planner", now=3.0, participant_index=0
    )
    angie = store.append_reply(turn.id, content="hi", agent_slug=None, now=3.0)
    assert planner.author == Author("agent", "planner")
    assert angie.author == Author("agent", "concierge")

    turn2, _ = accept(api, user, channel.id, ("planner",))
    store.start_turn(turn2.id, 4.0)
    store.participant_started(turn2.id, 0, 4.0)
    store.queue_handoff(user.id, channel.id, turn2.id, 0, "critic", "check it", 4.0)
    work = store.append_work_result(
        "msg_work_one",
        channel_id=channel.id,
        turn_id=turn.id,
        content="done",
        agent_slug=None,
        work={"item_id": "x"},
        now=5.0,
    )
    assert work.author == Author("agent", "concierge")
    store.finish_turn(turn2.id, error="it broke", now=6.0)

    turn3, _ = accept(api, user, channel.id)
    store.start_turn(turn3.id, 7.0)
    store.cancel_turn(user.id, channel.id, turn3.id, 7.0)
    store.finish_turn(turn3.id, error=None, now=8.0)

    messages = store.list_messages(user.id, channel.id)
    by_kind = {(m.kind, m.agent_slug): m.author for m in messages if m.role == "assistant"}
    assert by_kind[("agent_handoff", "planner")] == Author("agent", "planner")
    assert by_kind[("turn_error", None)] == Author("system", None)
    assert by_kind[("turn_cancelled", None)] == Author("system", None)
    assert by_kind[("message", None)] == Author("agent", "concierge")
    humans = [m.author for m in messages if m.role == "user"]
    assert humans == [Author("human", user.id, "Local Owner")] * 3
    assert all(m.author is not None for m in messages)


def test_message_created_events_name_the_author(api: Any) -> None:
    register(api)
    user = owner(api)
    store = api.ctx.collaboration
    channel = store.create_channel(user.id, "T", 1.0)
    turn, _ = accept(api, user, channel.id)
    store.start_turn(turn.id, 2.0)
    store.append_reply(turn.id, content="hi", agent_slug="builder", now=3.0)
    store.finish_turn(turn.id, error="nope", now=4.0)
    with api.harness.dstore.read() as session:
        data = [
            json.loads(row.data_json or "{}")
            for row in session.scalars(
                select(ApiEventRow)
                .where(ApiEventRow.type == "collaboration.message.created")
                .order_by(ApiEventRow.seq)
            )
        ]
    assert [(d["author_kind"], d["author_id"]) for d in data] == [
        ("human", user.id),
        ("agent", "builder"),
        ("system", None),
    ]


def test_messages_serialize_their_author_with_a_display_name(api: Any) -> None:
    api.ctx.concierge = FakeConcierge()
    headers = bearer(register(api))
    user = owner(api)
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@planner and angie, please look", "target_slugs": ["concierge"]},
    )
    assert accepted.status_code == 202, accepted.text
    body = accepted.json()
    assert body["message"]["author"] == {
        "kind": "human",
        "id": user.id,
        "display_name": "Local Owner",
    }
    assert body["turn"]["author_id"] == user.id
    assert body["turn"]["trigger"] == "human"
    assert body["turn"]["parent_turn_id"] is None
    done = settled(api.client, headers, channel, body["turn"]["id"])
    assert done["status"] == "completed", done

    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    authors = [m["author"] for m in messages]
    assert authors == [
        {"kind": "human", "id": user.id, "display_name": "Local Owner"},
        {"kind": "agent", "id": "concierge", "display_name": "Angie"},
        {"kind": "agent", "id": "planner", "display_name": "Planner"},
    ]
    for message in messages:
        assert set(message) >= OLD_MESSAGE_FIELDS
    assert [m["agent_slug"] for m in messages] == [None, "concierge", "planner"]
    assert [m["role"] for m in messages] == ["user", "assistant", "assistant"]


def test_a_human_without_a_full_name_is_shown_by_username(api: Any) -> None:
    headers = bearer(register(api))
    cleared = api.client.patch("/v1/users/me", json={"full_name": ""}, headers=headers)
    assert cleared.status_code == 200, cleared.text
    user = owner(api)
    channel = api.ctx.collaboration.create_channel(user.id, "T", 1.0)
    accept(api, user, channel.id)
    messages = api.client.get(f"/v1/channels/{channel.id}/messages", headers=headers).json()
    assert messages[0]["author"]["display_name"] == "owner"


def test_system_messages_have_no_author_id_or_name(api: Any) -> None:
    headers = bearer(register(api))
    user = owner(api)
    store = api.ctx.collaboration
    channel = store.create_channel(user.id, "T", 1.0)
    turn, _ = accept(api, user, channel.id)
    store.start_turn(turn.id, 2.0)
    store.finish_turn(turn.id, error="failed", now=3.0)
    messages = api.client.get(f"/v1/channels/{channel.id}/messages", headers=headers).json()
    assert messages[-1]["kind"] == "turn_error"
    assert messages[-1]["author"] == {"kind": "system", "id": None, "display_name": None}


def test_capabilities_advertise_message_authors(api: Any) -> None:
    response = api.client.get("/v1/capabilities", headers=api.bearer())
    assert response.status_code == 200
    assert "collaboration.message_authors" in response.json()["features"]


def test_rows_an_older_release_wrote_without_an_author_still_read_with_one(api: Any) -> None:
    """A rolled-back release writes no author; the reader applies the backfill rules."""
    from sqlalchemy import update

    from sbxloop.db.collaboration_models import MessageRow, TurnRow

    register(api)
    user = owner(api)
    store = api.ctx.collaboration
    channel = store.create_channel(user.id, "T", 1.0)
    turn, _ = accept(api, user, channel.id)
    store.start_turn(turn.id, 2.0)
    store.append_reply(turn.id, content="hi", agent_slug=None, now=3.0)
    store.finish_turn(turn.id, error="failed", now=4.0)
    with api.harness.dstore.transaction() as session:
        session.execute(update(MessageRow).values(author_kind=None, author_id=None))
        session.execute(update(TurnRow).values(author_kind=None, author_id=None))
    messages = store.list_messages(user.id, channel.id)
    assert [m.author for m in messages] == [
        Author("human", user.id, "Local Owner"),
        Author("agent", "concierge"),
        Author("system", None),
    ]
    reread = store.get_turn(user.id, channel.id, turn.id)
    assert reread.author == Author("human", user.id, "Local Owner")
