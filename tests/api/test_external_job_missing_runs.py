"""Retained conversation attempts survive pruned execution records."""

from typing import Any

from sqlalchemy import delete

from sbxloop.db.engine_models import Run
from tests.api.test_collaboration import bearer, register
from tests.api.test_external_work import _run, channels, external_item
from tests.api.test_work_delivery import setup_work


def test_a_missing_attempt_does_not_hide_the_conversation_or_other_attempts(api: Any) -> None:
    headers = bearer(register(api))
    item = external_item(api)
    _run(api, "first", "failed", api.clock(), item)
    api.ctx.project_work()
    channel_id = channels(api)[0].id
    before = api.client.get(f"/v1/channels/{channel_id}/jobs", headers=headers).json()[0]

    api.clock.t += 1
    api.harness.dstore.mark_failed(item.item_id, "retry", api.clock(), requeue=False)
    api.harness.dstore.upsert_new(item, api.clock())
    _run(api, "second", "completed", api.clock(), item)
    api.ctx.project_work()
    with api.harness.dstore.immediate_transaction() as session:
        session.execute(delete(Run).where(Run.run_id == "first"))

    response = api.client.get(f"/v1/channels/{channel_id}/jobs", headers=headers)
    assert response.status_code == 200, response.text
    attempts = {job["run_id"]: job for job in response.json()}
    missing = attempts["run_first"]
    assert missing["unavailable"] is True
    assert missing["state"] == "failed"
    assert missing["kind"] == "workload"
    assert missing["created_at"] == before["created_at"]
    assert missing["updated_at"] == before["updated_at"]
    assert missing["source"] == before["source"]
    assert missing["run_revision"] is None
    assert missing["run_actions"] == []
    assert missing["item_actions"] == []
    assert missing["artifacts"] == []
    assert attempts["run_second"]["unavailable"] is False
    assert attempts["run_second"]["state"] == "completed"


def test_a_pruned_latest_chat_attempt_does_not_create_a_second_queued_job(api: Any) -> None:
    headers, channel_id, item = setup_work(api)
    _run(api, "latest", "failed", api.clock(), item)
    api.ctx.project_work()
    with api.harness.dstore.immediate_transaction() as session:
        session.execute(delete(Run).where(Run.run_id == "latest"))

    response = api.client.get(f"/v1/channels/{channel_id}/jobs", headers=headers)
    assert response.status_code == 200, response.text
    attempts = response.json()
    assert len(attempts) == 1
    assert attempts[0]["run_id"] == "run_latest"
    assert attempts[0]["unavailable"] is True
    assert attempts[0]["turn_id"] is not None
