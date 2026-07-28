"""Prompt template rendering tests."""

import pytest

from sbxloop.engine.prompts import bullet_list, render


def test_render_decompose() -> None:
    text = render("decompose", outcome="Build the thing", max_tasks="5")
    assert "Build the thing" in text
    assert "At most 5 tasks" in text
    assert "$outcome" not in text


def test_execute_and_plan_carry_environment_notes() -> None:
    """Field regression: the agent burned its whole revision budget on
    `python3 -m venv` failing (missing ensurepip) and bare pip hitting
    PEP 668 — the prompts must state the environment facts."""
    execute = render(
        "execute",
        outcome="o",
        task_id="t1",
        task_title="tt",
        task_description="td",
        plan_steps="- s",
        expected_artifacts="- a",
        feedback="(none)",
        user_guidance="(none)",
    )
    plan = render(
        "plan",
        outcome="o",
        task_id="t1",
        task_title="tt",
        task_description="td",
        acceptance_criteria="- c",
        feedback="(none)",
        user_guidance="(none)",
    )
    for text in (execute, plan):
        assert "externally managed" in text
        assert "python3 -m venv" in text
        assert "sudo" in text
        assert "allowlist" in text
    # Plan-declared egress: the planner must know the field and its bounds,
    # and the executor must report blocked domains instead of retrying.
    assert "egress" in plan
    assert "egress" in execute
    assert "blocked domain" in execute
    # Field regression (rv4zfdb1m): the executor nested the project in a
    # subdirectory while root-relative verify commands failed every revision.
    # Both sides must be told verify runs from the workspace root.
    assert "workspace root" in plan
    assert "workspace root" in execute
    assert "cannot edit" in plan
    assert "cannot edit" in execute
    # 0.5.0 regression: environment notes buried the response-format section
    # and JSON compliance dropped. The format instructions must come LAST.
    assert plan.index("Environment facts") < plan.index("Response format")
    assert "ONLY the fenced JSON block" in plan


# Layer 3 (issue #142): the prompts must carry per-ecosystem environment
# notes at parity, so no single toolchain is the one a planner pattern-matches
# against. Each entry is (ecosystem, plan.md markers, execute.md markers);
# one row per language sub-issue.
ECOSYSTEM_NOTES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("Python", ("PEP 668", "python3 -m venv", ".venv/bin/pytest"), ("PEP 668", ".venv/bin/")),
]


@pytest.mark.parametrize(
    ("ecosystem", "plan_markers", "execute_markers"),
    ECOSYSTEM_NOTES,
    ids=[row[0] for row in ECOSYSTEM_NOTES],
)
def test_prompts_carry_ecosystem_notes(
    ecosystem: str,
    plan_markers: tuple[str, ...],
    execute_markers: tuple[str, ...],
) -> None:
    plan = render(
        "plan",
        outcome="o",
        task_id="t1",
        task_title="T",
        task_description="d",
        acceptance_criteria="- c",
        feedback="(none)",
        user_guidance="(none)",
    )
    execute = render(
        "execute",
        outcome="o",
        task_id="t1",
        task_title="T",
        task_description="d",
        plan_steps="- s",
        expected_artifacts="- a",
        feedback="(none)",
        user_guidance="(none)",
    )
    for text in (plan, execute):
        assert "Ecosystem notes" in text
        assert f"**{ecosystem}**" in text
    for marker in plan_markers:
        assert marker in plan, f"{ecosystem}: missing {marker!r} in plan.md"
    for marker in execute_markers:
        assert marker in execute, f"{ecosystem}: missing {marker!r} in execute.md"


