"""A job's conversation exists without a browser or an invented human turn."""

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select, update

from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.usagepool import fairness_key
from sbxloop.db.collaboration_models import ChannelRow, MessageRow, TurnRow
from sbxloop.db.daemon_models import WorkItemRow
from sbxloop.db.engine_models import Run
from sbxloop.db.job_models import ExternalJobRow, ExternalPendingRow, ExternalRunRow
from sbxloop_worker.protocol import Event
from tests.api.test_collaboration import bearer, register


def external_item(api: Any, *, number: int = 12, repo: str = "o/r") -> WorkItem:
    item = WorkItem(
        item_id=f"gh:{repo}:issue:{number}",
        source_key=str(number),
        repo=repo,
        title="Repair the checkout",
        body="The checkout fails during verification.",
        url=f"https://github.com/{repo}/issues/{number}",
    )
    api.harness.dstore.upsert_new(item, api.clock())
    return item


def channels(api: Any) -> list[Any]:
    with api.harness.dstore.read() as session:
        return list(session.scalars(select(ChannelRow)).all())


def test_external_work_contract_is_advertised_to_angie(api: Any) -> None:
    headers = bearer(register(api))
    response = api.client.get("/v1/capabilities", headers=headers)
    assert response.status_code == 200, response.text
    assert "collaboration.external_work" in response.json()["features"]


def test_external_queued_job_creates_one_truthful_workspace_conversation(api: Any) -> None:
    api.ctx.project_work()  # Establish the installation's import boundary.
    api.clock.t += 1
    item = external_item(api)
    before = fairness_key(item)
    api.ctx.project_work()
    api.ctx.project_work()
    created = channels(api)
    assert len(created) == 1
    channel = created[0]
    assert channel.visibility == "workspace"
    assert channel.created_by is None
    with api.harness.dstore.read() as session:
        messages = session.scalars(select(MessageRow)).all()
        assert len(messages) == 1
        assert messages[0].author_kind == "system"
        assert messages[0].turn_id is None
        assert item.url in messages[0].content
        assert session.scalars(select(TurnRow)).all() == []
    stored = api.harness.dstore.get(item.item_id)
    assert stored.channel_id is None
    assert fairness_key(stored) == before
    assert api.harness.runs == []


def test_external_job_is_available_through_additive_jobs_contract(api: Any) -> None:
    headers = bearer(register(api))
    external_item(api)
    api.ctx.project_work()
    created = channels(api)
    assert len(created) == 1
    response = api.client.get(f"/v1/channels/{created[0].id}/jobs", headers=headers)
    assert response.status_code == 200, response.text
    job = response.json()[0]
    assert job["work_id"].startswith("job_")
    assert job["item_id"].startswith("itm_")
    assert job["run_id"] is None
    assert job["turn_id"] is None
    assert job["state"] == "queued"
    assert job["source"]["repository"] == "o/r"


def test_run_without_item_gets_its_real_identity(api: Any) -> None:
    headers = bearer(register(api))
    api.loop.store.create_run("standalone", "Compile a report", kind="tool")
    api.ctx.project_work()
    created = channels(api)
    assert len(created) == 1
    response = api.client.get(f"/v1/channels/{created[0].id}/jobs", headers=headers)
    assert response.status_code == 200, response.text
    job = response.json()[0]
    assert job["item_id"] is None
    assert job["run_id"] == "run_standalone"
    assert job["turn_id"] is None
    with api.harness.dstore.read() as session:
        assert session.scalars(select(WorkItemRow)).all() == []


def _run(api: Any, run_id: str, state: str, at: float, item: WorkItem | None = None) -> None:
    api.loop.store.create_run(run_id, "Compile a report", kind="workload")
    if item is not None:
        api.harness.dstore.mark_running(item.item_id, run_id, at)
    with api.harness.dstore.immediate_transaction() as session:
        session.execute(
            update(Run)
            .where(Run.run_id == run_id)
            .values(state=state, created_at=at, updated_at=at)
        )


def _entries(api: Any, channel_id: str) -> list[Any]:
    with api.harness.dstore.read() as session:
        return list(
            session.scalars(
                select(MessageRow)
                .where(MessageRow.channel_id == channel_id)
                .order_by(MessageRow.sequence)
            )
        )


