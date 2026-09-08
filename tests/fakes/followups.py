"""Script a reviewer that actually calls the follow-up lookup host tool."""

from __future__ import annotations

import copy
import re
from typing import Any


def with_lookups(script: list[dict[str, Any]]) -> list[dict[str, Any]]:
    script = copy.deepcopy(script)
    attempt = 0
    for step in script:
        for followup in step.get("json", {}).get("followups", []):
            attempt += 1
            # The fixture models broad keyword searches, with an independently
            # scripted final decision. No fake bypass of the creation guard.
            words = re.findall(r"[a-zA-Z]{4,}", followup["title"])
            step.setdefault("host_tool_calls", []).append(
                {
                    "name": "lookup_followup",
                    "call_id": f"c{attempt}",
                    "arguments": {"followup": copy.deepcopy(followup), "queries": [words[0]]},
                }
            )
            followup.update(
                lookup_id=f"lookup-{attempt}",
                decision="new",
                rationale="The search has no issue covering this problem.",
            )
    return script
