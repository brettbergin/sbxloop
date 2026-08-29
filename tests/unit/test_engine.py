"""End-to-end LoopEngine tests: fake sbx + real worker + scripted echo backend.

Every agent phase (decompose, build, review, steer) consumes the next
scripted echo response in order, so a whole run is scripted as a list — one
BUILD entry per task attempt, one REVIEW entry per review round. Shell jobs
(the mechanical verify commands and the project gate) run for real inside
the fake sandbox fs and consume no script entries: a failing verify is
scripted by giving the task a failing command, and a revision loop by a
command that only passes once a later BUILD writes the file it looks for.

GitHub is a :class:`tests.fakes.fake_github.FakeGithub` handed in through
the engine's ``github_ops`` seam: delivery, the posted review, CI and the
merge all go through it, so a run with ``[github] repo`` set is scripted
all the way to ``merged`` without a network.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, ClassVar

import pytest

from sbxloop import hostgit
from sbxloop.config import Config
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.model import (
    PIPELINE_STAGES,
    RESUMABLE_RUN_STATES,
    TERMINAL_RUN_STATES,
)
from sbxloop.engine.store import StateStore
from sbxloop.errors import (
    BudgetExceededError,
    GithubOpsError,
    ProvisionError,
    StateError,
    WorkerError,
)
from sbxloop.events import Event, EventBus, HostEventTypes
from sbxloop.gh.ops import FailedCheck, GithubOps
from sbxloop.sbx.cli import SbxCLI
from tests.conftest import FakeSbx
from tests.fakes.fake_github import (
    BLOCKED_405,
    GREEN,
    MERGED,
    NO_CHECKS,
    PENDING,
    RED,
    STALE_409,
    FakeGithub,
    human_review,
)

# -- scripted responses ------------------------------------------------------


def taskgraph(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {"json": {"tasks": list(tasks)}}


def task(
    id: str,
    deps: list[str] | None = None,
    verify: list[str] | None = None,
    egress: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "id": id,
        "title": f"Task {id}",
        "description": f"description of {id}",
        "depends_on": deps or [],
        "acceptance_criteria": [f"{id} works"],
        "verify_commands": verify if verify is not None else ["true"],
    }
    if egress is not None:
        spec["egress"] = egress
    return spec


# BUILD plans and executes in one session and reports in prose (expect=text),
# so one scripted entry covers one whole task attempt.
BUILD = {"text": "work complete, files changed"}
# A delivery needs something to deliver: the pipeline tests' builder writes
# a file (the workspace is a plain directory, so delivery snapshots it).
FILES_BUILD = {"text": "wrote hello.txt", "files": {"hello.txt": "hi\n"}}

HAPPY_TASK = [BUILD]

# Budgets that make the first verify failure terminal: no revisions, no
# replans — the shortest scriptable path to a failed task.
NO_RETRY_BUDGETS = {"max_revisions_per_task": 0, "max_replans_per_task": 0}

FINDING = {
    "path": "hello.txt",
    "line": 1,
    "body": "hello.txt must greet with hello, not hi",
    "severity": "major",
}


def review(verdict: str, summary: str, *findings: dict[str, Any]) -> dict[str, Any]:
    return {"json": {"verdict": verdict, "summary": summary, "findings": list(findings)}}


REVIEW_OK = review("approve", "looked for the four failure modes and found none")
REVIEW_RC = review("request_changes", "one real problem", FINDING)

# CI-wait knobs that keep the tests fast: poll at once, trust "no check
# runs" immediately.
FAST_LANDING = {"ci_poll_interval_s": 0.01, "ci_settle_s": 0}


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

    def consumed(self) -> int:
        """How many scripted entries the workers have used so far."""
        state = self.script_path.with_suffix(".json.state")
        return int(state.read_text()) if state.is_file() else 0

    def engine(
        self,
        *,
        install_workers: bool = False,
        ops: GithubOps | None = None,
        **config_overrides: Any,
    ) -> LoopEngine:
        # Resource guardrails default OFF in the harness: the real worker
        # samples the host filesystem here, so default thresholds would make
        # tests depend on how full the developer's disk is.
        limits = config_overrides.pop(
            "limits", {"disk_warn": 0, "disk_abort": 0, "mem_warn": 0, "mem_abort": 0}
        )
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
            github_ops=(lambda client, run_id: ops) if ops is not None else None,
        )

    def pipeline(self, fake: FakeGithub, **config_overrides: Any) -> LoopEngine:
        """An engine that delivers to the fake's repository and lands there."""
        landing = FAST_LANDING | config_overrides.pop("landing", {})
        return self.engine(
            ops=fake, github={"repo": fake.repo}, landing=landing, **config_overrides
        )

    def event_types(self) -> list[str]:
        return [e.type for e in self.events]

    def run_states(self) -> list[str]:
        return [e.data["state"] for e in self.events if e.type == HostEventTypes.RUN_STATE]

    def sandboxes_left(self) -> list[str]:
        boxes = self.fake_sbx.state / "sandboxes"
        return sorted(p.name for p in boxes.iterdir()) if boxes.is_dir() else []

    def agent_jobs(self, run_id: str) -> list[dict[str, Any]]:
        """Every job request the agent sandbox received (keep_sandboxes runs
        only — teardown removes the fs the job files live in)."""
        fs = self.fake_sbx.sandbox_fs(f"sbxloop-{run_id}-agent")
        return [json.loads(p.read_text()) for p in (fs / "home/agent/.sbxloop/jobs").iterdir()]


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


