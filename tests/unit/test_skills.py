"""The skill tree and the ``load_skill`` host tool.

A skill is a procedure the agent pulls when it needs it: listed in the tool
it is loaded through, fetched in full only on request. These tests hold the
two properties that make that safe — a role can load only what was written
for it, and every call is answered rather than raising — plus the shape
rules that keep a skill loadable at all.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.engine.harness import ROLE_BY_PHASE, Role
from sbxloop.engine.model import TaskRecord, TaskSpec
from sbxloop.engine.phases import PhaseRunner
from sbxloop.engine.skilltools import SKILL_TOOL_NAME, answer_skill_call, skill_tool_spec
from sbxloop.skills import Skill, load_skills, skill_body, skills_for
from sbxloop_worker.protocol import HostToolCall, JobRequest, JobResult

ROLES: tuple[Role, ...] = ("planner", "builder", "critic", "operator", "concierge")

GRAPH = {"tasks": [{"id": "t1", "title": "Do it", "verify_commands": [".venv/bin/pytest -q"]}]}
VERDICT = {"verdict": "approve", "summary": "fine", "findings": []}


class TestTheTree:
    def test_the_tree_is_not_empty(self) -> None:
        assert load_skills(), "no skills ship, so the tool would be a dead end"

    def test_every_skill_is_loadable(self) -> None:
        """Frontmatter parses, the name matches its directory, and the
        description (the only part that costs tokens unconditionally) is
        one line."""
        for skill in load_skills():
            assert isinstance(skill, Skill)
            assert skill.description and "\n" not in skill.description, skill.name
            assert skill.body.strip(), skill.name
            assert skill.roles, skill.name

    def test_every_role_named_is_a_real_role(self) -> None:
        """A typo in `roles:` would silently make a skill unreachable."""
        for skill in load_skills():
            assert skill.roles <= set(ROLES), (skill.name, skill.roles)

    def test_every_skill_is_reachable_by_someone(self) -> None:
        reachable = {skill.name for role in ROLES for skill in skills_for(role)}
        assert reachable == {skill.name for skill in load_skills()}

    def test_the_concierge_has_the_operator_skill(self) -> None:
        """It is the one aimed at a human asking how to run the loop, and
        the concierge is the only session that talks to one."""
        assert "operate-sbxloop" in {s.name for s in skills_for("concierge")}

    def test_a_critic_is_not_offered_the_delivery_procedure(self) -> None:
        """A read-only critic reading about how to commit and open a pull
        request is being shown work it is forbidden to do."""
        assert "deliver-pr" not in {s.name for s in skills_for("critic")}

    def test_skill_body_is_none_for_an_unknown_name(self) -> None:
        assert skill_body("no-such-skill") is None


class TestTheHostTool:
    def test_the_spec_enumerates_only_this_roles_skills(self) -> None:
        """The enum is the barrier the model cannot even spell past."""
        for role in ROLES:
            spec = skill_tool_spec(role)
            names = {s.name for s in skills_for(role)}
            if not names:
                assert spec is None, role
                continue
            assert spec is not None and spec.name == SKILL_TOOL_NAME
            assert set(spec.parameters["properties"]["name"]["enum"]) == names, role

    def test_the_description_carries_the_catalogue(self) -> None:
        """The tool description is how the model learns what exists, so the
        prompt does not have to pay for a listing on every turn."""
        spec = skill_tool_spec("builder")
        assert spec is not None
        for skill in skills_for("builder"):
            assert skill.name in spec.description
            assert skill.description in spec.description

    def test_loading_returns_the_body(self) -> None:
        call = HostToolCall(call_id="c1", name=SKILL_TOOL_NAME, arguments={"name": "run-shape"})
        response = answer_skill_call(call, "builder")
        assert response.ok
        assert response.text == skill_body("run-shape")

    def test_a_skill_for_another_role_is_refused_not_returned(self) -> None:
        call = HostToolCall(
            call_id="c1", name=SKILL_TOOL_NAME, arguments={"name": "operate-sbxloop"}
        )
        response = answer_skill_call(call, "builder")
        assert not response.ok
        assert "operate-sbxloop" in (response.error or "")

    @pytest.mark.parametrize("arguments", [{}, {"name": ""}, {"name": "nope"}, {"name": 7}])
    def test_every_call_is_answered_never_raised(self, arguments: dict[str, Any]) -> None:
        """The model must always get something it can act on."""
        call = HostToolCall(call_id="c1", name=SKILL_TOOL_NAME, arguments=arguments)
        response = answer_skill_call(call, "builder")
        assert not response.ok
        assert response.error


class ToolAgent:
    """Records the tools each job carried, and can answer one tool call."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.jobs: list[tuple[str, JobRequest]] = []
        self.handlers: list[Any] = []

    def submit(
        self, job: JobRequest, *, agent: str | None = None, tool_handler: Any = None
    ) -> JobResult:
        self.jobs.append((agent or "", job))
        self.handlers.append(tool_handler)
        answer = self.responses.pop(0)
        return JobResult(
            job_id=job.job_id,
            status="ok",
            output_json=answer if isinstance(answer, dict) else None,
            output_text=answer if isinstance(answer, str) else json.dumps(answer),
        )


class TestEverySessionCanLoadASkill:
    """The acceptance test: the tool reaches the read-only critic too, which
    the service tools deliberately do not."""

    def test_planner_builder_and_critic_all_carry_the_tool(self) -> None:
        agent = ToolAgent([GRAPH, "done", VERDICT])
        runner = PhaseRunner(agent, Config(), "r1", "ship it")  # type: ignore[arg-type]
        runner.decompose()
        task = TaskRecord(spec=TaskSpec(id="t1", title="Do it"))
        runner.build(task)
        runner.review(diff="+x", pr_number=1, round=1, tasks=[task], history="", refuted=set())

        assert [persona for persona, _ in agent.jobs] == ["decomposer", "builder", "reviewer"]
        for persona, job in agent.jobs:
            names = [spec.name for spec in job.host_tools]
            assert SKILL_TOOL_NAME in names, persona

    def test_the_handler_answers_a_skill_call_with_no_service_tools(self) -> None:
        """A run with no credentials has no service tool and no handler of
        its own; the skill tool must still work."""
        agent = ToolAgent([GRAPH])
        PhaseRunner(agent, Config(), "r1", "ship it").decompose()  # type: ignore[arg-type]
        handler = agent.handlers[0]
        assert handler is not None
        call = HostToolCall(call_id="c1", name=SKILL_TOOL_NAME, arguments={"name": "run-shape"})
        assert handler(call).ok

    def test_an_unknown_tool_is_refused_when_there_is_no_delegate(self) -> None:
        agent = ToolAgent([GRAPH])
        PhaseRunner(agent, Config(), "r1", "ship it").decompose()  # type: ignore[arg-type]
        response = agent.handlers[0](HostToolCall(call_id="c1", name="call_service"))
        assert not response.ok

    def test_every_phase_that_runs_an_agent_resolves_a_role(self) -> None:
        """The tool is chosen by role, so an unmapped phase would raise at
        job build time rather than merely lose its skills."""
        assert set(ROLE_BY_PHASE) >= {"decompose", "build", "review"}
