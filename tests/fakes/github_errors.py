"""Record-and-replay for GithubOps error shapes (#226).

Production classifies GitHub failures by substring on the worker-shaped
error string (there is no structured status channel — #221), so a unit stub
that invents its own string is a second implementation of the truth: the
empty-repo bootstrap shipped against a stubbed 404 while real GitHub says
409 (#219). The stubs therefore load every status-bearing shape from
``tests/fixtures/github_field_errors.json`` through this module; the guard
test in ``tests/unit/test_github_field_errors.py`` keeps inline literals for
the load-bearing statuses out of the unit tests entirely.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from sbxloop.errors import GithubOpsError

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "github_field_errors.json"

# The two worker transports' failure lines (sbxloop_worker.githubops), each
# wrapped in the host facade's ``github op <op> failed: <type>: <message>``
# prefix (sbxloop.gh.ops). A fixture entry must match one of them: even a
# synthetic entry has to be a string the worker could actually emit.
_HOST_PREFIX = r"^github op (?P<op>[a-z_.]+) failed: GithubOpError: "
GH_CLI_SHAPE = re.compile(
    _HOST_PREFIX + r"gh api (?P<method>[A-Z]+) (?P<path>/\S+) failed \(rc=\d+\): "
    r"(?P<stderr>.*\(HTTP (?P<status>\d{3})\))$"
)
REST_SHAPE = re.compile(
    _HOST_PREFIX + r"(?P<method>[A-Z]+) (?P<url>https://\S+) -> HTTP (?P<status>\d{3}): .*$"
)
WORKER_SHAPES = (GH_CLI_SHAPE, REST_SHAPE)


@cache
def field_errors() -> dict[str, dict[str, Any]]:
    data = json.loads(FIXTURE_PATH.read_text())
    errors: dict[str, dict[str, Any]] = data["errors"]
    return errors


def field_error(name: str) -> str:
    """The recorded worker-shaped error string for fixture entry ``name``."""
    return str(field_errors()[name]["message"])


def github_error(name: str) -> GithubOpsError:
    """A GithubOpsError carrying the recorded string AND its status as
    structured data (#221) — the shape the worker really hands the host."""
    entry = field_errors()[name]
    return GithubOpsError(str(entry["message"]), http_status=int(entry["status"]))
