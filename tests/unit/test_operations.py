"""Every mutating control leaves a durable record before it acts, and a
process that comes back settles what a dead one left from evidence."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.control import ControlClient, ControlServer, dispatch
from sbxloop.daemon.controls import ControlError, ControlService, Principal
from sbxloop.daemon.controls.generation import GENERATION_KEY
from sbxloop.daemon.controls.operations import (
    IdempotencyConflict,
    OperationReplay,
    OperationRunner,
    OperationSpec,
    OperationStore,
    reconcile_operations,
)
from sbxloop.daemon.controls.results import PauseOutcome
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import RunResult
from sbxloop.events import EventBus
from sbxloop.paths import SbxloopHome
from tests.unit.test_daemon_discord import FakeLoop
from tests.unit.test_daemon_loop import Harness, gh_item

OPS = Principal.trusted("ops via test", "ctl")


class RecordingLoop(FakeLoop):
    """FakeLoop with an operation store, the way the real loop has one."""

    def __init__(self, dstore: DaemonStore) -> None:
        super().__init__(dstore)
        self.operations = OperationStore(dstore)
        self.generation = "g_test"
        self.now = 100.0

    def clock(self) -> float:
        return self.now


@pytest.fixture
def floop(tmp_path: Path) -> RecordingLoop:
    return RecordingLoop(DaemonStore(SbxloopHome(tmp_path).state_db))


def spec(**overrides: Any) -> OperationSpec:
    fields: dict[str, Any] = {
        "action": "daemon.pause",
        "target_kind": "hold",
        "target_key": "operator",
        "principal": OPS,
        "request": {"hold": "operator"},
    }
    fields.update(overrides)
    return OperationSpec(**fields)


class TestStore:
    def test_accept_writes_the_row_and_its_event_together(self, floop: RecordingLoop) -> None:
        op, created = floop.operations.accept(spec(), now=1.0)
        assert created and op.state == "accepted" and op.id.startswith("op_")
        assert op.effect == "the hold stands and nothing new is claimed"
        assert op.actor == OPS.audit() and op.request == {"hold": "operator"}
        (event,) = floop.operations.events()
        assert event["type"] == "operation.accepted" and event["operation_id"] == op.id
        assert event["actor"] == OPS.audit() and event["data"]["action"] == "daemon.pause"
        assert event["seq"] == 1 and event["source_seq"] is None

    def test_same_key_same_request_returns_the_same_operation(self, floop: RecordingLoop) -> None:
        first, created = floop.operations.accept(spec(idempotency=("c1", "k1")), now=1.0)
        again, created_again = floop.operations.accept(spec(idempotency=("c1", "k1")), now=2.0)
        assert created and not created_again and again.id == first.id
        # Another scope's key is another operation.
        other, _ = floop.operations.accept(spec(idempotency=("c2", "k1")), now=3.0)
        assert other.id != first.id
        assert len(floop.operations.recent()) == 2

    def test_same_key_different_request_is_a_conflict(self, floop: RecordingLoop) -> None:
        first, _ = floop.operations.accept(spec(idempotency=("c1", "k1")), now=1.0)
        with pytest.raises(IdempotencyConflict) as excinfo:
            floop.operations.accept(
                spec(idempotency=("c1", "k1"), request={"hold": "deploy"}), now=2.0
            )
        assert excinfo.value.existing.id == first.id

    def test_the_first_verdict_stands(self, floop: RecordingLoop) -> None:
        op, _ = floop.operations.accept(spec(), now=1.0)
        floop.operations.claim(op.id, "g1", now=2.0)
        done = floop.operations.finish(op.id, 3.0, state="succeeded", result={"holds": ["x"]})
        assert done is not None and done.state == "succeeded" and done.result == {"holds": ["x"]}
        assert done.claimed_generation == "g1" and done.finished_at == 3.0
        later = floop.operations.finish(op.id, 4.0, state="failed", error_code="late")
        assert later is not None and later.state == "succeeded" and later.error_code is None
        types = [e["type"] for e in floop.operations.events()]
        assert types == ["operation.accepted", "operation.finished"]

    def test_reads_are_bounded_and_filtered(self, floop: RecordingLoop) -> None:
        for i in range(5):
            op, _ = floop.operations.accept(
                spec(target_key=f"h{i}", request={"i": i}), now=float(i)
            )
            if i % 2:
                floop.operations.finish(op.id, 10.0, state="failed", error_code="x")
        assert [o.target_key for o in floop.operations.recent(limit=2)] == ["h4", "h3"]
        assert {o.target_key for o in floop.operations.recent(states=["failed"])} == {"h1", "h3"}
        assert [o.target_key for o in floop.operations.recent(target=("hold", "h2"))] == ["h2"]
        assert [o.target_key for o in floop.operations.pending()] == ["h0", "h2", "h4"]
        assert [e["seq"] for e in floop.operations.events(after_seq=5)] == [6, 7]


class TestRunner:
    def runner(self, floop: RecordingLoop) -> OperationRunner:
        return OperationRunner(
            floop.operations, generation=lambda: floop.generation, clock=floop.clock
        )

    def test_accept_claim_apply_finish(self, floop: RecordingLoop) -> None:
        seen: list[str] = []
        runner = self.runner(floop)
        runner.after_accept = lambda op: seen.append(f"accepted:{op.state}")
        runner.after_claim = lambda op: seen.append("claimed")
        runner.after_effect = lambda op: seen.append("effect")
        runner.after_commit = lambda op: seen.append("committed")
        outcome = runner.run_sync(spec(), lambda op_id: PauseOutcome(hold="operator", holds=["x"]))
        assert outcome.operation_id is not None and outcome.holds == ["x"]
        assert seen == ["accepted:accepted", "claimed", "effect", "committed"]
        stored = floop.operations.get(outcome.operation_id)
        assert stored is not None and stored.state == "succeeded"
        assert stored.claimed_generation == "g_test"
        assert stored.result == {
            "operation_id": None,
            "hold": "operator",
            "holds": ["x"],
            "fresh": True,
            "reason": "",
        }

    def test_a_refusal_finishes_the_record_failed_and_is_re_raised(
        self, floop: RecordingLoop
    ) -> None:
        def refuse(op_id: str) -> PauseOutcome:
            raise ControlError("not_eligible", "nothing is running.")

        with pytest.raises(ControlError):
            self.runner(floop).run_sync(spec(), refuse)
        (op,) = floop.operations.recent()
        assert (op.state, op.error_code, op.error_detail) == (
            "failed",
            "not_eligible",
            "nothing is running.",
        )

    def test_a_crash_in_the_effect_is_recorded_not_hidden(self, floop: RecordingLoop) -> None:
        def explode(op_id: str) -> PauseOutcome:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            self.runner(floop).run_sync(spec(), explode)
        (op,) = floop.operations.recent()
        assert op.state == "failed" and op.error_code == "crashed"
        assert op.error_detail == "RuntimeError: boom"

    def test_a_replay_never_applies_the_effect_twice(self, floop: RecordingLoop) -> None:
        applied: list[str] = []
        runner = self.runner(floop)
        first = runner.run_sync(
            spec(idempotency=("c", "k")),
            lambda op_id: (applied.append(op_id), PauseOutcome(hold="o", holds=[]))[1],
        )
        with pytest.raises(OperationReplay) as excinfo:
            runner.run_sync(
                spec(idempotency=("c", "k")),
                lambda op_id: (applied.append(op_id), PauseOutcome(hold="o", holds=[]))[1],
            )
        assert excinfo.value.existing.id == first.operation_id and applied == [first.operation_id]

    def test_a_deferred_effect_leaves_the_record_running(self, floop: RecordingLoop) -> None:
        outcome = self.runner(floop).run_sync(
            spec(action="run.cancel", target_kind="run", target_key="r1", deferred=True),
            lambda op_id: PauseOutcome(hold="o", holds=[]),
        )
        assert outcome.operation_id is not None
        stored = floop.operations.get(outcome.operation_id)
        assert stored is not None and stored.state == "running"

    def test_a_deferred_after_finishes_when_the_effect_runs(self, floop: RecordingLoop) -> None:
        """A stop's flag is set once the reply is on its way; the record
        says succeeded only once it has been."""
        service = ControlService(floop)
        stop = service.stop(OPS)
        assert stop.operation_id is not None
        before = floop.operations.get(stop.operation_id)
        assert before is not None and before.state == "running"
        assert not getattr(floop, "stopped", False)
        stop.after()
        assert floop.stopped
        after = floop.operations.get(stop.operation_id)
        assert after is not None and after.state == "succeeded"
        assert service.operation_ids == [stop.operation_id]


class TestEverySurfaceRecords:
    def test_ctl_pause_leaves_one_record_attributed_to_ctl(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / ".sbxloop")
        floop = RecordingLoop(DaemonStore(home.state_db))
        server = ControlServer(floop, home, poll_s=0.02)
        server.start()
        try:
            reply = ControlClient(home, by="brett via ctl").submit("pause --hold deploy-1")
        finally:
            server.close()
        assert reply is not None and reply.ok and reply.operation_id is not None
        (op,) = floop.operations.recent()
        assert op.id == reply.operation_id and op.state == "succeeded"
        assert op.action == "daemon.pause" and op.target_key == "deploy-1"
        assert op.actor["via"] == "ctl" and op.actor["display"] == "brett via ctl"
        assert op.result == {
            "operation_id": None,
            "hold": "deploy-1",
            "holds": ["deploy-1"],
            "fresh": True,
            "reason": "",
        }

    def test_reads_leave_no_record(self, floop: RecordingLoop) -> None:
        for cmd in ("status", "queue", "items"):
            reply = dispatch(floop, cmd)
            assert reply.ok and reply.operation_id is None
        assert floop.operations.recent() == []

    def test_a_refused_verb_is_recorded_as_failed(self, floop: RecordingLoop) -> None:
        floop.dstore.upsert_new(WorkItem(item_id="gh:issue:8", source_key="8", title="8"), 1.0)
        floop.dstore.mark_running("gh:issue:8", "r1", 2.0)
        floop.dstore.mark_done("gh:issue:8", now=3.0)
        reply = dispatch(floop, "retry gh:issue:8", by="ops")
        assert not reply.ok and reply.text.startswith("retry failed:")
        # The record carries the refusal; the prose edge carried the sentence.
        (op,) = floop.operations.recent()
        assert op.state == "failed" and op.error_code == "not_eligible"
        assert reply.operation_id == op.id


class TestCancelRecord:
    """The cancel's record is finished by what the run actually did."""

    def _tick_with_cancel(
        self, h: Harness, *, honour: bool, by: str = "ops via test"
    ) -> tuple[str, str | None]:
        started = threading.Event()
        release = threading.Event()
        run_ids: list[str] = []

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            run_ids.append(run_id)
            started.set()
            release.wait(5)
            if honour:
                from sbxloop.errors import RunCancelledError

                h.store.create_run(run_id, "outcome")
                h.store.set_run_state(run_id, "building")
                raise RunCancelledError("cancelled")
            return h.runner(item, cfg, run_id, bus, resume)

        h.loop._runner = runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        service = ControlService(h.loop)
        outcome = service.cancel_current(Principal.trusted(by, "ctl"))
        release.set()
        t.join(5)
        assert outcome.operation_id is not None
        assert outcome.target == run_ids[0]
        return outcome.operation_id, run_ids[0]

    def test_an_honoured_cancel_succeeds_when_the_run_settles(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.loop.recover()
        h.source.items = [gh_item()]
        op_id, run_id = self._tick_with_cancel(h, honour=True)
        op = h.loop.operations.get(op_id)
        assert op is not None and op.state == "succeeded" and op.target_key == run_id
        assert op.result == {"mode": "current", "retry": False, "run_id": run_id}
        assert h.dstore.get("gh:issue:1").state == "cancelled"  # type: ignore[union-attr]

    def test_a_late_cancel_reports_the_state_the_run_reached(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.loop.recover()
        h.source.items = [gh_item()]
        op_id, _ = self._tick_with_cancel(h, honour=False)
        op = h.loop.operations.get(op_id)
        assert op is not None and op.state == "failed"
        assert op.error_code == "target_already_terminal"
        assert op.error_detail is not None and "it is merged" in op.error_detail
        assert h.dstore.get("gh:issue:1").state == "done"  # type: ignore[union-attr]


class TestReconciler:
    """What a dead generation left, judged from evidence at recovery."""

    def test_recover_stamps_a_generation(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        assert h.loop.generation is None and h.loop.status()["generation"] is None
        h.loop.recover()
        assert h.loop.generation is not None
        assert h.dstore.get_value(GENERATION_KEY) == h.loop.generation
        assert h.loop.status()["generation"] == h.loop.generation

    def test_an_unclaimed_command_expires_rather_than_running_at_boot(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        op, _ = h.loop.operations.accept(spec(principal=OPS), now=1.0)
        h.loop.recover()
        settled = h.loop.operations.get(op.id)
        assert settled is not None and settled.state == "expired"
        assert settled.error_detail == "the daemon restarted before the command was claimed"

    def test_a_deadline_is_named_when_it_passed(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        op, _ = h.loop.operations.accept(spec(ttl_s=10.0), now=h.clock() - 100)
        h.loop.recover()
        settled = h.loop.operations.get(op.id)
        assert settled is not None and settled.state == "expired"
        assert settled.error_detail == "past its deadline before it was claimed"

    @pytest.mark.parametrize(
        ("run_state", "expected", "code"),
        [
            ("cancelled", "succeeded", None),
            ("merged", "failed", "target_already_terminal"),
            ("building", "failed", "interrupted_before_effect"),
        ],
    )
    def test_a_claimed_cancel_is_judged_from_the_run(
        self, tmp_path: Path, run_state: str, expected: str, code: str | None
    ) -> None:
        h = Harness(tmp_path)
        h.store.create_run("r1", "x")
        h.store.set_run_state("r1", run_state)  # type: ignore[arg-type]
        op, _ = h.loop.operations.accept(
            spec(action="run.cancel", target_kind="run", target_key="r1", deferred=True), now=1.0
        )
        h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        settled = h.loop.operations.get(op.id)
        assert settled is not None and (settled.state, settled.error_code) == (expected, code)

    def test_own_generation_claims_are_left_alone(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.loop.recover()
        assert h.loop.generation is not None
        op, _ = h.loop.operations.accept(
            spec(action="run.cancel", target_kind="run", target_key="r1"), now=1.0
        )
        h.loop.operations.claim(op.id, h.loop.generation, now=2.0)
        touched = reconcile_operations(h.loop, generation=h.loop.generation, now=3.0)
        assert touched == []
        assert h.loop.operations.get(op.id).state == "running"  # type: ignore[union-attr]

    def test_stop_and_restart_succeed_once_a_new_generation_answers(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        for action in ("daemon.stop", "daemon.restart"):
            op, _ = h.loop.operations.accept(
                spec(action=action, target_kind="daemon", target_key="daemon"), now=1.0
            )
            h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        assert {o.state for o in h.loop.operations.recent()} == {"succeeded"}

    def test_item_verbs_are_judged_from_the_item(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_running("gh:issue:1", "r1", now=2.0)
        h.dstore.abandon("gh:issue:1", "gave up", now=3.0)
        done, _ = h.loop.operations.accept(
            spec(action="item.abandon", target_kind="item", target_key="gh:issue:1"), now=1.0
        )
        lost, _ = h.loop.operations.accept(
            spec(action="item.requeue", target_kind="item", target_key="gh:issue:1"), now=1.5
        )
        for op in (done, lost):
            h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        assert h.loop.operations.get(done.id).state == "succeeded"  # type: ignore[union-attr]
        judged = h.loop.operations.get(lost.id)
        assert judged is not None and judged.state == "failed"
        assert judged.error_code == "interrupted_before_effect" and judged.error_detail == (
            "item is failed"
        )

    def test_what_evidence_cannot_decide_is_reconciling_never_succeeded(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        op, _ = h.loop.operations.accept(
            spec(action="run.review_resume", target_kind="target", target_key="r1"), now=1.0
        )
        h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        settled = h.loop.operations.get(op.id)
        assert settled is not None and settled.state == "reconciling"
        assert settled.error_detail == "the effect could not be established from the record"
        # And it stays that way across the next recovery: no second guess.
        h.loop.recover()
        assert h.loop.operations.get(op.id).state == "reconciling"  # type: ignore[union-attr]

    def test_the_record_survives_the_stamp_being_rewound(self, tmp_path: Path) -> None:
        """A rollback reinstalls the previous release against this
        database and re-runs the revision on the way back: the tables it
        meets are kept, rows and all."""
        h = Harness(tmp_path)
        op, _ = h.loop.operations.accept(spec(), now=1.0)
        h.dstore.close()
        h.store.close()
        with sqlite3.connect(h.config.paths.state_db) as conn:
            conn.execute("UPDATE alembic_version SET version_num = '0008'")
        reopened = DaemonStore(h.config.paths.state_db)
        rows = OperationStore(reopened).recent()
        assert [o.id for o in rows] == [op.id]
        assert json.loads(json.dumps(rows[0].request)) == {"hold": "operator"}