def test_initial_history_is_bounded_quiet_and_keeps_action_waits(api: Any) -> None:
    api.clock.t = 10_000_000
    now = api.clock()
    _run(api, "recent", "completed", now - 86400)
    _run(api, "old", "failed", now - 40 * 86400)
    _run(api, "waiting", "held", now - 40 * 86400)
    api.ctx.project_work()
    api.ctx.project_work()
    with api.harness.dstore.read() as session:
        bound = set(session.scalars(select(ExternalRunRow.run_id)))
        assert bound == {"recent", "waiting"}
        jobs = list(session.scalars(select(ExternalJobRow)))
        assert all(job.historical and job.read_baseline == 2 for job in jobs)
    from sbxloop.db.api_models import ApiEventRow

    with api.harness.dstore.read() as session:
        assert (
            session.scalars(
                select(ApiEventRow).where(
                    ApiEventRow.type == "collaboration.external_work.attention"
                )
            ).all()
            == []
        )
    assert all(
        json.loads(entry.origin_json)["historical"]
        for job in jobs
        for entry in _entries(api, job.channel_id)
    )


def test_history_window_uses_finish_not_creation_and_preserves_timestamp(api: Any) -> None:
    api.clock.t = 10_000_000
    _run(api, "long", "completed", api.clock() - 40 * 86400)
    finished = api.clock() - 1
    with api.harness.dstore.immediate_transaction() as session:
        session.execute(update(Run).where(Run.run_id == "long").values(updated_at=finished))
    api.ctx.project_work()
    channel = channels(api)[0]
    assert channel.updated_at == finished
    assert _entries(api, channel.id)[-1].created_at == finished


def test_same_issue_retry_keeps_attempts_and_tombstone(api: Any) -> None:
    from sbxloop.api.external_work import jobs

    item = external_item(api)
    _run(api, "first", "failed", api.clock(), item)
    api.ctx.project_work()
    channel = channels(api)[0]
    api.clock.t += 1
    api.harness.dstore.mark_failed(item.item_id, "retry", api.clock(), requeue=False)
    api.harness.dstore.upsert_new(item.model_copy(update={"body": "Changed details"}), api.clock())
    _run(api, "second", "completed", api.clock(), item)
    api.ctx.project_work()
    assert [row.id for row in channels(api)] == [channel.id]
    snapshots = jobs(api.ctx, channel.id)
    assert {row["run_id"] for row in snapshots} == {"run_first", "run_second"}
    assert {row["state"] for row in snapshots} == {"failed", "completed"}
    with api.harness.dstore.immediate_transaction() as session:
        session.execute(
            update(ChannelRow).where(ChannelRow.id == channel.id).values(state="deleted")
        )
    api.clock.t += 1
    api.harness.dstore.mark_failed(item.item_id, "retry", api.clock(), requeue=False)
    api.harness.dstore.upsert_new(item, api.clock())
    api.ctx.project_work()
    assert len(channels(api)) == 1
    assert channels(api)[0].state == "deleted"


def test_repository_and_schedule_occurrences_do_not_collide(api: Any) -> None:
    external_item(api, repo="one/r")
    external_item(api, repo="two/r")
    for occurrence in ("2026-09-01T00:00Z", "2026-09-02T00:00Z"):
        api.harness.dstore.upsert_new(
            WorkItem(
                item_id=f"sched:audit:{occurrence}",
                source_key=occurrence,
                title="Audit",
                kind="tool",
            ),
            api.clock(),
        )
    api.ctx.project_work()
    assert len(channels(api)) == 4


def test_concurrent_reconciliation_is_idempotent(api: Any) -> None:
    external_item(api)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: api.ctx.project_work(), range(6)))
    assert len(channels(api)) == 1
    assert len(_entries(api, channels(api)[0].id)) == 1


