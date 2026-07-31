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
from typing import Any, ClassVar

import pytest

from sbxloop.config import Config
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.model import DEFAULT_ARTIFACT_EXCLUDES
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

    def engine(self, *, install_workers: bool = False, **config_overrides: Any) -> LoopEngine:
        # Resource guardrails default OFF in the harness: the real worker
        # samples the host filesystem here, so default thresholds would make
        # tests depend on how full the developer's disk is.
        limits = config_overrides.pop("limits", {"disk_warn": 0, "disk_abort": 0, "mem_warn": 0})
        config = Config.model_validate(
            {
                "state_dir": str(self.state_dir),
                "budgets": config_overrides.pop("budgets", {}),
                "limits": limits,
            }
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
            install_workers=install_workers,
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

    def test_full_roster_announced_before_first_task_runs(self, harness: Harness) -> None:
        """Every decomposed task is announced (with title) before any task
        starts, so UIs can show the whole plan as waiting up front instead
        of revealing rows one at a time (#63)."""
        harness.script([taskgraph(task("t1"), task("t2", deps=["t1"])), *HAPPY_TASK, *HAPPY_TASK])
        result = harness.engine().start("two tasks up front")
        assert result.succeeded

        first_start = next(
            i for i, e in enumerate(harness.events) if e.type == HostEventTypes.TASK_START
        )
        roster = [
            (e.data["task_id"], e.data["state"])
            for e in harness.events[:first_start]
            if e.type == HostEventTypes.TASK_STATE and e.data.get("title")
        ]
        assert roster == [("t1", "pending"), ("t2", "pending")]

    def test_agent_messages_carry_phase_persona(self, harness: Harness) -> None:
        """Every agent.message names the persona that produced it, so the
        transcript header says WHO responded (planner, executor, ...), not a
        generic "agent". Echo only emits agent.message for entries with
        "text", so give each scripted phase reply some."""
        harness.script(
            [
                {**taskgraph(task("t1")), "text": "breaking it down"},
                {**PLAN, "text": "here is my plan"},
                EXECUTE,
                {**PASS, "text": "work checks out"},
                {**ACCEPT, "text": "criteria met"},
            ]
        )
        result = harness.engine().start("build the feature")

        assert result.succeeded
        speakers = [e.data.get("agent") for e in harness.events if e.type == "agent.message"]
        assert speakers == ["decomposer", "planner", "executor", "scrutinizer", "validator"]

    def test_default_run_is_github_less(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No [github].repo → only the agent sandbox exists, GH_TOKEN unneeded."""
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine().start("no github anywhere")
        assert result.succeeded
        created = [c[1].removeprefix("--name=") for c in harness.fake_sbx.invocations("create")]
        assert all(name.endswith("-agent") for name in created), created

    def test_configured_github_provisions_ops_sandbox(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine(github={"repo": "owner/repo"}).start("with github")
        assert result.succeeded
        created = [c[1].removeprefix("--name=") for c in harness.fake_sbx.invocations("create")]
        assert any(name.endswith("-github") for name in created), created

    def test_json_missing_reply_retries_instead_of_killing_run(self, harness: Harness) -> None:
        """Field failure (0.5.0): one chatty no-JSON reply on a JSON phase
        raised ExpectedJsonMissing straight through and the run died. It
        must retry once with format feedback instead."""
        chatty = {"text": "Sure! Let me think about the tasks for a moment..."}
        harness.script([chatty, taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine().start("survive a chatty decompose")
        assert result.state == "completed"

    def test_json_missing_twice_still_fails(self, harness: Harness) -> None:
        chatty = {"text": "no json from me"}
        harness.script([chatty, chatty])
        with pytest.raises(WorkerError, match="invalid output twice"):
            harness.engine().start("never json")

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

    def test_verify_exhaustion_spends_replan_with_fresh_plan(self, harness: Harness) -> None:
        # Field failure (rv4zfdb1m): the executor cannot edit verify_commands,
        # so a plan whose commands disagree with where the work landed burned
        # every revision and killed the task. Exhaustion from verify failures
        # must replan — a fresh plan regenerates the commands.
        bad_plan = {
            "json": {"steps": ["do"], "expected_artifacts": [], "verify_commands": ["exit 1"]}
        }
        harness.script(
            [
                taskgraph(task("t1")),
                bad_plan,
                EXECUTE,
                PASS,
                EXECUTE,
                PASS,
                EXECUTE,
                PASS,  # revisions exhausted -> replan instead of failed
                PLAN,  # fresh plan drops the broken command
                EXECUTE,
                PASS,
                ACCEPT,
            ]
        )
        result = harness.engine().start("verify unsticks via replan")
        assert result.state == "completed"
        assert result.tasks[0].state == "done"
        assert result.tasks[0].replans == 1

    def test_verify_failure_exhausts_revisions_and_replans(self, harness: Harness) -> None:
        # A spec-level verify command no plan can fix: revisions burn, one
        # replan burns, then the task fails — the loop is bounded.
        cycle = [EXECUTE, PASS, EXECUTE, PASS, EXECUTE, PASS]
        harness.script([taskgraph(task("t1", verify=["exit 1"])), PLAN, *cycle, PLAN, *cycle])
        result = harness.engine().start("verify never passes")
        assert result.state == "failed"
        assert result.tasks[0].state == "failed"
        assert result.tasks[0].replans == 1
        assert result.tasks[0].revisions == 3
        assert "verify command failed" in result.tasks[0].last_feedback

    def test_verify_exhaustion_without_replan_budget_fails(self, harness: Harness) -> None:
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
        result = harness.engine(budgets={"max_replans_per_task": 0}).start("verify never passes")
        assert result.state == "failed"
        assert result.tasks[0].state == "failed"
        assert result.tasks[0].revisions == 3

    def test_verify_failure_surfaces_in_event_stream(self, harness: Harness) -> None:
        # Field failure (rv4zfdb1m): the transcript jumped verifying -> failed
        # with the failing command visible only via sqlite on phase_attempts.
        harness.script(
            [
                taskgraph(task("t1", verify=["exit 1", "exit 2"])),
                PLAN,
                EXECUTE,
                PASS,
                EXECUTE,
                PASS,
                EXECUTE,
                PASS,
            ]
        )
        harness.engine(budgets={"max_replans_per_task": 0}).start("loud verify failure")
        fails = [e for e in harness.events if e.type == HostEventTypes.PHASE_END]
        assert fails, "verify failure emitted no phase.end event"
        first = fails[0].data
        assert first["task_id"] == "t1"
        assert first["phase"] == "verify"
        assert first["status"] == "failed"
        assert "verify command failed" in first["message"]
        assert "(+1 more)" in first["message"]  # both failing commands counted


DEGRADED_HEALTH = {"tool_failures": {"grep": 3, "glob": 1}, "permission_denials": {"shell": 1}}
DEGRADED_PASS = {"json": {"verdict": "pass"}, "health": DEGRADED_HEALTH}
DEGRADED_ACCEPT = {"json": {"verdict": "accept"}, "health": DEGRADED_HEALTH}


class TestDegradedCritic:
    """A critic that lost its tooling must not green-light work (#123)."""

    def test_degraded_pass_twice_is_downgraded_to_revise(self, harness: Harness) -> None:
        # Two blind passes -> downgrade -> one revision -> healthy pass.
        harness.script(
            [
                taskgraph(task("t1")),
                PLAN,
                EXECUTE,
                DEGRADED_PASS,
                DEGRADED_PASS,  # the guard's one re-run, still blind
                EXECUTE,
                PASS,
                ACCEPT,
            ]
        )
        engine = harness.engine()
        result = engine.start("blind critic must not pass")
        assert result.state == "completed"
        assert result.tasks[0].revisions == 1

        # The downgrade is persisted on the phase row: status revise, with
        # the session's tooling health and the downgraded marker.
        rows = [r for r in engine.store.phase_attempts(result.run_id) if r["phase"] == "scrutinize"]
        first = json.loads(rows[0]["output_json"])
        assert rows[0]["status"] == "revise"
        assert first["downgraded"] is True
        assert first["tooling_health"]["tool_failures"] == {"grep": 3, "glob": 1}
        assert any("degraded tooling" in i["detail"] for i in first["issues"])
        # ...and the healthy final pass carries neither marker.
        last = json.loads(rows[-1]["output_json"])
        assert rows[-1]["status"] == "pass"
        assert "tooling_health" not in last and "downgraded" not in last

        # The downgrade is in the live stream, not only in sqlite.
        degraded = [
            e
            for e in harness.events
            if e.type == HostEventTypes.PHASE_END and e.data.get("status") == "degraded"
        ]
        assert len(degraded) == 1
        assert degraded[0].data["phase"] == "scrutinize"
        assert "grep x3" in degraded[0].data["message"]

    def test_degraded_pass_then_healthy_rerun_is_trusted(self, harness: Harness) -> None:
        # A transient tool crash gets its second chance: the re-run comes
        # back healthy and clean, so no revision is spent.
        harness.script([taskgraph(task("t1")), PLAN, EXECUTE, DEGRADED_PASS, PASS, ACCEPT])
        result = harness.engine().start("transient tool crash")
        assert result.state == "completed"
        assert result.tasks[0].revisions == 0

    def test_degraded_accept_twice_is_downgraded_to_reject(self, harness: Harness) -> None:
        harness.script(
            [
                taskgraph(task("t1")),
                PLAN,
                EXECUTE,
                PASS,
                DEGRADED_ACCEPT,
                DEGRADED_ACCEPT,  # re-run, still blind -> reject -> replan
                PLAN,
                EXECUTE,
                PASS,
                ACCEPT,
            ]
        )
        result = harness.engine().start("blind validator must not accept")
        assert result.state == "completed"
        assert result.tasks[0].replans == 1
        assert result.tasks[0].state == "done"


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

    @pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
    def test_cancel_refuses_terminal_runs(self, harness: Harness, state: str) -> None:
        # Rewriting a finished run to cancelled would corrupt history.
        engine = harness.engine()
        engine.store.create_run("rterm", "x")
        engine.store.set_run_state("rterm", state)  # type: ignore[arg-type]
        with pytest.raises(StateError, match="nothing to cancel"):
            engine.cancel("rterm")
        assert engine.store.get_run("rterm").state == state

    def test_request_cancel_stops_at_phase_boundary_and_stays_resumable(
        self, harness: Harness
    ) -> None:
        # The in-process cancel (TUI Ctrl-C) stops the engine at the next
        # phase boundary without marking the run cancelled — it must remain
        # resumable, exactly like any other interrupted run.
        harness.script([taskgraph(task("t1"))])
        engine = harness.engine()
        engine.request_cancel()
        with pytest.raises(StateError, match="interrupted"):
            engine.start("interrupt me")
        run_id = engine.store.list_runs()[0].run_id
        assert engine.store.get_run(run_id).state == "running"
        assert harness.sandboxes_left() == []  # pair context cleaned up


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

    def test_resume_into_validating_carries_verify_evidence(self, harness: Harness) -> None:
        # #61: a task checkpointed 'validating' resumes straight into
        # VALIDATE in a fresh process. The judge must see the persisted
        # verify transcript, not a "(verification not run)" placeholder.
        harness.script(
            [
                taskgraph(task("t1", verify=["echo verify-evidence-61"])),
                PLAN,
                EXECUTE,
                PASS,
                {"fail": "killed before validate"},
            ]
        )
        engine = harness.engine()
        with pytest.raises(WorkerError, match="killed before validate"):
            engine.start("die in validate")
        run_id = engine.store.list_runs()[0].run_id
        assert engine.store.get_tasks(run_id)[0].state == "validating"

        harness.script([ACCEPT])
        engine2 = harness.engine(keep_sandboxes=True)
        result = engine2.resume(run_id)
        assert result.state == "completed"
        # evidence came from the stored verify attempt - verify did not re-run
        phases = [row["phase"] for row in engine2.store.phase_attempts(run_id)]
        assert phases.count("verify") == 1
        # the resumed validate job's prompt carried the real transcript
        fs = harness.fake_sbx.sandbox_fs(f"sbxloop-{run_id}-agent")
        jobs = [json.loads(p.read_text()) for p in (fs / "home/agent/.sbxloop/jobs").iterdir()]
        (validate_prompt,) = [j["prompt"] for j in jobs if j.get("prompt")]
        assert "verify-evidence-61" in validate_prompt
        assert "(verification not run)" not in validate_prompt

    def test_resume_validating_without_verify_row_reruns_verify(self, harness: Harness) -> None:
        # Defensive path for pre-upgrade checkpoints whose verify rows carry
        # no transcript: rewind to verifying (mechanical, idempotent) instead
        # of judging evidence-free.
        from sbxloop.engine.model import PlanModel, TaskSpec

        engine = harness.engine()
        engine.store.create_run("r61", "old checkpoint")
        engine.store.save_tasks("r61", [TaskSpec.model_validate(task("t1"))])
        record = engine.store.get_tasks("r61")[0]
        record.plan = PlanModel(steps=["do"], expected_artifacts=[], verify_commands=[])
        record.state = "validating"
        engine.store.update_task("r61", record)
        engine.store.set_run_state("r61", "running")

        harness.script([ACCEPT])
        result = engine.resume("r61")
        assert result.state == "completed"
        phases = [row["phase"] for row in engine.store.phase_attempts("r61")]
        assert phases == ["verify", "validate"]

    def test_no_class_level_verify_state(self) -> None:
        # #61: the per-run verify transcript must not live as mutable
        # class-level state on PhaseRunner.
        from sbxloop.engine.phases import PhaseRunner

        assert not hasattr(PhaseRunner, "_last_verify_results")

    def test_resume_completed_run_refused(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        engine = harness.engine()
        result = engine.start("finish it")
        with pytest.raises(StateError, match="only unfinished runs"):
            engine.resume(result.run_id)

    def _crashed_run(self, harness: Harness, **config_overrides: Any) -> str:
        """Start a run that crashes during t1's execute; returns its run id."""
        harness.script([taskgraph(task("t1")), PLAN, {"fail": "sandbox exploded"}])
        engine = harness.engine(**config_overrides)
        with pytest.raises(WorkerError, match="sandbox exploded"):
            engine.start("crashy run")
        return engine.store.list_runs()[0].run_id

    def test_resume_uses_persisted_config_not_current(self, harness: Harness) -> None:
        run_id = self._crashed_run(harness, budgets={"max_revisions_per_task": 2})

        # Resume under a *tighter* on-disk config (zero revisions). The
        # persisted budget must govern: one revise round still completes.
        harness.script([EXECUTE, REVISE, EXECUTE, PASS, ACCEPT])
        engine2 = harness.engine(budgets={"max_revisions_per_task": 0})
        result = engine2.resume(run_id)
        assert result.state == "completed"
        assert engine2.config.budgets.max_revisions_per_task == 2

        drift = [e for e in harness.events if e.type == HostEventTypes.RUN_CONFIG_DRIFT]
        assert len(drift) == 1
        assert "budgets.max_revisions_per_task" in drift[0].data["message"]

    def test_resume_with_unchanged_config_reports_no_drift(self, harness: Harness) -> None:
        run_id = self._crashed_run(harness)
        harness.script([EXECUTE, PASS, ACCEPT])
        result = harness.engine().resume(run_id)
        assert result.state == "completed"
        assert HostEventTypes.RUN_CONFIG_DRIFT not in harness.event_types()

    def test_resume_honors_current_keep_toggles(self, harness: Harness) -> None:
        # keep_sandboxes/keep_on_failure are resume-time operator intent
        # (debug this attempt), not run identity: the CURRENT config wins for
        # them, with no drift warning — unlike everything else rehydrated.
        run_id = self._crashed_run(harness)
        harness.script([EXECUTE, PASS, ACCEPT])
        engine2 = harness.engine(keep_sandboxes=True)
        result = engine2.resume(run_id)
        assert result.state == "completed"
        assert result.kept_sandboxes  # this attempt's pair was kept
        assert HostEventTypes.RUN_CONFIG_DRIFT not in harness.event_types()

    def test_resume_cannot_relocate_workspace_via_config(self, harness: Harness) -> None:
        run_id = self._crashed_run(harness)
        engine = harness.engine()
        original = engine.store.get_run(run_id).workspace
        assert original is not None

        # The current config now points the workspace somewhere else — the
        # run must continue in its recorded workspace, not a fresh empty one.
        elsewhere = harness.tmp_path / "elsewhere"
        harness.script([EXECUTE, PASS, ACCEPT])
        engine2 = harness.engine(sandbox={"workspace": str(elsewhere)})
        result = engine2.resume(run_id)
        assert result.state == "completed"
        assert result.workspace == original
        assert engine2.store.get_run(run_id).workspace == original
        assert not elsewhere.exists()
        drift = [e for e in harness.events if e.type == HostEventTypes.RUN_CONFIG_DRIFT]
        assert len(drift) == 1
        assert "sandbox.workspace" in drift[0].data["message"]

    def test_resume_legacy_row_still_pins_workspace_from_run_row(self, harness: Harness) -> None:
        # Rows created before config persistence carry config_json '{}'.
        # Rehydration has nothing to adopt, but the workspace must still
        # come from the runs table, never be recomputed from current config.
        run_id = self._crashed_run(harness)
        engine = harness.engine()
        original = engine.store.get_run(run_id).workspace
        engine.store._conn.execute("UPDATE runs SET config_json = '{}' WHERE run_id = ?", (run_id,))
        engine.store._conn.commit()

        elsewhere = harness.tmp_path / "elsewhere"
        harness.script([EXECUTE, PASS, ACCEPT])
        engine2 = harness.engine(sandbox={"workspace": str(elsewhere)})
        result = engine2.resume(run_id)
        assert result.state == "completed"
        assert result.workspace == original
        assert not elsewhere.exists()


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


PLAN_NPM = {
    "json": {
        "steps": ["npm install"],
        "expected_artifacts": [],
        "verify_commands": [],
        "egress": [{"domain": "registry.npmjs.org", "reason": "npm install"}],
    }
}

# npm is a well-known registry (in-bounds without config), so out-of-bounds
# tests need a domain no built-in tier covers.
PLAN_SAAS_API = {
    "json": {
        "steps": ["call the API"],
        "expected_artifacts": [],
        "verify_commands": [],
        "egress": [{"domain": "api.example-saas.com", "reason": "fetch data"}],
    }
}


class TestPlanEgress:
    """Plan-declared egress: bounded by [policy], granted just before EXECUTE."""

    def test_in_bounds_egress_granted_and_event_logged(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), PLAN_NPM, EXECUTE, PASS, ACCEPT])
        result = harness.engine(policy={"allow": ["registry.npmjs.org"]}).start("npm task")
        assert result.state == "completed"
        agent = f"sbxloop-{result.run_id}-agent"
        assert [
            "allow",
            "network",
            "registry.npmjs.org",
            "--sandbox",
            agent,
        ] in harness.fake_sbx.policies()
        (event,) = [e for e in harness.events if e.type == "policy.allow"]
        assert event.data["domain"] == "registry.npmjs.org"
        assert event.data["reason"] == "npm install"
        assert event.data["task_id"] == "t1"

    def test_well_known_registry_granted_without_policy_config(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), PLAN_NPM, EXECUTE, PASS, ACCEPT])
        result = harness.engine().start("npm task, default policy")
        assert result.state == "completed"
        agent = f"sbxloop-{result.run_id}-agent"
        assert [
            "allow",
            "network",
            "registry.npmjs.org",
            "--sandbox",
            agent,
        ] in harness.fake_sbx.policies()

    def test_out_of_bounds_egress_rejected_then_retried(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), PLAN_SAAS_API, PLAN, EXECUTE, PASS, ACCEPT])
        result = harness.engine().start("saas api denied")
        assert result.state == "completed"
        grants = [c for c in harness.fake_sbx.policies() if "api.example-saas.com" in c]
        assert grants == []

    def test_out_of_bounds_egress_twice_fails(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), PLAN_SAAS_API, PLAN_SAAS_API])
        with pytest.raises(WorkerError, match="invalid output twice"):
            harness.engine().start("insists on the saas api")


