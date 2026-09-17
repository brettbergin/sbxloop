"""TurnCoordinator: per-channel FIFO lanes over one bounded pool.

Turns in one channel run strictly one after another, in the order they
were submitted; turns in different channels share a pool of
``[concierge] max_concurrent_turns`` workers and overlap up to that width.
The unit tests drive the coordinator directly; the API tests drive it the
way the daemon does, through turn acceptance and restart recovery.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sbxloop.api.app import create_app
from sbxloop.api.auth.store import ApiAuthStore
from sbxloop.api.context import ApiContext
from sbxloop.api.turns import TurnCoordinator
from sbxloop.daemon.concierge import ConciergeReply
from sbxloop.daemon.store import DaemonStore
from tests.api.conftest import build
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled

WAIT_S = 5.0


@dataclass(frozen=True)
class Ticket:
    id: str
    channel_id: str


def turn(channel: str, name: str) -> Ticket:
    return Ticket(id=name, channel_id=channel)


class Recorder:
    """Records starts and ends, and how many runs were inside at once."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[str] = []
        self.inside = 0
        self.peak = 0

    def run(self, name: str, body: Callable[[], None] = lambda: None) -> Callable[[], None]:
        def execute() -> None:
            with self.lock:
                self.events.append(f"start {name}")
                self.inside += 1
                self.peak = max(self.peak, self.inside)
            try:
                body()
            finally:
                with self.lock:
                    self.inside -= 1
                    self.events.append(f"end {name}")

        return execute


@pytest.fixture
def coordinator() -> Any:
    made: list[TurnCoordinator] = []

    def make(width: int) -> TurnCoordinator:
        value = TurnCoordinator(width)
        made.append(value)
        return value

    yield make
    for value in made:
        value.shutdown()


class TestLanes:
    def test_turns_in_one_channel_run_strictly_in_submission_order(self, coordinator: Any) -> None:
        turns = coordinator(4)
        recorder = Recorder()
        names = [f"t{i}" for i in range(5)]
        for name in names:
            turns.submit(turn("a", name), recorder.run(name, lambda: time.sleep(0.02)))
        assert turns.wait_idle(WAIT_S)
        assert recorder.events == [e for name in names for e in (f"start {name}", f"end {name}")]
        assert recorder.peak == 1

    def test_turns_in_different_channels_overlap(self, coordinator: Any) -> None:
        turns = coordinator(2)
        gate = threading.Barrier(2, timeout=WAIT_S)
        met: list[str] = []

        def meet(name: str) -> Callable[[], None]:
            def body() -> None:
                gate.wait()
                met.append(name)

            return body

        turns.submit(turn("a", "a1"), meet("a1"))
        turns.submit(turn("b", "b1"), meet("b1"))
        assert turns.wait_idle(WAIT_S)
        assert sorted(met) == ["a1", "b1"]

    def test_never_more_than_the_pool_width_run_at_once(self, coordinator: Any) -> None:
        turns = coordinator(2)
        recorder = Recorder()
        for index in range(6):
            channel = f"c{index % 3}"
            name = f"{channel}-{index}"
            turns.submit(turn(channel, name), recorder.run(name, lambda: time.sleep(0.05)))
        assert turns.wait_idle(WAIT_S)
        assert recorder.peak == 2
        assert len(recorder.events) == 12

    def test_a_failing_turn_does_not_stall_its_channel(self, coordinator: Any) -> None:
        turns = coordinator(1)
        ran: list[str] = []

        def boom() -> None:
            raise RuntimeError("provider exploded")

        turns.submit(turn("a", "t1"), boom)
        turns.submit(turn("a", "t2"), lambda: ran.append("t2"))
        assert turns.wait_idle(WAIT_S)
        assert ran == ["t2"]


