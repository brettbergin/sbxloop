"""A run started with a named-agent assignment (plan S-A4), end to end.

The engine persists the assignment with the run, records who took each task
and each phase attempt, credits host events inside a task or phase to that
agent, and picks the same assignment back up on resume. A default
assignment leaves every job and event exactly as a run without one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop.agents.assignment import AgentAssignment, plan_assignment
from sbxloop.agents.registry import ConfigAgentRegistry
from sbxloop.engine.engine import LoopEngine
from sbxloop.errors import WorkerError
from sbxloop.events import HostEventTypes
from tests.conftest import FakeSbx
from tests.fakes.fake_github import GREEN, FakeGithub
from tests.unit.test_engine import (
    BUILD,
    FILES_BUILD,
    HAPPY_TASK,
    REVIEW_OK,
    REVIEW_RC,
    Harness,
    task,
    taskgraph,
)

ADA = {
    "slug": "ada",
    "name": "Ada",
    "instructions": "Prefer the smallest diff that meets the ask.",
    "roles": ["builder"],
}


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


def plan_for(engine: LoopEngine, **requested: str) -> AgentAssignment:
    return plan_assignment(
        ConfigAgentRegistry(engine.config),
        kind="code",
        lead=None,
        requested=requested,  # type: ignore[arg-type]
        channel_id="chan-1",
    )


def credited(harness: Harness, event_type: str) -> list[Any]:
    return [e.data.get("agent_slug") for e in harness.events if e.type == event_type]


class TestCustomAssignment:
    def run(self, harness: Harness) -> tuple[LoopEngine, str]:
        fake = FakeGithub(draft=True)
        fake.checks = [GREEN]
        harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_RC, BUILD, REVIEW_OK])
        engine = harness.pipeline(fake, agents=[ADA], keep_sandboxes=True)
        result = engine.start("write hello.txt", assignment=plan_for(engine, builder="ada"))
        assert result.state == "merged"
        return engine, result.run_id

    def test_the_assignment_is_persisted_with_the_run(self, harness: Harness) -> None:
        engine, run_id = self.run(harness)
        stored = engine.store.get_run_assignment(run_id)
        assert stored is not None
        assert AgentAssignment.from_json(stored) == plan_for(engine, builder="ada")

    def test_the_task_and_phase_rows_name_the_agent(self, harness: Harness) -> None:
        engine, run_id = self.run(harness)
        assignees = engine.store.task_assignees(run_id)
        assert assignees["t1"] == "ada"
        rows = {(row.phase, row.agent_slug) for row in engine.store.phase_attempts(run_id)}
        assert ("decompose", "planner") in rows
        assert ("build", "ada") in rows
        assert ("review", "critic") in rows
        # Mechanical stages belong to nobody.
        assert all(slug is None for phase, slug in rows if phase in {"verify", "gate"})

    def test_host_events_in_a_task_or_phase_credit_the_agent(self, harness: Harness) -> None:
        self.run(harness)
        assert set(credited(harness, HostEventTypes.TASK_START)) == {"ada"}
        assert set(credited(harness, HostEventTypes.TASK_END)) == {"ada"}
        assert set(credited(harness, HostEventTypes.REVIEW_VERDICT)) == {"critic"}
        assert set(credited(harness, HostEventTypes.RUN_DELIVER)) == {"concierge"}
        assert set(credited(harness, HostEventTypes.RUN_STATE)) == {None}

    def test_agent_events_carry_the_slug_and_name(self, harness: Harness) -> None:
        self.run(harness)
        agent_events = [e for e in harness.events if e.type.startswith("agent.")]
        assert agent_events
        builders = [e for e in agent_events if e.data.get("agent") == "builder"]
        assert builders
        assert {(e.data["agent_slug"], e.data["agent_name"]) for e in builders} == {("ada", "Ada")}
        # The echo worker reports a message for text replies only (the
        # builder's); each phase's identity is pinned at the PhaseRunner.
        assert all("agent_slug" in e.data for e in agent_events)

    def test_the_builder_session_is_told_its_persona(self, harness: Harness) -> None:
        _, run_id = self.run(harness)
        jobs = [j for j in harness.agent_jobs(run_id) if j.get("kind") == "agent.session"]
        builds = [j for j in jobs if "Prefer the smallest diff" in (j["system_message"] or "")]
        assert builds
        assert all("Prefer the smallest diff" not in j["prompt"] for j in jobs)


@pytest.mark.parametrize("assigned", [False, True], ids=["none", "default"])
def test_a_default_assignment_changes_no_event_or_row(harness: Harness, assigned: bool) -> None:
    harness.script([taskgraph(task("t1")), *HAPPY_TASK])
    engine = harness.engine()
    assignment = plan_for(engine) if assigned else None
    assert engine.start("build the feature", assignment=assignment).succeeded
    run_id = engine.store.list_runs()[0].run_id
    assert all("agent_slug" not in e.data for e in harness.events)
    assert all("agent_name" not in e.data for e in harness.events)
    stored = engine.store.get_run_assignment(run_id)
    assert (stored is not None) is assigned


def test_resume_picks_the_assignment_back_up(harness: Harness) -> None:
    harness.script([taskgraph(task("t1")), {"fail": "sandbox exploded"}])
    engine = harness.engine(agents=[ADA])
    with pytest.raises(WorkerError, match="sandbox exploded"):
        engine.start("crashy run", assignment=plan_for(engine, builder="ada"))
    run_id = engine.store.list_runs()[0].run_id

    harness.events.clear()
    harness.script([*HAPPY_TASK])
    # A fresh engine whose configuration no longer declares Ada: the run
    # keeps the agent it was given.
    engine2 = harness.engine()
    result = engine2.resume(run_id)
    assert result.state == "completed"
    builds = [
        e
        for e in harness.events
        if e.type.startswith("agent.") and e.data.get("agent") == "builder"
    ]
    assert builds and {e.data["agent_slug"] for e in builds} == {"ada"}
    rows = [r for r in engine2.store.phase_attempts(run_id) if r.phase == "build"]
    assert [r.agent_slug for r in rows] == ["ada"]