def test_environment_facts_lead_language_neutral() -> None:
    """Layer 3 (#142): the environment opener must be toolchain-neutral —
    per-ecosystem specifics belong in the Ecosystem notes block below it, not
    in the framing every task reads."""
    plan = render(
        "plan",
        outcome="o",
        task_id="t1",
        task_title="T",
        task_description="d",
        acceptance_criteria="- c",
        feedback="(none)",
        user_guidance="(none)",
    )
    opener = plan[plan.index("## Environment facts") : plan.index("Ecosystem notes")]
    # The universal contract stays in the opener...
    assert "workspace root" in opener
    assert "cannot edit" in opener
    # ...while no ecosystem gets to frame it.
    for ecosystem_specific in ("PEP 668", ".venv", "pytest"):
        assert ecosystem_specific not in opener, (
            f"{ecosystem_specific!r} leaked into the language-neutral opener"
        )


def test_render_all_templates_have_no_leftover_vars() -> None:
    contexts = {
        "decompose": {"outcome": "o", "max_tasks": "3"},
        "plan": {
            "outcome": "o",
            "task_id": "t1",
            "task_title": "T",
            "task_description": "d",
            "acceptance_criteria": "- c",
            "feedback": "f",
            "user_guidance": "g",
        },
        "execute": {
            "outcome": "o",
            "task_id": "t1",
            "task_title": "T",
            "task_description": "d",
            "plan_steps": "- s",
            "expected_artifacts": "- a",
            "feedback": "f",
            "user_guidance": "g",
        },
        "scrutinize": {
            "task_id": "t1",
            "task_title": "T",
            "task_description": "d",
            "acceptance_criteria": "- c",
            "plan_steps": "- s",
            "executor_report": "r",
            "evidence": "e",
        },
        "validate": {
            "outcome": "o",
            "task_id": "t1",
            "task_title": "T",
            "task_description": "d",
            "acceptance_criteria": "- c",
            "verify_results": "v",
        },
        "steer": {
            "outcome": "o",
            "tasks_summary": "- t1 [executing] T",
            "current_task": "Task t1: T",
            "user_guidance": "(none)",
            "user_message": "how is it going?",
        },
    }
    for name, context in contexts.items():
        text = render(name, **context)
        assert "$" not in text.replace("$?", ""), f"unsubstituted var in {name}"


def test_render_missing_variable_fails_loudly() -> None:
    with pytest.raises(KeyError):
        render("decompose", outcome="only outcome")


def test_retry_context_defaults_empty_and_substitutes() -> None:
    base = render("decompose", outcome="o", max_tasks="3")
    retried = render("decompose", outcome="o", max_tasks="3", retry_context="TRY AGAIN")
    assert "TRY AGAIN" not in base
    assert "TRY AGAIN" in retried


def test_steer_prompt_carries_chat_contract() -> None:
    """STEER must present the user's message, the three actions, and the
    read-only rule — direction changes flow through the engine, not edits."""
    text = render(
        "steer",
        outcome="build it",
        tasks_summary="- t1 [executing] Build",
        current_task="Task t1: Build (state: executing)",
        user_guidance="- use uv",
        user_message="switch the storage layer to postgres",
    )
    assert "switch the storage layer to postgres" in text
    for action in ("continue", "steer_task", "steer_run"):
        assert action in text
    assert "read-only" in text
    assert "Do not modify anything" in text
    assert "ONLY the fenced JSON block" in text


def test_plan_and_execute_render_standing_guidance() -> None:
    plan = render(
        "plan",
        outcome="o",
        task_id="t1",
        task_title="T",
        task_description="d",
        acceptance_criteria="- c",
        feedback="f",
        user_guidance="- always use postgres",
    )
    execute = render(
        "execute",
        outcome="o",
        task_id="t1",
        task_title="T",
        task_description="d",
        plan_steps="- s",
        expected_artifacts="- a",
        feedback="f",
        user_guidance="- always use postgres",
    )
    for text in (plan, execute):
        assert "Standing user guidance" in text
        assert "always use postgres" in text


def test_bullet_list() -> None:
    assert bullet_list([]) == "(none)"
    assert bullet_list(["a", "b"]) == "- a\n- b"
