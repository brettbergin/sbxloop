"""Managed work returns to its original conversation without replaying tools."""

import json
from typing import Any, cast

from sbxloop.api.collaboration import CollaborationStore
from sbxloop.api.projector import Projector
from sbxloop.api.stream import StreamHub
from sbxloop.daemon.model import WorkItem
from sbxloop.ghids import chat_item_id
from tests.api.test_collaboration import FakeConcierge, bearer, register


def setup_work(
    api: Any,
    *,
    suffix: str = "",
    request: dict[str, Any] | None = None,
) -> tuple[dict[str, str], str, WorkItem]:
    api.ctx.concierge = FakeConcierge()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "Prepare a report"}
        | (request or {"target_slugs": ["concierge", "operator"]}),
    ).json()
    assert api.ctx.turns.wait_idle(timeout=5)
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
    # A key that merely starts with the message's id names a different
    # message. Underscores must not become SQL LIKE wildcards either: the
    # link is an equality on the message id the item was admitted with.
    headers, channel, item = setup_work(api, suffix="unrelated")
    with api.harness.dstore.read() as session:
        from sbxloop.db.daemon_models import WorkItemRow

        row = session.get(WorkItemRow, item.item_id)
        assert row is not None
        assert row.source_key == item.source_key
        assert row.message_id == item.source_key
    response = api.client.get(f"/v1/channels/{channel}/work", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == []


def _work_results(api: Any, headers: dict[str, str], channel: str) -> list[dict[str, Any]]:
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    return [message for message in messages if message["kind"] == "work_result"]


def test_workload_runner_result_is_credited_to_angie(api: Any) -> None:
    # A runner turn names no participant; its result still has an author.
    headers, channel, item = setup_work(api, request={"intent": "workload"})
    queued = api.client.get(f"/v1/channels/{channel}/work", headers=headers).json()
    assert queued[0]["agent_slug"] == "concierge"
    api.harness.source.items = [item]
    api.harness.outcomes = ["completed"]
    api.clock.t += 10
    api.loop.tick()
    results = _work_results(api, headers, channel)
    assert len(results) == 1
    assert results[0]["agent_slug"] == "concierge"
    assert results[0]["work"]["agent_slug"] == "concierge"


def test_mentioned_agent_keeps_credit_for_its_result(api: Any) -> None:
    headers, channel, item = setup_work(api, request={"target_slugs": ["operator"]})
    api.harness.dstore.mark_failed(item.item_id, "sandbox unavailable", api.clock(), requeue=False)
    results = _work_results(api, headers, channel)
    assert len(results) == 1
    assert results[0]["agent_slug"] == "operator"
    assert results[0]["work"]["agent_slug"] == "operator"


def test_stored_unattributed_result_reads_back_as_angie(api: Any) -> None:
    # Rows written before attribution existed stay as stored; reads credit Angie.
    headers, channel, _ = setup_work(api, request={"intent": "workload"})
    snapshot = api.client.get(f"/v1/channels/{channel}/work", headers=headers).json()[0]
    stored = api.ctx.collaboration.append_work_result(
        "msg_work_legacy",
        channel_id=channel,
        turn_id=snapshot["turn_id"],
        content="Work completed.",
        agent_slug=None,
        work=snapshot | {"agent_slug": None, "state": "completed"},
        now=api.clock(),
    )
    assert stored is not None
    results = _work_results(api, headers, channel)
    legacy = next(message for message in results if message["id"] == "msg_work_legacy")
    assert legacy["agent_slug"] == "concierge"
    assert legacy["work"]["agent_slug"] == "concierge"
    assert legacy["work"]["state"] == "completed"
    with api.harness.dstore.read() as session:
        from sbxloop.db.collaboration_models import MessageRow

        row = session.get(MessageRow, "msg_work_legacy")
        assert row is not None
        assert row.agent_slug is None


def _complete_with_files(api: Any, item: WorkItem, files: dict[str, str]) -> str:
    """Finish the workload run with ``files`` delivered by its artifact sink,
    before anything has read the channel or catalogued the run."""
    api.harness.source.items = [item]
    api.harness.outcomes = ["completed"]
    api.clock.t += 10
    api.loop.tick()
    run_id = str(api.harness.runs[-1][0])
    home = api.ctx.config.paths
    api.harness.store.set_run_workspace(run_id, home.run_data(run_id), mounted=False)
    root = home.run_artifacts(run_id)
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (root / rel).write_text(text)
    (task,) = api.harness.store.get_tasks(run_id)
    assert task.output is not None
    task.output.files = list(files)
    api.harness.store.update_task(run_id, task)
    return run_id


def test_first_work_result_names_the_files_the_run_delivered(api: Any) -> None:
    headers, channel, item = setup_work(api)
    run_id = _complete_with_files(api, item, {"bread_items.md": "# Bread\n- flour\n"})
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    (result,) = [m for m in messages if m["kind"] == "work_result"]
    (artifact,) = result["work"]["artifacts"]
    assert artifact["id"].startswith("art_")
    assert artifact["run_id"] == f"run_{run_id}"
    assert artifact["relpath"] == "bread_items.md"
    assert artifact["media_type"] == "text/markdown"
    assert artifact["size"] == len("# Bread\n- flour\n")
    assert "the answer is 42" in result["content"]
    assert result["content"].endswith("\n\nFiles:\n- bread_items.md")
    # The same ids the run's own catalog serves.
    page = api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=headers).json()
    assert [a["id"] for a in page["data"]] == [artifact["id"]]
    work = api.client.get(f"/v1/channels/{channel}/work", headers=headers).json()
    assert [a["relpath"] for a in work[0]["artifacts"]] == ["bread_items.md"]


