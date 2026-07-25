"""PhaseRunner STEER tests: prompt/context assembly and guidance injection,
against a stub worker client that captures submitted jobs."""

from __future__ import annotations

from typing import Any

from sbxloop.config import Config
from sbxloop.engine.model import PlanModel, TaskRecord, TaskSpec
from sbxloop.engine.phases import PhaseRunner
from sbxloop_worker.protocol import JobRequest, JobResult


class StubAgent:
    """Captures every JobRequest; answers with a canned JobResult."""

    def __init__(self, output_json: Any) -> None:
        self.output_json = output_json
        self.jobs: list[JobRequest] = []

    def submit(self, job: JobRequest) -> JobResult:
        self.jobs.append(job)
        return JobResult(
            job_id=job.job_id,
            status="ok",
            output_json=self.output_json,
            output_text="",
        )


def runner(agent: StubAgent) -> PhaseRunner:
    return PhaseRunner(agent, Config(), "r1", "ship the feature")  # type: ignore[arg-type]


def record(state: str = "executing", planned: bool = True) -> TaskRecord:
    return TaskRecord(
        spec=TaskSpec(id="t1", title="Build it", description="build the thing"),
        state=state,  # type: ignore[arg-type]
        last_feedback="tests were red",
        plan=PlanModel(steps=["step one"]) if planned else None,
    )


class TestSteer:
    def test_prompt_carries_board_task_and_message(self) -> None:
        agent = StubAgent({"reply": "sure", "action": "continue", "guidance": ""})
        phases = runner(agent)
        verdict = phases.steer("why is t1 slow?", tasks=[record()], task=record())

        assert verdict.action == "continue"
        job = agent.jobs[0]
        assert job.kind == "agent.session"
        assert job.permission_mode == "read_only"
        assert job.expect == "json"
        prompt = job.prompt or ""
        assert "ship the feature" in prompt
        assert "t1 [executing] Build it" in prompt
        assert "step one" in prompt
        assert "tests were red" in prompt
        assert "why is t1 slow?" in prompt

    def test_no_active_task_says_so(self) -> None:
        agent = StubAgent({"reply": "ok", "action": "continue", "guidance": ""})
        phases = runner(agent)
        phases.steer("status?", tasks=[], task=None)
        prompt = agent.jobs[0].prompt or ""
        assert "no task is active" in prompt
        assert "not been decomposed" in prompt

    def test_guidance_injected_into_plan_and_execute_prompts(self) -> None:
        agent = StubAgent({"steps": ["s"], "expected_artifacts": [], "verify_commands": []})
        phases = runner(agent)
        phases.add_guidance("always use postgres")

        phases.plan(record(planned=False))
        assert "always use postgres" in (agent.jobs[0].prompt or "")

        task = record()
        assert task.plan is not None
        phases.execute(task, task.plan)
        assert "always use postgres" in (agent.jobs[1].prompt or "")

    def test_steer_prompt_lists_standing_guidance(self) -> None:
        agent = StubAgent({"reply": "ok", "action": "continue", "guidance": ""})
        phases = runner(agent)
        phases.add_guidance("always use postgres")
        phases.steer("hello", tasks=[], task=None)
        assert "always use postgres" in (agent.jobs[0].prompt or "")
