"""PhaseRunner degraded-critic guard tests (#123).

A read-only critic that lost its inspection tooling must not be able to
emit a clean verdict as if it had verified the work: a degraded ``pass`` /
``accept`` triggers one fresh re-run that is confronted with the failures,
and a still-degraded clean verdict is downgraded to ``revise`` / ``reject``.
Exercised against a stub worker client that scripts one JobResult (with
optional SessionHealth) per agent session.
"""

from __future__ import annotations

from typing import Any

from sbxloop.config import Config
from sbxloop.engine.model import PlanModel, TaskRecord, TaskSpec
from sbxloop.engine.phases import PhaseRunner
from sbxloop_worker.protocol import JobRequest, JobResult, SessionHealth

DEGRADED = SessionHealth(tool_failures={"grep": 3, "glob": 1})
DENIALS_ONLY = SessionHealth(permission_denials={"shell": 4})


class ScriptedAgent:
    """Answers agent sessions from a script of (output_json, health) pairs;
    shell jobs (scrutinize's evidence batch) get empty ok results and
    consume nothing."""

    def __init__(self, responses: list[tuple[Any, SessionHealth | None]]) -> None:
        self.responses = list(responses)
        self.session_jobs: list[JobRequest] = []

    def submit(self, job: JobRequest, *, agent: str | None = None) -> JobResult:
        if job.kind == "shell.batch":
            return JobResult(job_id=job.job_id, status="ok", exit_code=0, output_json=[])
        self.session_jobs.append(job)
        output_json, health = self.responses.pop(0)
        return JobResult(
            job_id=job.job_id,
            status="ok",
            output_json=output_json,
            output_text="",
            health=health,
        )


def runner(agent: ScriptedAgent) -> PhaseRunner:
    return PhaseRunner(agent, Config(), "r1", "ship the feature")  # type: ignore[arg-type]


def record() -> TaskRecord:
    return TaskRecord(
        spec=TaskSpec(
            id="t1",
            title="Build it",
            acceptance_criteria=["it works"],
            verify_commands=["test -f README.md"],
        ),
        state="scrutinizing",
        last_feedback="verify command failed: `grep -q x out.txt` (exit 1)",
        plan=PlanModel(steps=["step one"], verify_commands=["grep -q x out.txt"]),
    )


def scrutinize(agent: ScriptedAgent) -> Any:
    task = record()
    assert task.plan is not None
    return runner(agent).scrutinize(task, task.plan, "did the work")


PASS = {"verdict": "pass"}
REVISE = {"verdict": "revise", "feedback": "missing tests"}
ACCEPT = {"verdict": "accept"}


class TestScrutinizeDegradedGuard:
    def test_healthy_pass_is_trusted_first_time(self) -> None:
        agent = ScriptedAgent([(PASS, None)])
        outcome = scrutinize(agent)
        assert outcome.verdict.verdict == "pass"
        assert not outcome.downgraded
        assert outcome.health is None
        assert len(agent.session_jobs) == 1

    def test_denials_alone_do_not_trigger_the_guard(self) -> None:
        # A critic probing shell and being denied is the read-only barrier
        # working as designed, not a degraded session.
        agent = ScriptedAgent([(PASS, DENIALS_ONLY)])
        outcome = scrutinize(agent)
        assert outcome.verdict.verdict == "pass"
        assert not outcome.downgraded
        assert outcome.health == DENIALS_ONLY
        assert len(agent.session_jobs) == 1

    def test_degraded_pass_reruns_once_and_trusts_a_healthy_pass(self) -> None:
        agent = ScriptedAgent([(PASS, DEGRADED), (PASS, None)])
        outcome = scrutinize(agent)
        assert outcome.verdict.verdict == "pass"
        assert not outcome.downgraded
        assert len(agent.session_jobs) == 2
        # The re-run is confronted with what its predecessor lost.
        rerun_prompt = agent.session_jobs[1].prompt or ""
        assert "Degraded tooling warning" in rerun_prompt
        assert "grep x3" in rerun_prompt

    def test_degraded_pass_twice_downgrades_to_revise(self) -> None:
        agent = ScriptedAgent([(PASS, DEGRADED), (PASS, DEGRADED)])
        outcome = scrutinize(agent)
        assert outcome.verdict.verdict == "revise"
        assert outcome.downgraded
        assert outcome.health == DEGRADED
        assert any("degraded tooling" in issue.detail for issue in outcome.verdict.issues)
        assert "grep x3" in outcome.verdict.feedback
        assert len(agent.session_jobs) == 2

    def test_degraded_revise_is_returned_without_rerun(self) -> None:
        # A dirty verdict needs no guard: reduced coverage cannot have
        # green-lit anything.
        agent = ScriptedAgent([(REVISE, DEGRADED)])
        outcome = scrutinize(agent)
        assert outcome.verdict.verdict == "revise"
        assert outcome.verdict.feedback == "missing tests"
        assert not outcome.downgraded
        assert outcome.health == DEGRADED
        assert len(agent.session_jobs) == 1

    def test_rerun_revise_under_degradation_is_kept_as_is(self) -> None:
        agent = ScriptedAgent([(PASS, DEGRADED), (REVISE, DEGRADED)])
        outcome = scrutinize(agent)
        assert outcome.verdict.verdict == "revise"
        assert outcome.verdict.feedback == "missing tests"
        assert not outcome.downgraded


