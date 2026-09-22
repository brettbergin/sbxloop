"""External job conversations retain real provenance and durable read state."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, update

from sbxloop.db.collaboration_models import ChannelMemberRow, ChannelRow, MessageRow
from sbxloop.db.daemon_models import WorkItemRow
from sbxloop.db.job_models import ExternalJobRow
from tests.api.test_channel_access import _channel, _people, _user_id
from tests.api.test_external_work import _run, external_item


def _message(api: Any, channel_id: str, sequence: int, *, historical: bool) -> None:
    with api.harness.dstore.transaction() as session:
        session.add(
            MessageRow(
                id=f"msg_external_{channel_id}_{sequence}",
                channel_id=channel_id,
                sequence=sequence,
                role="assistant",
                kind="agent_update",
                content="The external run completed.",
                created_at=api.clock() - 60,
                author_kind="system",
                author_id="daemon",
                origin_json=json.dumps(
                    {
                        "source_work_id": "job_external",
                        "source_run_id": "run_external",
                        "historical": historical,
                    }
                ),
            )
        )


def test_run_only_message_keeps_source_and_historical_provenance(api: Any) -> None:
    owner, guest, _ = _people(api)
    channel_id = _channel(api, owner, "workspace")
    _message(api, channel_id, 1, historical=True)
    _message(api, channel_id, 2, historical=False)

    response = api.client.get(f"/v1/channels/{channel_id}/messages", headers=guest)
    assert response.status_code == 200, response.text
    messages = response.json()
    assert [(m["source_run_id"], m["historical"]) for m in messages] == [
        ("run_external", True),
        ("run_external", False),
    ]
    assert all(message["turn_id"] is None for message in messages)
    assert all(message["work"] is None for message in messages)
    assert all(message["author"]["kind"] == "system" for message in messages)
    assert all(message["origin"] is None for message in messages)
    assert all(message["source_work_id"] == "job_external" for message in messages)


def _external_channel(api: Any, *, historical: bool) -> str:
    channel_id = "chn_imported" if historical else "chn_new"
    created_at = api.clock() - 3600
    with api.harness.dstore.transaction() as session:
        session.add(
            ChannelRow(
                id=channel_id,
                workspace_id="local",
                user_id="system:daemon",
                title="Issue from outside chat",
                state="active",
                revision=1,
                created_at=created_at,
                updated_at=created_at,
                visibility="workspace",
            )
        )
        session.add(
            ExternalJobRow(
                work_id=f"job_{channel_id}",
                job_key=f"issue:example/repository:{channel_id}",
                workspace_id="local",
                channel_id=channel_id,
                title="Issue from outside chat",
                state="queued",
                source_json=json.dumps(
                    {
                        "kind": "github",
                        "repository": "example/repository",
                        "url": "https://example.test/issue/1",
                    }
                ),
                system_created=1,
                historical=int(historical),
                read_baseline=2 if historical else 0,
                created_at=created_at,
                updated_at=created_at,
            )
        )
    _message(api, channel_id, 1, historical=historical)
    _message(api, channel_id, 2, historical=historical)
    return channel_id


def test_new_job_is_unread_for_workspace_viewers_without_memberships(api: Any) -> None:
    owner, guest, admin = _people(api)
    channel_id = _external_channel(api, historical=False)
    for headers in (owner, guest, admin):
        listing = api.client.get("/v1/channels", headers=headers).json()["items"]
        assert len(listing) == 1
        channel = listing[0]
        assert channel["id"] == channel_id
        assert channel["unread_count"] == 2
        assert channel["my_role"] is None
        assert channel["created_by"] is None
        assert channel["external_work"]["source"]["repository"] == "example/repository"
        assert channel["external_work"]["read_baseline"] == 0
        assert channel["external_work"]["system_created"] is True
        detail = api.client.get(f"/v1/channels/{channel_id}", headers=headers).json()
        assert detail == channel

    with api.harness.dstore.read() as session:
        assert (
            session.scalars(
                select(ChannelMemberRow).where(ChannelMemberRow.channel_id == channel_id)
            ).all()
            == []
        )

    read = api.client.put(f"/v1/channels/{channel_id}/read", json={"sequence": 2}, headers=guest)
    assert read.status_code == 200, read.text
    assert read.json()["role"] == "member"
    assert read.json()["last_read_sequence"] == 2
    assert api.client.get(f"/v1/channels/{channel_id}", headers=guest).json()["unread_count"] == 0
    assert api.client.get(f"/v1/channels/{channel_id}", headers=owner).json()["unread_count"] == 2
    assert (
        api.client.patch(
            f"/v1/channels/{channel_id}", json={"title": "Mine"}, headers=guest
        ).status_code
        == 403
    )


def test_import_baseline_is_read_without_memberships_and_live_posts_are_unread(api: Any) -> None:
    owner, guest, _ = _people(api)
    channel_id = _external_channel(api, historical=True)
    original = api.client.get(f"/v1/channels/{channel_id}", headers=guest).json()
    assert original["unread_count"] == 0
    assert original["external_work"]["historical"] is True
    assert original["external_work"]["read_baseline"] == 2

    # A stale browser cannot move a historical viewer behind the import floor.
    read = api.client.put(f"/v1/channels/{channel_id}/read", json={"sequence": 0}, headers=guest)
    assert read.status_code == 200, read.text
    assert read.json()["last_read_sequence"] == 2
    _message(api, channel_id, 3, historical=False)
    for headers in (owner, guest):
        channel = api.client.get(f"/v1/channels/{channel_id}", headers=headers).json()
        assert channel["unread_count"] == 1
        assert channel["created_at"] == original["created_at"]
        assert channel["updated_at"] == original["updated_at"]

    read = api.client.put(f"/v1/channels/{channel_id}/read", json={"sequence": 999}, headers=guest)
    assert read.json()["last_read_sequence"] == 3
    late = api.client.put(f"/v1/channels/{channel_id}/read", json={"sequence": 1}, headers=guest)
    assert late.json()["last_read_sequence"] == 3
    guest_id = _user_id(api, guest)
    with api.harness.dstore.read() as session:
        members = session.scalars(
            select(ChannelMemberRow).where(ChannelMemberRow.channel_id == channel_id)
        ).all()
        assert [(entry.user_id, entry.role) for entry in members] == [(guest_id, "member")]


def test_ordinary_workspace_channel_keeps_its_existing_unread_behavior(api: Any) -> None:
    owner, guest, _ = _people(api)
    channel_id = _channel(api, owner, "workspace")
    _message(api, channel_id, 1, historical=False)
    channel = api.client.get(f"/v1/channels/{channel_id}", headers=guest).json()
    assert channel["unread_count"] is None
    assert channel["external_work"] is None


def test_delayed_history_does_not_mark_live_activity_read_or_add_unread(api: Any) -> None:
    _, guest, _ = _people(api)
    channel_id = _external_channel(api, historical=True)
    _message(api, channel_id, 3, historical=False)
    _message(api, channel_id, 4, historical=True)
    channel = api.client.get(f"/v1/channels/{channel_id}", headers=guest).json()
    assert channel["external_work"]["read_baseline"] == 2
    assert channel["unread_count"] == 1
    read = api.client.put(f"/v1/channels/{channel_id}/read", json={"sequence": 3}, headers=guest)
    assert read.status_code == 200, read.text
    assert api.client.get(f"/v1/channels/{channel_id}", headers=guest).json()["unread_count"] == 0


def test_imported_no_turn_private_work_stays_read_without_external_metadata(api: Any) -> None:
    owner, guest, _ = _people(api)
    channel_id = _channel(api, owner, "private")
    item = external_item(api).model_copy(update={"channel_id": channel_id})
    with api.harness.dstore.immediate_transaction() as session:
        session.execute(
            update(WorkItemRow)
            .where(WorkItemRow.item_id == item.item_id)
            .values(channel_id=channel_id)
        )
    _run(api, "private-history", "completed", api.clock(), item)
    api.ctx.project_work()
    messages = api.client.get(f"/v1/channels/{channel_id}/messages", headers=owner).json()
    assert messages
    assert all(message["historical"] and message["turn_id"] is None for message in messages)

    channel = api.client.get(f"/v1/channels/{channel_id}", headers=owner).json()
    assert channel["external_work"] is None
    assert channel["my_role"] == "owner"
    assert channel["unread_count"] == 0
    assert api.client.get(f"/v1/channels/{channel_id}", headers=guest).status_code == 404

    last_sequence = max(message["sequence"] for message in messages)
    _message(api, channel_id, last_sequence + 1, historical=False)
    _message(api, channel_id, last_sequence + 2, historical=True)
    channel = api.client.get(f"/v1/channels/{channel_id}", headers=owner).json()
    assert channel["unread_count"] == 1
