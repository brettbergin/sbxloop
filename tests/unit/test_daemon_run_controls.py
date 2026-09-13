"""Holds that survive a restart, a cancel bound to one run, and a resume the
daemon owns — across the three run kinds and the states a run can be in."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbxloop.cli.app import app
from sbxloop.config import Config
from sbxloop.daemon.control import dispatch
from sbxloop.daemon.controls import ControlError, ControlService, Principal
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import RunResult
from sbxloop.engine.store import StateStore
from sbxloop.errors import RunCancelledError
from sbxloop.events import EventBus
from sbxloop.paths import SbxloopHome
from tests.unit.test_daemon_loop import Harness, RecordingFrontend, gh_item

OPS = Principal.trusted("ops via test", "ctl")


def restarted(h: Harness) -> DaemonLoop:
    """A new process over the same stores, recovered the way the daemon is."""
    loop = DaemonLoop(
        h.config,
        store=h.store,
        dstore=h.dstore,
        source=h.source,
        runner=h.runner,
        clock=h.clock,
        frontend=RecordingFrontend(),
    )
    loop.recover()
    return loop


class TestHoldsSurviveARestart:
    def test_every_hold_stands_after_a_restart_and_says_whose_it_is(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.loop.pause(by="brett", via="discord")
        h.loop.pause("deploy-7", by="github-actions", via="ctl", reason="deploy 1.2.3")
        again = restarted(h)
        assert again.holds == ["deploy-7", "operator"] and again.paused
        assert again.tick().idle_kind == "paused"
        details = {d["name"]: d for d in again.status()["hold_details"]}
        assert details["operator"]["owner"] == "brett" and details["operator"]["via"] == "discord"
        assert details["deploy-7"]["reason"] == "deploy 1.2.3"
        # Recovery narrates what still stands, once, naming the owners.
        frontend = again.frontend
        assert isinstance(frontend, RecordingFrontend)
        (notice,) = [n for n in frontend.notices if n.kind == "daemon.holds_restored"]
        assert "deploy-7 (github-actions)" in notice.text and "operator (brett)" in notice.text

    def test_a_release_is_durable_too(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.loop.pause("deploy-7")
        h.loop.pause()
        assert h.loop.unpause("deploy-7") == ["operator"]
        again = restarted(h)
        assert again.holds == ["operator"]
        assert again.unpause(None) == []
        assert restarted(h).holds == []

    def test_taking_a_hold_twice_narrates_once(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.loop.frontend = RecordingFrontend()
        h.loop.pause("x", by="a")
        h.loop.pause("x", by="b")
        frontend = h.loop.frontend
        assert isinstance(frontend, RecordingFrontend)
        assert [n.kind for n in frontend.notices] == ["daemon.paused"]
        # The first owner keeps the hold.
        assert h.dstore.holds()[0].owner_display == "a"

    def test_the_service_records_the_owner_and_the_operation(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.loop.recover()
        service = ControlService(h.loop)
        paused = service.pause(Principal.trusted("brett via ctl", "ctl"), "deploy-9")
        assert paused.fresh and paused.operation_id is not None
        (hold,) = h.dstore.holds()
        assert hold.owner_display == "brett via ctl" and hold.via == "ctl"
        assert hold.operation_id == paused.operation_id
        again = service.pause(OPS, "deploy-9")
        assert not again.fresh

    def test_a_restart_with_a_hold_reconciles_the_pause_operation_as_succeeded(
        self, tmp_path: Path
    ) -> None:
        """The reconciler judges a claimed pause from the row that survived."""
        from sbxloop.daemon.controls.operations import OperationSpec

        h = Harness(tmp_path)
        h.loop.pause("deploy-1")
        op, _ = h.loop.operations.accept(
            OperationSpec(
                action="daemon.pause", target_kind="hold", target_key="deploy-1", principal=OPS
            ),
            now=1.0,
        )
        h.loop.operations.claim(op.id, "g_dead", now=2.0)
        again = restarted(h)
        assert again.operations.get(op.id).state == "succeeded"  # type: ignore[union-attr]


class TestRevisions:
    def test_every_write_bumps_the_run_and_item_revision(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.store.create_run("r1", "x")
        assert h.store.get_run("r1").revision == 0
        h.store.set_run_state("r1", "building")
        h.store.set_run_reason("r1", "why")
        assert h.store.get_run("r1").revision == 2
        h.dstore.upsert_new(gh_item(), now=1.0)
        assert h.dstore.get("gh:issue:1").revision == 0  # type: ignore[union-attr]
        h.dstore.mark_claimed("gh:issue:1", now=2.0)
        assert h.dstore.get("gh:issue:1").revision == 1  # type: ignore[union-attr]
        # A raw write from any release bumps it too: the trigger is the rule.
        with sqlite3.connect(h.config.paths.state_db) as conn:
            conn.execute("UPDATE runs SET reason = 'raw' WHERE run_id = 'r1'")
        assert h.store.get_run("r1").revision == 3

    def test_a_stale_revision_is_refused_before_anything_happens(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r1", now=2.0)
        h.store.create_run("r1", "x")
        h.store.set_run_state("r1", "building")
        h.dstore.mark_cancelled("gh:issue:1", "stopped", 3.0)
        current = h.store.get_run("r1").revision
        with pytest.raises(ControlError) as excinfo:
            h.loop.resume_run("r1", expected_revision=current + 5)
        assert excinfo.value.code == "stale_revision"
        assert excinfo.value.detail == {"revision": current}
        assert h.dstore.get("gh:issue:1").state == "cancelled"  # type: ignore[union-attr]
        # And the right one is accepted.
        assert h.loop.resume_run("r1", expected_revision=current).item_id == "gh:issue:1"


class TestCancelRun:
    def _in_flight(self, h: Harness, *, honour: bool = True) -> tuple[threading.Thread, list[str]]:
        started = threading.Event()
        release = threading.Event()
        run_ids: list[str] = []

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            run_ids.append(run_id)
            if honour:
                h.store.create_run(run_id, "outcome", kind=item.kind)
                h.store.set_run_state(run_id, "building")
            started.set()
            release.wait(5)
            if honour:
                raise RunCancelledError("cancelled")
            return h.runner(item, cfg, run_id, bus, resume)

        h.loop._runner = runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        h.release = release  # type: ignore[attr-defined]
        return t, run_ids

    def test_the_run_in_flight_by_identity(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        t, run_ids = self._in_flight(h)
        outcome = h.loop.cancel_run(run_ids[0], by="brett")
        assert outcome.mode == "current" and outcome.target == run_ids[0]
        h.release.set()  # type: ignore[attr-defined]
        t.join(5)
        assert h.dstore.get("gh:issue:1").state == "cancelled"  # type: ignore[union-attr]

    def test_a_cancel_of_run_a_after_run_b_started_never_touches_b(self, tmp_path: Path) -> None:
        """The delayed command names the run it meant; a later run is not
        the current run's proxy."""
        h = Harness(tmp_path)
        h.source.items = [gh_item("1"), gh_item("2")]
        assert h.loop.tick().outcome == "done"  # run A, merged (scripted runner)
        run_a = h.runs[0][0]
        t, _ = self._in_flight(h, honour=False)  # run B in flight
        with pytest.raises(ControlError) as excinfo:
            h.loop.cancel_run(run_a, by="late")
        assert excinfo.value.code == "already_terminal"
        assert excinfo.value.message == f"run {run_a} is merged"
        h.release.set()  # type: ignore[attr-defined]
        t.join(5)
        assert h.dstore.get("gh:issue:2").state == "done"  # type: ignore[union-attr]
        assert not any(c[0] == "cancelled" for c in h.source.calls)

    def test_a_pending_resume_is_settled_without_a_sandbox(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_live", now=2.0)
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "building")
        h.loop.recover()  # queues the resume
        assert h.dstore.get("gh:issue:1").state == "queued"  # type: ignore[union-attr]
        outcome = h.loop.cancel_run("r_live", by="brett")
        assert outcome.mode == "queued"
        assert h.dstore.get("gh:issue:1").state == "cancelled"  # type: ignore[union-attr]
        assert h.store.get_run("r_live").state == "cancelled"
        assert h.loop.tick().idle_reason == "no_work" and h.runs == []

    def test_unknown_and_foreign_runs_are_named(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        with pytest.raises(ControlError) as excinfo:
            h.loop.cancel_run("r_nope")
        assert excinfo.value.code == "unknown_target"
        # A run no work item pins and no daemon holds: not this daemon's.
        h.store.create_run("r_cli", "x")
        h.store.set_run_state("r_cli", "building")
        with pytest.raises(ControlError) as excinfo:
            h.loop.cancel_run("r_cli")
        assert excinfo.value.code == "not_eligible"

    def test_a_tool_run_can_be_cancelled_like_any_other(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [
            gh_item(kind="tool", recipe="entrygraph", recipe_target="acme/one"),
        ]
        t, run_ids = self._in_flight(h)
        assert h.loop.cancel_run(run_ids[0]).mode == "current"
        h.release.set()  # type: ignore[attr-defined]
        t.join(5)

    def test_bare_cancel_still_means_the_current_run(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        assert h.loop.cancel_current("ops") is False
        h.source.items = [gh_item()]
        t, _ = self._in_flight(h)
        assert h.loop.cancel_current("ops", retry=True) is True
        h.release.set()  # type: ignore[attr-defined]
        t.join(5)
        assert h.dstore.get("gh:issue:1").state == "queued"  # type: ignore[union-attr]


class TestResumeRun:
    def _interrupted(self, h: Harness, kind: str = "code", state: str = "building") -> None:
        h.dstore.upsert_new(gh_item(kind=kind), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_live", now=2.0)
        h.store.create_run("r_live", "x", kind=kind)  # type: ignore[arg-type]
        h.store.set_run_state("r_live", state)  # type: ignore[arg-type]
        h.dstore.mark_cancelled("gh:issue:1", "cancelled by ops", 3.0)
        h.store.set_run_state("r_live", "cancelled")

    @pytest.mark.parametrize("kind", ["code", "workload", "tool"])
    def test_a_cancelled_run_is_resumed_by_the_tick_not_a_second_engine(
        self, tmp_path: Path, kind: str
    ) -> None:
        h = Harness(tmp_path)
        self._interrupted(h, kind=kind)
        outcome = h.loop.resume_run("r_live", by="brett")
        assert outcome == outcome.model_copy(update={"run_id": "r_live", "item_id": "gh:issue:1"})
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued" and item.run_id == "r_live"
        assert h.runs == []
        h.outcomes = ["completed" if kind != "code" else "merged"]
        assert h.loop.tick().outcome == "done"
        assert h.runs == [("r_live", True)]

    def test_the_run_in_flight_is_refused(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        started, release = threading.Event(), threading.Event()
        run_ids: list[str] = []

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            run_ids.append(run_id)
            started.set()
            release.wait(5)
            return h.runner(item, cfg, run_id, bus, resume)

        h.loop._runner = runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        with pytest.raises(ControlError) as excinfo:
            h.loop.resume_run(run_ids[0])
        assert excinfo.value.message == f"run {run_ids[0]} is in flight"
        release.set()
        t.join(5)

    def test_refusals_name_the_reason(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        with pytest.raises(ControlError) as excinfo:
            h.loop.resume_run("r_nope")
        assert excinfo.value.code == "unknown_target"
        # Finished: nothing to resume.
        h.dstore.upsert_new(gh_item("2"), now=1.0)
        h.dstore.mark_running("gh:issue:2", "r_done", 1.0)
        h.store.create_run("r_done", "x")
        h.store.set_run_state("r_done", "merged")
        h.dstore.mark_done("gh:issue:2", now=2.0)
        with pytest.raises(ControlError) as excinfo:
            h.loop.resume_run("r_done")
        assert excinfo.value.message == "run is merged"
        # Unpinned (retried): the item moved on.
        self._interrupted(h)
        h.dstore.retry("gh:issue:1", 4.0)
        with pytest.raises(ControlError) as excinfo:
            h.loop.resume_run("r_live")
        assert excinfo.value.message == "work item is queued"

    def test_the_resume_budget_is_honoured(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": {"max_resumes_per_item": 1},
            }
        )
        h = Harness(tmp_path, cfg)
        self._interrupted(h)
        h.dstore.mark_resuming("gh:issue:1", "r_live", 5.0)
        h.dstore.mark_cancelled("gh:issue:1", "again", 6.0)
        with pytest.raises(ControlError) as excinfo:
            h.loop.resume_run("r_live")
        assert "resume budget (1 of 1" in excinfo.value.message
        assert "retry gh:issue:1" in excinfo.value.message

    def test_an_exhausted_run_needs_rounds_first(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        self._interrupted(h, state="reviewing")
        h.store.set_run_exhausted("r_live", "review")
        with pytest.raises(ControlError) as excinfo:
            h.loop.resume_run("r_live")
        assert "grant-rounds r_live N" in excinfo.value.message


class TestProseEdge:
    def test_ctl_verbs(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.loop.recover()
        TestResumeRun()._interrupted(h)
        reply = dispatch(h.loop, "resume-run r_live", by="ops")
        assert (
            reply.ok
            and reply.text == "`r_live` queued to resume for `gh:issue:1` at the next tick."
        )
        assert reply.operation_id is not None
        # Asking again while the resume is pending is the same answer, not a
        # second admission.
        again = dispatch(h.loop, "resume-run r_live", by="ops")
        assert again.ok and again.text == reply.text
        cancelled = dispatch(h.loop, "cancel-run r_live", by="ops")
        assert cancelled.ok and cancelled.text == "cancelled by ops before its resume"
        assert h.dstore.get("gh:issue:1").state == "cancelled"  # type: ignore[union-attr]
        assert (
            dispatch(h.loop, "cancel-run r_live").text
            == "cancel-run failed: run r_live is cancelled"
        )
        assert not dispatch(h.loop, "resume-run").ok and not dispatch(h.loop, "cancel-run --x").ok
        # Every one of them left a record, finished at once — nothing here
        # waits on a run boundary — the refused cancel included.
        assert sorted((o.action, o.state) for o in h.loop.operations.recent()) == [
            ("run.cancel", "failed"),
            ("run.cancel", "succeeded"),
            ("run.resume", "succeeded"),
            ("run.resume", "succeeded"),
        ]

    def test_stop_and_restart_say_holds_survive(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        assert "still stand" in dispatch(h.loop, "stop").text
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("INVOCATION_ID", "1")
            assert "still stand" in dispatch(h.loop, "restart").text


class TestCliResume:
    def test_a_daemon_owned_run_is_refused_and_pointed_at_the_daemon(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        home = SbxloopHome(tmp_path / ".sbxloop")
        store = StateStore(home.state_db)
        store.create_run("r_owned", "x")
        store.set_run_state("r_owned", "building")
        store.close()
        dstore = DaemonStore(home.state_db)
        dstore.upsert_new(gh_item(), now=1.0)
        dstore.mark_running("gh:issue:1", "r_owned", 1.0)
        dstore.close()
        result = CliRunner().invoke(app, ["resume", "r_owned", "--no-tui"])
        assert result.exit_code == 2
        assert "sbxloop daemon ctl resume-run r_owned" in result.output
        assert "gh:issue:1" in result.output

    def test_a_run_no_daemon_owns_is_not_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No daemon ledger at all (a host that only ever ran `sbxloop
        run`) must not stop a resume: the refusal is about ownership."""
        monkeypatch.chdir(tmp_path)
        home = SbxloopHome(tmp_path / ".sbxloop")
        store = StateStore(home.state_db)
        store.create_run("r_mine", "x")
        store.set_run_state("r_mine", "merged")  # not resumable: fails later, past the check
        store.close()
        result = CliRunner().invoke(app, ["resume", "r_mine", "--no-tui"])
        assert "resume-run" not in result.output
        assert result.exit_code == 2 and "only unfinished runs can resume" in result.output


class TestSteerRun:
    """#1038: an instruction reaches the run in flight through the same
    input path a chat thread uses, and only that run."""

    def _in_flight(self, h: Harness) -> tuple[threading.Thread, list[str], threading.Event]:
        started, release = threading.Event(), threading.Event()
        run_ids: list[str] = []

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            run_ids.append(run_id)
            h.store.create_run(run_id, "outcome", kind=item.kind)
            h.store.set_run_state(run_id, "building")
            started.set()
            release.wait(5)
            raise RunCancelledError("cancelled")

        h.loop._runner = runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        return t, run_ids, release

    def test_the_run_in_flight_takes_the_message(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        t, run_ids, release = self._in_flight(h)
        try:
            mid = h.loop.steer_run(run_ids[0], "Skip the migration", by="brett")
            handle = h.loop._current
            assert handle is not None
            queued = handle.engine._chat_queue.get_nowait()
            assert (queued.message_id, queued.text) == (mid, "Skip the migration")
            with pytest.raises(ControlError) as stale:
                h.loop.steer_run(run_ids[0], "x", expected_revision=999)
            assert stale.value.code == "stale_revision"
            with pytest.raises(ControlError) as other:
                h.loop.steer_run("rnope", "x")
            assert other.value.code == "unknown_target"
        finally:
            release.set()
            t.join(5)
        with pytest.raises(ControlError) as ended:
            h.loop.steer_run(run_ids[0], "too late")
        assert ended.value.code == "not_eligible" and "in flight" in ended.value.message

    def test_a_tool_run_has_nothing_to_steer(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item("2", kind="tool", recipe="entrygraph", recipe_target="o/r")]
        t, run_ids, release = self._in_flight(h)
        try:
            with pytest.raises(ControlError) as excinfo:
                h.loop.steer_run(run_ids[0], "x")
            assert excinfo.value.code == "unsupported_for_kind"
        finally:
            release.set()
            t.join(5)

    def test_the_service_records_the_instruction_and_its_fate(self, tmp_path: Path) -> None:
        from sbxloop.daemon.controls.steering import SteeringStore

        h = Harness(tmp_path)
        h.loop.recover()
        service = ControlService(h.loop)
        h.source.items = [gh_item()]
        t, run_ids, release = self._in_flight(h)
        try:
            outcome = service.steer(OPS, run_ids[0], "Do it differently", source_refs=["m1"])
            record = SteeringStore(h.dstore).get(outcome.steering_id)
            assert record is not None and record.status == "delivered"
            assert record.message_id == outcome.message_id and record.source_refs == ["m1"]
            assert record.operation_id == outcome.operation_id
            assert h.loop.operations.get(outcome.operation_id).state == "succeeded"  # type: ignore[union-attr]
        finally:
            release.set()
            t.join(5)
        with pytest.raises(ControlError) as excinfo:
            service.steer(OPS, run_ids[0], "too late")
        failed = SteeringStore(h.dstore).get(str(excinfo.value.detail["steering_id"]))
        assert failed is not None and failed.status == "failed" and failed.error
        with pytest.raises(ControlError) as blank:
            service.steer(OPS, run_ids[0], "   ")
        assert blank.value.code == "invalid_argument"

    def test_a_restart_judges_a_claimed_steer_from_its_record(self, tmp_path: Path) -> None:
        from sbxloop.daemon.controls.operations import OperationSpec
        from sbxloop.daemon.controls.steering import SteeringStore

        h = Harness(tmp_path)
        store = SteeringStore(h.dstore)
        spec = OperationSpec(action="run.steer", target_kind="run", target_key="r1", principal=OPS)
        delivered, _ = h.loop.operations.accept(spec, now=1.0)
        h.loop.operations.claim(delivered.id, "g_dead", now=2.0)
        row = store.create(
            run_id="r1",
            text="x",
            principal=OPS,
            source_refs=[],
            expected_revision=None,
            now=1.0,
            deadline_at=None,
            operation_id=delivered.id,
        )
        store.delivered(row.id, "m1", 2.0)
        lost, _ = h.loop.operations.accept(
            OperationSpec(action="run.steer", target_kind="run", target_key="r2", principal=OPS),
            now=3.0,
        )
        h.loop.operations.claim(lost.id, "g_dead", now=4.0)
        again = restarted(h)
        assert again.operations.get(delivered.id).state == "succeeded"  # type: ignore[union-attr]
        judged = again.operations.get(lost.id)
        assert judged is not None and judged.state == "failed"
        assert judged.error_code == "interrupted_before_effect"