def test_repeated_terminal_polls_and_resume_have_distinct_real_transitions(api: Any) -> None:
    api.ctx.project_work()
    api.clock.t += 1
    _run(api, "resumable", "failed", api.clock())
    api.ctx.project_work()
    channel = channels(api)[0]
    api.ctx.project_work()
    assert len([entry for entry in _entries(api, channel.id) if entry.kind == "work_result"]) == 1
    with api.harness.dstore.immediate_transaction() as session:
        session.execute(update(Run).where(Run.run_id == "resumable").values(state="building"))
    api.ctx.project_work()
    with api.harness.dstore.immediate_transaction() as session:
        session.execute(
            update(Run)
            .where(Run.run_id == "resumable")
            .values(state="failed", reason="a different failure")
        )
    api.ctx.project_work()
    results = [entry for entry in _entries(api, channel.id) if entry.kind == "work_result"]
    assert len(results) == 2
    assert results[-1].content == "a different failure"


def test_replay_progress_is_durable_and_live_messages_are_not_historical(api: Any) -> None:
    _run(api, "active", "building", api.clock() - 1)
    api.loop.store.append_event(
        Event(
            type="run.tasks", run_id="active", ts=api.clock() - 1, data={"tasks": [{"id": "one"}]}
        )
    )
    api.ctx.project_work()
    channel = channels(api)[0]
    api.clock.t += 1
    api.loop.store.append_event(
        Event(
            type="task.end",
            run_id="active",
            ts=api.clock(),
            data={"task_id": "one", "state": "done"},
        )
    )
    api.ctx.project_work()
    api.ctx.project_work()
    entries = _entries(api, channel.id)
    assert len(entries) == 3
    assert json.loads(entries[1].origin_json)["historical"] is True
    assert json.loads(entries[2].origin_json)["historical"] is False
    assert json.loads(entries[2].origin_json)["source_run_id"] == "run_active"


def test_old_public_attempt_never_leaks_later_private_admission(api: Any) -> None:
    from sbxloop.api.external_work import jobs

    headers = bearer(register(api))
    item = external_item(api)
    _run(api, "public", "failed", api.clock(), item)
    api.ctx.project_work()
    public_channel = channels(api)[0]
    private = api.client.post("/v1/channels", json={"title": "Private"}, headers=headers).json()[
        "id"
    ]
    api.clock.t += 1
    with api.harness.dstore.immediate_transaction() as session:
        session.execute(
            update(WorkItemRow)
            .where(WorkItemRow.item_id == item.item_id)
            .values(
                channel_id=private,
                title="Private strategy",
                body="secret details",
                state="queued",
                run_id=None,
            )
        )
    api.ctx.project_work()
    _run(api, "private", "building", api.clock(), item)
    api.ctx.project_work()
    old = jobs(api.ctx, public_channel.id)
    assert len(old) == 1
    assert old[0]["run_id"] == "run_public"
    assert old[0]["title"] == "Repair the checkout"
    assert old[0]["item_id"] is None
    assert old[0]["item_actions"] == []
    assert "Private strategy" not in json.dumps(old)
    with api.harness.dstore.read() as session:
        assert session.get(ExternalRunRow, "public").channel_id == public_channel.id
        assert session.get(ExternalRunRow, "private").channel_id == private


def test_completed_backfill_uses_dirty_queue_without_rescanning_history(api: Any) -> None:
    from sbxloop.api.external_work import reconcile

    _run(api, "queued", "building", api.clock())
    for _ in range(6):
        reconcile(api.ctx, limit=1)
    with api.harness.dstore.immediate_transaction() as session:
        # Changes written by old packages after rollback still hit the DB trigger.
        session.execute(update(Run).where(Run.run_id == "queued").values(state="completed"))
    assert reconcile(api.ctx, limit=1) == 1
    assert reconcile(api.ctx, limit=1) == 0
    with api.harness.dstore.read() as session:
        assert session.scalars(select(ExternalPendingRow)).all() == []


def test_no_turn_existing_private_channel_gets_truthful_delivery(api: Any) -> None:
    headers = bearer(register(api))
    channel = api.client.post(
        "/v1/channels", json={"title": "Private work"}, headers=headers
    ).json()["id"]
    item = external_item(api).model_copy(update={"channel_id": channel})
    with api.harness.dstore.immediate_transaction() as session:
        session.execute(
            update(WorkItemRow)
            .where(WorkItemRow.item_id == item.item_id)
            .values(channel_id=channel)
        )
    _run(api, "private-no-turn", "completed", api.clock(), item)
    api.ctx.project_work()
    response = api.client.get(f"/v1/channels/{channel}/messages", headers=headers)
    assert response.status_code == 200, response.text
    messages = response.json()
    assert len(channels(api)) == 1
    assert any(message["kind"] == "work_result" for message in messages)
    assert all(
        message["turn_id"] is None and message["author"]["kind"] == "system" for message in messages
    )


