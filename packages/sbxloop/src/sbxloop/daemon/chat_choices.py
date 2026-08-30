"""Transport-free model for clarifying questions with enumerable answers.

This module deliberately imports nothing from any chat backend so the same
question can be rendered as Discord components, Slack blocks, or plain prose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MARKER_LANG",
    "MAX_CHOICES",
    "MIN_CHOICES",
    "Choice",
    "ChoiceQuestion",
    "match_free_text",
    "parse_choice_question",
    "render_prose",
]

MARKER_LANG = "sbx-choices"
MIN_CHOICES = 2
# Discord allows at most 5 buttons in an action row.
MAX_CHOICES = 5

_BLOCK_RE = re.compile(
    r"^[ \t]*```[ \t]*" + re.escape(MARKER_LANG) + r"[ \t]*\r?\n(.*?)\r?\n?^[ \t]*```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


@dataclass(frozen=True)
class Choice:
    """One selectable answer."""

    value: str
    label: str
    description: str | None = None


@dataclass(frozen=True)
class ChoiceQuestion:
    """A clarifying question whose plausible answers are enumerable."""

    prompt: str
    choices: tuple[Choice, ...]
    allow_free_text: bool = True

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(c.value for c in self.choices)


def _as_choice(raw: Any) -> Choice | None:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        return Choice(value=text, label=text)
    if not isinstance(raw, dict):
        return None
    value = raw.get("value")
    label = raw.get("label")
    if value is None and label is None:
        return None
    if value is None:
        value = label
    if label is None:
        label = value
    if not isinstance(value, str) or not isinstance(label, str):
        return None
    value = value.strip()
    label = label.strip()
    if not value or not label:
        return None
    description = raw.get("description")
    if description is not None:
        if not isinstance(description, str) or not description.strip():
            description = None
        else:
            description = description.strip()
    return Choice(value=value, label=label, description=description)


def _build(spec: Any, fallback_prompt: str) -> ChoiceQuestion | None:
    if not isinstance(spec, dict):
        return None
    raw_choices = spec.get("choices")
    if not isinstance(raw_choices, list) or not raw_choices:
        return None
    choices: list[Choice] = []
    seen: set[str] = set()
    for raw in raw_choices:
        choice = _as_choice(raw)
        if choice is None:
            return None
        if choice.value in seen:
            continue
        seen.add(choice.value)
        choices.append(choice)
    if not (MIN_CHOICES <= len(choices) <= MAX_CHOICES):
        return None
    prompt = spec.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = fallback_prompt
    allow_free_text = spec.get("allow_free_text", True)
    if not isinstance(allow_free_text, bool):
        allow_free_text = True
    return ChoiceQuestion(
        prompt=prompt.strip(),
        choices=tuple(choices),
        allow_free_text=allow_free_text,
    )


def parse_choice_question(text: str) -> tuple[str, ChoiceQuestion | None]:
    """Split a reply into its prose and an optional choice question.

    The marker is a fenced ```sbx-choices JSON block. It is always stripped
    from the returned text; a malformed or unusable spec simply yields None so
    the reply is posted as ordinary prose.
    """
    if not text:
        return text, None
    match = _BLOCK_RE.search(text)
    if match is None:
        return text, None
    clean = (text[: match.start()] + text[match.end() :]).strip()
    body = match.group(1).strip()
    if not body:
        return clean, None
    try:
        spec = json.loads(body)
    except (ValueError, TypeError):
        return clean, None
    return clean, _build(spec, clean)


def render_prose(question: ChoiceQuestion) -> str:
    """Numbered plain-text rendering for backends without components."""
    lines = [question.prompt.strip()] if question.prompt.strip() else []
    for index, choice in enumerate(question.choices, start=1):
        line = f"{index}. {choice.label}"
        if choice.description:
            line += f" — {choice.description}"
        lines.append(line)
    if question.allow_free_text:
        lines.append("Reply with a number or option name, or answer in your own words.")
    else:
        lines.append("Reply with a number or option name.")
    return "\n".join(lines)


def match_free_text(question: ChoiceQuestion, reply: str) -> str | None:
    """Map a typed reply onto a choice value, or None if it does not name one."""
    if not reply:
        return None
    text = reply.strip()
    if not text:
        return None
    stripped = text.rstrip(".)").strip()

    if stripped.isdigit():
        index = int(stripped)
        if 1 <= index <= len(question.choices):
            return question.choices[index - 1].value
        return None

    lowered = stripped.casefold()
    for choice in question.choices:
        if lowered in (choice.value.casefold(), choice.label.casefold()):
            return choice.value

    prefix_hits = [
        choice
        for choice in question.choices
        if choice.label.casefold().startswith(lowered)
        or choice.value.casefold().startswith(lowered)
    ]
    if len(prefix_hits) == 1:
        return prefix_hits[0].value
    return None
