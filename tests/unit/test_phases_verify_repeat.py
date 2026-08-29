"""#387: a verify command that cannot pass fails identically every attempt.
The engine detects that mechanically — same command, same normalised output —
and says so in the feedback rather than burning the whole budget re-running
an unpassable check."""

from __future__ import annotations

from sbxloop.config import Config
from sbxloop.engine.model import TaskRecord, TaskSpec
from sbxloop.engine.phases import (
    VERIFY_REPEAT_NOTE,
    PhaseRunner,
    normalise_verify_output,
    verify_fingerprint,
)
from sbxloop_worker.protocol import BatchCommandResult, JobRequest, JobResult

MYPY = "uv run mypy packages"

RUN_A = (
    "/host/runs/rrhb28j7n/workspace/packages/sbxloop/hatch_build.py:20: error: "
    "Cannot find implementation or library stub for module named "
    '"hatchling.builders.hooks.plugin.interface"\n'
    "Found 1 error in 1 file (checked 74 source files)\n"
    "finished in 209.4s\n"
)
# Same failure, different run: different absolute path prefix and timings.
RUN_B = (
    "/host/runs/zz99abcde/workspace/packages/sbxloop/hatch_build.py:20: error: "
    "Cannot find implementation or library stub for module named "
    '"hatchling.builders.hooks.plugin.interface"\n'
    "Found 1 error in 1 file (checked 74 source files)\n"
    "finished in 208.1s\n"
)
OTHER = "tests/unit/test_x.py:3: error: something else entirely\n"


class StubAgent:
    """Answers shell.batch with scripted exits/outputs."""

    def __init__(self, exits: dict[str, int], outputs: dict[str, str]) -> None:
        self.exits = exits
        self.outputs = outputs

    def submit(self, job: JobRequest, **_: object) -> JobResult:
        return JobResult(
            job_id=job.job_id,
            status="ok",
            output_json=[
                BatchCommandResult(
                    command=command,
                    exit_code=self.exits.get(command, 0),
                    output=self.outputs.get(command, ""),
                )
                for command in job.commands
            ],
        )


def runner(agent: StubAgent) -> PhaseRunner:
    return PhaseRunner(agent, Config(), "r1", "ship it", workdir="/work")  # type: ignore[arg-type]


def record() -> TaskRecord:
    return TaskRecord(
        spec=TaskSpec(id="t5", title="Type-check", verify_commands=[MYPY]),
        state="verifying",
    )


def run_verify(output: str, prior: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    agent = StubAgent({MYPY: 1}, {MYPY: output})
    return runner(agent).verify(record(), prior_fingerprints=prior)


class TestFingerprint:
    def test_normalisation_strips_timing_and_absolute_paths(self) -> None:
        assert normalise_verify_output(RUN_A) == normalise_verify_output(RUN_B)
        assert "209" not in normalise_verify_output(RUN_A)
        assert "rrhb28j7n" not in normalise_verify_output(RUN_A)

    def test_trailing_whitespace_ignored(self) -> None:
        assert verify_fingerprint(MYPY, "boom   \n") == verify_fingerprint(MYPY, "boom")

    def test_different_command_different_fingerprint(self) -> None:
        assert verify_fingerprint(MYPY, RUN_A) != verify_fingerprint("uv run mypy", RUN_A)

    def test_different_output_different_fingerprint(self) -> None:
        assert verify_fingerprint(MYPY, RUN_A) != verify_fingerprint(MYPY, OTHER)


class TestVerifyRepeatDetection:
    def test_first_failure_is_not_a_repeat(self) -> None:
        outcome = run_verify(RUN_A)

        assert not outcome.passed
        assert not outcome.repeated
        assert outcome.fingerprints == (verify_fingerprint(MYPY, RUN_A),)
        assert VERIFY_REPEAT_NOTE not in outcome.feedback

    def test_two_different_failures_are_not_flagged(self) -> None:
        first = run_verify(RUN_A)
        second = run_verify(OTHER, prior=first.fingerprints)

        assert not second.repeated
        assert VERIFY_REPEAT_NOTE not in second.feedback

    def test_same_failure_modulo_timings_and_paths_is_flagged(self) -> None:
        first = run_verify(RUN_A)
        second = run_verify(RUN_B, prior=first.fingerprints)

        assert second.repeated
        assert second.fingerprints == first.fingerprints

    def test_repeat_note_reaches_the_feedback_text(self) -> None:
        first = run_verify(RUN_A)
        second = run_verify(RUN_B, prior=first.fingerprints)

        assert VERIFY_REPEAT_NOTE in second.feedback
        assert "suspect" in second.feedback
        assert "verify command" in second.feedback
        # the original failure detail is still there for the next phase
        assert "hatchling.builders.hooks.plugin.interface" in second.feedback

    def test_passing_verify_is_never_a_repeat(self) -> None:
        agent = StubAgent({}, {})
        outcome = runner(agent).verify(
            record(), prior_fingerprints=(verify_fingerprint(MYPY, RUN_A),)
        )

        assert outcome.passed
        assert not outcome.repeated
        assert outcome.fingerprints == ()


class TestTaskRecordFlag:
    def test_flag_defaults_off_and_round_trips_through_the_store(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from sbxloop.engine.store import StateStore

        store = StateStore(tmp_path / "state.db")
        store.create_run("r1", "goal")
        spec = TaskSpec(id="t5", title="Type-check", verify_commands=[MYPY])
        store.save_tasks("r1", [spec])

        task = store.get_tasks("r1")[0]
        assert task.verify_repeat is False

        task.verify_repeat = True
        store.update_task("r1", task)
        assert store.get_tasks("r1")[0].verify_repeat is True

    def test_store_collects_prior_verify_fingerprints(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import json

        from sbxloop.engine.store import StateStore

        store = StateStore(tmp_path / "state.db")
        store.create_run("r1", "goal")
        store.save_tasks("r1", [TaskSpec(id="t5", title="Type-check")])
        fp = verify_fingerprint(MYPY, RUN_A)
        store.record_phase(
            "r1",
            "verify",
            task_id="t5",
            attempt=1,
            status="failed",
            output_json=json.dumps({"passed": False, "fingerprints": [fp]}),
            started_at=0.0,
        )

        assert store.verify_fingerprints("r1", "t5") == [fp]
        assert store.verify_fingerprints("r1", "nope") == []