def test_no_run_failure_is_visible_and_alerts_only_once(api: Any) -> None:
    from sbxloop.db.api_models import ApiEventRow

    api.ctx.project_work()
    api.clock.t += 1
    item = external_item(api)
    api.ctx.project_work()
    api.clock.t += 1
    api.harness.dstore.mark_failed(item.item_id, "checkout unavailable", api.clock(), requeue=False)
    api.ctx.project_work()
    api.ctx.project_work()
    results = [entry for entry in _entries(api, channels(api)[0].id) if entry.kind == "work_result"]
    assert [entry.content for entry in results] == ["checkout unavailable"]
    with api.harness.dstore.read() as session:
        alerts = list(
            session.scalars(
                select(ApiEventRow).where(
                    ApiEventRow.type == "collaboration.external_work.attention"
                )
            )
        )
    assert len(alerts) == 1
    alert = json.loads(alerts[0].data_json)
    assert alert["kind"] == "failure"
    assert alert["run_id"] is None
    assert alert["historical"] is False


def test_pending_code_stays_in_additive_jobs_before_discovery(api: Any) -> None:
    from tests.api.test_code_work_delivery import CodeConcierge
    from tests.api.test_collaboration_recovery import settled

    api.ctx.concierge = CodeConcierge()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "Add a feature", "intent": "code"},
    ).json()
    settled(api.client, headers, channel, accepted["turn"]["id"])
    response = api.client.get(f"/v1/channels/{channel}/jobs", headers=headers)
    assert response.status_code == 200, response.text
    pending = response.json()
    assert len(pending) == 1
    assert pending[0]["state"] == "awaiting_dispatch"
    assert pending[0]["item_id"] is None
    assert pending[0]["turn_id"] == accepted["turn"]["id"]
    assert (
        pending[0]["work_id"]
        == api.client.get(f"/v1/channels/{channel}/jobs", headers=headers).json()[0]["work_id"]
    )


def test_existing_no_turn_live_chronicle_and_replay_share_a_ledger(api: Any) -> None:
    from sbxloop.agents.chronicle import RunChronicle

    headers = bearer(register(api))
    for number, live_first in ((21, True), (22, False)):
        channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        item = external_item(api, number=number).model_copy(update={"channel_id": channel})
        with api.harness.dstore.immediate_transaction() as session:
            session.execute(
                update(WorkItemRow)
                .where(WorkItemRow.item_id == item.item_id)
                .values(channel_id=channel)
            )
        run_id = f"live-{number}"
        _run(api, run_id, "building", api.clock(), item)
        api.ctx.project_work()
        chronicle = RunChronicle(api.ctx.poster, None, item, api.ctx.config, api.clock)
        event = Event(
            type="run.tasks", run_id=run_id, ts=api.clock(), data={"tasks": [{"id": "one"}]}
        )
        api.loop.store.append_event(event)
        if live_first:
            chronicle.on_event(event)
        api.ctx.project_work()
        if not live_first:
            chronicle.on_event(event)
        posts = [entry for entry in _entries(api, channel) if entry.kind == "agent_update"]
        assert len(posts) == 1


def test_resume_during_artifact_read_never_posts_old_result_as_new_transition(
    api: Any, monkeypatch: Any
) -> None:
    from sbxloop.api import external_work

    _run(api, "racy", "completed", api.clock())
    changed = False

    def resumed(ctx: Any, record: Any) -> list[Any]:
        nonlocal changed
        if not changed:
            changed = True
            with api.harness.dstore.immediate_transaction() as session:
                row = session.get(Run, record.run_id)
                row.state = "building"
                session.flush()
                external_work._bind_run(ctx, session, row, historical=False)
        return []

    monkeypatch.setattr(external_work, "_artifacts", resumed)
    api.ctx.project_work()
    api.ctx.project_work()
    assert not any(entry.kind == "work_result" for entry in _entries(api, channels(api)[0].id))
