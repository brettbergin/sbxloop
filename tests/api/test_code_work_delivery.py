"""Code jobs retain the exact channel and repository that requested them."""

from concurrent.futures import Future
from typing import Any

import pytest

from sbxloop.api.collaboration import CollaborationStore
from sbxloop.daemon.concierge import ConciergeReply
from sbxloop.daemon.model import WorkItem
from sbxloop.ghids import issue_item_id
from tests.api.test_collaboration import bearer, register
from tests.api.test_collaboration_recovery import settled


class CodeConcierge:
    def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
        kwargs["on_code_work"]("owner/repo", 12, "Add a feature")
        kwargs["on_code_work"]("owner/repo", 12, "Add a feature")
        future: Future[ConciergeReply] = Future()
        future.set_result(ConciergeReply("Queued the requested issue."))
        return future


@pytest.mark.parametrize("outcome", ["failed", "merged"])
def test_code_origin_survives_dispatch_and_store_reopen_without_cross_channel_leaks(
    api: Any,
    outcome: str,
) -> None:
    api.ctx.concierge = CodeConcierge()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    other = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "Add a feature", "intent": "code"},
    ).json()
    settled(api.client, headers, channel, accepted["turn"]["id"])
    path = f"/v1/channels/{channel}/work"
    pending = api.client.get(path, headers=headers).json()
    assert len(pending) == 1
    assert pending[0]["state"] == "awaiting_dispatch"
    assert pending[0]["run_actions"] == []
    assert api.client.get(f"/v1/channels/{other}/work", headers=headers).json() == []
    # Another repository's identically numbered issue must never satisfy the link.
    wrong = WorkItem(
        item_id=issue_item_id(12, "other/repo"),
        source_key="12",
        repo="other/repo",
        title="Unrelated",
        kind="code",
    )
    api.harness.dstore.upsert_new(wrong, api.clock())
    assert api.client.get(path, headers=headers).json()[0]["state"] == "awaiting_dispatch"
    item = WorkItem(
        item_id=issue_item_id(12, "owner/repo"),
        source_key="12",
        repo="owner/repo",
        title="Add a feature",
        kind="code",
    )
    api.harness.dstore.upsert_new(item, api.clock())
    api.ctx._collaboration = CollaborationStore(api.harness.dstore)
    queued = api.client.get(path, headers=headers).json()
    assert len(queued) == 1
    detail = api.client.get(f"/v1/items/{queued[0]['item_id']}", headers=headers).json()
    assert detail["title"] == "Add a feature"
    assert queued[0]["state"] == "queued"
    if outcome == "failed":
        api.harness.dstore.mark_failed(
            item.item_id, "checkout unavailable", api.clock(), requeue=False
        )
    else:
        api.harness.dstore.mark_cancelled(wrong.item_id, "unrelated fixture", api.clock())
        api.harness.source.items = [item]
        api.harness.outcomes = ["merged"]
        api.clock.t += 10
        api.loop.tick()
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    results = [message for message in messages if message["kind"] == "work_result"]
    assert len(results) == 1
    assert (
        "checkout unavailable" in results[0]["content"]
        if outcome == "failed"
        else "View pull request" in results[0]["content"]
    )
    assert results[0]["work"]["state"] == outcome
    assert results[0]["work"]["kind"] == "code"
    api.client.get(path, headers=headers)
    assert api.client.get(f"/v1/channels/{other}/work", headers=headers).json() == []
    api.client.delete(f"/v1/channels/{channel}", headers=headers)
    assert api.ctx.project_work() == []
