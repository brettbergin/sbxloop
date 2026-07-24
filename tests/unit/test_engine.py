"""End-to-end LoopEngine tests: fake sbx + real worker + scripted echo backend.

Every agent phase consumes the next scripted echo response in order, so a
whole run is scripted as a list. Shell jobs (evidence gathering, verify
commands) run for real inside the fake sandbox fs and do not consume script
entries.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.store import StateStore
from sbxloop.errors import BudgetExceededError, StateError, WorkerError
from sbxloop.events import Event, EventBus, HostEventTypes
from sbxloop.sbx.cli import SbxCLI
from tests.conftest import FakeSbx

# -- scripted responses ------------------------------------------------------


def taskgraph(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {"json": {"tasks": list(tasks)}}


def task(id: str, deps: list[str] | None = None, verify: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": id,
        "title": f"Task {id}",
        "description": f"description of {id}",
        "depends_on": deps or [],
        "acceptance_criteria": [f"{id} works"],
        "verify_commands": verify if verify is not None else ["true"],
    }


PLAN = {"json": {"steps": ["do the work"], "expected_artifacts": [], "verify_commands": []}}
EXECUTE = {"text": "work complete, files changed"}
PASS = {"json": {"verdict": "pass"}}
REVISE = {"json": {"verdict": "revise", "feedback": "missed an edge case"}}
ACCEPT = {"json": {"verdict": "accept"}}
REJECT = {"json": {"verdict": "reject", "feedback": "criterion 1 unmet"}}

HAPPY_TASK = [PLAN, EXECUTE, PASS, ACCEPT]


class Harness:
    def __init__(
        self,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.fake_sbx = fake_sbx
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.script_path = tmp_path / "echo-script.json"
        self.state_dir = tmp_path / "state"
        self.events: list[Event] = []
        monkeypatch.setenv("SBXLOOP_WORKER_BACKEND", "echo")
        monkeypatch.setenv("SBXLOOP_ECHO_SCRIPT", str(self.script_path))
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot_tok")
        monkeypatch.setenv("GH_TOKEN", "gh_tok")

    def script(self, responses: list[dict[str, Any]]) -> None:
        self.script_path.write_text(json.dumps(responses))
        state = self.script_path.with_suffix(".json.state")
        state.unlink(missing_ok=True)

    def engine(self, **config_overrides: Any) -> LoopEngine:
        config = Config.model_validate(
            {"state_dir": str(self.state_dir), "budgets": config_overrides.pop("budgets", {})}
            | config_overrides
        )
        bus = EventBus()
        bus.subscribe(self.events.append)
        return LoopEngine(
            config,
            store=StateStore(self.state_dir / "state.db"),
            bus=bus,
            sbx=SbxCLI(binary=str(self.fake_sbx.binary)),
            worker_python=sys.executable,
            install_workers=False,
        )

    def event_types(self) -> list[str]:
        return [e.type for e in self.events]

    def sandboxes_left(self) -> list[str]:
        boxes = self.fake_sbx.state / "sandboxes"
        return sorted(p.name for p in boxes.iterdir()) if boxes.is_dir() else []


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


class TestHappyPath:
    def test_single_task_completes(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine().start("build the feature")

        assert result.state == "completed"
        assert result.succeeded
        assert [t.state for t in result.tasks] == ["done"]
        assert result.tasks[0].session_id is not None

        types = harness.event_types()
        assert HostEventTypes.RUN_START in types
        assert HostEventTypes.TASK_END in types
        assert HostEventTypes.RUN_END in types
        # sandboxes cleaned up
        assert harness.sandboxes_left() == []

    def test_two_tasks_dependency_order(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t2", deps=["t1"]), task("t1")), *HAPPY_TASK, *HAPPY_TASK])
        engine = harness.engine()
        result = engine.start("two things")
        assert result.state == "completed"
        # persisted in topo order: t1 before t2
        assert [t.spec.id for t in result.tasks] == ["t1", "t2"]

    def test_phase_attempts_recorded(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        engine = harness.engine()
        result = engine.start("record phases")
        phases = [row["phase"] for row in engine.store.phase_attempts(result.run_id)]
        assert phases == ["decompose", "plan", "execute", "scrutinize", "verify", "validate"]


class TestReviseAndVerify:
    def test_scrutinize_revise_then_pass(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), PLAN, EXECUTE, REVISE, EXECUTE, PASS, ACCEPT])
        result = harness.engine().start("revise once")
        assert result.state == "completed"
        assert result.tasks[0].revisions == 1

    def test_verify_failure_exhausts_revisions(self, harness: Harness) -> None:
        # verify command always fails -> execute/scrutinize repeat until the
        # revision budget (2) is exhausted -> task failed, run failed
        harness.script(
            [
                taskgraph(task("t1", verify=["exit 1"])),
                PLAN,
                EXECUTE,
                PASS,
                EXECUTE,
                PASS,
                EXECUTE,
                PASS,
            ]
        )
        result = harness.engine().start("verify never passes")
        assert result.state == "failed"
        assert result.tasks[0].state == "failed"
        assert result.tasks[0].revisions == 3
        assert "verify command failed" in result.tasks[0].last_feedback


class TestReplanAndSkip:
    def test_validate_reject_replans_then_accepts(self, harness: Harness) -> None:
        harness.script(
            [taskgraph(task("t1")), PLAN, EXECUTE, PASS, REJECT, PLAN, EXECUTE, PASS, ACCEPT]
        )
        result = harness.engine().start("reject then accept")
        assert result.state == "completed"
        assert result.tasks[0].replans == 1
        assert result.tasks[0].state == "done"

    def test_replan_budget_exhaustion_skips_dependents(self, harness: Harness) -> None:
        harness.script(
            [
                taskgraph(task("t1"), task("t2", deps=["t1"])),
                PLAN,
                EXECUTE,
                PASS,
                REJECT,  # replan 1
                PLAN,
                EXECUTE,
                PASS,
                REJECT,  # replans exhausted -> t1 failed
            ]
        )
        result = harness.engine().start("fail and skip")
        assert result.state == "failed"
        by_id = {t.spec.id: t for t in result.tasks}
        assert by_id["t1"].state == "failed"
        assert by_id["t2"].state == "skipped"
        end_states = [e.data["state"] for e in harness.events if e.type == HostEventTypes.TASK_END]
        assert end_states == ["failed", "skipped"]


class TestBudgetsAndCancel:
    def test_wall_clock_budget(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1"))])
        ticks = iter([0.0, 10_000.0, 20_000.0, 30_000.0, 40_000.0])
        engine = harness.engine(budgets={"max_wall_clock_s": 5.0})
        engine.clock = lambda: next(ticks)
        with pytest.raises(BudgetExceededError, match="max_wall_clock_s"):
            engine.start("too slow")
        run_id = engine.store.list_runs()[0].run_id
        assert engine.store.get_run(run_id).state == "failed"
        assert harness.sandboxes_left() == []  # pair context cleaned up

    def test_too_many_tasks_rejected(self, harness: Harness) -> None:
        harness.script([taskgraph(*(task(f"t{i}") for i in range(1, 5)))])
        engine = harness.engine(budgets={"max_tasks": 2})
        with pytest.raises(BudgetExceededError, match="produced 4 tasks"):
            engine.start("too many")

    def test_cancelled_run_stops_and_wont_resume(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1"))])
        engine = harness.engine()
        engine.store.create_run("rcancel", "x")
        engine.cancel("rcancel")
        assert engine.store.get_run("rcancel").state == "cancelled"
        with pytest.raises(StateError, match="only unfinished runs"):
            engine.resume("rcancel")


class TestResume:
    def test_resume_after_crash_continues(self, harness: Harness) -> None:
        # Crash during t1's execute (worker returns an error result).
        harness.script([taskgraph(task("t1")), PLAN, {"fail": "sandbox exploded"}])
        engine = harness.engine()
        with pytest.raises(WorkerError, match="sandbox exploded"):
            engine.start("crashy run")

        run_id = engine.store.list_runs()[0].run_id
        run = engine.store.get_run(run_id)
        assert run.state == "running"  # persisted mid-flight
        tasks = engine.store.get_tasks(run_id)
        assert tasks[0].state == "executing"
        assert tasks[0].plan is not None  # plan was committed before the crash

        # Fresh engine (new sandbox pair), remaining script picks up at
        # EXECUTE - decompose and plan are NOT re-run.
        harness.script([EXECUTE, PASS, ACCEPT])
        engine2 = harness.engine()
        result = engine2.resume(run_id)
        assert result.state == "completed"
        assert result.tasks[0].state == "done"
        phases = [row["phase"] for row in engine2.store.phase_attempts(run_id)]
        assert phases.count("decompose") == 1
        assert phases.count("plan") == 1
        # the crashed attempt never committed a phase row - that is exactly the
        # "uncommitted phases re-run" resume semantic
        assert phases.count("execute") == 1

    def test_resume_completed_run_refused(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        engine = harness.engine()
        result = engine.start("finish it")
        with pytest.raises(StateError, match="only unfinished runs"):
            engine.resume(result.run_id)


class TestJsonRetry:
    def test_invalid_decompose_retried_once_with_error(self, harness: Harness) -> None:
        harness.script(
            [
                {"json": {"tasks": [{"id": "t1"}]}},  # missing required title
                taskgraph(task("t1")),
                *HAPPY_TASK,
            ]
        )
        result = harness.engine().start("retry decompose")
        assert result.state == "completed"

    def test_invalid_twice_raises(self, harness: Harness) -> None:
        harness.script([{"json": {"tasks": [{"id": "t1"}]}}, {"json": {"tasks": [{"id": "t1"}]}}])
        with pytest.raises(WorkerError, match="invalid output twice"):
            harness.engine().start("never valid")


class TestWorkspaceExecution:
    """The artifacts linchpin: jobs run in the workspace mount, so files the
    executor writes appear on the host live and survive sandbox teardown."""

    def test_mounted_run_lands_artifacts_on_host(self, harness: Harness) -> None:
        execute = {"text": "wrote hello.txt", "files": {"hello.txt": "hi\n"}}
        harness.script([taskgraph(task("t1")), PLAN, execute, PASS, ACCEPT])
        engine = harness.engine()
        result = engine.start("write hello.txt containing hi")

        assert result.state == "completed"
        assert result.mounted
        assert result.workspace is not None
        # the sandboxes are gone; the artifact is on the host
        assert harness.sandboxes_left() == []
        assert (result.workspace / "hello.txt").read_text() == "hi\n"

        # persisted for post-run reads (sbxloop artifacts / resume)
        record = engine.store.get_run(result.run_id)
        assert record.workspace == result.workspace
        assert record.mounted

    def test_every_job_carries_workspace_cwd(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        engine = harness.engine(keep_sandboxes=True)
        result = engine.start("check job cwd")
        assert result.state == "completed"
        fs = harness.fake_sbx.sandbox_fs(f"sbxloop-{result.run_id}-agent")
        jobs = [json.loads(p.read_text()) for p in (fs / "home/agent/.sbxloop/jobs").iterdir()]
        assert jobs
        # every job of every kind — agent phases, evidence, verify — runs in
        # the workspace, so scrutiny and verification see the produced files
        workdirs = {j["cwd"] for j in jobs}
        assert len(workdirs) == 1
        assert workdirs != {None}

    def test_unmounted_run_harvests_artifacts(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SBX_FAKE_NO_MOUNT", "1")
        execute = {"text": "wrote hello.txt", "files": {"hello.txt": "hi\n", "sub/deep.txt": "d"}}
        harness.script([taskgraph(task("t1")), PLAN, execute, PASS, ACCEPT])
        engine = harness.engine()
        result = engine.start("write hello.txt in harvest mode")

        assert result.state == "completed"
        assert not result.mounted
        assert result.workspace is not None
        # not mounted: nothing lands in the live workspace...
        assert not (result.workspace / "hello.txt").exists()
        # ...but the sbx cp harvest brought the work dir contents to the host
        harvested = harness.state_dir / "runs" / result.run_id / "artifacts"
        assert (harvested / "hello.txt").read_text() == "hi\n"
        assert (harvested / "sub/deep.txt").read_text() == "d"
        assert not engine.store.get_run(result.run_id).mounted
        reports = [e for e in harness.events if e.type == HostEventTypes.RUN_ARTIFACTS]
        assert reports
        assert reports[-1].data == {
            "path": str(harvested),
            "files": 2,
            "mounted": False,
        }

    def test_mounted_run_reports_artifacts_event(self, harness: Harness) -> None:
        execute = {"text": "wrote hello.txt", "files": {"hello.txt": "hi\n"}}
        harness.script([taskgraph(task("t1")), PLAN, execute, PASS, ACCEPT])
        result = harness.engine().start("write hello.txt")
        assert result.state == "completed"
        reports = [e for e in harness.events if e.type == HostEventTypes.RUN_ARTIFACTS]
        assert [e.data["mounted"] for e in reports] == [True]
        assert reports[0].data["files"] == 1
        assert reports[0].data["path"] == str(result.workspace)


class TestDeliverHook:
    """The engine's finalize hook; the git-data flow itself is covered in
    test_deliver.py. deliver_workspace is patched — no GitHub, no network."""

    def deliver_engine(self, harness: Harness) -> LoopEngine:
        return harness.engine(deliver={"repo": "o/r"})

    def test_completed_run_delivers_and_emits_pr(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sbxloop.engine.engine as engine_mod
        from sbxloop.gh.ops import PrRef

        calls: list[dict[str, Any]] = []

        def fake_deliver(ops: Any, repo: str, **kwargs: Any) -> PrRef:
            calls.append({"repo": repo, **kwargs})
            return PrRef(number=3, url="https://github.com/o/r/pull/3")

        monkeypatch.setattr(engine_mod, "deliver_workspace", fake_deliver)
        execute = {"text": "done", "files": {"hello.txt": "hi"}}
        harness.script([taskgraph(task("t1")), PLAN, execute, PASS, ACCEPT])
        result = self.deliver_engine(harness).start("ship it")

        assert result.state == "completed"
        assert len(calls) == 1
        assert calls[0]["repo"] == "o/r"
        assert calls[0]["run_id"] == result.run_id
        assert calls[0]["outcome"] == "ship it"
        assert calls[0]["source_dir"] == result.workspace
        deliver_events = [e for e in harness.events if e.type == HostEventTypes.RUN_DELIVER]
        assert [e.data for e in deliver_events] == [
            {"repo": "o/r", "pr": 3, "url": "https://github.com/o/r/pull/3"}
        ]

    def test_delivery_failure_is_loud_but_nonfatal(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sbxloop.engine.engine as engine_mod
        from sbxloop.errors import DeliveryError

        def fake_deliver(*args: Any, **kwargs: Any) -> Any:
            raise DeliveryError("boom")

        monkeypatch.setattr(engine_mod, "deliver_workspace", fake_deliver)
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = self.deliver_engine(harness).start("ship it")

        assert result.state == "completed"  # the run itself still succeeded
        deliver_events = [e for e in harness.events if e.type == HostEventTypes.RUN_DELIVER]
        assert [e.data for e in deliver_events] == [{"repo": "o/r", "error": "boom"}]

    def test_failed_run_never_delivers(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sbxloop.engine.engine as engine_mod

        def fake_deliver(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("must not deliver a failed run")

        monkeypatch.setattr(engine_mod, "deliver_workspace", fake_deliver)
        # execute passes but validate rejects until replan budget exhausts
        harness.script(
            [taskgraph(task("t1")), PLAN, EXECUTE, PASS, REJECT, PLAN, EXECUTE, PASS, REJECT]
        )
        result = self.deliver_engine(harness).start("doomed")
        assert result.state == "failed"
        assert [e for e in harness.events if e.type == HostEventTypes.RUN_DELIVER] == []

    def test_no_repo_configured_never_delivers(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sbxloop.engine.engine as engine_mod

        def fake_deliver(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("must not deliver without a configured repo")

        monkeypatch.setattr(engine_mod, "deliver_workspace", fake_deliver)
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine().start("plain run")
        assert result.state == "completed"
        assert [e for e in harness.events if e.type == HostEventTypes.RUN_DELIVER] == []
