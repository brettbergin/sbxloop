"""Tool activity is emitted by execution and remains scoped to its channel."""

from concurrent.futures import Future
from typing import Any

from sbxloop.daemon.concierge import ConciergeReply
from tests.api.test_collaboration import bearer, register
from tests.api.test_collaboration_recovery import settled


class ToolConcierge:
    def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
        activity = kwargs["on_tool_activity"]
        activity("sbx_control", "started", None)
        activity("sbx_control", "completed", True)
        future: Future[ConciergeReply] = Future()
        future.set_result(ConciergeReply("The queue is empty."))
        return future


def test_tool_lifecycle_in_public_chronology_has_channel_and_no_arguments(api: Any) -> None:
    api.ctx.concierge = ToolConcierge()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns", json={"content": "@operator check queue"}, headers=headers
    ).json()
    settled(api.client, headers, channel, accepted["turn"]["id"])
    events = api.client.get("/v1/events?type_prefix=collaboration.tool.", headers=headers).json()[
        "data"
    ]
    assert [event["type"] for event in events] == [
        "collaboration.tool.started",
        "collaboration.tool.completed",
    ]
    for event in events:
        assert event["data"] == {
            "channel_id": channel,
            "turn_id": accepted["turn"]["id"],
            "index": 0,
            "agent_slug": "operator",
            "tool": "sbx_control",
            "ok": None if event["type"].endswith("started") else True,
        }
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    assert messages[0]["reactions"] == ["✅"]


def test_late_tool_event_does_not_revive_a_finished_turn(api: Any) -> None:
    api.ctx.concierge = ToolConcierge()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    turn = api.client.post(
        f"/v1/channels/{channel}/turns", json={"content": "hello"}, headers=headers
    ).json()["turn"]
    settled(api.client, headers, channel, turn["id"])
    api.ctx.collaboration.record_tool_activity(
        turn["id"], 0, "late", "started", None, api.ctx.clock()
    )
    events = api.client.get("/v1/events?type_prefix=collaboration.tool.", headers=headers).json()[
        "data"
    ]
    assert len(events) == 2
