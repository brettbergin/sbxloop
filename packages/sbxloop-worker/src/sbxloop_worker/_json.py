"""Extraction of structured JSON from agent output text."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)

_decoder = json.JSONDecoder()


def extract_json(text: str) -> dict[str, Any] | list[Any] | None:
    """Pull a JSON object/array out of agent output.

    Preference order: the last fenced ``json`` block (agents often narrate
    before the final answer), then the whole text, then any JSON value
    embedded in surrounding prose (models sometimes reply "Here is the
    plan: {...}" with no fence — field failure on 0.5.0). Returns None when
    nothing parses to a dict or list.
    """
    candidates = [match.group(1) for match in _FENCE_RE.finditer(text)]
    candidates.reverse()
    candidates.append(text)
    for candidate in candidates:
        try:
            value = json.loads(candidate.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict | list):
            return value
    return _embedded_json(text)


_OPENER_RE = re.compile(r"[{\[]")


def _embedded_json(text: str) -> dict[str, Any] | list[Any] | None:
    """The last top-level JSON object/array embedded in prose, if any.

    Scans ``{``/``[`` openers as potential starts, raw-decoding from each;
    a successful decode consumes its whole span so nested openers inside it
    are not re-tried as candidates. The last top-level hit wins, matching
    the fenced-block preference (the final answer follows the narration).
    """
    found: dict[str, Any] | list[Any] | None = None
    position = 0
    while match := _OPENER_RE.search(text, position):
        try:
            value, end = _decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            position = match.start() + 1
            continue
        position = end
        if isinstance(value, dict | list):
            found = value
    return found
