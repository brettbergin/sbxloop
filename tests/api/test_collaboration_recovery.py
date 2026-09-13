"""Recovery and retry contracts at the persistent collaboration boundary."""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

from sbxloop.api.app import create_app
from sbxloop.api.auth.store import ApiAuthStore
from sbxloop.api.context import ApiContext
from sbxloop.daemon.store import DaemonStore
from tests.api.test_collaboration import FakeConcierge, bearer, register


def settled(client: Any, headers: dict[str, str], channel: str, turn: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f"/v1/channels/{channel}/turns/{turn}", headers=headers).json()
        if result["status"] not in {"accepted", "running"}:
            return dict(result)
        time.sleep(0.01)
    raise AssertionError("turn never settled")


def test_retry_cannot_change_delegation(api: Any) -> None:
    api.ctx.concierge = FakeConcierge()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    url = f"/v1/channels/{channel}/turns"
    original = {"content": "inspect this", "client_turn_id": "stable"}
    assert api.client.post(url, headers=headers, json=original).status_code == 202
    for changes in ({"intent": "delegate"}, {"target_slugs": ["github"]}):
        conflict = api.client.post(url, headers=headers, json={**original, **changes})
        assert conflict.status_code == 409, conflict.text


def test_restart_resumes_only_unstarted_turns_and_preserves_replies(api: Any) -> None:
    headers = bearer(register(api))
    # Resolve through persisted profile rather than a second auth identity.
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    from sqlalchemy import select

    from sbxloop.db.collaboration_models import LocalUserRow

    with api.loop.dstore.read() as session:
        user_id = session.scalars(select(LocalUserRow)).one().id
    store = api.ctx.collaboration

    def accepted(key: str, intent: str = "conversation") -> Any:
        return store.accept_turn(
            user_id,
            channel,
            content=key,
            targets=(),
            intent=intent,
            client_turn_id=key,
            client_message_id=None,
            actor=None,
            now=api.clock(),
        )[0]

    interrupted = accepted("interrupted", "delegate")
    store.start_turn(interrupted.id, api.clock())
    recorded = accepted("recorded")
    store.start_turn(recorded.id, api.clock())
    store.append_reply(recorded.id, content="already answered", agent_slug=None, now=api.clock())
    queued = accepted("queued", "delegate")

    api.ctx.close()
    path = api.loop.dstore.path
    api.loop.dstore.close()
    reopened = DaemonStore(path)
    api.loop.dstore = reopened
    concierge = FakeConcierge()
    restarted = ApiContext(
        api.ctx.config,
        loop=api.loop,
        auth=ApiAuthStore(reopened),
        keys=api.keys,
        clock=api.clock,
        concierge=concierge,
    )
    restarted.ready.set()
    try:
        with TestClient(create_app(restarted)) as client:
            assert settled(client, headers, channel, interrupted.id)["status"] == "failed"
            assert settled(client, headers, channel, recorded.id)["status"] == "completed"
            assert settled(client, headers, channel, queued.id)["status"] == "completed"
            messages = client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
            assert sum(m["content"] == "already answered" for m in messages) == 1
            assert len(concierge.calls) == 1
            assert concierge.calls[0]["text"] == "queued"
            assert concierge.calls[0]["allow_actions"] is True
            assert any(m["kind"] == "turn_error" for m in messages)
            assert (
                restarted.collaboration.append_reply(
                    interrupted.id, content="late", agent_slug=None, now=api.clock()
                )
                is None
            )
    finally:
        restarted.close()
        reopened.close()


def test_history_contains_only_this_channels_prior_turns(api: Any) -> None:
    concierge = FakeConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channels = [
        api.client.post("/v1/channels", json={}, headers=headers).json()["id"] for _ in range(2)
    ]
    for channel, content in (
        (channels[0], "remember blue"),
        (channels[1], "private red"),
        (channels[0], "@github recall our topic"),
    ):
        response = api.client.post(
            f"/v1/channels/{channel}/turns", headers=headers, json={"content": content}
        )
        settled(api.client, headers, channel, response.json()["turn"]["id"])
    history = concierge.calls[-1]["history"]
    assert "remember blue" in history
    assert "private red" not in history
    assert "@github recall our topic" not in history


