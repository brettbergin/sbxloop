from __future__ import annotations

import ast
import inspect

import pytest

from sbxloop.daemon import chat_choices
from sbxloop.daemon.chat_choices import (
    Choice,
    ChoiceQuestion,
    match_free_text,
    parse_choice_question,
    render_prose,
)


def block(body: str) -> str:
    return "```sbx-choices\n" + body + "\n```"


def test_module_has_no_backend_imports() -> None:
    tree = ast.parse(inspect.getsource(chat_choices))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert names
    for name in names:
        root = name.split(".")[0]
        assert root not in {"discord", "slack_sdk", "slack"}


def test_parse_success_strips_marker() -> None:
    text = "What do you want changed?\n\n" + block(
        '{"prompt": "Pick one", "choices": ['
        '{"value": "ui", "label": "The UI", "description": "layout"},'
        '"Something else"]}'
    )
    clean, question = parse_choice_question(text)
    assert clean == "What do you want changed?"
    assert question is not None
    assert question.prompt == "Pick one"
    assert question.values == ("ui", "Something else")
    assert question.choices[0] == Choice("ui", "The UI", "layout")
    assert question.allow_free_text is True


def test_prompt_falls_back_to_clean_text() -> None:
    text = "Which one?\n" + block('{"choices": ["a", "b"]}')
    clean, question = parse_choice_question(text)
    assert clean == "Which one?"
    assert question is not None and question.prompt == "Which one?"


def test_no_marker_leaves_text_alone() -> None:
    clean, question = parse_choice_question("just prose")
    assert clean == "just prose"
    assert question is None


@pytest.mark.parametrize(
    "body",
    [
        "not json at all {",
        "",
        "[1, 2, 3]",
        '{"choices": []}',
        '{"choices": "nope"}',
        '{"choices": ["only one"]}',
        '{"choices": ["a", "b", "c", "d", "e", "f"]}',
        '{"choices": ["a", {"description": "x"}]}',
        '{"choices": ["a", "  "]}',
    ],
)
def test_degradations_return_none_but_strip_marker(body: str) -> None:
    clean, question = parse_choice_question("Prose here.\n" + block(body))
    assert clean == "Prose here."
    assert question is None


def test_duplicate_values_collapse_below_minimum() -> None:
    clean, question = parse_choice_question("P\n" + block('{"choices": ["a", "a"]}'))
    assert clean == "P"
    assert question is None


def test_allow_free_text_false_respected() -> None:
    _, question = parse_choice_question(
        block('{"prompt": "P", "choices": ["a", "b"], "allow_free_text": false}')
    )
    assert question is not None and question.allow_free_text is False


def question() -> ChoiceQuestion:
    return ChoiceQuestion(
        prompt="What should change?",
        choices=(
            Choice("ui", "The UI", "layout and colours"),
            Choice("api", "The API"),
            Choice("docs", "Documentation"),
        ),
    )


def test_render_prose_numbers_choices() -> None:
    out = render_prose(question())
    assert out.splitlines() == [
        "What should change?",
        "1. The UI — layout and colours",
        "2. The API",
        "3. Documentation",
        "Reply with a number or option name, or answer in your own words.",
    ]


def test_render_prose_without_free_text() -> None:
    q = ChoiceQuestion("Pick", (Choice("a", "A"), Choice("b", "B")), allow_free_text=False)
    assert render_prose(q).endswith("Reply with a number or option name.")


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("2", "api"),
        ("1.", "ui"),
        ("3)", "docs"),
        ("The API", "api"),
        ("the api", "api"),
        ("docs", "docs"),
        ("documen", "docs"),
        ("the u", "ui"),
    ],
)
def test_match_free_text_resolves(reply: str, expected: str) -> None:
    assert match_free_text(question(), reply) == expected


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "   ",
        "0",
        "4",
        "the",  # ambiguous prefix across "The UI" and "The API"
        "here is a traceback you asked for",
        "maybe",
    ],
)
def test_match_free_text_returns_none(reply: str) -> None:
    assert match_free_text(question(), reply) is None
