"""PhaseRunner shell batching (#125): verify and scrutinize evidence must
each cost ONE worker job, not one per command — the per-job round-trip
(stage JSON, cold interpreter, fetch result) dwarfs the commands' real work."""

from __future__ import annotations

from sbxloop.config import Config
from sbxloop.engine.model import PlanModel, TaskRecord, TaskSpec
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
        assert batches[0].commands == [command for _, command in EVIDENCE_COMMANDS]
        # evidence output reaches the critic's prompt under its label
        prompt = next(j for j in agent.jobs if j.kind == "agent.session").prompt or ""
        assert "### git status" in prompt
        assert "output of git status --short 2>&1 | head -50" in prompt

    def test_evidence_job_failure_is_not_fatal(self) -> None:
        agent = BatchStubAgent(batch_error=True)
        outcome = runner(agent).scrutinize(record(), plan(), "did the work")

        assert outcome.verdict.verdict == "pass"
        prompt = next(j for j in agent.jobs if j.kind == "agent.session").prompt or ""
        assert "(no evidence gathered)" in prompt