class TestCancel:
    def test_cancel_channel_drops_queued_turns_and_asks_the_running_one_to_stop(
        self, coordinator: Any
    ) -> None:
        turns = coordinator(4)
        release = threading.Event()
        started = threading.Event()
        ran: list[str] = []
        cancelled: list[str] = []

        def first() -> None:
            started.set()
            release.wait(WAIT_S)
            ran.append("a1")

        def stop(name: str) -> bool:
            cancelled.append(name)
            return True

        turns.submit(turn("a", "a1"), first, cancel=lambda: stop("a1"))
        for name in ("a2", "a3"):
            turns.submit(
                turn("a", name),
                lambda name=name: ran.append(name),
                cancel=lambda name=name: stop(name),
            )
        assert started.wait(WAIT_S)
        assert turns.cancel_channel("a") == ["a1", "a2", "a3"]
        assert cancelled == ["a1", "a2", "a3"]
        release.set()
        assert turns.wait_idle(WAIT_S)
        # The running turn decides how to stop; queued ones never start.
        assert ran == ["a1"]

    def test_a_queued_turn_can_be_cancelled_before_it_starts(self, coordinator: Any) -> None:
        turns = coordinator(1)
        release = threading.Event()
        ran: list[str] = []
        cancelled: list[str] = []
        turns.submit(turn("a", "a1"), lambda: release.wait(WAIT_S))
        # b1 waits for the only worker; cancelling its channel settles it.
        turns.submit(
            turn("b", "b1"),
            lambda: ran.append("b1"),
            cancel=lambda: cancelled.append("b1") or True,
        )
        assert turns.cancel_channel("b") == ["b1"]
        release.set()
        assert turns.wait_idle(WAIT_S)
        assert cancelled == ["b1"]
        assert ran == []

    def test_cancel_channel_leaves_other_channels_alone(self, coordinator: Any) -> None:
        turns = coordinator(1)
        release = threading.Event()
        ran: list[str] = []
        cancelled: list[str] = []
        turns.submit(turn("a", "a1"), lambda: release.wait(WAIT_S))
        turns.submit(turn("a", "a2"), lambda: ran.append("a2"))
        turns.submit(
            turn("c", "c1"),
            lambda: ran.append("c1"),
            cancel=lambda: cancelled.append("c") or True,
        )
        assert turns.cancel_channel("b") == []
        release.set()
        assert turns.wait_idle(WAIT_S)
        assert sorted(ran) == ["a2", "c1"]
        assert cancelled == []

    def test_cancel_channel_reports_only_the_turns_it_stopped(self, coordinator: Any) -> None:
        turns = coordinator(1)
        release = threading.Event()
        started = threading.Event()
        asked: list[str] = []

        def first() -> None:
            started.set()
            release.wait(WAIT_S)

        def already_finished() -> bool:
            # The running turn completed before the request reached it.
            asked.append("a1")
            return False

        def stopped() -> bool:
            asked.append("a2")
            return True

        turns.submit(turn("a", "a1"), first, cancel=already_finished)
        turns.submit(turn("a", "a2"), lambda: None, cancel=stopped)
        assert started.wait(WAIT_S)
        assert turns.cancel_channel("a") == ["a2"]
        assert asked == ["a1", "a2"]
        release.set()
        assert turns.wait_idle(WAIT_S)


class TestShutdown:
    def test_shutdown_drops_queued_turns_without_cancelling_them(self, coordinator: Any) -> None:
        turns = coordinator(1)
        release = threading.Event()
        started = threading.Event()
        ran: list[str] = []
        cancelled: list[str] = []

        def first() -> None:
            started.set()
            release.wait(WAIT_S)
            ran.append("a1")

        def stop(name: str) -> bool:
            cancelled.append(name)
            return True

        turns.submit(turn("a", "a1"), first)
        turns.submit(
            turn("a", "a2"), lambda: ran.append("a2"), cancel=lambda: cancelled.append("a2")
        )
        assert started.wait(WAIT_S)
        # Queued work stays accepted in the store, for recovery to pick up.
        assert turns.shutdown() == ["a2"]
        with pytest.raises(RuntimeError):
            turns.submit(turn("a", "a3"), lambda: ran.append("a3"))
        release.set()
        assert turns.wait_idle(WAIT_S)
        assert ran == ["a1"]
        assert cancelled == []


