"""Progress and stopping must describe real execution, including races."""

import time
from concurrent.futures import Future
from typing import Any

from sbxloop.daemon.concierge import ConciergeReply
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled


class Blocking(FakeConcierge):
    def __init__(self) -> None:
        super().__init__()
        self.first: Future[ConciergeReply] = Future()

    def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
        if not self.calls:
            self.calls.append({"text": text, **kwargs})
            return self.first
        return super().submit_turn(text, **kwargs)


def test_progress_and_stop_preserve_running_result_but_skip_queued_members(api: Any) -> None:
    concierge = Blocking()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    route = f"/v1/channels/{channel}/turns"
    first = api.client.post(
        route, json={"content": "@github @software-dev inspect"}, headers=headers
    ).json()["turn"]
    second = api.client.post(route, json={"content": "queued"}, headers=headers).json()["turn"]
    try:
        deadline = time.monotonic() + 2
        while not concierge.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        active = api.client.get(route + "?active_only=true", headers=headers)
        assert active.status_code == 200
        assert len(active.json()) == 2
        progress = api.client.get(route + "/" + first["id"], headers=headers).json()["participants"]
        assert [p["status"] for p in progress] == ["running", "queued"]
        stopped = api.client.post(route + "/" + first["id"] + "/cancel", headers=headers)
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "cancelling"
        stopped_queued = api.client.post(route + "/" + second["id"] + "/cancel", headers=headers)
        assert stopped_queued.json()["status"] == "cancelled"
        concierge.first.set_result(ConciergeReply("Actual running result"))
        api.ctx.turn_executor.submit(lambda: None).result(timeout=5)
        done = api.client.get(route + "/" + first["id"], headers=headers).json()
        assert done["status"] == "cancelled"
        assert [p["status"] for p in done["participants"]] == ["completed", "cancelled"]
        assert len(concierge.calls) == 1
        messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
        assert sum(m["content"] == "Actual running result" for m in messages) == 1
        assert api.client.get(route + "?active_only=true", headers=headers).json() == []
        assert (
            api.client.post(route + "/" + first["id"] + "/cancel", headers=headers).json() == done
        )
    finally:
        if not concierge.first.done():
            concierge.first.set_result(ConciergeReply("released"))


def test_stop_cannot_cross_channel_boundary(api: Any) -> None:
    api.ctx.concierge = FakeConcierge()
    headers = bearer(register(api))
    channels = [
        api.client.post("/v1/channels", json={}, headers=headers).json()["id"] for _ in range(2)
    ]
    turn = api.client.post(
        f"/v1/channels/{channels[0]}/turns", json={"content": "hello"}, headers=headers
    ).json()["turn"]
    settled(api.client, headers, channels[0], turn["id"])
    assert (
        api.client.post(
            f"/v1/channels/{channels[1]}/turns/{turn['id']}/cancel", headers=headers
        ).status_code
        == 404
    )
