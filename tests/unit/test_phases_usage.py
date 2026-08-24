"""PhaseRunner usage tally (drain_spend).

Every agent job's usage accrues on the runner and is drained by the engine
when it records the phase attempt, so JSON retries and critic re-runs bill
to the phase row they served instead of vanishing with the failed attempt.
"""

from __future__ import annotations

from typing import Any

from sbxloop.config import Config
from sbxloop.engine.model import SteerVerdict
from sbxloop.engine.phases import PhaseRunner
from sbxloop_worker.protocol import JobRequest, JobResult, Usage


class UsageAgent:
    """Answers agent sessions from a script of (output_json, usage, turns)."""

    def __init__(self, responses: list[tuple[Any, Usage | None, int | None]]) -> None:
        self.responses = list(responses)

    def submit(self, job: JobRequest, *, agent: str | None = None) -> JobResult:
        output_json, usage, turns = self.responses.pop(0)
        return JobResult(
            job_id=job.job_id,
            status="ok",
            output_json=output_json,
            output_text="",
            usage=usage,
            turns=turns,
        )


def runner(agent: UsageAgent) -> PhaseRunner:
    return PhaseRunner(agent, Config(), "r1", "ship the feature")  # type: ignore[arg-type]


STEER = {"reply": "ok", "action": "continue"}


class TestDrainSpend:
    def test_single_job_billed_and_reset(self) -> None:
        phases = runner(UsageAgent([(STEER, Usage(input_tokens=100, output_tokens=5), 2)]))
        verdict = phases.steer("hi", tasks=[], task=None)
        assert isinstance(verdict, SteerVerdict)
        spend = phases.drain_spend()
        assert spend.usage is not None
        assert spend.usage.input_tokens == 100
        assert spend.turns == 2
        # drained: the next drain starts from zero
        empty = phases.drain_spend()
        assert empty.usage is None and empty.turns is None

    def test_retry_attempts_merge_into_one_bill(self) -> None:
        """An invalid first reply spends real tokens; the drained spend must
        carry both attempts, not just the one that validated."""
        phases = runner(
            UsageAgent(
                [
                    ({"nonsense": True}, Usage(input_tokens=40, output_tokens=1), 1),
                    (STEER, Usage(input_tokens=60, output_tokens=2), 1),
                ]
            )
        )
        phases.steer("hi", tasks=[], task=None)
        spend = phases.drain_spend()
        assert spend.usage is not None
        assert spend.usage.input_tokens == 100
        assert spend.usage.output_tokens == 3
        assert spend.turns == 2

    def test_job_without_usage_drains_none(self) -> None:
        phases = runner(UsageAgent([(STEER, None, None)]))
        phases.steer("hi", tasks=[], task=None)
        spend = phases.drain_spend()
        assert spend.usage is None and spend.turns is None
