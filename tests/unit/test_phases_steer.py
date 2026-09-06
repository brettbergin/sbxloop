"""PhaseRunner STEER tests: prompt/context assembly and guidance injection,
against a stub worker client that captures submitted jobs."""

from __future__ import annotations

from typing import Any

from sbxloop.config import Config
from sbxloop.engine.model import TaskRecord, TaskSpec
from sbxloop.engine.phases import PhaseRunner
from sbxloop_worker.protocol import JobRequest, JobResult


class StubAgent:
    """Captures every JobRequest; answers with a canned JobResult."""

    def __init__(self, output_json: Any) -> None:
        self.output_json = output_json
        self.jobs: list[JobRequest] = []

    def submit(
        self, job: JobRequest, *, agent: str | None = None, tool_handler: Any = None
    ) -> JobResult:
        self.jobs.append(job)
        self.agents: list[str | None] = getattr(self, "agents", [])
        self.agents.append(agent)
        return JobResult(
            job_id=job.job_id,
            status="ok",
            output_json=self.output_json,
            output_text="",
        )


def runner(agent: StubAgent) -> PhaseRunner:
    return PhaseRunner(agent, Config(), "r1", "ship the feature")  # type: ignore[arg-type]


def record(state: str = "executing") -> TaskRecord:
    return TaskRecord(
        spec=TaskSpec(id="t1", title="Build it", description="build the thing"),
        state=state,  # type: ignore[arg-type]
        last_feedback="tests were red",
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
        assert "tests were red" in prompt
        assert "why is t1 slow?" in prompt

    def test_no_active_task_says_so(self) -> None:
        agent = StubAgent({"reply": "ok", "action": "continue", "guidance": ""})
        phases = runner(agent)
        phases.steer("status?", tasks=[], task=None)
        prompt = agent.jobs[0].prompt or ""
        assert "no task is active" in prompt
        assert "not been decomposed" in prompt

    def test_stage_names_where_the_run_is_when_no_task_is_active(self) -> None:
        """Throughout the post-build stages there is no current task; the
        engine says where the run is instead (a CI wait on a PR, say)."""
        agent = StubAgent({"reply": "ok", "action": "continue", "guidance": ""})
        phases = runner(agent)
        phases.steer("status?", tasks=[record("done")], task=None, stage="awaiting CI on PR #12")
        prompt = agent.jobs[0].prompt or ""
        assert "no task is active right now — the run is awaiting CI on PR #12" in prompt
        assert "between tasks" not in prompt
        assert "t1 [done] Build it" in prompt

    def test_stage_is_ignored_while_a_task_is_active(self) -> None:
        agent = StubAgent({"reply": "ok", "action": "continue", "guidance": ""})
        phases = runner(agent)
        phases.steer("status?", tasks=[record()], task=record(), stage="gating")
        prompt = agent.jobs[0].prompt or ""
        assert "Task t1: Build it (state: executing" in prompt
        assert "the run is gating" not in prompt

    def test_guidance_injected_into_build_prompts(self) -> None:
        agent = StubAgent(None)
        phases = runner(agent)
        phases.add_guidance("always use postgres")

        phases.build(record())
        assert "always use postgres" in (agent.jobs[0].prompt or "")

    def test_steer_prompt_lists_standing_guidance(self) -> None:
        agent = StubAgent({"reply": "ok", "action": "continue", "guidance": ""})
        phases = runner(agent)
        phases.add_guidance("always use postgres")
        phases.steer("hello", tasks=[], task=None)
        assert "always use postgres" in (agent.jobs[0].prompt or "")
