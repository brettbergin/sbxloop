"""A ``workload`` run (#755): the operator's stage list on the same run
shape — one sandbox, its own data directory, no repository.

The engine harness drives a scripted agent through plan → execute →
judge → publish, and the assertions are on what the run leaves behind:
the sandboxes it created, the states it walked, the row it persisted and
what a resume makes of them. The developer loop's own trail is gated
separately (``test_code_run_trail.py``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.engine.model import RESUMABLE_RUN_STATES, WORKLOAD_STAGES
from sbxloop.errors import WorkerError
from sbxloop.events import HostEventTypes
from tests.conftest import FakeSbx
from tests.fakes.fake_github import FakeGithub
from tests.unit.test_engine import BUILD, FILES_BUILD, Harness, task, taskgraph

WORKLOAD_STATES = ["provisioning", "planning", "executing", "judging", "publishing", "completed"]


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


class TestWorkloadRun:
    def test_one_sandbox_and_the_operator_stages(self, harness: Harness) -> None:
        """No `[github]`: the agent box alone, the four stages in order,
        `completed` at the end — and the run says what it is."""
        harness.script([taskgraph(task("t1", verify=["test -f hello.txt"])), FILES_BUILD])
        engine = harness.engine(keep_sandboxes=True)
        result = engine.start("write hello.txt", kind="workload")

        assert result.state == "completed" and result.kind == "workload"
        assert result.succeeded
        assert harness.run_states() == WORKLOAD_STATES
        assert harness.sandboxes_left() == [f"sbxloop-{result.run_id}-agent"]
        run = engine.store.get_run(result.run_id)
        assert run.kind == "workload" and run.state == "completed"
        assert run.stage == "publishing", "the last non-terminal stage, for a resume"
        # The data directory: the per-run dir an unconfigured code run gets,
        # harvested rather than mounted as the work.
        assert result.workspace == harness.state_dir / "runs" / result.run_id / "workspace"
        (start,) = [e for e in harness.events if e.type == HostEventTypes.RUN_START]
        assert start.data["kind"] == "workload"
        assert start.data["workspace"] is None
        assert start.data["workspace_source"] == "data-dir"
        # The toolchain set is the config's answer, not a detection over an
        # empty directory.
        (languages,) = [e for e in harness.events if e.type == "sandbox.languages"]
        assert languages.data["source"] == "default"
        # The judgment re-ran the task's check on the finished workspace.
        (judge,) = [r for r in engine.store.phase_attempts(result.run_id) if r["phase"] == "judge"]
        assert judge["status"] == "ok" and judge["task_id"] is None
        ends = [
            (e.data["phase"], e.data["status"])
            for e in harness.events
            if e.type == HostEventTypes.PHASE_END and e.data.get("task_id") is None
        ]
        assert ends == [("judge", "ok")], "no gate, no review: a workload's own stages only"

    def test_a_configured_repository_changes_nothing(self, harness: Harness) -> None:
        """`[github]` set but the run is a workload: still one sandbox, no
        delivery, and GitHub is never called."""
        fake = FakeGithub()
        harness.script([taskgraph(task("t1")), BUILD])
        engine = harness.engine(ops=fake, github={"repo": fake.repo}, keep_sandboxes=True)
        result = engine.start("count the things", kind="workload")

        assert result.state == "completed"
        assert harness.run_states() == WORKLOAD_STATES
        assert harness.sandboxes_left() == [f"sbxloop-{result.run_id}-agent"]
        assert result.pr_number is None and fake.pr_create_calls == 0
        assert not any(e.type.startswith("run.deliver") for e in harness.events)
        assert not any(e.type.startswith("sandbox.workspace_clone") for e in harness.events)

    def test_the_workspace_config_belongs_to_code_runs(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        """A configured `[sandbox] workspace` is not mounted into a workload:
        the run works in its data directory whatever the config names."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "README.md").write_text("theirs\n")
        harness.script([taskgraph(task("t1")), BUILD])
        engine = harness.engine(
            sandbox={"workspace": str(checkout), "workspace_isolation": "in-place"}
        )
        result = engine.start("count the things", kind="workload")
        assert result.state == "completed"
        assert result.workspace == harness.state_dir / "runs" / result.run_id / "workspace"
        assert not (result.workspace / "README.md").exists()

    def test_setup_commands_are_a_checkouts(self, harness: Harness) -> None:
        """`setup_commands` prepare a clone; a workload has none to prepare."""
        harness.script([taskgraph(task("t1")), BUILD])
        engine = harness.engine(sandbox={"setup_commands": ["exit 9"]})
        result = engine.start("count the things", kind="workload")
        assert result.state == "completed"
        assert not any(e.type == HostEventTypes.SANDBOX_SETUP for e in harness.events)

    def test_a_red_judgment_ends_the_run_named(self, harness: Harness) -> None:
        """A later task undid what an earlier one proved: the judge catches
        it on the finished workspace and the run fails naming the check."""
        gate = "grep -q green state.txt"
        harness.script(
            [
                taskgraph(task("t1", verify=[gate]), task("t2", deps=["t1"])),
                {"text": "t1", "files": {"state.txt": "green\n"}},
                {"text": "t2 broke it", "files": {"state.txt": "red\n"}},
            ]
        )
        engine = harness.engine()
        result = engine.start("keep it green", kind="workload")

        assert result.state == "failed"
        assert result.reason is not None
        # t2's own `true` is the other check: the count is over every task's.
        assert result.reason == f"the judgment failed: 1 of 2 check(s) red — `{gate}` (exit 1)"
        assert harness.run_states() == [
            "provisioning",
            "planning",
            "executing",
            "judging",
            "failed",
        ]
        assert [(t.spec.id, t.state) for t in result.tasks] == [("t1", "done"), ("t2", "done")]
        (judge,) = [r for r in engine.store.phase_attempts(result.run_id) if r["phase"] == "judge"]
        assert judge["status"] == "failed"
        run = engine.store.get_run(result.run_id)
        assert run.state == "failed" and run.stage == "judging"

    def test_a_failed_task_fails_the_run_before_the_judgment(self, harness: Harness) -> None:
        harness.script(
            [
                taskgraph(task("t1", verify=["test -f never.txt"])),
                BUILD,
                BUILD,
                BUILD,
            ]
        )
        engine = harness.engine(budgets={"max_revisions_per_task": 0, "max_replans_per_task": 0})
        result = engine.start("write never.txt", kind="workload")
        assert result.state == "failed"
        assert "judging" not in harness.run_states()
        assert not any(r["phase"] == "judge" for r in engine.store.phase_attempts(result.run_id))