# -- through the daemon's API ---------------------------------------------------


class Meeting(FakeConcierge):
    """Answers only once ``width`` turns are in flight at the same time."""

    def __init__(self, width: int) -> None:
        super().__init__()
        self.gate = threading.Barrier(width, timeout=WAIT_S)
        self.lock = threading.Lock()

    def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
        with self.lock:
            self.calls.append({"text": text, **kwargs})
        future: Future[ConciergeReply] = Future()
        try:
            self.gate.wait()
        except threading.BrokenBarrierError:
            future.set_result(ConciergeReply("", ok=False, error="alone"))
            return future
        future.set_result(ConciergeReply(f"met {text}"))
        return future


class Timed(FakeConcierge):
    """Records when each turn's call starts and ends."""

    def __init__(self) -> None:
        super().__init__()
        self.recorder = Recorder()
        self.lock = threading.Lock()

    def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
        with self.lock:
            self.calls.append({"text": text, **kwargs})
        self.recorder.run(text, lambda: time.sleep(0.05))()
        future: Future[ConciergeReply] = Future()
        future.set_result(ConciergeReply(f"reply {text}"))
        return future


def test_turns_in_different_channels_run_side_by_side(tmp_path: Path) -> None:
    api = build(tmp_path, config={"concierge": {"max_concurrent_turns": 2}})
    try:
        with api.client:
            api.ctx.concierge = Meeting(2)
            headers = bearer(register(api))
            channels = [
                api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
                for _ in range(2)
            ]
            accepted = [
                api.client.post(
                    f"/v1/channels/{channel}/turns", headers=headers, json={"content": channel}
                ).json()["turn"]["id"]
                for channel in channels
            ]
            results = [
                settled(api.client, headers, channel, turn_id)
                for channel, turn_id in zip(channels, accepted, strict=True)
            ]
            assert [r["status"] for r in results] == ["completed", "completed"]
    finally:
        api.ctx.close()


def test_turns_in_one_channel_still_run_one_at_a_time(tmp_path: Path) -> None:
    api = build(tmp_path, config={"concierge": {"max_concurrent_turns": 4}})
    try:
        with api.client:
            concierge = Timed()
            api.ctx.concierge = concierge
            headers = bearer(register(api))
            channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
            accepted = [
                api.client.post(
                    f"/v1/channels/{channel}/turns", headers=headers, json={"content": name}
                ).json()["turn"]["id"]
                for name in ("one", "two", "three")
            ]
            for turn_id in accepted:
                assert settled(api.client, headers, channel, turn_id)["status"] == "completed"
            assert api.ctx.turns.wait_idle(WAIT_S)
            assert concierge.recorder.events == [
                "start one",
                "end one",
                "start two",
                "end two",
                "start three",
                "end three",
            ]
    finally:
        api.ctx.close()


def test_cancel_channel_settles_queued_turns_and_stops_the_running_one(tmp_path: Path) -> None:
    api = build(tmp_path, config={"concierge": {"max_concurrent_turns": 4}})
    try:
        with api.client:
            first: Future[ConciergeReply] = Future()
            calls: list[str] = []

            class Held(FakeConcierge):
                def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
                    calls.append(text)
                    if text == "running":
                        return first
                    return super().submit_turn(text, **kwargs)

            api.ctx.concierge = Held()
            headers = bearer(register(api))
            channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
            route = f"/v1/channels/{channel}/turns"
            running = api.client.post(route, headers=headers, json={"content": "running"}).json()
            queued = api.client.post(route, headers=headers, json={"content": "queued"}).json()
            deadline = time.monotonic() + WAIT_S
            while not calls and time.monotonic() < deadline:
                time.sleep(0.01)
            ids = [running["turn"]["id"], queued["turn"]["id"]]
            try:
                assert api.ctx.turns.cancel_channel(channel) == ids
                assert settled(api.client, headers, channel, ids[1])["status"] == "cancelled"
            finally:
                first.set_result(ConciergeReply("finished anyway"))
            assert api.ctx.turns.wait_idle(WAIT_S)
            assert settled(api.client, headers, channel, ids[0])["status"] == "cancelled"
            assert calls == ["running"]
    finally:
        api.ctx.close()


