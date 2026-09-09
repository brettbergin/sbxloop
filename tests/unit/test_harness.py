"""Every agent session is told what it is running inside (engine.harness).

Four personas used to learn their situation from four prompts, each
restating a slice of the same machine and each free to drift from it. The
briefing is now written once and composed onto every session's system
message, so these tests are about *reach* (does every phase get it, with the
right role's tail) at least as much as about wording.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.engine.harness import (
    ROLE_BY_PHASE,
    Role,
    brief_for_phase,
    harness_context,
)
from sbxloop.engine.model import TaskRecord, TaskSpec
from sbxloop.engine.phases import AGENT_NAMES, PhaseRunner
from sbxloop_worker.protocol import JobRequest, JobResult

ROLES: tuple[Role, ...] = ("planner", "builder", "critic", "operator", "concierge")

# The opening of the landing paragraph, which only the planner gets.
LANDING_MARKER = "Work that passes the loop's gate"

GRAPH = {"tasks": [{"id": "t1", "title": "Do it", "verify_commands": [".venv/bin/pytest -q"]}]}
VERDICT = {"verdict": "approve", "summary": "fine", "findings": []}


class TestHarnessContext:
    def test_every_role_gets_the_same_head(self) -> None:
        config = Config()
        heads = {harness_context(config, role=role).split("\n\n")[0] for role in ROLES}
        assert heads == {"# Where you are"}

    @pytest.mark.parametrize("role", ROLES)
    def test_the_two_facts_that_cost_a_turn_are_always_said(self, role: Role) -> None:
        """A stage that misreads either of these burns a revision on it: a
        blocked domain retried as a flake, or work written outside the
        workspace and lost with the sandbox."""
        text = harness_context(Config(), role=role)
        assert "fails closed" in text
        assert "Only the workspace survives" in text

    def test_each_role_gets_its_own_tail(self) -> None:
        config = Config()
        tails = [harness_context(config, role=role) for role in ROLES]
        assert len(set(tails)) == len(ROLES), "two roles are being told the same thing"

    def test_a_critic_is_told_it_is_read_only_and_a_builder_is_not(self) -> None:
        config = Config()
        assert "read-only" in harness_context(config, role="critic")
        assert "read-only" not in harness_context(config, role="builder")

    def test_a_critic_is_told_to_report_a_capability_it_lost(self) -> None:
        """The degraded-critic guard exists because a critic that lost its
        tooling must not emit a clean verdict; the briefing says so too."""
        assert "could not check this" in harness_context(Config(), role="critic")

    def test_the_pull_request_framing_follows_the_github_gate(self) -> None:
        """`[github] repo` is the gate for delivery, so a run with no
        repository must not be told its work lands as a pull request."""
        bare = harness_context(Config(), role="planner")
        landing = harness_context(
            Config.model_validate({"github": {"repo": "owner/name"}}), role="planner"
        )
        assert "pull request" not in bare
        assert "pull request" in landing

    def test_only_the_planner_is_told_about_landing(self) -> None:
        """`build.md` and `review.md` already name the branch and the PR
        with the real path and number; the briefing must not say it twice.

        The operator's tail mentions a pull request to rule one *out* — its
        result is not code — so the marker is the landing paragraph itself,
        not the phrase.
        """
        config = Config.model_validate({"github": {"repo": "owner/name"}})
        assert LANDING_MARKER in harness_context(config, role="planner")
        others: tuple[Role, ...] = ("builder", "critic", "operator", "concierge")
        for role in others:
            assert LANDING_MARKER not in harness_context(config, role=role), role

    def test_it_is_domain_neutral(self) -> None:
        """`AGENTS.md`: prompts carry no language-specific examples. The
        briefing is handed to every run on every kind of repository."""
        text = "".join(harness_context(Config(), role=role) for role in ROLES).lower()
        for word in ("python", "pytest", "npm", "make lint", "ruff", "go.mod", "dotnet"):
            assert word not in text, word


class TestBriefForPhase:
    def test_every_phase_that_runs_an_agent_has_a_role(self) -> None:
        """A new phase must choose a role rather than silently inheriting
        the builder's briefing."""
        assert set(ROLE_BY_PHASE) == set(AGENT_NAMES)

    def test_the_phases_own_message_follows_the_briefing(self) -> None:
        text = brief_for_phase(Config(), "operator_execute", "You are the operator.")
        assert text.startswith("# Where you are")
        assert text.rstrip().endswith("You are the operator.")

    def test_no_phase_message_leaves_only_the_briefing(self) -> None:
        assert brief_for_phase(Config(), "build", None) == harness_context(Config(), role="builder")
        assert brief_for_phase(Config(), "build", "   ") == harness_context(
            Config(), role="builder"
        )

    def test_an_unmapped_phase_is_a_loud_error(self) -> None:
        with pytest.raises(ValueError, match="no harness role"):
            brief_for_phase(Config(), "not_a_phase", None)


class BriefingAgent:
    """Records the system message of every job it is handed."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.briefings: list[tuple[str, str | None]] = []  # (persona, system_message)

    def submit(
        self, job: JobRequest, *, agent: str | None = None, tool_handler: Any = None
    ) -> JobResult:
        self.briefings.append((agent or "", job.system_message))
        answer = self.responses.pop(0)
        return JobResult(
            job_id=job.job_id,
            status="ok",
            output_json=answer if isinstance(answer, dict) else None,
            output_text=answer if isinstance(answer, str) else json.dumps(answer),
        )


class TestEverySessionIsBriefed:
    """The acceptance test: drive the real phases and read what the
    sessions were actually opened with."""

    def test_decompose_build_and_review(self) -> None:
        agent = BriefingAgent([GRAPH, "done", VERDICT])
        runner = PhaseRunner(agent, Config(), "r1", "ship it")  # type: ignore[arg-type]
        runner.decompose()
        task = TaskRecord(spec=TaskSpec(id="t1", title="Do it"))
        runner.build(task)
        runner.review(diff="+x", pr_number=1, round=1, tasks=[task], history="", refuted=set())

        personas = [persona for persona, _ in agent.briefings]
        assert personas == ["decomposer", "builder", "reviewer"]
        for persona, briefing in agent.briefings:
            assert briefing is not None, persona
            assert briefing.startswith("# Where you are"), persona
        by_persona = dict(agent.briefings)
        assert "You plan; you do not build" in (by_persona["decomposer"] or "")
        assert "actually does the work" in (by_persona["builder"] or "")
        assert "read-only" in (by_persona["reviewer"] or "")