class TestWorkloadResume:
    def _interrupted_in_executing(self, harness: Harness) -> tuple[Harness, str]:
        harness.script([taskgraph(task("t1")), {"fail": "sandbox exploded"}])
        engine = harness.engine()
        with pytest.raises(WorkerError, match="sandbox exploded"):
            engine.start("count the things", kind="workload")
        (run,) = engine.store.list_runs()
        assert run.kind == "workload"
        assert run.state == "executing" and run.stage == "executing"
        assert [t.state for t in engine.store.get_tasks(run.run_id)] == ["executing"]
        return harness, run.run_id

    def test_resume_re_enters_executing_and_keeps_the_kind(self, harness: Harness) -> None:
        """The kind is the row's, never the config's: a fresh engine under
        a config that says nothing of workloads resumes the workload."""
        harness, run_id = self._interrupted_in_executing(harness)
        harness.events.clear()
        harness.script([BUILD])
        result = harness.engine(github={"repo": "o/r"}).resume(run_id)

        assert result.state == "completed" and result.kind == "workload"
        assert harness.run_states() == [
            "provisioning",
            "executing",
            "judging",
            "publishing",
            "completed",
        ]
        (start,) = [e for e in harness.events if e.type == HostEventTypes.RUN_START]
        assert start.data["resumed"] is True and start.data["kind"] == "workload"
        # Still one sandbox, still the same data directory.
        assert harness.sandboxes_left() == []
        assert result.workspace == harness.state_dir / "runs" / run_id / "workspace"
        phases = [r["phase"] for r in harness.engine().store.phase_attempts(run_id)]
        assert phases.count("decompose") == 1 and phases.count("build") == 1

    def test_resume_at_judging_re_judges(self, harness: Harness) -> None:
        harness.script([taskgraph(task("t1", verify=["test -f hello.txt"])), FILES_BUILD])
        engine = harness.engine()
        result = engine.start("write hello.txt", kind="workload")
        assert result.state == "completed"
        # Park the run as if it had died in the judgment.
        engine.store.set_run_state(result.run_id, "judging")
        harness.events.clear()
        harness.script([])
        resumed = harness.engine().resume(result.run_id)
        assert resumed.state == "completed"
        assert harness.run_states() == ["provisioning", "judging", "publishing", "completed"]
        judged = [r for r in engine.store.phase_attempts(result.run_id) if r["phase"] == "judge"]
        assert [r["attempt"] for r in judged] == [1, 2]

    def test_every_workload_stage_is_resumable(self) -> None:
        assert set(WORKLOAD_STAGES) <= RESUMABLE_RUN_STATES
