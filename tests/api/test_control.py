"""Steering the run in flight, deciding a gate at the revision the person
saw, and the run-level controls — every one a recorded operation through
the same service ctl and chat use."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.daemon.model import WorkItem
from sbxloop.engine.model import RunResult
from sbxloop.errors import RunCancelledError
from sbxloop.events import EventBus
from sbxloop.vcs.protocol import Capability
from sbxloop_worker.protocol import Event
from tests.api.conftest import Api, build
from tests.fakes.fake_github import FakeGithub
from tests.unit.test_daemon_loop import PR_URL, gh_item
from tests.unit.test_daemon_merge_gate import FakeDaemonGithub, GatedSource
from tests.unit.test_daemon_publish_hold import HeldSource


def in_flight(api: Api, item: WorkItem) -> tuple[threading.Thread, str, threading.Event]:
    """Dispatch ``item`` on a runner that blocks until released; returns
    the tick thread, the run id and the release event."""
    started, release = threading.Event(), threading.Event()
    run_ids: list[str] = []

    def runner(item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool) -> RunResult:
        run_ids.append(run_id)
        api.harness.store.create_run(run_id, "outcome", kind=item.kind)
        api.harness.store.set_run_state(run_id, "building")
        started.set()
        release.wait(10)
        raise RunCancelledError("cancelled")

    api.loop._runner = runner
    api.harness.source.items = [item]
    thread = threading.Thread(target=api.loop.tick)
    thread.start()
    assert started.wait(5)
    return thread, run_ids[0], release


def run_public(run_id: str) -> str:
    return f"run_{run_id}"


class TestSteering:
    def test_an_instruction_is_delivered_then_handled_by_the_reply(self, api: Api) -> None:
        thread, run_id, release = in_flight(api, gh_item("1"))
        try:
            headers = api.bearer()
            response = api.client.post(
                f"/v1/runs/{run_public(run_id)}/steering",
                json={"text": "Skip the migration", "source_refs": ["msg:41"]},
                headers=headers,
            )
            assert response.status_code == 202, response.text
            body = response.json()
            steering = body["steering"]
            assert steering["id"].startswith("str_") and steering["status"] == "delivered"
            assert steering["text"] == "Skip the migration"
            assert steering["source_refs"] == ["msg:41"]
            assert steering["actor"]["kind"] == "client" and steering["delivered_at"]
            assert steering["deadline_at"] and steering["reply"] is None
            assert body["operation"]["action"] == "run.steer"
            assert body["operation"]["state"] == "succeeded"
            # The engine holds the message the reply will name.
            handle = api.loop._current
            assert handle is not None
            queued = handle.engine._chat_queue.get_nowait()
            assert queued.text == "Skip the migration"
            # The agent answers: the run's chat.reply settles the record.
            api.harness.store.append_event(
                Event(
                    ts=api.clock(),
                    run_id=run_id,
                    type="chat.reply",
                    data={
                        "message_id": queued.message_id,
                        "reply": "Skipping it",
                        "action": "steer_run",
                    },
                )
            )
            listed = api.client.get(
                f"/v1/runs/{run_public(run_id)}/steering", headers=headers
            ).json()["data"]
            assert [s["status"] for s in listed] == ["handled"]
            assert listed[0]["reply"] == "Skipping it" and listed[0]["action"] == "steer_run"
            assert listed[0]["handled_at"]
            # A second instruction the run never answers is undelivered once
            # the run ends.
            second = api.client.post(
                f"/v1/runs/{run_public(run_id)}/steering",
                json={"text": "Also add tests"},
                headers=headers,
            )
            assert second.status_code == 202
        finally:
            release.set()
            thread.join(10)
        listed = api.client.get(f"/v1/runs/{run_public(run_id)}/steering", headers=headers).json()
        assert [s["status"] for s in listed["data"]] == ["handled", "undelivered"]
        # The chronology carries the receipt and the reply in order.
        events = api.client.get(
            "/v1/events", params={"type_prefix": "chat."}, headers=headers
        ).json()["data"]
        assert [e["type"] for e in events] == ["chat.reply"]

    def test_refused_for_a_run_not_in_flight_or_a_tool_run(self, api: Api) -> None:
        api.harness.source.items = [gh_item("1")]
        api.harness.outcomes = ["merged"]
        api.loop.tick()
        finished = api.harness.runs[-1][0]
        headers = api.bearer()
        gone = api.client.post(
            f"/v1/runs/{run_public(finished)}/steering", json={"text": "x"}, headers=headers
        )
        assert gone.status_code == 409 and gone.json()["code"] == "not_eligible"
        assert "in flight" in gone.json()["detail"]
        # The refusal is on the record too.
        listed = api.client.get(f"/v1/runs/{run_public(finished)}/steering", headers=headers)
        assert [s["status"] for s in listed.json()["data"]] == ["failed"]
        assert listed.json()["data"][0]["error"]
        tool = gh_item("2", kind="tool", recipe="entrygraph", recipe_target="o/r")
        thread, run_id, release = in_flight(api, tool)
        try:
            refused = api.client.post(
                f"/v1/runs/{run_public(run_id)}/steering", json={"text": "x"}, headers=headers
            )
            assert refused.status_code == 409
            assert refused.json()["code"] == "unsupported_for_kind"
        finally:
            release.set()
            thread.join(10)
        assert (
            api.client.post(
                "/v1/runs/run_nope/steering", json={"text": "x"}, headers=headers
            ).status_code
            == 404
        )
        blank = api.client.post(
            f"/v1/runs/{run_public(finished)}/steering", json={"text": ""}, headers=headers
        )
        assert blank.status_code == 422

    def test_a_stale_revision_is_refused_and_a_key_replays(self, api: Api) -> None:
        thread, run_id, release = in_flight(api, gh_item("1"))
        try:
            headers = api.bearer()
            run = api.client.get(f"/v1/runs/{run_public(run_id)}", headers=headers).json()
            stale = api.client.post(
                f"/v1/runs/{run_public(run_id)}/steering",
                json={"text": "x", "expected_revision": run["revision"] + 7},
                headers=headers,
            )
            assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
            keyed = {**headers, "Idempotency-Key": "s1"}
            first = api.client.post(
                f"/v1/runs/{run_public(run_id)}/steering",
                json={"text": "x", "expected_revision": run["revision"]},
                headers=keyed,
            )
            replay = api.client.post(
                f"/v1/runs/{run_public(run_id)}/steering",
                json={"text": "x", "expected_revision": run["revision"]},
                headers=keyed,
            )
            assert first.status_code == 202 and replay.status_code == 202
            assert replay.json()["steering"]["id"] == first.json()["steering"]["id"]
            assert replay.json()["operation"]["id"] == first.json()["operation"]["id"]
            handle = api.loop._current
            assert handle is not None and handle.engine._chat_queue.qsize() == 1
        finally:
            release.set()
            thread.join(10)

    def test_steering_needs_its_capability(self, api: Api) -> None:
        headers = api.bearer(frozenset({"runs:read", "runs:control"}))
        response = api.client.post("/v1/runs/run_x/steering", json={"text": "x"}, headers=headers)
        assert response.status_code == 403 and response.json()["capability"] == "runs:steer"


class TestRunControls:
    def test_cancel_is_honoured_at_the_boundary_and_finishes_its_record(self, api: Api) -> None:
        thread, run_id, release = in_flight(api, gh_item("1"))
        headers = api.bearer()
        try:
            response = api.client.post(
                f"/v1/runs/{run_public(run_id)}/cancel", json={"reason": "scope"}, headers=headers
            )
            assert response.status_code == 202, response.text
            body = response.json()
            assert body["operation"]["action"] == "run.cancel"
            assert body["operation"]["state"] == "running"
            assert response.headers["Location"] == f"/v1/operations/{body['operation']['id']}"
            op_id = body["operation"]["id"]
        finally:
            release.set()
            thread.join(10)
        op = api.client.get(f"/v1/operations/{op_id}", headers=headers).json()
        assert op["state"] == "succeeded"
        run = api.client.get(f"/v1/runs/{run_public(run_id)}", headers=headers).json()
        assert run["state"] == "cancelled"
        # A cancelled run can be resumed through the daemon's own queue.
        resumed = api.client.post(f"/v1/runs/{run_public(run_id)}/resume", headers=headers)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["operation"]["action"] == "run.resume"
        item = api.loop.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued" and item.run_id == run_id
        # Cancelling a finished run names its state; a stale revision is refused.
        api.harness.source.items = [gh_item("2")]
        api.harness.outcomes = ["merged"]
        api.loop._runner = api.harness.runner
        api.loop.tick()
        done = api.harness.runs[-1][0]
        terminal = api.client.post(f"/v1/runs/{run_public(done)}/cancel", headers=headers)
        assert terminal.status_code == 409 and terminal.json()["code"] == "already_terminal"
        stale = api.client.post(
            f"/v1/runs/{run_public(run_id)}/resume",
            json={"expected_revision": 999},
            headers=headers,
        )
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        assert api.client.post("/v1/runs/run_nope/cancel", headers=headers).status_code == 404

    def test_round_grants_are_bounded_and_re_admit_the_run(self, api: Api) -> None:
        api.harness.source.items = [gh_item("1")]
        api.harness.outcomes = ["exhausted"]
        api.clock.t += 10
        api.loop.tick()
        run_id = api.harness.runs[-1][0]
        headers = api.bearer()
        run = api.client.get(f"/v1/runs/{run_public(run_id)}", headers=headers).json()
        granted = api.client.post(
            f"/v1/runs/{run_public(run_id)}/round-grants",
            json={"rounds": 2, "expected_revision": run["revision"]},
            headers=headers,
        )
        assert granted.status_code == 200, granted.text
        assert granted.json()["operation"]["action"] == "run.grant_rounds"
        assert granted.json()["run"]["rounds"]["granted"] == run["rounds"]["granted"] + 2
        item = api.loop.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued" and item.run_id == run_id
        zero = api.client.post(
            f"/v1/runs/{run_public(run_id)}/round-grants", json={"rounds": 0}, headers=headers
        )
        assert zero.status_code == 422
        reader = api.bearer(frozenset({"runs:read", "runs:control"}))
        assert (
            api.client.post(
                f"/v1/runs/{run_public(run_id)}/round-grants", json={"rounds": 1}, headers=reader
            ).status_code
            == 403
        )

    def test_the_review_wait_can_be_re_armed(self, api: Api) -> None:
        fake = FakeGithub(number=9)
        fake.pr["html_url"] = PR_URL
        api.loop.github = FakeDaemonGithub(fake)  # type: ignore[assignment]
        api.harness.source.items = [gh_item("1")]
        api.harness.outcomes = ["awaiting_review"]
        api.loop.tick()
        run_id = api.harness.runs[-1][0]
        headers = api.bearer()
        run = api.client.get(f"/v1/runs/{run_public(run_id)}", headers=headers).json()
        assert run["review_wait"] == "open" and "review_wait_resume" in run["available_actions"]
        response = api.client.post(
            f"/v1/runs/{run_public(run_id)}/review-wait/resume", headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["operation"]["action"] == "run.review_resume"
        assert "waiting for a review" in response.json()["message"]


def gated(api: Api, *, kind: str = "merge") -> str:
    """Park one run behind a gate of ``kind``; returns the run id."""
    if kind == "publish":
        api.harness.source = HeldSource()
        api.loop.source = api.harness.source
        api.harness.source.items = [gh_item("1", kind="workload")]
        api.harness.outcomes = ["held"]
    else:
        api.harness.source = GatedSource()
        api.loop.source = api.harness.source
        api.harness.source.items = [gh_item("1")]
        api.harness.outcomes = ["gated"]
    fake = FakeGithub(number=9)
    fake.pr["html_url"] = PR_URL
    api.loop.github = FakeDaemonGithub(fake)  # type: ignore[assignment]
    api.clock.t += 10
    result = api.loop.tick()
    assert result.outcome in ("gated", "held"), result
    return api.harness.runs[-1][0]


def landed(api: Api, run_id: str) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        threads = [t for t in threading.enumerate() if t.name == f"sbxloop-merge-{run_id}"]
        if not threads:
            return
        for thread in threads:
            thread.join(timeout=0.05)
    raise AssertionError("the landing thread did not finish")


class TestGates:
    def test_gates_are_listed_with_their_revision_and_subject(self, api: Api) -> None:
        run_id = gated(api)
        headers = api.bearer()
        gates = api.client.get("/v1/gates", headers=headers).json()["data"]
        (gate,) = gates
        assert gate["id"].startswith("gate_") and gate["kind"] == "merge"
        assert gate["state"] == "open" and gate["run_id"] == run_public(run_id)
        assert gate["item_id"].startswith("itm_") and gate["repository"] == "o/r"
        assert gate["pull_request"]["number"] == 9 and gate["head_sha"] == "abc"
        assert gate["required_capability"] == "gates:approve"
        assert gate["available_actions"] == ["approve"] and gate["revision"] >= 0
        assert api.client.get(f"/v1/gates/{gate['id']}", headers=headers).json() == gate
        assert api.client.get("/v1/gates/gate_nope", headers=headers).status_code == 404
        only_open = api.client.get("/v1/gates", params={"state": "open"}, headers=headers).json()
        assert len(only_open["data"]) == 1
        assert (
            api.client.get("/v1/gates", params={"state": "odd"}, headers=headers).status_code == 422
        )
        run = api.client.get(f"/v1/runs/{run_public(run_id)}", headers=headers).json()
        assert run["gate"] == {"kind": "merge", "state": "open", "revision": gate["revision"]}

    def test_approval_binds_to_the_revision_and_completes_the_landing(self, api: Api) -> None:
        run_id = gated(api)
        headers = api.bearer()
        (gate,) = api.client.get("/v1/gates", headers=headers).json()["data"]
        missing = api.client.post(f"/v1/gates/{gate['id']}/approve", json={}, headers=headers)
        assert missing.status_code == 422
        stale = api.client.post(
            f"/v1/gates/{gate['id']}/approve",
            json={"expected_revision": gate["revision"] + 1},
            headers=headers,
        )
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        assert stale.json()["revision"] == gate["revision"]
        approved = api.client.post(
            f"/v1/gates/{gate['id']}/approve",
            json={"expected_revision": gate["revision"]},
            headers=headers,
        )
        assert approved.status_code == 202, approved.text
        body = approved.json()
        assert body["operation"]["action"] == "gate.approve"
        assert body["operation"]["state"] == "succeeded"
        assert body["gate"]["state"] in ("approving", "merged")
        assert "approved by" in body["message"]
        # A second approval at the old revision has lost: the gate moved.
        again = api.client.post(
            f"/v1/gates/{gate['id']}/approve",
            json={"expected_revision": gate["revision"]},
            headers=headers,
        )
        assert again.status_code == 409
        assert again.json()["code"] in ("stale_revision", "already_in_progress", "not_eligible")
        landed(api, run_id)
        final = api.client.get(f"/v1/gates/{gate['id']}", headers=headers).json()
        assert final["state"] == "merged" and final["available_actions"] == []
        assert final["resolved_by"]
        run = api.client.get(f"/v1/runs/{run_public(run_id)}", headers=headers).json()
        assert run["state"] == "merged"
        types = [
            e["type"]
            for e in api.client.get(
                "/v1/events", params={"type_prefix": "gate."}, headers=headers
            ).json()["data"]
        ]
        assert types == ["gate.opened", "gate.resolved"]

    def test_a_held_publication_is_released_the_same_way(self, api: Api) -> None:
        run_id = gated(api, kind="publish")
        headers = api.bearer()
        (gate,) = api.client.get("/v1/gates", headers=headers).json()["data"]
        assert gate["kind"] == "publish" and gate["pull_request"] is None
        released = api.client.post(
            f"/v1/gates/{gate['id']}/approve",
            json={"expected_revision": gate["revision"]},
            headers=headers,
        )
        assert released.status_code == 202, released.text
        assert released.json()["gate"]["state"] == "approving"
        item = api.loop.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued" and item.run_id == run_id

    def test_the_forge_must_be_able_to_act(self, api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
        from sbxloop.vcs.github.ops import GithubOps

        gated(api)
        headers = api.bearer()
        (gate,) = api.client.get("/v1/gates", headers=headers).json()["data"]
        monkeypatch.setitem(
            GithubOps.CAPABILITIES, "required_checks_introspection", Capability.UNKNOWN
        )
        unknown = api.client.post(
            f"/v1/gates/{gate['id']}/approve",
            json={"expected_revision": gate["revision"]},
            headers=headers,
        )
        assert unknown.status_code == 409 and unknown.json()["code"] == "capability_unknown"
        api.loop.github = None  # type: ignore[assignment]
        without = api.client.post(
            f"/v1/gates/{gate['id']}/approve",
            json={"expected_revision": gate["revision"]},
            headers=headers,
        )
        assert without.status_code == 409 and without.json()["code"] == "capability_unsupported"
        # Nothing moved: the gate is still open at the same revision.
        fresh = api.client.get(f"/v1/gates/{gate['id']}", headers=headers).json()
        assert fresh["state"] == "open" and fresh["revision"] == gate["revision"]

    def test_approval_needs_its_capability(self, api: Api) -> None:
        gated(api)
        reader = api.bearer(frozenset({"runs:read", "runs:control"}))
        (gate,) = api.client.get("/v1/gates", headers=reader).json()["data"]
        refused = api.client.post(
            f"/v1/gates/{gate['id']}/approve",
            json={"expected_revision": gate["revision"]},
            headers=reader,
        )
        assert refused.status_code == 403 and refused.json()["capability"] == "gates:approve"


class TestOverTheSocket:
    def test_a_gate_decision_and_a_steer_as_commands(self, api: Api) -> None:
        import json

        gated(api)
        headers = api.bearer()
        (gate,) = api.client.get("/v1/gates", headers=headers).json()["data"]
        with api.client.websocket_connect("/v1/ws", headers=headers) as ws:
            assert json.loads(ws.receive_text())["type"] == "hello"
            ws.send_text(
                json.dumps(
                    {"type": "command", "id": "g1", "action": "gate.approve", "target": gate["id"]}
                )
            )
            reply = json.loads(ws.receive_text())
            assert not reply["ok"] and reply["problem"]["code"] == "invalid_request"
            ws.send_text(
                json.dumps(
                    {
                        "type": "command",
                        "id": "g2",
                        "action": "gate.approve",
                        "target": gate["id"],
                        "expected_revision": gate["revision"],
                    }
                )
            )
            reply = json.loads(ws.receive_text())
            assert reply["ok"] and reply["result"]["operation"]["action"] == "gate.approve"
            ws.send_text(
                json.dumps(
                    {"type": "command", "id": "s1", "action": "run.steer", "target": "run_nope"}
                )
            )
            reply = json.loads(ws.receive_text())
            assert not reply["ok"] and reply["problem"]["code"] == "invalid_request"


def test_a_daemon_restart_settles_what_a_dead_run_never_answered(tmp_path: Path) -> None:
    """A steering row left `delivered` by a process that died is
    `undelivered` when the listener comes back: nothing will answer it."""
    from sbxloop.api.app import create_app
    from sbxloop.api.server import ApiServer
    from sbxloop.daemon.controls.principal import Principal
    from sbxloop.daemon.controls.steering import SteeringStore

    api = build(tmp_path)
    store = SteeringStore(api.loop.dstore)
    record = store.create(
        run_id="rdead",
        text="x",
        principal=Principal.trusted("ops", "ctl"),
        source_refs=[],
        expected_revision=None,
        now=1.0,
        deadline_at=None,
        operation_id=None,
    )
    store.delivered(record.id, "m1", 2.0)
    ephemeral = api.ctx.config.api.model_copy(update={"port": 0})
    server = ApiServer(create_app(api.ctx), ephemeral, ctx=api.ctx)
    server.start()
    try:
        fresh = store.get(record.id)
        assert fresh is not None and fresh.status == "undelivered"
    finally:
        server.close()
    api.ctx.close()
