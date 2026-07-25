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
    )
    plan = render(
        "plan",
        outcome="o",
        task_id="t1",
        task_title="tt",
        task_description="td",
        acceptance_criteria="- c",
        feedback="(none)",
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
    # 0.5.0 regression: environment notes buried the response-format section
    # and JSON compliance dropped. The format instructions must come LAST.
    assert plan.index("Environment facts") < plan.index("Response format")
    assert "ONLY the fenced JSON block" in plan


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
        },
        "execute": {
            "outcome": "o",
            "task_id": "t1",
            "task_title": "T",
            "task_description": "d",
            "plan_steps": "- s",
            "expected_artifacts": "- a",
            "feedback": "f",
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


def test_bullet_list() -> None:
    assert bullet_list([]) == "(none)"
    assert bullet_list(["a", "b"]) == "- a\n- b"
