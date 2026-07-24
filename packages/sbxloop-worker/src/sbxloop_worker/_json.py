"""Extraction of structured JSON from agent output text."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | list[Any] | None:
    """Pull a JSON object/array out of agent output.

    Preference order: the last fenced ``json`` block (agents often narrate
    before the final answer), then the whole text. Returns None when nothing
    parses to a dict or list.
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
    return None