class TestKeepOnFailure:
    def test_failed_run_keeps_pair_and_marks_db(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), PLAN, EXECUTE, REVISE])
        engine = harness.engine(keep_on_failure=True, budgets={"max_revisions_per_task": 0})
        result = engine.start("doomed outcome")

        assert result.state == "failed"
        assert result.kept_sandboxes == [f"sbxloop-{result.run_id}-agent"]
        assert harness.sandboxes_left() == result.kept_sandboxes
        assert engine.store.get_run(result.run_id).kept_reason == "debug"
        keep_events = [e for e in harness.events if e.type == "run.keep"]
        assert len(keep_events) == 1
        assert "sbxloop shell" in str(keep_events[0].data.get("message"))

    def test_infra_failure_keeps_pair(self, harness: Harness) -> None:
        # Bad decompose output twice -> WorkerError: the exception path must
        # keep the evidence too (install/worker crashes are diagnosed
        # in-sandbox).
        bad = {"json": {"tasks": [{"id": "t1"}]}}
        harness.script([bad, bad])
        engine = harness.engine(keep_on_failure=True)
        with pytest.raises(WorkerError):
            engine.start("impossible")
        assert len(harness.sandboxes_left()) == 1
        run = engine.store.list_runs()[0]
        assert run.kept_reason == "debug"
        assert "run.keep" in harness.event_types()

    def test_completed_run_still_cleans_up(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        engine = harness.engine(keep_on_failure=True)
        result = engine.start("all fine")
        assert result.state == "completed"
        assert result.kept_sandboxes == []
        assert harness.sandboxes_left() == []
        assert engine.store.get_run(result.run_id).kept_reason is None
        assert "run.keep" not in harness.event_types()

    def test_keep_sandboxes_marks_manual(self, harness: Harness) -> None:
        # `sandbox prune` must understand manually kept pairs as well.
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        engine = harness.engine(keep_sandboxes=True)
        result = engine.start("kept run")
        assert engine.store.get_run(result.run_id).kept_reason == "manual"
        assert result.kept_sandboxes == [f"sbxloop-{result.run_id}-agent"]


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
        assert "excluded" not in reports[0].data  # nothing was excluded

    def test_artifacts_event_surfaces_exclusions(self, harness: Harness) -> None:
        """Dot-path artifacts count as files; only the denylist (.git) is
        excluded, and the exclusion is visible in the event (#67)."""
        execute = {
            "text": "added CI",
            "files": {
                ".github/workflows/ci.yml": "on: push\n",
                ".gitignore": "*.pyc\n",
                ".git/HEAD": "ref\n",
            },
        }
        harness.script([taskgraph(task("t1")), PLAN, execute, PASS, ACCEPT])
        result = harness.engine().start("add CI")
        assert result.state == "completed"
        reports = [e for e in harness.events if e.type == HostEventTypes.RUN_ARTIFACTS]
        assert reports[0].data["files"] == 2  # .github/workflows/ci.yml, .gitignore
        assert reports[0].data["excluded"] == {".git": 1}

    def test_harvest_respects_excludes_at_transfer_time(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Excluded dirs (.git) must not appear in the harvested artifacts
        even in unmounted mode — they must be stripped by tar inside the VM,
        not just at listing/delivery time (#128)."""
        monkeypatch.setenv("SBX_FAKE_NO_MOUNT", "1")
        execute = {
            "text": "wrote files",
            "files": {
                "hello.txt": "hi\n",
                ".git/HEAD": "ref: refs/heads/main\n",
                ".git/objects/abc": "blob",
            },
        }
        harness.script([taskgraph(task("t1")), PLAN, execute, PASS, ACCEPT])
        result = harness.engine().start("write files in harvest mode")

        assert result.state == "completed"
        harvested = harness.state_dir / "runs" / result.run_id / "artifacts"
        # Regular file must arrive
        assert (harvested / "hello.txt").read_text() == "hi\n"
        # .git must be absent — tar excluded it before the copy
        assert not (harvested / ".git").exists()

    def test_harvest_strips_dependency_trees_before_the_copy(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of excluding build output by default is that an
        unmounted run never pays to copy it back — so it must be stripped by
        tar in the VM, not merely filtered out of the host-side listing."""
        monkeypatch.setenv("SBX_FAKE_NO_MOUNT", "1")
        execute = {
            "text": "built it",
            "files": {
                "package.json": '{"name":"app"}\n',
                "node_modules/left-pad/index.js": "module.exports = 1\n",
                "target/release/app": "ELF\n",
                "src/__pycache__/main.cpython-312.pyc": "\x00\n",
            },
        }
        harness.script([taskgraph(task("t1")), PLAN, execute, PASS, ACCEPT])
        result = harness.engine().start("build the app")

        assert result.state == "completed"
        harvested = harness.state_dir / "runs" / result.run_id / "artifacts"
        assert (harvested / "package.json").exists()
        assert not (harvested / "node_modules").exists()
        assert not (harvested / "target").exists()
        assert not (harvested / "src" / "__pycache__").exists()

    def test_harvest_mode_final_skips_per_task_harvest(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With harvest_mode='final', mid-run copies are skipped; only the
        authoritative sweep at finalize runs.  The result is still complete
        and artifacts are delivered — just with fewer sbx exec tar calls."""
        monkeypatch.setenv("SBX_FAKE_NO_MOUNT", "1")
        execute = {"text": "wrote hello.txt", "files": {"hello.txt": "hi\n"}}
        harness.script(
            [
                taskgraph(task("t1"), task("t2", deps=["t1"])),
                PLAN,
                execute,
                PASS,
                ACCEPT,
                PLAN,
                execute,
                PASS,
                ACCEPT,
            ]
        )
        engine = harness.engine(artifacts={"harvest_mode": "final"})
        result = engine.start("two-task harvest-final run")

        assert result.state == "completed"
        harvested = harness.state_dir / "runs" / result.run_id / "artifacts"
        assert (harvested / "hello.txt").read_text() == "hi\n"

        # Per-task mode would run tar once per task + once at finalize.
        # Final mode runs tar only at finalize (1 call).
        tar_calls = [inv for inv in harness.fake_sbx.invocations("exec") if "tar" in inv]
        assert len(tar_calls) == 1


class TestDeliverHook:
    """The engine's finalize hook; the git-data flow itself is covered in
    test_deliver.py. deliver_workspace is patched — no GitHub, no network."""

    def deliver_engine(self, harness: Harness) -> LoopEngine:
        return harness.engine(github={"repo": "o/r", "deliver": True})

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
        # config default threaded through
        assert calls[0]["exclude"] == list(DEFAULT_ARTIFACT_EXCLUDES)
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

    def test_delivery_infra_error_never_fails_a_completed_run(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WorkerError/SbxError from the op jobs (not just DeliveryError)
        must stay inside _deliver: the run already succeeded (#59)."""
        import sbxloop.engine.engine as engine_mod
        from sbxloop.errors import SbxError, WorkerTimeoutError

        for exc in (
            WorkerError("worker for job j1 produced no result file"),
            WorkerTimeoutError("job j1 exceeded 120s"),
            SbxError("sbx command failed: cp", stderr="daemon exploded"),
        ):

            def fake_deliver(*args: Any, _exc: Exception = exc, **kwargs: Any) -> Any:
                raise _exc

            monkeypatch.setattr(engine_mod, "deliver_workspace", fake_deliver)
            harness.script([taskgraph(task("t1")), *HAPPY_TASK])
            harness.events.clear()
            result = self.deliver_engine(harness).start(f"ship {type(exc).__name__}")

            assert result.state == "completed", type(exc).__name__
            deliver_events = [e for e in harness.events if e.type == HostEventTypes.RUN_DELIVER]
            assert len(deliver_events) == 1
            assert str(exc) in deliver_events[0].data["error"]

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


class TestPrebakedTemplate:
    """[sandbox].template + a baked template: install verifies and skips the
    ladder, and the run emits sandbox.prebaked telemetry."""

    REF = "sbxloop-baked:latest"

    def seed_template(self, harness: Harness, *, version: str | None = None) -> None:
        """Bake a template the fake-sbx way: seed sandbox fs with a manifest
        whose interpreter is the test python, save, remove."""
        import sbxloop
        from sbxloop.sbx.models import SandboxSpec
        from sbxloop.sbx.sandbox import Sandbox

        cli = SbxCLI(binary=str(harness.fake_sbx.binary))
        workspace = harness.tmp_path / "seed-ws"
        workspace.mkdir(exist_ok=True)
        cli.create(SandboxSpec(name="seed", role="agent", workspace=workspace))
        manifest = {
            "worker_version": version or sbxloop.__version__,
            "python": sys.executable,
            "runtime_cached": True,
            "baked_at": 0.0,
        }
        Sandbox(cli, "seed").write_text("/home/agent/.sbxloop/bake.json", json.dumps(manifest))
        cli.template_save("seed", self.REF)
        cli.rm("seed")

    def test_prebaked_run_skips_install_and_emits_event(self, harness: Harness) -> None:
        self.seed_template(harness)
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine(install_workers=True, sandbox={"template": self.REF}).start(
            "use the baked template"
        )

        assert result.state == "completed"
        # provisioning created the agent sandbox FROM the template
        creates = harness.fake_sbx.invocations("create")
        assert any(self.REF in arg for c in creates for arg in c)
        # verified prebaked → the whole install ladder was skipped
        joined = [" ".join(c) for c in harness.fake_sbx.invocations("exec")]
        assert not [j for j in joined if "-m venv" in j or "pip install" in j or "apt-get" in j]
        prebaked = [e for e in harness.events if e.type == HostEventTypes.SANDBOX_PREBAKED]
        assert len(prebaked) == 1
        assert prebaked[0].data["prebaked"] is True
        assert prebaked[0].data["template"] == self.REF

    def test_prebaked_pair_installs_both_and_emits_two_events(self, harness: Harness) -> None:
        """With [github].repo the pair installs concurrently (#127); both
        sandboxes verify their baked worker and each emits its own event."""
        self.seed_template(harness)
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine(
            install_workers=True,
            sandbox={"template": self.REF},
            github={"repo": "owner/repo"},
        ).start("use the baked template with github")

        assert result.state == "completed"
        joined = [" ".join(c) for c in harness.fake_sbx.invocations("exec")]
        assert not [j for j in joined if "-m venv" in j or "pip install" in j or "apt-get" in j]
        prebaked = [e for e in harness.events if e.type == HostEventTypes.SANDBOX_PREBAKED]
        assert len(prebaked) == 2
        assert all(e.data["prebaked"] is True for e in prebaked)

    def test_no_template_emits_no_prebaked_event(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine().start("plain run")
        assert result.state == "completed"
        assert [e for e in harness.events if e.type == HostEventTypes.SANDBOX_PREBAKED] == []


class TestResourceGuardrail:
    """[limits] guardrails end-to-end: the real worker samples the real
    filesystem, so a tiny threshold reliably classifies warn/abort."""

    def test_disk_abort_fails_task_with_diagnosis(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1"))])
        # disk_warn=0 (disabled) + microscopic disk_abort: any real fs
        # sample crosses it, so the decompose job's baseline sample puts the
        # agent sandbox at level=abort before the task loop starts.
        result = harness.engine(limits={"disk_warn": 0, "disk_abort": 0.1}).start("doomed")
        assert result.state == "failed"
        assert [t.state for t in result.tasks] == ["failed"]
        assert "sandbox disk exhausted" in (result.tasks[0].last_feedback or "")

    def test_disk_warn_never_fails_tasks(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine(limits={"disk_warn": 0.1, "disk_abort": 95.0}).start("warned")
        assert result.succeeded
        warnings = [e for e in harness.events if e.type == "sandbox.resources_warning"]
        assert warnings and warnings[0].data["role"] == "agent"

    def test_artifacts_event_carries_disk_pressure_note(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        harness.engine(limits={"disk_warn": 0.1, "disk_abort": 95.0}).start("pressured")
        artifacts = [e for e in harness.events if e.type == HostEventTypes.RUN_ARTIFACTS]
        assert artifacts
        assert artifacts[-1].data.get("resources_level") == "warn"
        assert artifacts[-1].data.get("disk_used_pct") is not None

    def test_resource_events_are_persisted_for_post_hoc_queries(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        engine = harness.engine(limits={"disk_warn": 0.1, "disk_abort": 95.0})
        result = engine.start("queryable")
        rows = engine.store.events(result.run_id, type_prefix="sandbox.resources")
        assert rows, "sandbox.resources events were not persisted"


class TestGithubReporting:
    """Wiring for --report: the engine must open the tracking issue after
    the github sandbox exists, mirror task ends, and post the summary while
    the sandbox is still alive (#58). GithubOps is patched at the engine
    module — no GitHub, no network."""

    class RecordingOps:
        instances: ClassVar[list[TestGithubReporting.RecordingOps]] = []

        def __init__(self, client: Any, run_id: str, **kwargs: Any) -> None:
            self.run_id = run_id
            self.created: list[str] = []
            self.comments: list[str] = []
            type(self).instances.append(self)

        def search_issues(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
            return []

        def issue_create(self, repo: str, title: str, body: str = "", labels: Any = None) -> Any:
            from sbxloop.gh.ops import IssueRef

            self.created.append(title)
            return IssueRef(number=11, url="https://x/11")

        def issue_comment(self, repo: str, number: int, body: str) -> str:
            self.comments.append(body)
            return "https://c"

    @pytest.fixture(autouse=True)
    def _patch_ops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sbxloop.engine.engine as engine_mod

        self.RecordingOps.instances = []
        monkeypatch.setattr(engine_mod, "GithubOps", self.RecordingOps)

    def test_report_opens_comments_and_summarizes(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine(github={"repo": "o/r", "report": True}).start("do it")

        assert result.state == "completed"
        assert len(self.RecordingOps.instances) == 1
        ops = self.RecordingOps.instances[0]
        assert ops.created == [f"sbxloop run {result.run_id}"]
        # one comment per task end + the final summary
        assert len(ops.comments) == 2
        assert "✅ `t1`" in ops.comments[0]
        assert "finished: **completed**" in ops.comments[1]

    def test_failed_run_summary_reports_failed(self, harness: Harness) -> None:
        harness.script(
            [taskgraph(task("t1")), PLAN, EXECUTE, PASS, REJECT, PLAN, EXECUTE, PASS, REJECT]
        )
        result = harness.engine(github={"repo": "o/r", "report": True}).start("doomed")

        assert result.state == "failed"
        ops = self.RecordingOps.instances[0]
        assert "❌ `t1`" in ops.comments[0]
        assert "finished: **failed**" in ops.comments[-1]

    def test_report_disabled_touches_nothing(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine(github={"repo": "o/r"}).start("quiet")
        assert result.state == "completed"
        assert self.RecordingOps.instances == []


class TestInteractiveChat:
    """post_user_message → STEER at the next phase boundary → applied verdict."""

    STEER_CONTINUE: ClassVar[dict[str, Any]] = {
        "json": {"reply": "all on track", "action": "continue"}
    }
    STEER_RUN: ClassVar[dict[str, Any]] = {
        "json": {
            "reply": "switching direction",
            "action": "steer_run",
            "guidance": "use postgres everywhere",
        }
    }
    STEER_TASK: ClassVar[dict[str, Any]] = {
        "json": {
            "reply": "re-planning this task",
            "action": "steer_task",
            "guidance": "write it in Go instead",
        }
    }

    def test_continue_replies_without_changing_course(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), self.STEER_CONTINUE, *HAPPY_TASK])
        engine = harness.engine()
        message_id = engine.post_user_message("how is it going?")
        result = engine.start("build the feature")

        assert result.state == "completed"
        assert result.tasks[0].revisions == 0
        assert result.tasks[0].replans == 0
        chat_message = next(e for e in harness.events if e.type == "chat.message")
        assert chat_message.data["text"] == "how is it going?"
        assert chat_message.data["message_id"] == message_id
        reply = next(e for e in harness.events if e.type == "chat.reply")
        assert reply.data["reply"] == "all on track"
        assert reply.data["action"] == "continue"
        assert reply.data["message_id"] == message_id
        assert "chat.action" not in harness.event_types()
        attempts = engine.store.phase_attempts(result.run_id)
        steer = [a for a in attempts if a["phase"] == "steer"]
        assert len(steer) == 1
        assert steer[0]["status"] == "continue"
        assert steer[0]["task_id"] == "t1"

    def test_steer_run_persists_standing_guidance(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), self.STEER_RUN, *HAPPY_TASK])
        engine = harness.engine()
        engine.post_user_message("please use postgres")
        result = engine.start("build the feature")

        assert result.state == "completed"
        assert engine.store.get_run_guidance(result.run_id) == ["use postgres everywhere"]
        action = next(e for e in harness.events if e.type == "chat.action")
        assert action.data["action"] == "steer_run"
        assert "use postgres everywhere" in action.data["message"]
        # chat events are persisted like everything else
        persisted = [
            event.type
            for _, event in engine.store.events(result.run_id)
            if event.type.startswith("chat.")
        ]
        assert persisted == ["chat.message", "chat.action", "chat.reply"]

    def test_steer_task_replans_without_spending_budgets(self, harness: Harness) -> None:
        # Message absorbed at the first boundary (task in `planning`): the
        # steer verdict re-plans it with the guidance as feedback.
        harness.script([taskgraph(task("t1")), self.STEER_TASK, *HAPPY_TASK])
        engine = harness.engine()
        engine.post_user_message("do it in Go")
        result = engine.start("build the feature")

        assert result.state == "completed"
        assert result.tasks[0].replans == 0
        assert result.tasks[0].revisions == 0
        action = next(e for e in harness.events if e.type == "chat.action")
        assert action.data["action"] == "steer_task"
        assert action.data["task_id"] == "t1"
        attempts = engine.store.phase_attempts(result.run_id)
        steer = next(a for a in attempts if a["phase"] == "steer")
        assert steer["status"] == "steer_task"
        output = json.loads(steer["output_json"])
        assert output["applied"] == "steer_task"
        assert output["message"] == "do it in Go"

    def test_steer_failure_never_fails_the_run(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), {"fail": "backend exploded"}, *HAPPY_TASK])
        engine = harness.engine()
        engine.post_user_message("hello?")
        result = engine.start("build the feature")

        assert result.state == "completed"
        reply = next(e for e in harness.events if e.type == "chat.reply")
        assert "error" in reply.data
        attempts = engine.store.phase_attempts(result.run_id)
        steer = next(a for a in attempts if a["phase"] == "steer")
        assert steer["status"] == "error"

    def test_invalid_steer_verdict_is_retried(self, harness: Harness) -> None:
        # steer_task without guidance fails SteerVerdict validation; the
        # retry consumes the next script entry.
        bad = {"json": {"reply": "ok", "action": "steer_task", "guidance": ""}}
        harness.script([taskgraph(task("t1")), bad, self.STEER_CONTINUE, *HAPPY_TASK])
        engine = harness.engine()
        engine.post_user_message("tweak it")
        result = engine.start("build the feature")

        assert result.state == "completed"
        reply = next(e for e in harness.events if e.type == "chat.reply")
        assert reply.data["action"] == "continue"

    def test_multiple_messages_processed_fifo(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), self.STEER_CONTINUE, self.STEER_RUN, *HAPPY_TASK])
        engine = harness.engine()
        first = engine.post_user_message("first")
        second = engine.post_user_message("second")
        result = engine.start("build the feature")

        assert result.state == "completed"
        messages = [e for e in harness.events if e.type == "chat.message"]
        assert [m.data["message_id"] for m in messages] == [first, second]
        assert engine.store.get_run_guidance(result.run_id) == ["use postgres everywhere"]

    def test_resume_replays_persisted_guidance_into_prompts(self, harness: Harness) -> None:
        from sbxloop.engine.phases import PhaseRunner

        # Run 1: steer_run lands guidance, then the task fails its only
        # scrutiny (max_revisions=0) and the run fails — a resumable state.
        harness.script([taskgraph(task("t1")), self.STEER_RUN, PLAN, EXECUTE, REVISE])
        engine = harness.engine(budgets={"max_revisions_per_task": 0})
        engine.post_user_message("use postgres")
        result = engine.start("build the feature")
        assert result.state == "failed"
        assert engine.store.get_run_guidance(result.run_id) == ["use postgres everywhere"]

        # Run 2 (resume): the fresh PhaseRunner must be rehydrated with the
        # persisted guidance before any phase runs.
        captured: dict[str, PhaseRunner] = {}
        original_init = PhaseRunner.__init__

        def spy_init(self: PhaseRunner, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            captured["phases"] = self

        harness.monkeypatch.setattr(PhaseRunner, "__init__", spy_init)
        harness.script([])
        resumed = harness.engine(budgets={"max_revisions_per_task": 0}).resume(result.run_id)
        assert resumed.state == "failed"  # all tasks already terminal
        assert captured["phases"].user_guidance == ["use postgres everywhere"]

    def test_steer_task_with_no_live_task_downgrades_to_steer_run(self, harness: Harness) -> None:
        from sbxloop.engine.model import SteerVerdict
        from sbxloop.engine.phases import PhaseRunner

        engine = harness.engine()
        engine.store.create_run("r1chat", "outcome", "{}")
        phases = PhaseRunner(None, engine.config, "r1chat", "outcome")  # type: ignore[arg-type]
        verdict = SteerVerdict(reply="ok", action="steer_task", guidance="switch approach")

        applied = engine._apply_steer("r1chat", None, verdict, phases)

        assert applied == "steer_run"
        assert engine.store.get_run_guidance("r1chat") == ["switch approach"]
        assert phases.user_guidance == ["switch approach"]
