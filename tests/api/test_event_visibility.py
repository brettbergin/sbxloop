"""Who sees which public events.

A workspace member sees the events of the channels they can open and
nothing of anyone else's private channel, whether they page through
``/v1/events``, read a run's events, follow the SSE stream or subscribe on
the WebSocket. Events meant for one person reach only that person. A run's
events follow the channel that asked for the run; a run no channel asked
for is shown to workspace owners and admins only. A plain API client and a
workspace owner or admin see everything, as before.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from sbxloop.api import ws as ws_module
from sbxloop.api.auth.deps import resolve_token
from sbxloop.api.routes import events as events_route
from sbxloop.api.routes.events import sse_frames
from sbxloop.daemon.model import WorkItem
from sbxloop.ghids import chat_item_id
from tests.api.conftest import Api
from tests.api.test_channel_access import _channel, _invite
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled
from tests.unit.test_daemon_loop import gh_item


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events_route, "WAIT_S", 0.05)
    monkeypatch.setattr(ws_module, "WAIT_S", 0.05)


def _people(api: Api) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Tokens for the workspace owner, a plain member and an admin."""
    owner = register(api)
    guest = _invite(api, "member", "guest")
    admin = _invite(api, "admin", "admin")
    return owner, guest, admin


def _all_events(api: Api, headers: dict[str, str], **params: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        query = {"limit": 200, **params} | ({"after": after} if after else {})
        page = api.client.get("/v1/events", params=query, headers=headers)
        assert page.status_code == 200, page.text
        body = page.json()
        events.extend(body["data"])
        if not body["has_more"]:
            return events
        after = body["next_cursor"]


def _channels_in(events: list[dict[str, Any]]) -> set[str]:
    return {str(e["data"]["channel_id"]) for e in events if e["data"].get("channel_id")}


def _touch(api: Api, headers: dict[str, str], channel_id: str, slug: str = "planner") -> None:
    """Write one channel event (a participant change)."""
    response = api.client.put(
        f"/v1/channels/{channel_id}/participants/{slug}", headers=headers, json={}
    )
    assert response.status_code == 200, response.text


def test_a_member_lists_only_the_events_of_channels_they_can_open(api: Api) -> None:
    owner, guest, admin = _people(api)
    private = _channel(api, bearer(owner))
    shared = _channel(api, bearer(owner), "workspace")
    _touch(api, bearer(owner), private)
    _touch(api, bearer(owner), shared)

    seen = _channels_in(_all_events(api, bearer(guest)))
    assert private not in seen
    assert shared in seen

    for everyone in (bearer(owner), bearer(admin), api.bearer()):
        assert {private, shared} <= _channels_in(_all_events(api, everyone))

    # Once the guest is added, the private channel's history is theirs too.
    added = api.client.post(
        f"/v1/channels/{private}/members",
        headers=bearer(owner),
        json={"user_id": api.client.get("/v1/users/me", headers=bearer(guest)).json()["id"]},
    )
    assert added.status_code == 201, added.text
    assert private in _channels_in(_all_events(api, bearer(guest)))


def test_the_channel_filter_narrows_to_one_channel(api: Api) -> None:
    owner, guest, _ = _people(api)
    private = _channel(api, bearer(owner))
    shared = _channel(api, bearer(owner), "workspace")
    _touch(api, bearer(owner), private)
    _touch(api, bearer(owner), shared)

    only = _all_events(api, bearer(owner), channel_id=shared)
    assert only
    assert _channels_in(only) == {shared}
    assert all(e["data"].get("channel_id") == shared for e in only)
    assert _all_events(api, bearer(guest), channel_id=private) == []
    assert _channels_in(_all_events(api, bearer(guest), channel_id=shared)) == {shared}


def test_pages_skip_hidden_events_and_are_never_short(api: Api) -> None:
    owner, guest, _ = _people(api)
    private = _channel(api, bearer(owner))
    shared = _channel(api, bearer(owner), "workspace")
    visible: list[str] = []
    for slug in ("planner", "builder", "critic"):
        _touch(api, bearer(owner), private, slug)
        _touch(api, bearer(owner), private, slug)
        _touch(api, bearer(owner), shared, slug)
        newest = _all_events(api, bearer(owner), channel_id=shared)[-1]
        visible.append(newest["id"])
    headers = bearer(guest)
    start = visible[0]

    page = api.client.get("/v1/events", params={"after": start, "limit": 2}, headers=headers)
    body = page.json()
    assert [e["id"] for e in body["data"]] == visible[1:]
    assert body["has_more"] is False

    first = api.client.get(
        "/v1/events", params={"after": start, "limit": 1}, headers=headers
    ).json()
    assert [e["id"] for e in first["data"]] == [visible[1]]
    assert first["has_more"] is True
    assert first["next_cursor"] == visible[1]
    second = api.client.get(
        "/v1/events", params={"after": first["next_cursor"], "limit": 1}, headers=headers
    ).json()
    assert [e["id"] for e in second["data"]] == [visible[2]]
    assert second["has_more"] is False


def test_events_for_one_person_reach_only_that_person(api: Api) -> None:
    owner, guest, _ = _people(api)
    for token in (owner, guest):
        saved = api.client.put(
            "/v1/prompts/style", headers=bearer(token), json={"content": "brief"}
        )
        assert saved.status_code == 200, saved.text

    def preference_events(headers: dict[str, str]) -> int:
        return len(_all_events(api, headers, type_prefix="collaboration.preference."))

    assert preference_events(bearer(guest)) == 1
    assert preference_events(bearer(owner)) == 1
    assert preference_events(api.bearer()) == 2


def test_memory_events_follow_the_channel_the_memory_came_from(api: Api) -> None:
    owner, guest, admin = _people(api)
    private = _channel(api, bearer(owner))
    shared = _channel(api, bearer(owner), "workspace")
    made: dict[str | None, str] = {}
    for channel in (private, shared, None):
        body: dict[str, Any] = {"content": f"note {channel}"}
        if channel is not None:
            body["channel_id"] = channel
        created = api.client.post("/v1/agents/planner/memories", headers=bearer(owner), json=body)
        assert created.status_code == 201, created.text
        made[channel] = created.json()["id"]

    def memories_seen(headers: dict[str, str]) -> set[str]:
        return {
            str(e["data"]["memory_id"])
            for e in _all_events(api, headers, type_prefix="agent.memory.")
        }

    assert memories_seen(bearer(guest)) == {made[shared], made[None]}
    for everyone in (bearer(owner), bearer(admin), api.bearer()):
        assert memories_seen(everyone) == set(made.values())


def _collect_sse(api: Api, token: dict[str, Any], *, until: int) -> list[dict[str, Any]]:
    auth = resolve_token(api.ctx, token["access_token"])

    async def go() -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        gen: AsyncIterator[str] = sse_frames(api.ctx, auth, after=0, run_id=None, type_prefix=None)
        async for frame in gen:
            if frame.startswith(":"):
                continue
            data = next(line for line in frame.splitlines() if line.startswith("data:"))
            frames.append(json.loads(data.removeprefix("data:").strip()))
            if len(frames) >= until:
                break
        return frames

    async def bounded() -> list[dict[str, Any]]:
        return await asyncio.wait_for(go(), timeout=30)

    return asyncio.run(bounded())


def test_the_stream_carries_only_visible_events(api: Api) -> None:
    owner, guest, _ = _people(api)
    private = _channel(api, bearer(owner))
    shared = _channel(api, bearer(owner), "workspace")
    _touch(api, bearer(owner), private)
    _touch(api, bearer(owner), shared)
    wanted = len(_all_events(api, bearer(guest)))

    frames = _collect_sse(api, guest, until=wanted)
    assert private not in _channels_in(frames)
    assert shared in _channels_in(frames)
    assert [f["id"] for f in frames] == [e["id"] for e in _all_events(api, bearer(guest))]

    owner_frames = _collect_sse(api, owner, until=len(_all_events(api, bearer(owner))))
    assert private in _channels_in(owner_frames)


def test_an_idle_stream_moves_its_cursor_past_hidden_events(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, guest, _ = _people(api)
    private = _channel(api, bearer(owner))
    shared = _channel(api, bearer(owner), "workspace")
    auth = resolve_token(api.ctx, guest["access_token"])
    starts: list[int] = []
    original = api.ctx.chronology.read

    def spy(**kwargs: Any) -> Any:
        starts.append(int(kwargs.get("after", 0)))
        return original(**kwargs)

    monkeypatch.setattr(api.ctx.chronology, "read", spy)

    async def go() -> dict[str, Any]:
        gen = sse_frames(api.ctx, auth, after=0, run_id=None, type_prefix=None)
        async for frame in gen:
            if frame.startswith(":"):
                continue
            payload = json.loads(frame.split("data:", 1)[1].strip())
            assert payload["data"].get("channel_id") != private
            if payload["type"] == "collaboration.participant.added":
                return dict(payload)
        raise AssertionError("stream ended")

    async def writer() -> None:
        await asyncio.sleep(0.2)
        await asyncio.to_thread(_touch, api, bearer(owner), private)
        api.ctx.hub.notify()
        await asyncio.sleep(0.2)
        await asyncio.to_thread(_touch, api, bearer(owner), shared)
        api.ctx.hub.notify()

    async def both() -> dict[str, Any]:
        task = asyncio.create_task(writer())
        result = await asyncio.wait_for(go(), timeout=10)
        await task
        return result

    event = asyncio.run(both())
    assert event["data"]["channel_id"] == shared
    hidden = [
        int(e["id"].removeprefix("evt_"))
        for e in _all_events(api, bearer(owner), channel_id=private)
    ]
    # Some read started beyond the private channel's events without the
    # guest ever receiving one of them.
    assert any(start >= max(hidden) for start in starts)


def test_the_websocket_carries_only_visible_events(api: Api) -> None:
    owner, guest, _ = _people(api)
    private = _channel(api, bearer(owner))
    shared = _channel(api, bearer(owner), "workspace")
    with api.client.websocket_connect("/v1/ws", headers=bearer(guest)) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello"
        ws.send_text(json.dumps({"type": "subscribe", "after": hello["watermark"]}))
        assert json.loads(ws.receive_text())["type"] == "subscribed"
        _touch(api, bearer(owner), private)
        _touch(api, bearer(owner), shared)
        api.ctx.hub.notify()
        while True:
            frame = json.loads(ws.receive_text())
            if frame["type"] != "event":
                continue
            data = frame["event"]["data"]
            assert data.get("channel_id") != private
            if data.get("channel_id") == shared:
                break


def _user_id(api: Api, token: dict[str, Any]) -> str:
    return str(api.client.get("/v1/users/me", headers=bearer(token)).json()["id"])


def _remove(api: Api, actor: dict[str, Any], user_id: str) -> None:
    removed = api.client.delete(f"/v1/workspace/members/{user_id}", headers=bearer(actor))
    assert removed.status_code == 204, removed.text


def test_a_removed_member_receives_nothing_more_on_an_open_stream(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person removed from the workspace keeps an access token that still
    verifies, but an event stream they opened as a member ends at the next
    re-check with ``access_revoked``: it never falls back to the unfiltered
    view a plain client gets."""
    monkeypatch.setattr(events_route, "ACCESS_RECHECK_S", 0.0)
    owner, guest, _ = _people(api)
    private = _channel(api, bearer(owner))
    shared = _channel(api, bearer(owner), "workspace")
    _touch(api, bearer(owner), shared)
    wanted = len(_all_events(api, bearer(guest)))
    auth = resolve_token(api.ctx, guest["access_token"])

    async def go() -> list[str]:
        gen = sse_frames(api.ctx, auth, after=0, run_id=None, type_prefix=None)
        delivered = 0
        async for frame in gen:
            if frame.startswith("id: evt_"):
                delivered += 1
            if delivered >= wanted:
                break
        # The member is removed while the stream is open; every event that
        # lands afterwards, private or shared, must stay unseen.
        await asyncio.to_thread(_remove, api, owner, _user_id(api, guest))
        await asyncio.to_thread(_touch, api, bearer(owner), private)
        await asyncio.to_thread(_touch, api, bearer(owner), shared)
        api.ctx.hub.notify()
        after: list[str] = []
        async for frame in gen:
            after.append(frame)
            if frame.startswith("event: stream.closed") or frame.startswith("id: evt_"):
                break
        return after

    after = asyncio.run(asyncio.wait_for(go(), timeout=30))
    assert after == ['event: stream.closed\ndata: {"reason":"access_revoked"}\n\n'], after
    assert private in _channels_in(_all_events(api, bearer(owner), channel_id=private))


def test_a_plain_client_stream_outlives_the_access_re_check(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream opened by an API client with no local user is not pinned
    to a member: it keeps delivering across re-checks."""
    monkeypatch.setattr(events_route, "ACCESS_RECHECK_S", 0.0)
    auth = resolve_token(api.ctx, api.token()["access_token"])
    api.ctx.chronology.record("daemon.notice", api.clock(), data={"n": 1})

    async def go() -> list[str]:
        gen = sse_frames(api.ctx, auth, after=0, run_id=None, type_prefix=None)
        seen = [await gen.__anext__()]
        api.ctx.chronology.record("daemon.notice", api.clock(), data={"n": 2})
        api.ctx.hub.notify()
        async for frame in gen:
            if frame.startswith("id: evt_") or frame.startswith("event: stream.closed"):
                seen.append(frame)
                break
        return seen

    seen = asyncio.run(asyncio.wait_for(go(), timeout=30))
    assert all(frame.startswith("id: evt_") for frame in seen), seen
    assert '"n":2' in seen[-1]


def _linked_run(api: Api, headers: dict[str, str], channel_id: str, text: str) -> str:
    """Run a workload asked for in ``channel_id``; returns the run id."""
    accepted = api.client.post(
        f"/v1/channels/{channel_id}/turns", headers=headers, json={"content": text}
    )
    assert accepted.status_code == 202, accepted.text
    settled(api.client, headers, channel_id, accepted.json()["turn"]["id"])
    key = accepted.json()["turn"]["input_message_id"]
    item = WorkItem(
        item_id=chat_item_id(key), source_key=key, title=text, body=text, kind="workload"
    )
    api.harness.dstore.upsert_new(item, api.clock())
    api.harness.source.items = [item]
    api.harness.outcomes = ["completed"]
    api.clock.t += 10
    api.loop.tick()
    run_id = str(api.harness.runs[-1][0])
    from sbxloop_worker.protocol import Event

    api.harness.store.append_event(
        Event(ts=api.clock(), run_id=run_id, type="phase.start", data={"phase": "plan"})
    )
    return run_id


def test_run_events_follow_the_channel_that_asked_for_the_run(api: Api) -> None:
    api.ctx.concierge = FakeConcierge()
    owner, guest, admin = _people(api)
    private = _channel(api, bearer(owner))
    shared = _channel(api, bearer(owner), "workspace")
    hidden_run = _linked_run(api, bearer(owner), private, "Private report")
    shared_run = _linked_run(api, bearer(owner), shared, "Shared report")

    def run_types(headers: dict[str, str], run_id: str) -> list[str]:
        page = api.client.get(f"/v1/runs/run_{run_id}/events", headers=headers)
        assert page.status_code == 200, page.text
        return [e["type"] for e in page.json()["data"]]

    for everyone in (bearer(owner), bearer(admin), api.bearer()):
        assert "phase.start" in run_types(everyone, hidden_run)
    assert run_types(bearer(guest), hidden_run) == []
    shared_types = run_types(bearer(guest), shared_run)
    assert "run.started" in shared_types
    assert "phase.start" in shared_types

    listed = {e["run_id"] for e in _all_events(api, bearer(guest)) if e["run_id"]}
    assert f"run_{hidden_run}" not in listed
    assert f"run_{shared_run}" in listed


def test_a_run_no_channel_asked_for_is_shown_to_owners_and_admins(api: Api) -> None:
    owner, guest, admin = _people(api)
    api.harness.source.items = [gh_item("1")]
    api.harness.outcomes = ["merged"]
    api.clock.t += 10
    api.loop.tick()
    run_id = f"run_{api.harness.runs[-1][0]}"

    def runs_seen(headers: dict[str, str]) -> set[str]:
        return {str(e["run_id"]) for e in _all_events(api, headers) if e["run_id"]}

    assert run_id not in runs_seen(bearer(guest))
    for everyone in (bearer(owner), bearer(admin), api.bearer()):
        assert run_id in runs_seen(everyone)


def test_capabilities_advertise_scoped_events(api: Api) -> None:
    features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "events.scoped" in features