def test_failed_turn_is_visible_in_durable_history(api: Any) -> None:
    from concurrent.futures import Future

    from sbxloop.daemon.concierge import ConciergeReply

    class Broken(FakeConcierge):
        def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
            result: Future[ConciergeReply] = Future()
            result.set_result(ConciergeReply("", ok=False, error="provider unavailable"))
            return result

    api.ctx.concierge = Broken()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    response = api.client.post(
        f"/v1/channels/{channel}/turns", headers=headers, json={"content": "hello"}
    )
    result = settled(api.client, headers, channel, response.json()["turn"]["id"])
    assert result["status"] == "failed"
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    assert messages[-1]["kind"] == "turn_error"
    assert "provider unavailable" in messages[-1]["content"]


def test_team_turn_finishes_before_next_turn_and_shares_prior_reply(api: Any) -> None:
    from concurrent.futures import Future

    from sbxloop.daemon.concierge import ConciergeReply

    class Blocking(FakeConcierge):
        def __init__(self) -> None:
            super().__init__()
            self.first: Future[ConciergeReply] = Future()

        def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
            if not self.calls:
                self.calls.append({"text": text, **kwargs})
                return self.first
            return super().submit_turn(text, **kwargs)

    concierge = Blocking()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    first = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@github @software-dev first"},
    ).json()["turn"]
    second = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "follow up"},
    ).json()["turn"]
    try:
        deadline = time.monotonic() + 2
        while not concierge.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(concierge.calls) == 1
        assert (
            api.client.get(
                f"/v1/channels/{channel}/turns/{second['id']}",
                headers=headers,
            ).json()["status"]
            == "accepted"
        )
        concierge.first.set_result(ConciergeReply("first role answer"))
        assert settled(api.client, headers, channel, first["id"])["status"] == "completed"
        assert settled(api.client, headers, channel, second["id"])["status"] == "completed"
        assert [call["session_key"].rsplit(":", 1)[-1] for call in concierge.calls] == [
            "github",
            "software-dev",
            "angie",
        ]
        assert "first role answer" in concierge.calls[-1]["history"]
        assert "first role answer" in concierge.calls[1]["history"]
    finally:
        if not concierge.first.done():
            concierge.first.set_result(ConciergeReply("released"))


def test_tombstone_cancels_queued_work_and_drops_late_reply(api: Any) -> None:
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    user = api.ctx.collaboration.user_by_username("owner")
    assert user is not None
    store = api.ctx.collaboration
    turn = store.accept_turn(
        user.id,
        channel,
        content="hello",
        targets=(),
        client_turn_id="delete-race",
        client_message_id=None,
        actor=None,
        now=api.clock(),
    )[0]
    assert api.client.delete(f"/v1/channels/{channel}", headers=headers).status_code == 204
    assert not store.start_turn(turn.id, api.clock())
    assert store.append_reply(turn.id, content="late", agent_slug=None, now=api.clock()) is None
    assert api.client.get("/v1/channels", headers=headers).json()["items"] == []


def test_deleting_channel_stops_remaining_team_members(api: Any) -> None:
    from concurrent.futures import Future

    from sbxloop.daemon.concierge import ConciergeReply

    class Blocking(FakeConcierge):
        def __init__(self) -> None:
            super().__init__()
            self.first: Future[ConciergeReply] = Future()

        def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
            if not self.calls:
                self.calls.append({"text": text, **kwargs})
                return self.first
            return super().submit_turn(text, **kwargs)

    concierge = Blocking()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@github @software-dev first"},
    )
    deadline = time.monotonic() + 2
    while not concierge.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    after_calls: list[str] = []
    try:
        assert len(concierge.calls) == 1
        assert api.client.delete(f"/v1/channels/{channel}", headers=headers).status_code == 204
    finally:
        concierge.first.set_result(
            ConciergeReply("late result", after=lambda: after_calls.append("delivered"))
        )
    # Drain the single turn queue without a timing-based assertion.
    api.ctx.turn_executor.submit(lambda: None).result(timeout=5)
    assert len(concierge.calls) == 1
    assert after_calls == []