class TestHappyPath:
    def test_single_task_completes(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine().start("build the feature")

        # No repository: the run stops after its gate, `completed`.
        assert result.state == "completed"
        assert result.succeeded
        assert result.pr_number is None
        assert [t.state for t in result.tasks] == ["done"]
        assert result.tasks[0].session_id is not None

        types = harness.event_types()
        assert HostEventTypes.RUN_START in types
        assert HostEventTypes.TASK_END in types
        assert HostEventTypes.RUN_END in types
        assert harness.run_states() == [
            "provisioning",
            "decomposing",
            "building",
            "gating",
            "completed",
        ]
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

    def test_structured_phases_emit_what_they_decided(self, harness: Harness) -> None:
        """The roster reaches the bus as parsed data, and each build's report
        excerpt rides on its phase.end. The decomposer answers in JSON and
        the builder narrates in prose, so a surface that does not show raw
        JSON (Discord) has no other way to say what was decided or done."""
        harness.script(
            [
                taskgraph(task("t1"), task("t2", deps=["t1"])),
                {"text": "wrote it\nand tested it"},
                BUILD,
            ]
        )
        result = harness.engine().start("build the feature")
        assert result.succeeded

        (roster,) = [e for e in harness.events if e.type == HostEventTypes.RUN_TASKS]
        assert [(t["id"], t["title"], t["state"]) for t in roster.data["tasks"]] == [
            ("t1", "Task t1", "pending"),
            ("t2", "Task t2", "pending"),
        ]
        assert roster.data["tasks"][1]["depends_on"] == ["t1"]

        builds = [
            e
            for e in harness.events
            if e.type == HostEventTypes.PHASE_END and e.data["phase"] == "build"
        ]
        assert [(e.data["task_id"], e.data["status"], e.data["attempt"]) for e in builds] == [
            ("t1", "ok", 1),
            ("t2", "ok", 1),
        ]
        # The excerpt is whitespace-collapsed prose, not the raw report.
        assert builds[0].data["message"] == "wrote it and tested it"
        assert builds[1].data["message"] == "work complete, files changed"

    def test_roster_is_re_announced_with_persisted_state_on_resume(self, harness: Harness) -> None:
        """A resumed run gets a fresh thread, so the roster is re-announced —
        carrying the state each task was left in, not a fresh 'pending'."""
        harness.script([taskgraph(task("t1"), task("t2", deps=["t1"])), {"fail": "boom"}])
        engine = harness.engine()
        with pytest.raises(WorkerError, match="boom"):
            engine.start("crashy run")
        run_id = engine.store.list_runs()[0].run_id

        mark = len(harness.events)
        harness.script([*HAPPY_TASK, *HAPPY_TASK])
        assert harness.engine().resume(run_id).succeeded
        (roster,) = [e for e in harness.events[mark:] if e.type == HostEventTypes.RUN_TASKS]
        assert [(t["id"], t["state"]) for t in roster.data["tasks"]] == [
            ("t1", "executing"),
            ("t2", "pending"),
        ]

    def test_agent_messages_carry_phase_persona(self, harness: Harness) -> None:
        """Every agent.message names the persona that produced it, so the
        transcript header says WHO responded (decomposer, builder), not a
        generic "agent". Echo only emits agent.message for entries with
        "text", so give the decomposer's scripted reply some."""
        harness.script(
            [
                {**taskgraph(task("t1")), "text": "breaking it down"},
                BUILD,
            ]
        )
        result = harness.engine().start("build the feature")

        assert result.succeeded
        speakers = [e.data.get("agent") for e in harness.events if e.type == "agent.message"]
        assert speakers == ["decomposer", "builder"]

    def test_default_run_is_github_less(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No [github].repo → only the agent sandbox exists, GH_TOKEN unneeded,
        and the run ends `completed` (nothing to deliver to)."""
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine().start("no github anywhere")
        assert result.state == "completed"
        created = [c[1].removeprefix("--name=") for c in harness.fake_sbx.invocations("create")]
        assert all(name.endswith("-agent") for name in created), created
        assert HostEventTypes.RUN_DELIVER not in harness.event_types()

    def test_configured_github_provisions_ops_sandbox(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        result = harness.pipeline(FakeGithub()).start("with github")
        assert result.state == "merged"
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
        rows = engine.store.phase_attempts(result.run_id)
        assert [row["phase"] for row in rows] == ["decompose", "build", "verify", "gate"]
        # A project that declares no gate records the skip, not a pass.
        assert rows[-1]["status"] == "skipped"

    def test_phase_attempts_carry_usage(self, harness: Harness) -> None:
        """Every agent phase row bills its session's tokens and turns; the
        mechanical verify row stays NULL."""
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        engine = harness.engine()
        result = engine.start("bill phases")
        rows = {row["phase"]: row for row in engine.store.phase_attempts(result.run_id)}
        for phase in ("decompose", "build"):
            assert rows[phase]["input_tokens"] is not None, phase
            assert rows[phase]["turns"] == 1, phase
        assert rows["verify"]["input_tokens"] is None
        assert rows["verify"]["turns"] is None


class TestReviseAndVerify:
    def test_verify_failure_revises_then_passes(self, harness: Harness) -> None:
        # The revision loop is verify-driven now: the first attempt leaves
        # the checked file unwritten, verify fails, and the revision (same
        # session, feedback in hand) produces it.
        harness.script(
            [
                taskgraph(task("t1", verify=["test -f done.txt"])),
                BUILD,
                {"text": "wrote done.txt this time", "files": {"done.txt": "ok\n"}},
            ]
        )
        result = harness.engine().start("revise once")
        assert result.state == "completed"
        assert result.tasks[0].revisions == 1
        assert result.tasks[0].replans == 0

    def test_verify_exhaustion_spends_replan_with_fresh_session(self, harness: Harness) -> None:
        # Field failure (rv4zfdb1m): the builder cannot edit the
        # decomposer-authored verify_commands, so an approach that disagrees
        # with where the checks look burned every revision and killed the
        # task. Exhaustion from verify failures must discard the session and
        # start over — a fresh approach can route the work to where the
        # commands expect files.
        harness.script(
            [
                taskgraph(task("t1", verify=["test -f done.txt"])),
                BUILD,
                BUILD,  # same failure twice -> verify-suspect replan, fresh session
                {"text": "fresh approach, wrote done.txt", "files": {"done.txt": "ok\n"}},
            ]
        )
        engine = harness.engine(keep_sandboxes=True)
        result = engine.start("verify unsticks via fresh session")
        assert result.state == "completed"
        assert result.tasks[0].state == "done"
        assert result.tasks[0].replans == 1
        assert result.tasks[0].verify_suspect
        # revisions were reset when the session was discarded, and the fresh
        # attempt passed first try
        assert result.tasks[0].revisions == 0

        jobs = [j for j in harness.agent_jobs(result.run_id) if j.get("kind") == "agent.session"]
        # The two in-budget revisions continued their own session; nothing
        # else did (decompose is always fresh, and so is the replan build).
        assert len([j for j in jobs if j.get("resume_session_id")]) == 1
        (fresh,) = [j for j in jobs if "VERIFY COMMAND SUSPECT" in (j.get("prompt") or "")]
        assert fresh.get("resume_session_id") is None

    def test_verify_failure_exhausts_revisions_and_replans(self, harness: Harness) -> None:
        # A verify command no attempt can satisfy: revisions burn, one
        # fresh-session replan burns, then the task fails — the loop is
        # bounded.
        harness.script([taskgraph(task("t1", verify=["false"])), *([BUILD] * 6)])
        result = harness.engine().start("verify never passes")
        assert result.state == "failed"
        assert "verify command that never changed its result" in (result.reason or "")
        assert "`false`" in (result.reason or "")
        assert result.tasks[0].state == "failed"
        assert result.tasks[0].replans == 1
        assert result.tasks[0].verify_suspect
        # The suspect signal fires on the second identical failure, so the
        # replan is spent there instead of after three wasted revisions
        # (#387); the remaining budget is revisions carrying the suspect
        # wording forward.
        assert result.tasks[0].revisions == 3
        assert "VERIFY COMMAND SUSPECT" in result.tasks[0].last_feedback

    def test_verify_exhaustion_without_replan_budget_fails(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1", verify=["false"])), *([BUILD] * 3)])
        result = harness.engine(budgets={"max_replans_per_task": 0}).start("verify never passes")
        assert result.state == "failed"
        assert result.tasks[0].state == "failed"
        # With no replan to spend, the second identical failure fails the
        # task immediately rather than burning the remaining revisions.
        assert result.tasks[0].verify_suspect

    def test_verify_failure_surfaces_in_event_stream(self, harness: Harness) -> None:
        # Field failure (rv4zfdb1m): the transcript jumped verifying -> failed
        # with the failing command visible only via sqlite on phase_attempts.
        harness.script([taskgraph(task("t1", verify=["exit 1", "exit 2"])), BUILD])
        harness.engine(budgets=NO_RETRY_BUDGETS).start("loud verify failure")
        fails = [
            e
            for e in harness.events
            if e.type == HostEventTypes.PHASE_END and e.data["phase"] == "verify"
        ]
        assert fails, "verify failure emitted no phase.end event"
        first = fails[0].data
        assert first["task_id"] == "t1"
        assert first["status"] == "failed"
        assert "verify command failed" in first["message"]
        assert "(+1 more)" in first["message"]  # both failing commands counted


class TestReplanAndSkip:
    def test_replan_budget_exhaustion_skips_dependents(self, harness: Harness) -> None:
        harness.script(
            [
                taskgraph(task("t1", verify=["false"]), task("t2", deps=["t1"])),
                BUILD,  # verify fails with no budget left -> t1 failed
            ]
        )
        result = harness.engine(budgets=NO_RETRY_BUDGETS).start("fail and skip")
        assert result.state == "failed"
        by_id = {t.spec.id: t for t in result.tasks}
        assert by_id["t1"].state == "failed"
        assert by_id["t2"].state == "skipped"
        end_states = [e.data["state"] for e in harness.events if e.type == HostEventTypes.TASK_END]
        assert end_states == ["failed", "skipped"]
        # A failed graph never reaches the gate, let alone a delivery.
        assert "gating" not in harness.run_states()


class TestBudgetsAndCancel:
    def test_wall_clock_budget(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1"))])
        ticks = iter([0.0, 10_000.0, 20_000.0, 30_000.0, 40_000.0])
        engine = harness.engine(budgets={"max_wall_clock_s": 5.0})
        engine.clock = lambda: next(ticks)
        with pytest.raises(BudgetExceededError, match="max_wall_clock_s"):
            engine.start("too slow")
        run_id = engine.store.list_runs()[0].run_id
        run = engine.store.get_run(run_id)
        assert run.state == "failed"
        assert run.reason == "exceeded max_wall_clock_s=5"
        assert harness.sandboxes_left() == []  # pair context cleaned up

    def test_too_many_tasks_rejected(self, harness: Harness) -> None:
        harness.script([taskgraph(*(task(f"t{i}") for i in range(1, 5)))])
        engine = harness.engine(budgets={"max_tasks": 2})
        with pytest.raises(BudgetExceededError, match="produced 4 tasks"):
            engine.start("too many")

    def test_cancelled_run_is_terminal_but_operator_resumable(self, harness: Harness) -> None:
        """#374: a cancelled run is terminal for liveness/reporting (so it
        never shows as active) yet an operator may still resume it, exactly
        like a failed run."""
        harness.script([taskgraph(task("t1"))])
        engine = harness.engine()
        engine.store.create_run("rcancel", "x")
        engine.cancel("rcancel")
        assert engine.store.get_run("rcancel").state == "cancelled"
        assert "cancelled" in RESUMABLE_RUN_STATES
        assert "cancelled" in TERMINAL_RUN_STATES

    @pytest.mark.parametrize("state", sorted(TERMINAL_RUN_STATES))
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
        assert engine.store.get_run(run_id).state == "building"
        assert harness.sandboxes_left() == []  # pair context cleaned up


class TestResume:
    def test_resume_after_crash_continues(self, harness: Harness) -> None:
        # Crash during t1's build (worker returns an error result).
        harness.script([taskgraph(task("t1")), {"fail": "sandbox exploded"}])
        engine = harness.engine()
        with pytest.raises(WorkerError, match="sandbox exploded"):
            engine.start("crashy run")

        run_id = engine.store.list_runs()[0].run_id
        run = engine.store.get_run(run_id)
        assert run.state == "building"  # persisted mid-flight
        assert run.stage == "building"
        tasks = engine.store.get_tasks(run_id)
        assert tasks[0].state == "executing"

        # Fresh engine (new sandbox pair), remaining script picks up at
        # BUILD - decompose is NOT re-run.
        harness.script([*HAPPY_TASK])
        engine2 = harness.engine()
        result = engine2.resume(run_id)
        assert result.state == "completed"
        assert result.tasks[0].state == "done"
        phases = [row["phase"] for row in engine2.store.phase_attempts(run_id)]
        assert phases.count("decompose") == 1
        # the crashed attempt never committed a phase row - that is exactly the
        # "uncommitted phases re-run" resume semantic
        assert phases.count("build") == 1

    @pytest.mark.parametrize("state", ["completed", "merged"])
    def test_resume_finished_run_refused(self, harness: Harness, state: str) -> None:
        engine = harness.engine()
        engine.store.create_run("rdone", "x", engine.config.model_dump_json())
        engine.store.set_run_state("rdone", state)  # type: ignore[arg-type]
        with pytest.raises(StateError, match="only unfinished runs"):
            engine.resume("rdone")

    def _crashed_run(
        self,
        harness: Harness,
        *,
        verify: list[str] | None = None,
        **config_overrides: Any,
    ) -> str:
        """Start a run that crashes during t1's build; returns its run id."""
        harness.script([taskgraph(task("t1", verify=verify)), {"fail": "sandbox exploded"}])
        engine = harness.engine(**config_overrides)
        with pytest.raises(WorkerError, match="sandbox exploded"):
            engine.start("crashy run")
        return engine.store.list_runs()[0].run_id

    def test_resume_uses_persisted_config_not_current(self, harness: Harness) -> None:
        run_id = self._crashed_run(
            harness,
            verify=["test -f done.txt"],
            budgets={"max_revisions_per_task": 2},
        )

        # Resume under a *tighter* on-disk config (zero revisions). The
        # persisted budget must govern: one revise round still completes —
        # as an in-budget revision, not by spending the replan the current
        # config would force.
        harness.script([BUILD, {"text": "wrote it", "files": {"done.txt": "ok\n"}}])
        engine2 = harness.engine(budgets={"max_revisions_per_task": 0})
        result = engine2.resume(run_id)
        assert result.state == "completed"
        assert result.tasks[0].revisions == 1
        assert result.tasks[0].replans == 0
        assert engine2.config.budgets.max_revisions_per_task == 2

        drift = [e for e in harness.events if e.type == HostEventTypes.RUN_CONFIG_DRIFT]
        assert len(drift) == 1
        assert "budgets.max_revisions_per_task" in drift[0].data["message"]

    def test_resume_with_unchanged_config_reports_no_drift(self, harness: Harness) -> None:
        run_id = self._crashed_run(harness)
        harness.script([*HAPPY_TASK])
        result = harness.engine().resume(run_id)
        assert result.state == "completed"
        assert HostEventTypes.RUN_CONFIG_DRIFT not in harness.event_types()

    def test_resume_honors_current_keep_toggles(self, harness: Harness) -> None:
        # keep_sandboxes/keep_on_failure are resume-time operator intent
        # (debug this attempt), not run identity: the CURRENT config wins for
        # them, with no drift warning — unlike everything else rehydrated.
        run_id = self._crashed_run(harness)
        harness.script([*HAPPY_TASK])
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
        harness.script([*HAPPY_TASK])
        engine2 = harness.engine(sandbox={"workspace": str(elsewhere)})
        result = engine2.resume(run_id)
        assert result.state == "completed"
        assert result.workspace == original
        assert engine2.store.get_run(run_id).workspace == original
        assert not elsewhere.exists()
        drift = [e for e in harness.events if e.type == HostEventTypes.RUN_CONFIG_DRIFT]
        assert len(drift) == 1
        assert "sandbox.workspace" in drift[0].data["message"]

    def test_run_with_git_workspace_pins_clone(self, harness: Harness) -> None:
        """An isolated run works in the per-run clone and pins IT, and the
        source checkout's working tree stays byte-identical."""
        from tests.unit.test_hostgit import make_repo

        source = make_repo(harness.tmp_path)
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        engine = harness.engine(sandbox={"workspace": str(source)})
        result = engine.start("improve the project")

        assert result.state == "completed"
        clone_dir = (harness.tmp_path / "state" / "runs" / result.run_id / "workspace").resolve()
        assert result.workspace == clone_dir
        assert engine.store.get_run(result.run_id).workspace == clone_dir
        assert (source / "hello.txt").read_text() == "hi\n"
        assert not (source / ".git" / "refs" / "heads" / "sbxloop").exists()

    def test_phase_runner_sees_the_run_workspace(self, harness: Harness) -> None:
        # #250: the verify-command lint keys on the host workspace (a
        # `uv.lock` there flips the Python convention), so the runner must
        # be handed the run's actual workspace path, not left blind.
        from sbxloop.engine.phases import PhaseRunner

        captured: dict[str, PhaseRunner] = {}
        original_init = PhaseRunner.__init__

        def spy_init(self: PhaseRunner, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            captured["phases"] = self

        harness.monkeypatch.setattr(PhaseRunner, "__init__", spy_init)
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine().start("improve the project")
        assert result.state == "completed"
        assert captured["phases"].workspace == result.workspace
        assert result.workspace is not None

    def test_resume_isolated_run_reuses_clone(self, harness: Harness) -> None:
        from tests.unit.test_hostgit import make_repo

        source = make_repo(harness.tmp_path)
        run_id = self._crashed_run(harness, sandbox={"workspace": str(source)})
        engine = harness.engine()
        pinned = engine.store.get_run(run_id).workspace
        assert pinned is not None and pinned.name == "workspace"
        sentinel = pinned / "agent-work.txt"
        sentinel.write_text("precious\n")

        harness.script([*HAPPY_TASK])
        result = harness.engine().resume(run_id)
        assert result.state == "completed"
        assert result.workspace == pinned
        assert sentinel.read_text() == "precious\n"
        fresh_clones = [
            e
            for e in harness.events
            if e.type == "sandbox.workspace_clone" and not e.data.get("reused")
        ]
        assert len(fresh_clones) == 1  # only the original run cloned

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
        harness.script([*HAPPY_TASK])
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


NPM_EGRESS = [{"domain": "registry.npmjs.org", "reason": "npm install"}]
GEMS_EGRESS = [{"domain": "rubygems.org", "reason": "bundle install"}]

# Every supported language's registry is baseline (#141), so out-of-bounds
# tests need a domain no built-in tier covers.
SAAS_EGRESS = [{"domain": "api.example-saas.com", "reason": "fetch data"}]


class TestTaskEgress:
    """Task-declared egress: authored by the decomposer on the taskgraph,
    bounded by [policy] at graph acceptance, granted just before BUILD."""

    def test_in_bounds_egress_granted_and_event_logged(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1", egress=SAAS_EGRESS)), *HAPPY_TASK])
        result = harness.engine(policy={"allow": ["api.example-saas.com"]}).start("saas task")
        assert result.state == "completed"
        agent = f"sbxloop-{result.run_id}-agent"
        assert [
            "allow",
            "network",
            "api.example-saas.com",
            "--sandbox",
            agent,
        ] in harness.fake_sbx.policies()
        (event,) = [e for e in harness.events if e.type == "policy.allow"]
        assert event.data["domain"] == "api.example-saas.com"
        assert event.data["reason"] == "fetch data"
        assert event.data["task_id"] == "t1"
        # granted at BUILD entry: the grant precedes the build's phase.end
        types = harness.event_types()
        assert types.index("policy.allow") < types.index(HostEventTypes.PHASE_END)

    def test_rails_app_bundle_installs_without_a_declaration(self, harness: Harness) -> None:
        # #159: the motivating case for the declarable tier — "write a Rails
        # app" — now runs off the baseline. Declaring rubygems.org anyway
        # stays valid and costs nothing: seeded at provision time, so no
        # grant-late call and no policy event.
        harness.script([taskgraph(task("t1", egress=GEMS_EGRESS)), *HAPPY_TASK])
        result = harness.engine().start("gem task, default policy")
        assert result.state == "completed"
        seeds = [c for c in harness.fake_sbx.policies() if "rubygems.org" in c]
        assert len(seeds) == 1
        assert [e for e in harness.events if e.type.startswith("policy.")] == []

    def test_baseline_registry_needs_no_grant(self, harness: Harness) -> None:
        # #148: an npm build must not depend on the decomposer remembering
        # to declare the registry — and declaring it anyway costs nothing,
        # because the sandbox was provisioned with it.
        harness.script([taskgraph(task("t1", egress=NPM_EGRESS)), *HAPPY_TASK])
        result = harness.engine().start("npm task, default policy")
        assert result.state == "completed"
        # Seeded once, at provision time — not granted late at BUILD entry,
        # and so not event-logged: there is no grant to log.
        seeds = [c for c in harness.fake_sbx.policies() if "registry.npmjs.org" in c]
        assert len(seeds) == 1
        assert seeds[0][:3] == ["allow", "network", "registry.npmjs.org"]
        assert [e for e in harness.events if e.type.startswith("policy.")] == []

    def test_typecheck_only_task_needs_no_egress(self, harness: Harness) -> None:
        # #151: the minimal TypeScript case — dependencies already vendored,
        # so the task only runs `tsc`. An empty `egress` must be a complete
        # declaration, not one that forgot something: no grants, no policy
        # events, no failure.
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        result = harness.engine().start("type-check the project")
        assert result.state == "completed"
        assert [e for e in harness.events if e.type.startswith("policy.")] == []

    def test_out_of_bounds_egress_rejected_then_retried(self, harness: Harness) -> None:
        harness.script(
            [taskgraph(task("t1", egress=SAAS_EGRESS)), taskgraph(task("t1")), *HAPPY_TASK]
        )
        result = harness.engine().start("saas api denied")
        assert result.state == "completed"
        grants = [c for c in harness.fake_sbx.policies() if "api.example-saas.com" in c]
        assert grants == []

    def test_out_of_bounds_egress_twice_fails(self, harness: Harness) -> None:
        bad_graph = taskgraph(task("t1", egress=SAAS_EGRESS))
        harness.script([bad_graph, bad_graph])
        with pytest.raises(WorkerError, match="invalid output twice"):
            harness.engine().start("insists on the saas api")


class TestVerifyCommandLint:
    """Bare-interpreter verify commands are rejected at JSON acceptance —
    one retry with the rule quoted, never a revision cycle plus an in-VM
    apt workaround (field failure r12ygfd7t). Verify commands only come
    from the decomposer now, so every rejection is a decompose retry."""

    def test_decomposer_bare_python_rejected_then_retried(self, harness: Harness) -> None:
        bad_graph = taskgraph(task("t1", verify=["python -m pytest test_app.py -q"]))
        good_graph = taskgraph(task("t1", verify=["true"]))
        harness.script([bad_graph, good_graph, *HAPPY_TASK])
        result = harness.engine().start("add a flag")
        assert result.state == "completed"

    def test_decomposer_bare_python_twice_fails(self, harness: Harness) -> None:
        bad_graph = taskgraph(task("t1", verify=["python app.py"]))
        harness.script([bad_graph, bad_graph])
        with pytest.raises(WorkerError, match="invalid output twice"):
            harness.engine().start("insists on bare python")

    def test_decomposer_sudo_rejected_then_retried(self, harness: Harness) -> None:
        # Environment mutation (sudo/apt) in a verify command is rejected
        # the same way — the retried graph drops it.
        bad_graph = taskgraph(task("t1", verify=["sudo apt-get install -y jq && test -f out.json"]))
        good_graph = taskgraph(task("t1", verify=["true"]))
        harness.script([bad_graph, good_graph, *HAPPY_TASK])
        result = harness.engine().start("no sudo in verify")
        assert result.state == "completed"

    def test_compliant_commands_accepted_first_try(self, harness: Harness) -> None:
        # `test -d` trips no rules; the graph is accepted with no retry
        # (the script would run dry if a retry were consumed).
        graph = taskgraph(task("t1", verify=["test -d ."]))
        harness.script([graph, *HAPPY_TASK])
        result = harness.engine().start("clean commands")
        assert result.state == "completed"


class TestKeepOnFailure:
    def test_failed_run_keeps_pair_and_marks_db(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1", verify=["false"])), BUILD])
        engine = harness.engine(keep_on_failure=True, budgets=NO_RETRY_BUDGETS)
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

    def test_merged_run_still_cleans_up(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        engine = harness.pipeline(FakeGithub(), keep_on_failure=True)
        result = engine.start("all fine, shipped")
        assert result.state == "merged"
        assert harness.sandboxes_left() == []
        assert "run.keep" not in harness.event_types()

    def test_blocked_run_keeps_pair(self, harness: Harness) -> None:
        """Blocked is not success: the evidence is kept like a failure's."""
        fake = FakeGithub()
        fake.merge_outcomes = [BLOCKED_405]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        engine = harness.pipeline(fake, keep_on_failure=True)
        result = engine.start("blocked at the door")
        assert result.state == "blocked"
        assert len(result.kept_sandboxes) == 2
        assert engine.store.get_run(result.run_id).kept_reason == "debug"

    def test_keep_sandboxes_marks_manual(self, harness: Harness) -> None:
        # `sandbox prune` must understand manually kept pairs as well.
        harness.script([taskgraph(task("t1")), *HAPPY_TASK])
        engine = harness.engine(keep_sandboxes=True)
        result = engine.start("kept run")
        assert engine.store.get_run(result.run_id).kept_reason == "manual"
        assert result.kept_sandboxes == [f"sbxloop-{result.run_id}-agent"]


class TestWorkspaceExecution:
    """The artifacts linchpin: jobs run in the workspace mount, so files the
    builder writes appear on the host live and survive sandbox teardown."""

    def test_mounted_run_lands_artifacts_on_host(self, harness: Harness) -> None:
        build = {"text": "wrote hello.txt", "files": {"hello.txt": "hi\n"}}
        harness.script([taskgraph(task("t1")), build])
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
        jobs = harness.agent_jobs(result.run_id)
        assert jobs
        # every job of every kind — agent phases and verify — runs in the
        # workspace, so verification sees the produced files
        workdirs = {j["cwd"] for j in jobs}
        assert len(workdirs) == 1
        assert workdirs != {None}

    def test_unmounted_run_harvests_artifacts(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SBX_FAKE_NO_MOUNT", "1")
        build = {"text": "wrote hello.txt", "files": {"hello.txt": "hi\n", "sub/deep.txt": "d"}}
        harness.script([taskgraph(task("t1")), build])
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

    def test_unmounted_run_delivers_the_harvest(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delivery reads the harvest, refreshed right before it — the
        fix round's writes must be in the PR, not just the first build's."""
        monkeypatch.setenv("SBX_FAKE_NO_MOUNT", "1")
        fake = FakeGithub()
        fix = {"text": "fixed", "files": {"hello.txt": "hello\n"}}
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_RC, fix, REVIEW_OK])
        result = harness.pipeline(fake).start("write hello.txt in harvest mode")
        assert result.state == "merged"
        assert not result.mounted
        delivered = [json.loads(json.dumps(b)) for b in fake.blob_batches]
        assert [[e["path"] for e in batch] for batch in delivered] == [
            ["hello.txt"],
            ["hello.txt"],
        ]
        assert delivered[0][0]["content_b64"] != delivered[1][0]["content_b64"]

    def test_mounted_run_reports_artifacts_event(self, harness: Harness) -> None:
        build = {"text": "wrote hello.txt", "files": {"hello.txt": "hi\n"}}
        harness.script([taskgraph(task("t1")), build])
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
        build = {
            "text": "added CI",
            "files": {
                ".github/workflows/ci.yml": "on: push\n",
                ".gitignore": "*.pyc\n",
                ".git/HEAD": "ref\n",
            },
        }
        harness.script([taskgraph(task("t1")), build])
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
        build = {
            "text": "wrote files",
            "files": {
                "hello.txt": "hi\n",
                ".git/HEAD": "ref: refs/heads/main\n",
                ".git/objects/abc": "blob",
            },
        }
        harness.script([taskgraph(task("t1")), build])
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
        build = {
            "text": "built it",
            "files": {
                "package.json": '{"name":"app"}\n',
                "node_modules/left-pad/index.js": "module.exports = 1\n",
                "target/release/app": "ELF\n",
                "src/__pycache__/main.cpython-312.pyc": "\x00\n",
            },
        }
        harness.script([taskgraph(task("t1")), build])
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
        authoritative sweep at the end runs.  The result is still complete
        and artifacts are on the host — just with fewer sbx exec tar calls."""
        monkeypatch.setenv("SBX_FAKE_NO_MOUNT", "1")
        build = {"text": "wrote hello.txt", "files": {"hello.txt": "hi\n"}}
        harness.script([taskgraph(task("t1"), task("t2", deps=["t1"])), build, build])
        engine = harness.engine(artifacts={"harvest_mode": "final"})
        result = engine.start("two-task harvest-final run")

        assert result.state == "completed"
        harvested = harness.state_dir / "runs" / result.run_id / "artifacts"
        assert (harvested / "hello.txt").read_text() == "hi\n"

        # Per-task mode would run tar once per task + once at the end.
        # Final mode runs tar only at the end (1 call).
        tar_calls = [inv for inv in harness.fake_sbx.invocations("exec") if "tar" in inv]
        assert len(tar_calls) == 1


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
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        result = harness.pipeline(
            FakeGithub(),
            install_workers=True,
            sandbox={"template": self.REF},
        ).start("use the baked template with github")

        assert result.state == "merged"
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

    def test_mem_abort_reason_names_memory(self, harness: Harness) -> None:
        # #253: /proc/meminfo is Linux-only, so the memory sample is injected
        # the way _track_resources would record it from a worker event.
        engine = harness.engine(limits={"disk_warn": 0, "disk_abort": 95.0, "mem_abort": 97.0})
        engine._last_resources["agent"] = {
            "level": "abort",
            "disk_used_pct": 40.0,
            "mem_used_pct": 98.2,
        }
        reason = engine._resource_abort_reason()
        assert reason is not None
        assert reason.startswith("sandbox memory exhausted: 98.2%")
        assert "limits.mem_abort=97.0%" in reason
        # Disk is named when it is the one that tripped (both crossing).
        engine._last_resources["agent"]["disk_used_pct"] = 99.0
        assert (engine._resource_abort_reason() or "").startswith("sandbox disk exhausted")

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


class Clock:
    """A manual clock for the engine's wall-clock and CI-settle arithmetic."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TestPipeline:
    """GATE → DELIVER → REVIEW ⇄ FIX → CI → LAND, scripted end to end against
    a FakeGithub through the engine's ``github_ops`` seam."""

    def _events(self, harness: Harness, type: str) -> list[Event]:
        return [e for e in harness.events if e.type == type]

    def test_issue_to_merge(self, harness: Harness) -> None:
        """The whole pipeline once: a draft PR, a review that asks for a
        change, one fix round re-delivered onto the same branch, an
        approval, green CI, un-draft, merge, branch gone."""
        fake = FakeGithub(draft=True)
        fake.checks = [GREEN]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_RC, BUILD, REVIEW_OK])
        engine = harness.pipeline(fake)
        result = engine.start("write hello.txt")

        assert result.state == "merged"
        assert result.succeeded
        assert result.reason is None
        assert result.pr_number == 7
        assert result.pr_url == "https://github.com/o/r/pull/7"
        assert [(t.spec.id, t.state) for t in result.tasks] == [("t1", "done"), ("fix-1", "done")]
        fix = result.tasks[1].spec
        assert FINDING["body"] in fix.description
        assert "`hello.txt:1` [major]" in fix.description
        assert fix.verify_commands == ["true"]

        # Two deliveries, one pull request: the fix round refreshed the
        # branch under PR #7 and never POSTed a second one (#387 field run).
        assert fake.pr_create_calls == 1
        # The review verdict is ours; it is posted for the record.
        assert [event for event, _, _ in fake.reviews] == ["REQUEST_CHANGES", "APPROVE"]
        _, body, comments = fake.reviews[0]
        assert "one real problem" in body and f"run `{result.run_id}`" in body
        assert [(c.path, c.line) for c in comments] == [("hello.txt", 1)]
        # Delivered as a draft, un-drafted once, merged at the round-2 head,
        # branch tidied away.
        assert fake.pr_kwargs["draft"] is True
        assert fake.pr_kwargs["head"] == f"sbxloop/{result.run_id}"
        assert fake.ready_calls == ["PR_node7"]
        assert fake.merges == [(7, "squash", "commit2")]
        assert fake.deleted_branches == [f"sbxloop/{result.run_id}"]
        assert fake.pr["merged"] is True

        assert harness.run_states() == [
            "provisioning",
            "decomposing",
            "building",
            "gating",
            "delivering",
            "reviewing",
            "fixing",
            "gating",
            "delivering",
            "reviewing",
            "awaiting_ci",
            "landing",
            "merged",
        ]
        deliveries = self._events(harness, HostEventTypes.RUN_DELIVER)
        assert [(e.data["round"], e.data["head_sha"], e.data["pr"]) for e in deliveries] == [
            (1, "commit1", 7),
            (2, "commit2", 7),
        ]
        verdicts = self._events(harness, HostEventTypes.REVIEW_VERDICT)
        assert [(e.data["round"], e.data["verdict"], e.data["blocking"]) for e in verdicts] == [
            (1, "request_changes", 1),
            (2, "approve", 0),
        ]
        (fix_round,) = self._events(harness, HostEventTypes.FIX_ROUND)
        assert fix_round.data["kind"] == "review"
        assert fix_round.data["task_id"] == "fix-1"
        assert fix_round.data["budget"] == "1/3"
        (merged,) = self._events(harness, HostEventTypes.RUN_MERGED)
        assert merged.data["sha"] == "merge0001" and merged.data["by_human"] is False
        (end,) = self._events(harness, HostEventTypes.RUN_END)
        assert end.data["state"] == "merged" and end.data["pr"] == 7

        run = engine.store.get_run(result.run_id)
        assert run.state == "merged"
        assert run.stage == "landing"
        assert run.review_rounds == 1
        assert run.ci_rounds == 0
        assert run.pr_number == 7
        assert run.pr_node_id == "PR_node7"
        assert run.branch == f"sbxloop/{result.run_id}"
        assert run.head_sha == "commit2"
        assert run.last_verdict == "approve"
        phases = [(row["phase"], row["status"]) for row in engine.store.phase_attempts(run.run_id)]
        assert phases == [
            ("decompose", "ok"),
            ("build", "ok"),
            ("verify", "ok"),
            ("gate", "skipped"),
            ("review", "request_changes"),
            ("build", "ok"),
            ("verify", "ok"),
            ("gate", "skipped"),
            ("review", "approve"),
        ]

    def test_review_rounds_carry_history_and_refutations(self, harness: Harness) -> None:
        """Round 2 sees round 1's findings and the fixer's response; a
        verdict built only on a refuted finding is sent back once."""
        fake = FakeGithub()
        refute = {"text": "Left as is.\n\nrefuted: hello.txt:1 — the greeting is specified as hi"}
        harness.script(
            [taskgraph(task("t1")), FILES_BUILD, REVIEW_RC, refute, REVIEW_RC, REVIEW_OK]
        )
        engine = harness.pipeline(fake, keep_sandboxes=True)
        result = engine.start("write hello.txt")
        assert result.state == "merged"
        assert [event for event, _, _ in fake.reviews] == ["REQUEST_CHANGES", "APPROVE"]
        reviews = [
            j
            for j in harness.agent_jobs(result.run_id)
            if (j.get("prompt") or "").startswith("# Review the pull request")
        ]
        assert len(reviews) == 3  # round 1, round 2, round 2's retry
        (first,) = [j for j in reviews if "(first review of this pull request)" in j["prompt"]]
        assert "### Round" not in first["prompt"]
        second = [j["prompt"] for j in reviews if "### Round 1 — request_changes" in j["prompt"]]
        assert len(second) == 2
        assert all("the greeting is specified as hi" in p for p in second)
        (retry,) = [p for p in second if "already refuted" in p]
        assert "hello.txt:1" in retry
        rows = [r for r in engine.store.phase_attempts(result.run_id) if r["phase"] == "review"]
        assert [r["attempt"] for r in rows] == [1, 2]

    def test_delivery_opens_one_draft_pr_and_refreshes_it(self, harness: Harness) -> None:
        fake = FakeGithub()
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_RC, BUILD, REVIEW_OK])
        result = harness.pipeline(fake).start("ship hello")
        assert result.state == "merged"
        assert fake.pr_kwargs == {
            "repo": "o/r",
            "base": "main",
            "head": f"sbxloop/{result.run_id}",
            "title": "sbxloop: ship hello",
            "body": fake.pr_kwargs["body"],
            "draft": True,
        }
        assert "hello.txt" in fake.pr_kwargs["body"]
        commits = [p for m, p, _ in fake.raw_calls if m == "POST" and p.endswith("/git/commits")]
        assert len(commits) == 2
        patches = [p for m, p, _ in fake.raw_calls if m == "PATCH"]
        assert patches == [f"/repos/o/r/git/refs/heads/sbxloop/{result.run_id}"]

    def test_the_review_still_counts_when_github_refuses_the_post(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.fail_once["pr_review_create"] = GithubOpsError("reviews closed", http_status=422)
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        result = harness.pipeline(fake).start("ship hello")
        assert result.state == "merged"
        (verdict,) = self._events(harness, HostEventTypes.REVIEW_VERDICT)
        assert verdict.data["verdict"] == "approve" and verdict.data["url"] == ""

    def test_review_exhaustion_fails_with_the_pr_left_a_draft(self, harness: Harness) -> None:
        fake = FakeGithub()
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_RC, BUILD, REVIEW_RC])
        engine = harness.pipeline(fake, landing={"max_review_rounds": 1})
        result = engine.start("never good enough")
        assert result.state == "failed"
        assert result.reason is not None
        assert "review fix rounds exhausted (1 allowed by [landing] review_rounds)" in result.reason
        assert result.pr_number == 7
        assert fake.pr["draft"] is True
        assert fake.merges == [] and fake.ready_calls == []
        assert [t.spec.id for t in result.tasks] == ["t1", "fix-1"]
        run = engine.store.get_run(result.run_id)
        assert run.review_rounds == 2 and run.stage == "reviewing"
        assert run.reason == result.reason

    def test_gate_red_spends_a_ci_round_before_delivering(self, harness: Harness) -> None:
        """A later task breaks what an earlier one proved: the gate over the
        whole tree catches it before GitHub does, on the CI budget."""
        gate = "grep -q green state.txt"
        fake = FakeGithub()
        harness.script(
            [
                taskgraph(task("t1", verify=[gate]), task("t2", deps=["t1"])),
                {"text": "t1", "files": {"state.txt": "green\n", "hello.txt": "hi\n"}},
                {"text": "t2 broke it", "files": {"state.txt": "red\n"}},
                {"text": "fixed", "files": {"state.txt": "green\n"}},
                REVIEW_OK,
            ]
        )
        engine = harness.pipeline(fake, sandbox={"gate_command": gate})
        result = engine.start("keep the gate green")
        assert result.state == "merged"
        assert [(t.spec.id, t.state) for t in result.tasks] == [
            ("t1", "done"),
            ("t2", "done"),
            ("fix-1", "done"),
        ]
        fix = result.tasks[2].spec
        assert fix.description.startswith("The work in this tree is not yet acceptable")
        assert f"#### `{gate}` (failure)" in fix.description
        assert fix.verify_commands == [gate, "true"]
        assert fix.acceptance_criteria[0] == "the project gate passes"
        (fix_round,) = self._events(harness, HostEventTypes.FIX_ROUND)
        assert fix_round.data["kind"] == "gate" and fix_round.data["pr"] is None
        gates = [
            (e.data["status"], e.data["attempt"])
            for e in self._events(harness, HostEventTypes.PHASE_END)
            if e.data["phase"] == "gate"
        ]
        assert gates[0] == ("failed", 1)
        run = engine.store.get_run(result.run_id)
        assert run.ci_rounds == 1 and run.review_rounds == 0
        assert harness.run_states()[3:5] == ["gating", "fixing"]
        # The tree that was delivered is green: the fix task's own verify
        # ran the gate (it is in its verify commands) and passed.
        assert (result.workspace / "state.txt").read_text() == "green\n"  # type: ignore[operator]

    def test_the_gate_is_re_run_after_a_gate_fix_round(self, harness: Harness) -> None:
        gate = "grep -q green state.txt"
        fake = FakeGithub()
        harness.script(
            [
                taskgraph(task("t1", verify=[gate]), task("t2", deps=["t1"])),
                {"text": "t1", "files": {"state.txt": "green\n", "hello.txt": "hi\n"}},
                {"text": "t2 broke it", "files": {"state.txt": "red\n"}},
                {"text": "fixed", "files": {"state.txt": "green\n"}},
                REVIEW_OK,
            ]
        )
        engine = harness.pipeline(fake, sandbox={"gate_command": gate})
        result = engine.start("keep the gate green")
        assert result.state == "merged"
        gates = [
            (e.data["status"], e.data["attempt"])
            for e in self._events(harness, HostEventTypes.PHASE_END)
            if e.data["phase"] == "gate"
        ]
        assert gates == [("failed", 1), ("ok", 2)]
        assert harness.run_states()[3:7] == ["gating", "fixing", "gating", "delivering"]

    def test_ci_red_fix_round_carries_the_log_excerpt(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.checks = [RED, GREEN]
        fake.failed_logs = [
            FailedCheck("lint", "failure", "E501 hello.txt:1 too long", "https://x")
        ]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK, BUILD, REVIEW_OK])
        engine = harness.pipeline(fake)
        result = engine.start("pass CI")
        assert result.state == "merged"
        fix = result.tasks[1].spec
        assert "E501 hello.txt:1 too long" in fix.description
        assert "#### `lint` (failure)" in fix.description
        assert "the `lint` check passes" in fix.acceptance_criteria
        (fix_round,) = self._events(harness, HostEventTypes.FIX_ROUND)
        assert fix_round.data["kind"] == "ci"
        assert fix_round.data["why"] == "1 of 1 check(s) failed: ci"
        statuses = [e.data["state"] for e in self._events(harness, HostEventTypes.CI_STATUS)]
        assert statuses == ["red", "green"]
        run = engine.store.get_run(result.run_id)
        assert run.ci_rounds == 1 and run.review_rounds == 0
        assert fake.merges == [(7, "squash", "commit2")]

    def test_ci_red_past_the_budget_fails(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.checks = [RED]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        result = harness.pipeline(fake, landing={"max_ci_rounds": 0}).start("pass CI")
        assert result.state == "failed"
        assert result.reason is not None
        assert "ci fix rounds exhausted (0 allowed by [landing] ci_rounds)" in result.reason
        assert fake.merges == []
        assert [t.spec.id for t in result.tasks] == ["t1"]

    def test_ci_that_never_reports_blocks(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.checks = [PENDING]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        engine = harness.pipeline(fake, landing={"ci_timeout_s": 0.05})
        result = engine.start("wait for CI")
        assert result.state == "blocked"
        assert result.reason is not None
        assert "ci_timeout_s=0.05" in result.reason
        assert fake.merges == []
        run = engine.store.get_run(result.run_id)
        assert run.state == "blocked" and run.stage == "awaiting_ci"

    def test_no_check_runs_is_trusted_only_after_the_settle_window(self, harness: Harness) -> None:
        """Actions registers check runs seconds after a push: "no checks
        yet" must not merge before CI has started."""
        clock = Clock()

        class SlowCi(FakeGithub):
            def pr_checks(self, repo: str, sha: str) -> Any:
                clock.advance(30.0)
                return super().pr_checks(repo, sha)

        fake = SlowCi()
        fake.checks = [NO_CHECKS]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        engine = harness.pipeline(fake, landing={"ci_settle_s": 100.0})
        engine.clock = clock
        result = engine.start("no CI here")
        assert result.state == "merged"
        # delivered at t; polled at +30, +60, +90 (waiting), +120 (settled);
        # then landing reads the checks once more
        assert len(fake.checks_calls) == 5
        (status,) = self._events(harness, HostEventTypes.CI_STATUS)
        assert status.data["total"] == 0 and status.data["state"] == "green"

    def test_a_long_ci_wait_is_not_charged_to_the_wall_clock(self, harness: Harness) -> None:
        clock = Clock()

        class SleepingEvent(threading.Event):
            def wait(self, timeout: float | None = None) -> bool:
                clock.advance(100.0)
                return True

        fake = FakeGithub()
        fake.checks = [PENDING, PENDING, GREEN]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        engine = harness.pipeline(fake, budgets={"max_wall_clock_s": 10.0})
        engine.clock = clock
        engine._wake = SleepingEvent()
        result = engine.start("slow CI")
        assert result.state == "merged"
        # two CI polls that found checks pending, plus the un-draft settle
        # read before the merge: 300s of waiting against a 10s budget
        assert engine._waited_s == 300.0

    def test_landing_405_blocks_and_a_resume_finishes(self, harness: Harness) -> None:
        """A protection rule no round can satisfy hands the PR to a human;
        once they have acted, the run resumes at landing — no decompose, no
        build, no review re-run."""
        fake = FakeGithub()
        fake.merge_outcomes = [BLOCKED_405]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        engine = harness.pipeline(fake)
        result = engine.start("land it")
        assert result.state == "blocked"
        assert result.reason == BLOCKED_405.reason
        assert not result.succeeded
        (blocked,) = self._events(harness, HostEventTypes.RUN_BLOCKED)
        assert blocked.data["pr"] == 7 and blocked.data["why"] == BLOCKED_405.reason
        run = engine.store.get_run(result.run_id)
        assert run.state == "blocked" and run.stage == "landing"
        assert run.reason == BLOCKED_405.reason
        assert fake.deleted_branches == []
        assert "blocked" in RESUMABLE_RUN_STATES

        fake.merge_outcomes = [MERGED]
        harness.script([])
        resumed = harness.pipeline(fake).resume(result.run_id)
        assert resumed.state == "merged"
        assert resumed.reason is None
        assert resumed.pr_number == 7
        assert harness.consumed() == 0, "a resume at landing must not re-run any agent phase"
        assert fake.merges == [(7, "squash", "commit1"), (7, "squash", "commit1")]
        run = engine.store.get_run(result.run_id)
        assert run.state == "merged" and run.reason is None

    def test_landing_409_re_judges_then_merges(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.merge_outcomes = [STALE_409, MERGED]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        result = harness.pipeline(fake).start("land it")
        assert result.state == "merged"
        assert len(fake.merges) == 2

    def test_a_pr_stuck_behind_its_base_is_bounded_then_blocked(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "behind"
        fake.update_ok = False
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        engine = harness.pipeline(fake, landing={"merge_update_attempts": 2})
        result = engine.start("land it")
        assert result.state == "blocked"
        assert result.reason is not None
        assert "still behind its base after 2 update(s)" in result.reason
        assert fake.updates == [(7, "commit1"), (7, "commit1")]
        updates = self._events(harness, HostEventTypes.LAND_UPDATE)
        assert [(e.data["attempt"], e.data["accepted"]) for e in updates] == [
            (1, False),
            (2, False),
        ]
        run = engine.store.get_run(result.run_id)
        assert run.update_attempts == 2 and run.update_head is None

    def test_a_conflict_spends_a_fix_round(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.pr["mergeable"] = False
        fake.pr["mergeable_state"] = "dirty"
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK, BUILD, REVIEW_OK])
        engine = harness.pipeline(fake)

        def rebased(event: Event) -> None:
            # The fix round's re-delivery rebuilds the commit on the base.
            if event.type == HostEventTypes.FIX_ROUND:
                fake.pr["mergeable"] = True
                fake.pr["mergeable_state"] = "clean"

        engine.bus.subscribe(rebased)
        result = engine.start("land it")
        assert result.state == "merged"
        (fix_round,) = self._events(harness, HostEventTypes.FIX_ROUND)
        assert fix_round.data["kind"] == "conflict"
        assert "conflicts with its base branch" in result.tasks[1].spec.description
        assert engine.store.get_run(result.run_id).ci_rounds == 1

    def test_conflict_fix_round_merges_the_base_into_the_clone(self, harness: Harness) -> None:
        """A conflicting PR cannot be fixed on the run's files alone (delivery
        overlays the tree onto the *current* base), so the round first merges
        origin/<base> into the clone and briefs the fixer on the markers."""
        fake = FakeGithub()
        fake.pr["mergeable"] = False
        fake.pr["mergeable_state"] = "dirty"
        merges: list[tuple[Path, str]] = []

        def merge_from_base(
            repo_path: Path, base_branch: str, *, remote: str = "origin"
        ) -> hostgit.MergeResult:
            merges.append((repo_path, base_branch))
            return hostgit.MergeResult(False, ("docs/x.md",), "merged origin/main: 1 conflict")

        harness.monkeypatch.setattr(hostgit, "merge_from_base", merge_from_base)
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK, BUILD, REVIEW_OK])
        engine = harness.pipeline(fake)

        def resolved(event: Event) -> None:
            if event.type == HostEventTypes.FIX_ROUND:
                fake.pr["mergeable"] = True
                fake.pr["mergeable_state"] = "clean"

        engine.bus.subscribe(resolved)
        result = engine.start("land it")

        assert result.state == "merged"
        assert result.mounted and result.workspace is not None
        # The run's own clone, against the repository's default branch
        # (FakeGithub's repo_get says main; no [github] deliver_base is set).
        assert merges == [(result.workspace, "main")]
        (fix_round,) = self._events(harness, HostEventTypes.FIX_ROUND)
        assert fix_round.data["kind"] == "conflict"
        assert fix_round.data["task_id"] == "fix-1"
        assert "conflicts with its base branch" in fix_round.data["why"]
        assert "merged origin/main: 1 conflict" in fix_round.data["why"]
        fix = result.tasks[1].spec
        assert fix.id == "fix-1"
        assert "merged origin/main: 1 conflict" in fix.description
        assert "left conflict markers in:" in fix.description
        assert "- `docs/x.md`" in fix.description
        assert "git add -A && git commit --no-edit" in fix.description

    def test_a_failed_base_merge_still_spends_the_conflict_round(self, harness: Harness) -> None:
        """A fetch/merge failure is logged, not fatal: the round runs on the
        tree as it is, and the brief carries no conflict section."""
        fake = FakeGithub()
        fake.pr["mergeable"] = False
        fake.pr["mergeable_state"] = "dirty"

        def merge_from_base(
            repo_path: Path, base_branch: str, *, remote: str = "origin"
        ) -> hostgit.MergeResult:
            raise ProvisionError("git fetch origin main failed: no route to host")

        harness.monkeypatch.setattr(hostgit, "merge_from_base", merge_from_base)
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK, BUILD, REVIEW_OK])
        engine = harness.pipeline(fake)

        def resolved(event: Event) -> None:
            if event.type == HostEventTypes.FIX_ROUND:
                fake.pr["mergeable"] = True
                fake.pr["mergeable_state"] = "clean"

        engine.bus.subscribe(resolved)
        result = engine.start("land it")

        assert result.state == "merged"
        assert [(t.spec.id, t.state) for t in result.tasks] == [("t1", "done"), ("fix-1", "done")]
        (fix_round,) = self._events(harness, HostEventTypes.FIX_ROUND)
        assert fix_round.data["kind"] == "conflict"
        assert "no route to host" not in fix_round.data["why"]
        fix = result.tasks[1].spec
        assert "conflicts with its base branch" in fix.description
        assert "conflict markers in" not in fix.description
        assert "git add -A" not in fix.description

    def test_the_review_diffs_against_the_current_base(self, harness: Harness) -> None:
        """The reviewer's diff is taken against the base branch's tip as
        GitHub has it now (ref_lookup heads/<base>), not the commit the
        clone was cut from — after a conflict round merged the base in, the
        latter would show the base's own movement as the run's changes."""
        fake = FakeGithub()
        bases: list[str | None] = []

        def diff_text(repo_path: Path, remote_base_sha: str | None) -> str | None:
            bases.append(remote_base_sha)
            return "diff --git a/hello.txt b/hello.txt\n+hi\n"

        harness.monkeypatch.setattr(hostgit, "diff_text", diff_text)
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        result = harness.pipeline(fake).start("land it")

        assert result.state == "merged"
        assert bases == ["base123"], "FakeGithub.ref_lookup answers base123 for heads/main"

    def test_a_humans_objection_spends_a_fix_round_with_their_words(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review("alice", "CHANGES_REQUESTED", "please say hello")]
        fake.feedback = "please say hello\n\n- `hello.txt:1`: hi is too casual"
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK, BUILD, REVIEW_OK])
        engine = harness.pipeline(fake)

        def satisfied(event: Event) -> None:
            if event.type == HostEventTypes.FIX_ROUND:
                fake.reviews_payload = [human_review("alice", "APPROVED", "thanks")]

        engine.bus.subscribe(satisfied)
        result = engine.start("land it")
        assert result.state == "merged"
        (fix_round,) = self._events(harness, HostEventTypes.FIX_ROUND)
        assert fix_round.data["kind"] == "human"
        brief = result.tasks[1].spec.description
        assert "Review comments a human left on the PR" in brief
        assert "hi is too casual" in brief
        # The loop read its own login to tell alice's review from its own.
        assert ("GET", "/user", None) in fake.raw_calls

    def test_the_loops_own_review_never_objects_to_itself(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review("sbxloop-bot", "CHANGES_REQUESTED", "round 1")]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        result = harness.pipeline(fake).start("land it")
        assert result.state == "merged"
        assert self._events(harness, HostEventTypes.FIX_ROUND) == []

    def test_a_pr_closed_by_a_human_fails_the_run(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.pr["state"] = "closed"
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        result = harness.pipeline(fake).start("land it")
        assert result.state == "failed"
        assert result.reason == "the pull request was closed without being merged"
        assert fake.merges == []

    def test_a_pr_merged_by_a_human_lands_without_a_merge_call(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.pr["merged"] = True
        fake.pr["merge_commit_sha"] = "human123"
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        result = harness.pipeline(fake).start("land it")
        assert result.state == "merged"
        assert fake.merges == []
        (merged,) = self._events(harness, HostEventTypes.RUN_MERGED)
        assert merged.data["sha"] == "human123" and merged.data["by_human"] is True

    def test_failed_graph_never_delivers(self, harness: Harness) -> None:
        fake = FakeGithub()
        harness.script([taskgraph(task("t1", verify=["false"])), BUILD])
        result = harness.pipeline(fake, budgets=NO_RETRY_BUDGETS).start("doomed")
        assert result.state == "failed"
        assert result.pr_number is None
        assert fake.pr_created is False
        assert self._events(harness, HostEventTypes.RUN_DELIVER) == []

    def test_a_missing_repository_fails_before_any_work(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.repo_lookup = lambda repo: None  # type: ignore[method-assign]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK])
        from sbxloop.errors import DeliveryError

        with pytest.raises(DeliveryError, match="does not exist"):
            harness.pipeline(fake).start("nowhere to go")
        assert harness.consumed() == 0

    # -- chat during the pipeline -------------------------------------------

    STEER_CONTINUE: ClassVar[dict[str, Any]] = {
        "json": {"reply": "still waiting on CI", "action": "continue"}
    }
    STEER_TASK: ClassVar[dict[str, Any]] = {
        "json": {
            "reply": "restarting the fix",
            "action": "steer_task",
            "guidance": "greet in French",
        }
    }

    def test_a_chat_message_cuts_a_ci_wait_short(self, harness: Harness) -> None:
        """A poll interval must never delay an answer: the message wakes the
        wait, and the reply lands at the next tick — before the run ends."""
        fake = FakeGithub()
        fake.checks = [PENDING, PENDING, GREEN]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_OK, self.STEER_CONTINUE])
        # not a draft: no un-draft settle wait muddies the arithmetic below
        engine = harness.pipeline(fake, landing={"ci_poll_interval_s": 3.0, "deliver_draft": False})
        posted: list[str] = []

        def ask_during_the_wait(event: Event) -> None:
            if event.type == HostEventTypes.CI_STATUS and not posted:
                posted.append("armed")
                threading.Timer(0.2, lambda: engine.post_user_message("how is CI?")).start()

        engine.bus.subscribe(ask_during_the_wait)
        result = engine.start("wait for CI")
        assert result.state == "merged"
        # two full intervals would be 6s of waiting; the first was cut short
        assert engine._waited_s < 5.0, engine._waited_s
        types = harness.event_types()
        assert types.index("chat.reply") < types.index(HostEventTypes.RUN_END)
        reply = next(e for e in harness.events if e.type == "chat.reply")
        assert reply.data["reply"] == "still waiting on CI"
        steer = next(r for r in engine.store.phase_attempts(result.run_id) if r["phase"] == "steer")
        assert steer["task_id"] is None

    def test_steer_task_on_a_fix_task_restarts_its_build(self, harness: Harness) -> None:
        fake = FakeGithub()
        harness.script(
            [
                taskgraph(task("t1")),
                FILES_BUILD,
                REVIEW_RC,
                BUILD,  # fix-1, first attempt
                self.STEER_TASK,
                {"text": "bonjour", "files": {"hello.txt": "bonjour\n"}},  # restarted
                REVIEW_OK,
            ]
        )
        engine = harness.pipeline(fake, keep_sandboxes=True)
        posted: list[str] = []

        def steer_after_the_fix_build(event: Event) -> None:
            if (
                event.type == HostEventTypes.PHASE_END
                and event.data.get("phase") == "build"
                and event.data.get("task_id") == "fix-1"
                and not posted
            ):
                posted.append(engine.post_user_message("do it in French"))

        engine.bus.subscribe(steer_after_the_fix_build)
        result = engine.start("write hello.txt")
        assert result.state == "merged"
        fix = result.tasks[1]
        assert fix.state == "done" and fix.revisions == 0 and fix.replans == 0
        action = next(e for e in harness.events if e.type == "chat.action")
        assert action.data["action"] == "steer_task" and action.data["task_id"] == "fix-1"
        builds = [
            r
            for r in engine.store.phase_attempts(result.run_id)
            if r["phase"] == "build" and r["task_id"] == "fix-1"
        ]
        assert len(builds) == 2
        prompts = [
            j["prompt"]
            for j in harness.agent_jobs(result.run_id)
            if "user steering (must be honored): greet in French" in (j.get("prompt") or "")
        ]
        assert len(prompts) == 1
        assert (result.workspace / "hello.txt").read_text() == "bonjour\n"  # type: ignore[operator]

    # -- resume at every post-delivery stage ----------------------------------

    def _interrupted(
        self, harness: Harness, fake: FakeGithub, script: list[dict[str, Any]], exc: type[Exception]
    ) -> tuple[LoopEngine, str]:
        harness.script(script)
        engine = harness.pipeline(fake)
        with pytest.raises(exc):
            engine.start("resume me")
        return engine, engine.store.list_runs()[0].run_id

    def test_resume_at_delivering_re_delivers(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.fail_once["blobs_create_many"] = GithubOpsError("blob store down", http_status=502)
        engine, run_id = self._interrupted(
            harness, fake, [taskgraph(task("t1")), FILES_BUILD], GithubOpsError
        )
        run = engine.store.get_run(run_id)
        assert run.state == "delivering" and run.stage == "delivering"
        assert run.pr_number is None
        harness.script([REVIEW_OK])
        result = harness.pipeline(fake).resume(run_id)
        assert result.state == "merged"
        assert fake.pr_created and result.pr_number == 7
        # decompose/build never re-ran: the resume consumed only the review
        assert harness.consumed() == 1
        assert harness.run_states()[-6:] == [
            "provisioning",
            "delivering",
            "reviewing",
            "awaiting_ci",
            "landing",
            "merged",
        ]

    def test_resume_at_reviewing_reviews_without_re_delivering(self, harness: Harness) -> None:
        fake = FakeGithub()
        engine, run_id = self._interrupted(
            harness,
            fake,
            [taskgraph(task("t1")), FILES_BUILD, {"fail": "reviewer exploded"}],
            WorkerError,
        )
        run = engine.store.get_run(run_id)
        assert run.state == "reviewing" and run.pr_number == 7
        harness.script([REVIEW_OK])
        result = harness.pipeline(fake).resume(run_id)
        assert result.state == "merged"
        commits = [p for m, p, _ in fake.raw_calls if m == "POST" and p.endswith("/git/commits")]
        assert len(commits) == 1, "the PR already carried the work; no re-delivery"
        assert fake.merges == [(7, "squash", "commit1")]

    def test_resume_at_fixing_finishes_the_fix_task(self, harness: Harness) -> None:
        fake = FakeGithub()
        engine, run_id = self._interrupted(
            harness,
            fake,
            [taskgraph(task("t1")), FILES_BUILD, REVIEW_RC, {"fail": "fixer exploded"}],
            WorkerError,
        )
        run = engine.store.get_run(run_id)
        assert run.state == "fixing" and run.review_rounds == 1
        assert [(t.spec.id, t.state) for t in engine.store.get_tasks(run_id)] == [
            ("t1", "done"),
            ("fix-1", "executing"),
        ]
        harness.script([BUILD, REVIEW_OK])
        result = harness.pipeline(fake).resume(run_id)
        assert result.state == "merged"
        assert [(t.spec.id, t.state) for t in result.tasks] == [("t1", "done"), ("fix-1", "done")]
        assert engine.store.get_run(run_id).review_rounds == 1, "a resume spends no round"
        assert fake.merges == [(7, "squash", "commit2")]

    def test_resume_at_awaiting_ci_re_polls(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.fail_once["pr_checks"] = GithubOpsError("github down", http_status=502)
        engine, run_id = self._interrupted(
            harness, fake, [taskgraph(task("t1")), FILES_BUILD, REVIEW_OK], GithubOpsError
        )
        run = engine.store.get_run(run_id)
        assert run.state == "awaiting_ci" and run.head_sha == "commit1"
        harness.script([])
        result = harness.pipeline(fake).resume(run_id)
        assert result.state == "merged"
        assert harness.consumed() == 0
        reviews = [r for r in engine.store.phase_attempts(run_id) if r["phase"] == "review"]
        assert len(reviews) == 1

    def test_resume_at_landing_lands(self, harness: Harness) -> None:
        fake = FakeGithub()
        fake.fail_once["pr_merge"] = GithubOpsError("github down", http_status=502)
        engine, run_id = self._interrupted(
            harness, fake, [taskgraph(task("t1")), FILES_BUILD, REVIEW_OK], GithubOpsError
        )
        assert engine.store.get_run(run_id).state == "landing"
        harness.script([])
        result = harness.pipeline(fake).resume(run_id)
        assert result.state == "merged"
        assert harness.consumed() == 0
        assert len(fake.merges) == 2

    def test_every_pipeline_stage_is_resumable(self) -> None:
        assert set(PIPELINE_STAGES) <= RESUMABLE_RUN_STATES


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
            "reply": "restarting this task",
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

    def test_steer_task_restarts_build_without_spending_budgets(self, harness: Harness) -> None:
        # Message absorbed at the first boundary (task in `executing`): the
        # steer verdict discards the build session and restarts it with the
        # guidance as feedback — user direction, not a failure, so neither
        # budget counter is spent.
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
        assert "restarting task t1" in action.data["message"]
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

    def test_a_message_after_the_last_task_is_answered_before_the_gate(
        self, harness: Harness
    ) -> None:
        """A message arriving during the last build has no task boundary
        left to land on; it is answered between the graph and the gate."""
        harness.script([taskgraph(task("t1")), BUILD, self.STEER_CONTINUE])
        engine = harness.engine()

        def ask_late(event: Event) -> None:
            if event.type == HostEventTypes.TASK_END:
                engine.post_user_message("are we done?")

        engine.bus.subscribe(ask_late)
        result = engine.start("build the feature")
        assert result.state == "completed"
        types = harness.event_types()
        assert types.index("chat.reply") < types.index(HostEventTypes.RUN_END)
        steer = next(r for r in engine.store.phase_attempts(result.run_id) if r["phase"] == "steer")
        assert steer["task_id"] is None

    def test_resume_replays_persisted_guidance_into_prompts(self, harness: Harness) -> None:
        from sbxloop.engine.phases import PhaseRunner

        # Run 1: steer_run lands guidance, then the task fails its only
        # verify (no retry budgets) and the run fails — a resumable state.
        harness.script([taskgraph(task("t1", verify=["false"])), self.STEER_RUN, BUILD])
        engine = harness.engine(budgets=NO_RETRY_BUDGETS)
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
        resumed = harness.engine(budgets=NO_RETRY_BUDGETS).resume(result.run_id)
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


class TestReviewPostFallback:
    """Field run rx8amxxvm (#130 → PR #503): the reviewer approved with two
    nits anchored to lines outside the diff, GitHub 422'd the APPROVE and its
    COMMENT fallback alike, and nothing reached the PR. The findings matter
    more than their anchors."""

    def test_a_review_refused_for_its_anchors_is_reposted_in_the_body(
        self, harness: Harness
    ) -> None:
        nit = {"path": "hello.txt", "line": 1, "body": "stale docstring", "severity": "nit"}
        fake = FakeGithub(draft=True)
        fake.refuse_inline_comments = True
        harness.script([taskgraph(task("t1")), FILES_BUILD, review("approve", "fine", nit)])
        result = harness.pipeline(fake).start("write hello.txt")
        assert result.state == "merged"
        assert [event for event, _, _ in fake.reviews] == ["APPROVE"]
        _, body, comments = fake.reviews[0]
        assert comments == []
        assert "Findings:" in body and "`hello.txt:1` [nit] stale docstring" in body
        assert "fine" in body