class TestValidateDegradedGuard:
    def test_degraded_accept_twice_downgrades_to_reject(self) -> None:
        agent = ScriptedAgent([(ACCEPT, DEGRADED), (ACCEPT, DEGRADED)])
        outcome = runner(agent).validate(record(), "$ true\n(exit 0)")
        assert outcome.verdict.verdict == "reject"
        assert outcome.downgraded
        assert "could not be verified" in outcome.verdict.feedback

    def test_healthy_accept_is_trusted(self) -> None:
        agent = ScriptedAgent([(ACCEPT, None)])
        outcome = runner(agent).validate(record(), "$ true\n(exit 0)")
        assert outcome.verdict.verdict == "accept"
        assert not outcome.downgraded
        assert len(agent.session_jobs) == 1


class TestScrutinizeVerifySuspect:
    """The scrutinizer judges the checks as well as the work (#231): it is
    shown the exact verify commands VERIFY will run and the feedback the
    executor was addressing, and may rule the check itself wrong."""

    def test_prompt_carries_verify_commands_and_prior_feedback(self) -> None:
        agent = ScriptedAgent([(PASS, None)])
        scrutinize(agent)
        prompt = agent.session_jobs[0].prompt or ""
        # Spec-level and plan-level checks, in VERIFY's order.
        assert prompt.index("- test -f README.md") < prompt.index("- grep -q x out.txt")
        assert "verify command failed: `grep -q x out.txt`" in prompt
        assert '"verify_suspect": false' in prompt

    def test_verify_suspect_ruling_is_parsed(self) -> None:
        agent = ScriptedAgent(
            [({**PASS, "verify_suspect": True, "verify_suspect_reason": "wrong bytes"}, None)]
        )
        outcome = scrutinize(agent)
        assert outcome.verdict.verdict == "pass"
        assert outcome.verdict.verify_suspect
        assert outcome.verdict.verify_suspect_reason == "wrong bytes"

    def test_verify_suspect_defaults_off(self) -> None:
        outcome = scrutinize(ScriptedAgent([(PASS, None)]))
        assert not outcome.verdict.verify_suspect
        assert outcome.verdict.verify_suspect_reason == ""

    def test_verify_suspect_without_reason_is_retried(self) -> None:
        # The reason is what the planner is told; a bare flag would spend a
        # replan on nothing, so it is rejected and the critic gets one retry.
        agent = ScriptedAgent(
            [
                ({**PASS, "verify_suspect": True, "verify_suspect_reason": "  "}, None),
                ({**PASS, "verify_suspect": True, "verify_suspect_reason": "wrong bytes"}, None),
            ]
        )
        outcome = scrutinize(agent)
        assert outcome.verdict.verify_suspect_reason == "wrong bytes"
        assert len(agent.session_jobs) == 2
        assert "verify_suspect_reason" in (agent.session_jobs[1].prompt or "")