def test_work_result_lists_only_available_files_in_path_order(api: Any) -> None:
    headers, channel, item = setup_work(api)
    run_id = _complete_with_files(api, item, {"b.txt": "b", "a.txt": "a", "gone.txt": "x"})
    api.ctx.artifacts.catalog_run(api.harness.store.get_run(run_id))
    gone = next(a for a in api.ctx.artifacts.for_run(run_id) if a.relpath == "gone.txt")
    api.ctx.artifacts.tombstone(gone.id)
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    (result,) = [m for m in messages if m["kind"] == "work_result"]
    assert [a["relpath"] for a in result["work"]["artifacts"]] == ["a.txt", "b.txt"]
    assert result["content"].endswith("\n\nFiles:\n- a.txt\n- b.txt")


def test_work_without_files_has_no_artifacts(api: Any) -> None:
    headers, channel, item = setup_work(api)
    api.harness.source.items = [item]
    api.harness.outcomes = ["completed"]
    api.clock.t += 10
    api.loop.tick()
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    (result,) = [m for m in messages if m["kind"] == "work_result"]
    assert result["work"]["artifacts"] == []
    assert "Files:" not in result["content"]


def test_a_stored_result_from_before_artifacts_still_serializes(api: Any) -> None:
    headers, channel, item = setup_work(api)
    api.harness.dstore.mark_failed(item.item_id, "sandbox unavailable", api.clock(), requeue=False)
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    result = next(m for m in messages if m["kind"] == "work_result")
    from sbxloop.db.collaboration_models import MessageRow

    with api.harness.dstore.immediate_transaction() as session:
        row = session.get(MessageRow, result["id"])
        assert row is not None
        stored = json.loads(row.work_json)
        stored.pop("artifacts", None)
        row.work_json = json.dumps(stored)
    again = api.client.get(f"/v1/channels/{channel}/messages", headers=headers)
    assert again.status_code == 200, again.text
    old = next(m for m in again.json() if m["id"] == result["id"])
    assert old["work"]["artifacts"] == []


def test_projector_catalogues_finished_runs_before_delivering_work() -> None:
    order: list[str] = []

    class Chronology:
        def project(self, now: float) -> int:
            return 0

        def watermark(self) -> int:
            return 0

        def prune(self, before: float) -> None: ...

    projector = Projector(cast(Any, Chronology()), StreamHub(), clock=lambda: 0.0, retention_s=60.0)
    projector.catalog_with(lambda run_id: order.append(f"catalog:{run_id}"))
    projector.deliver_work_with(lambda: order.append("deliver"))
    projector.catalog("r1")
    projector.step()
    assert order == ["catalog:r1", "deliver"]


def test_a_run_whose_catalog_fails_does_not_stop_delivery(api: Any, monkeypatch: Any) -> None:
    """One run's unreadable files cost that result its file list, not every
    channel's delivery nor the channel's own history."""
    from sbxloop.db.collaboration_models import MessageRow

    headers, channel, item = setup_work(api)
    other = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    accepted = api.client.post(
        f"/v1/channels/{other}/turns",
        headers=headers,
        json={"content": "Prepare a report", "target_slugs": ["concierge", "operator"]},
    ).json()
    assert api.ctx.turns.wait_idle(timeout=5)
    key = accepted["turn"]["input_message_id"]
    second = WorkItem(
        item_id=chat_item_id(key),
        source_key=key,
        title="Report",
        body="Prepare a report",
        kind="workload",
    )
    api.harness.dstore.upsert_new(second, api.clock())
    catalog_run = api.ctx.artifacts.catalog_run

    def failing(record: Any) -> int:
        if record.run_id == api.harness.runs[0][0]:
            raise OSError("the run's files could not be read")
        return int(catalog_run(record))

    monkeypatch.setattr(api.ctx.artifacts, "catalog_run", failing)
    for work in (item, second):
        api.harness.source.items = [work]
        api.harness.outcomes = ["completed"]
        api.clock.t += 10
        api.loop.tick()
    assert len(api.harness.runs) == 2
    # The projector's pass over every channel carries on past the failure.
    api.ctx.project_work()
    with api.harness.dstore.read() as session:
        delivered = {
            str(row.channel_id)
            for row in session.query(MessageRow).filter(MessageRow.kind == "work_result")
        }
    assert delivered == {channel, other}
    response = api.client.get(f"/v1/channels/{channel}/messages", headers=headers)
    assert response.status_code == 200, response.text
    (result,) = [m for m in response.json() if m["kind"] == "work_result"]
    assert result["work"]["state"] == "completed"
    assert result["work"]["artifacts"] == []
    assert "the answer is 42" in result["content"]
