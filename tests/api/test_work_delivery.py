"""Managed work returns to its original conversation without replaying tools."""

from typing import Any

from sbxloop.api.collaboration import CollaborationStore
from sbxloop.daemon.model import WorkItem
from sbxloop.ghids import chat_item_id
from tests.api.test_collaboration import FakeConcierge, bearer, register


def setup_work(api: Any, *, suffix: str = "") -> tuple[dict[str, str], str, WorkItem]:
    api.ctx.concierge = FakeConcierge()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "Prepare a report", "target_slugs": ["concierge", "operator"]},
    ).json()
    api.ctx.turn_executor.submit(lambda: None).result(timeout=5)
    key = accepted["turn"]["input_message_id"] + suffix
    item = WorkItem(
        item_id=chat_item_id(key),
        source_key=key,
        title="Report",
        body="Prepare a report",
        kind="workload",
    )
    api.harness.dstore.upsert_new(item, api.clock())
    return headers, channel, item


def test_work_delivers_once_after_completion_and_store_reopen(api: Any) -> None:
    headers, channel, item = setup_work(api)
    url = f"/v1/channels/{channel}"
    queued = api.client.get(url + "/work", headers=headers)
    assert queued.status_code == 200, queued.text
    assert queued.json()[0]["state"] == "queued"
    api.harness.source.items = [item]
    api.harness.outcomes = ["completed"]
    api.clock.t += 10
    api.loop.tick()
    messages = api.client.get(url + "/messages", headers=headers).json()
    results = [m for m in messages if m["kind"] == "work_result"]
    assert len(results) == 1
    assert "the answer is 42" in results[0]["content"]
    assert results[0]["work"]["run_id"].startswith("run_")
    assert results[0]["agent_slug"] == "concierge"
    api.ctx._collaboration = CollaborationStore(api.harness.dstore)
    api.ctx.project_work()
    again = api.client.get(url + "/messages", headers=headers).json()
    assert [m["id"] for m in again] == [m["id"] for m in messages]
    assert len(api.harness.runs) == 1


def test_held_work_does_not_publish_unapproved_output(api: Any) -> None:
    headers, channel, item = setup_work(api, suffix=":1")
    api.harness.source.report_held = lambda _item: True
    api.harness.source.items = [item]
    api.harness.outcomes = ["held"]
    api.clock.t += 10
    api.loop.tick()
    work = api.client.get(f"/v1/channels/{channel}/work", headers=headers)
    assert work.status_code == 200, work.text
    assert work.json()[0]["state"] == "held"
    assert work.json()[0]["agent_slug"] == "operator"
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    assert not any(m["kind"] == "work_result" for m in messages)
    assert "the answer is 42" not in str(messages)


def test_unrelated_and_deleted_channels_do_not_receive_work(api: Any) -> None:
    headers, channel, _ = setup_work(api)
    other = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    assert api.client.get(f"/v1/channels/{other}/work", headers=headers).json() == []
    assert api.client.get(f"/v1/channels/{channel}/work").status_code == 401
    api.client.delete(f"/v1/channels/{channel}", headers=headers)
    assert api.client.get(f"/v1/channels/{channel}/work", headers=headers).status_code == 404
    api.ctx.project_work()


def test_failure_before_run_creation_is_delivered(api: Any) -> None:
    headers, channel, item = setup_work(api)
    api.harness.dstore.mark_failed(item.item_id, "sandbox unavailable", api.clock(), requeue=False)
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    result = next(message for message in messages if message["kind"] == "work_result")
    assert result["content"] == "sandbox unavailable"
    assert result["work"]["state"] == "failed"
    assert result["work"]["run_id"] is None


def test_source_identity_requires_a_complete_message_id(api: Any) -> None:
    headers, channel, item = setup_work(api)
    # Underscores must not become SQL LIKE wildcards; nor may a mere prefix match.
    with api.harness.dstore.immediate_transaction() as session:
        from sbxloop.db.daemon_models import WorkItemRow

        row = session.get(WorkItemRow, item.item_id)
        assert row is not None
        row.source_key = item.source_key + "unrelated"
    response = api.client.get(f"/v1/channels/{channel}/work", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == []
