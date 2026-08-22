"""PhaseRunner shell batching (#125): verify and scrutinize evidence must
each cost ONE worker job, not one per command — the per-job round-trip
(stage JSON, cold interpreter, fetch result) dwarfs the commands' real work."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.engine.model import PlanModel, TaskGraph, TaskRecord, TaskSpec
from sbxloop.engine.phases import (
    EVIDENCE_COMMANDS,
    VERIFY_HEAD_CLIP,
    VERIFY_TAIL_CLIP,
    PhaseRunner,
    clip_head_tail,
)
from sbxloop_worker.protocol import BatchCommandResult, ErrorInfo, JobRequest, JobResult


class BatchStubAgent:
    """Answers shell.batch jobs with scripted per-command exits and
    agent.session jobs with a canned verdict; captures every JobRequest."""

    def __init__(
        self,
        *,
        exits: dict[str, int] | None = None,
        verdict: dict[str, str] | None = None,
        batch_error: bool = False,
        outputs: dict[str, str] | None = None,
    ) -> None:
        self.exits = exits or {}
        self.outputs = outputs or {}
        self.verdict = verdict or {"verdict": "pass"}
        self.batch_error = batch_error
        self.jobs: list[JobRequest] = []

    def submit(self, job: JobRequest, *, agent: str | None = None) -> JobResult:
        self.jobs.append(job)
        if job.kind == "shell.batch":
            if self.batch_error:
                return JobResult(
                    job_id=job.job_id,
                    status="error",
                    error=ErrorInfo(type="Boom", message="worker died"),
                )
            assert job.commands is not None
            results = [
                BatchCommandResult(
                    command=command,
                    exit_code=self.exits.get(command, 0),
                    output=self.outputs.get(command, f"output of {command}"),
                ).model_dump()
                for command in job.commands
            ]
            return JobResult(job_id=job.job_id, status="ok", output_json=results)
        return JobResult(job_id=job.job_id, status="ok", output_json=self.verdict)


def runner(agent: BatchStubAgent) -> PhaseRunner:
    return PhaseRunner(agent, Config(), "r1", "ship it", workdir="/work")  # type: ignore[arg-type]


def record(verify: list[str] | None = None) -> TaskRecord:
    return TaskRecord(
        spec=TaskSpec(id="t1", title="Build it", verify_commands=verify or []),
        state="verifying",
    )


def plan(verify: list[str] | None = None) -> PlanModel:
    return PlanModel(steps=["do"], expected_artifacts=[], verify_commands=verify or [])


class TestVerifyBatching:
    def test_all_commands_ride_one_job(self) -> None:
        agent = BatchStubAgent()
        outcome = runner(agent).verify(record(["pytest -q", "ruff check ."]), plan(["mypy ."]))

        assert outcome.passed
        assert len(agent.jobs) == 1
        job = agent.jobs[0]
        assert job.kind == "shell.batch"
        assert job.commands == ["pytest -q", "ruff check .", "mypy ."]
        assert job.cwd == "/work"
        # per-command semantics preserved: each command gets the per-job
        # timeout; the job budget covers the worst case of all of them
        per_job = Config().budgets.per_job_timeout_s
        assert job.command_timeout_s == per_job
        assert job.timeout_s == per_job * 3

    def test_duplicate_commands_deduped(self) -> None:
        agent = BatchStubAgent()
        runner(agent).verify(record(["pytest -q"]), plan(["pytest -q", "mypy ."]))
        assert agent.jobs[0].commands == ["pytest -q", "mypy ."]

    def test_failures_reported_per_command(self) -> None:
        agent = BatchStubAgent(exits={"exit 2": 2})
        outcome = runner(agent).verify(record(["true", "exit 2"]), plan())

        assert not outcome.passed
        assert "verify command failed: `exit 2` (exit 2)" in outcome.feedback
        assert "$ true\n(exit 0)" in outcome.results
        assert "$ exit 2\n(exit 2)" in outcome.results

    def test_long_output_keeps_head_and_tail(self) -> None:
        # #253: a long pytest run prints its first traceback near the top
        # and the "N failed" summary at the bottom; a tail-only clip handed
        # the critic a summary with no assertion text.
        head = "FAILED tests/test_a.py::test_x - AssertionError: first\n" + "a" * 3_000
        tail = "b" * 5_000 + "\n=== 1 failed, 999 passed ==="
        agent = BatchStubAgent(exits={"pytest -q": 1}, outputs={"pytest -q": head + tail})
        outcome = runner(agent).verify(record(["pytest -q"]), plan())

        assert not outcome.passed
        assert "AssertionError: first" in outcome.feedback
        assert "1 failed, 999 passed" in outcome.feedback
        assert "...(clipped" in outcome.feedback
        assert len(outcome.feedback) < VERIFY_HEAD_CLIP + VERIFY_TAIL_CLIP + 200
        assert "AssertionError: first" in outcome.results

    def test_clip_head_tail_passes_short_text_through(self) -> None:
        assert clip_head_tail("short", 2, 3) == "short"
        assert clip_head_tail(None) == ""
        clipped = clip_head_tail("0123456789", 2, 3)
        assert clipped.startswith("01\n...(clipped 5 chars)...\n789")

    def test_no_commands_submits_no_job(self) -> None:
        agent = BatchStubAgent()
        outcome = runner(agent).verify(record(), plan())
        assert outcome.passed
        assert outcome.results == "(no verify commands)"
        assert agent.jobs == []


class TestScrutinizeEvidenceBatching:
    def test_evidence_rides_one_job(self) -> None:
        agent = BatchStubAgent()
        outcome = runner(agent).scrutinize(record(), plan(), "did the work")

        assert outcome.verdict.verdict == "pass"
        batches = [j for j in agent.jobs if j.kind == "shell.batch"]
        assert len(batches) == 1
        assert batches[0].commands == [item.command for item in EVIDENCE_COMMANDS]
        # evidence output reaches the critic's prompt under its label
        prompt = next(j for j in agent.jobs if j.kind == "agent.session").prompt or ""
        assert "### git status" in prompt
        assert "output of git status --short 2>&1 | head -50" in prompt

    def test_the_diff_gets_a_far_larger_budget_than_the_other_evidence(self) -> None:
        """The critic is meant to judge from the diff rather than rediscover
        the change by hand: every turn it spends hunting re-sends the whole
        session context, so a patch that survives into the prompt is worth
        far more than the bytes it costs. A 1.5k clip would have truncated
        every real diff into uselessness."""
        diff = next(item for item in EVIDENCE_COMMANDS if item.label == "git diff")
        assert diff.command == "git diff HEAD 2>&1"  # staged work included
        assert diff.limit >= 10 * max(
            item.limit for item in EVIDENCE_COMMANDS if item.label != "git diff"
        )

        body = "\n".join(f"+line {i}" for i in range(4_000))
        agent = BatchStubAgent(outputs={diff.command: body})
        runner(agent).scrutinize(record(), plan(), "did the work")
        prompt = next(j for j in agent.jobs if j.kind == "agent.session").prompt or ""

        assert "### git diff" in prompt
        assert "+line 0" in prompt  # head survives...
        assert "+line 3999" in prompt  # ...and so does the tail
        assert "(clipped" in prompt  # and the elision is declared

    def test_evidence_job_failure_is_not_fatal(self) -> None:
        agent = BatchStubAgent(batch_error=True)
        outcome = runner(agent).scrutinize(record(), plan(), "did the work")

        assert outcome.verdict.verdict == "pass"
        prompt = next(j for j in agent.jobs if j.kind == "agent.session").prompt or ""
        assert "(no evidence gathered)" in prompt


class TestExecutorContinuity:
    """A revision continues the same agent's own work on the same task.

    Field failure rrhb28j7n/t5: five executor sessions on one task, each with
    its own session id, each re-running `uv sync --all-packages` and the whole
    lint gate from scratch, each concluding "no changes needed". A revision
    was told what the critic objected to and nothing about what the previous
    attempt had already established — so it re-established it.
    """

    def test_the_prior_report_reaches_the_prompt(self) -> None:
        agent = BatchStubAgent()
        runner(agent).execute(record(), plan(), prior_report="already ran uv sync; gate is green")
        prompt = next(j for j in agent.jobs if j.kind == "agent.session").prompt or ""
        assert "already ran uv sync; gate is green" in prompt
        assert "What the previous attempt already did" in prompt

    def test_a_first_attempt_says_so_rather_than_leaving_a_hole(self) -> None:
        agent = BatchStubAgent()
        runner(agent).execute(record(), plan())
        prompt = next(j for j in agent.jobs if j.kind == "agent.session").prompt or ""
        assert "(none — this is the first attempt)" in prompt

    def test_a_long_prior_report_is_clipped(self) -> None:
        agent = BatchStubAgent()
        runner(agent).execute(record(), plan(), prior_report="x" * 50_000)
        prompt = next(j for j in agent.jobs if j.kind == "agent.session").prompt or ""
        assert "(clipped)" in prompt

    def test_the_session_id_rides_on_the_job(self) -> None:
        agent = BatchStubAgent()
        runner(agent).execute(record(), plan(), resume_session_id="sess-42")
        job = next(j for j in agent.jobs if j.kind == "agent.session")
        assert job.resume_session_id == "sess-42"

    def test_execute_starts_fresh_when_not_given_one(self) -> None:
        agent = BatchStubAgent()
        runner(agent).execute(record(), plan())
        job = next(j for j in agent.jobs if j.kind == "agent.session")
        assert job.resume_session_id is None

    def test_no_critic_phase_ever_resumes(self) -> None:
        """The critics are fresh by design: a reviewer that inherited the
        executor's session would inherit its conclusions, and that
        independence is the loop's integrity check."""
        scrutiny = BatchStubAgent()
        runner(scrutiny).scrutinize(record(), plan(), "did the work")
        validation = BatchStubAgent(verdict={"verdict": "accept"})
        runner(validation).validate(record(), "(no verify commands)")
        sessions = [
            j for agent in (scrutiny, validation) for j in agent.jobs if j.kind == "agent.session"
        ]
        assert len(sessions) == 2
        assert all(j.resume_session_id is None for j in sessions)


class TestProjectGateAtGraphScope:
    """A delivered PR (#389) failed `mdformat` and `security` — both plain
    `make check` targets — because nothing in the run ran what CI enforces.

    The gate is required of the graph, not of each task: demanding it per
    task would run a multi-minute check once per task for no extra signal.
    """

    def _graph(self, *per_task: list[str]) -> TaskGraph:
        return TaskGraph(
            tasks=[
                TaskSpec(id=f"t{i}", title=f"T{i}", verify_commands=commands)
                for i, commands in enumerate(per_task, start=1)
            ]
        )

    def _runner(self, tmp_path: Path, *, gate: bool) -> PhaseRunner:
        if gate:
            (tmp_path / "Makefile").write_text("check:\n\t@echo ok\n")
        agent = BatchStubAgent()
        return PhaseRunner(  # type: ignore[arg-type]
            agent, Config(), "r1", "ship it", workdir="/work", workspace=tmp_path
        )

    def test_one_task_carrying_the_gate_satisfies_the_graph(self, tmp_path: Path) -> None:
        runner_ = self._runner(tmp_path, gate=True)
        graph = self._graph(["uv run pytest -q"], ["make check"])
        runner_._check_taskgraph_verify_commands(graph)  # does not raise

    def test_a_graph_that_never_runs_the_gate_is_rejected(self, tmp_path: Path) -> None:
        runner_ = self._runner(tmp_path, gate=True)
        graph = self._graph(["uv run pytest -q"], ["uv run ruff check ."])
        with pytest.raises(ValueError, match="make check"):
            runner_._check_taskgraph_verify_commands(graph)

    def test_a_project_without_a_gate_is_not_held_to_one(self, tmp_path: Path) -> None:
        runner_ = self._runner(tmp_path, gate=False)
        runner_._check_taskgraph_verify_commands(self._graph(["uv run pytest -q"]))

    def test_the_gate_is_not_demanded_of_every_task(self, tmp_path: Path) -> None:
        """The scope decision, pinned: a per-task rule would make a 4-minute
        check run once per task."""
        runner_ = self._runner(tmp_path, gate=True)
        assert runner_._lint_verify_commands(["uv run pytest -q"]) == []
