"""Prompt template rendering tests."""

import pytest

from sbxloop.engine.prompts import bullet_list, render


def test_render_decompose() -> None:
    text = render("decompose", outcome="Build the thing", max_tasks="5")
    assert "Build the thing" in text
    assert "At most 5 tasks" in text
    assert "$outcome" not in text


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