def test_cancel_channel_settles_queued_turns_of_a_deleted_channel(tmp_path: Path) -> None:
    api = build(tmp_path, config={"concierge": {"max_concurrent_turns": 4}})
    try:
        with api.client:
            first: Future[ConciergeReply] = Future()
            calls: list[str] = []

            class Held(FakeConcierge):
                def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
                    calls.append(text)
                    if text == "running":
                        return first
                    return super().submit_turn(text, **kwargs)

            api.ctx.concierge = Held()
            headers = bearer(register(api))
            channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
            route = f"/v1/channels/{channel}/turns"
            running = api.client.post(route, headers=headers, json={"content": "running"}).json()
            queued = api.client.post(route, headers=headers, json={"content": "queued"}).json()
            deadline = time.monotonic() + WAIT_S
            while not calls and time.monotonic() < deadline:
                time.sleep(0.01)
            ids = [running["turn"]["id"], queued["turn"]["id"]]
            try:
                # The stop flow tombstones the channel first, then clears its lane.
                assert (
                    api.client.delete(f"/v1/channels/{channel}", headers=headers).status_code == 204
                )
                assert api.ctx.turns.cancel_channel(channel) == ids
            finally:
                first.set_result(ConciergeReply("finished anyway"))
            assert api.ctx.turns.wait_idle(WAIT_S)
            from sbxloop.db.collaboration_models import MessageRow, TurnRow

            with api.loop.dstore.read() as session:
                rows = [session.get(TurnRow, turn_id) for turn_id in ids]
                assert [row.status if row else None for row in rows] == ["cancelled", "cancelled"]
                input_message = session.get(MessageRow, rows[1].input_message_id)
                assert input_message is not None
                assert "⏳" not in input_message.reactions_json
            assert calls == ["running"]
    finally:
        api.ctx.close()


def test_recovery_keeps_each_channels_turns_in_sequence(api: Any) -> None:
    headers = bearer(register(api))
    channels = [
        api.client.post("/v1/channels", json={}, headers=headers).json()["id"] for _ in range(2)
    ]
    from sqlalchemy import select

    from sbxloop.db.collaboration_models import LocalUserRow

    with api.loop.dstore.read() as session:
        user_id = session.scalars(select(LocalUserRow)).one().id
    store = api.ctx.collaboration
    order = [(channels[0], "a1"), (channels[1], "b1"), (channels[0], "a2"), (channels[0], "a3")]
    for channel, key in order:
        store.accept_turn(
            user_id,
            channel,
            content=key,
            targets=(),
            intent="conversation",
            client_turn_id=key,
            client_message_id=None,
            actor=None,
            now=api.clock(),
        )

    api.ctx.close()
    path = api.loop.dstore.path
    api.loop.dstore.close()
    reopened = DaemonStore(path)
    api.loop.dstore = reopened
    concierge = Timed()
    config = api.ctx.config.model_copy(
        update={
            "concierge": api.ctx.config.concierge.model_copy(update={"max_concurrent_turns": 4})
        }
    )
    restarted = ApiContext(
        config,
        loop=api.loop,
        auth=ApiAuthStore(reopened),
        keys=api.keys,
        clock=api.clock,
        concierge=concierge,
    )
    restarted.ready.set()
    try:
        with TestClient(create_app(restarted)):
            assert restarted.turns.wait_idle(WAIT_S)
            events = concierge.recorder.events
            in_a = [event for event in events if event.split()[1].startswith("a")]
            assert in_a == ["start a1", "end a1", "start a2", "end a2", "start a3", "end a3"]
            assert {"start b1", "end b1"} <= set(events)
    finally:
        restarted.close()
        reopened.close()
