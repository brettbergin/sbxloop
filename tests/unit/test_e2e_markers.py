"""Every ``TODO(e2e ...)`` seam must point at something that can retire it (#226).

A field-unverified behavior gets a ``TODO(e2e)`` marker by project
convention; the marker is only useful while somebody can find it again. So
each one has to name either an issue (``TODO(e2e #123)``) or an e2e workflow
step that exercises the seam (``TODO(e2e "Assert artifacts landed on the
host")``). This audit checks the mapping is well-formed and that named
steps exist in ``.github/workflows/e2e.yml``; whether a referenced issue is
still open is a review-time question — the unit suite has no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = [
    REPO_ROOT / "packages" / "sbxloop" / "src",
    REPO_ROOT / "packages" / "sbxloop-worker" / "src",
]
E2E_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "e2e.yml"

MARKER = re.compile(r"TODO\(e2e(?P<locator>[^)]*)\)")
ISSUE_LOCATOR = re.compile(r"^ #\d+$")
STEP_LOCATOR = re.compile(r'^ "(?P<step>[^"]+)"$')


def e2e_step_names(workflow: Path = E2E_WORKFLOW) -> set[str]:
    return {
        m.group(1).strip() for m in re.finditer(r"^\s+- name: (.+)$", workflow.read_text(), re.M)
    }


def find_markers(roots: list[Path] = SOURCE_ROOTS) -> list[tuple[str, str]]:
    """(location, locator) for every marker under ``roots``."""
    found: list[tuple[str, str]] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                for match in MARKER.finditer(line):
                    found.append(
                        (f"{path.relative_to(REPO_ROOT)}:{lineno}", match.group("locator"))
                    )
    return found


def unmapped(markers: list[tuple[str, str]], steps: set[str]) -> list[str]:
    problems: list[str] = []
    for location, locator in markers:
        if ISSUE_LOCATOR.match(locator):
            continue
        step = STEP_LOCATOR.match(locator)
        if step and step.group("step") in steps:
            continue
        if step:
            problems.append(f"{location}: no e2e.yml step named {step.group('step')!r}")
        else:
            problems.append(
                f"{location}: TODO(e2e{locator}) must be "
                'TODO(e2e #<issue>) or TODO(e2e "<e2e step>")'
            )
    return problems


class TestAudit:
    def test_repo_markers_are_mapped(self) -> None:
        markers = find_markers()
        assert markers, "expected at least one TODO(e2e) marker; did the grammar change?"
        assert unmapped(markers, e2e_step_names()) == []

    def test_e2e_workflow_has_named_steps(self) -> None:
        assert "Assert artifacts landed on the host" in e2e_step_names()

    @pytest.mark.parametrize(
        ("locator", "ok"),
        [
            (" #226", True),
            (' "Smoke run"', True),
            ('"Smoke run"', False),  # no space: not the grammar
            (' "no such step"', False),
            ("", False),
            (":", False),  # the pre-#226 bare form
            (" #", False),
        ],
    )
    def test_locator_grammar(self, locator: str, ok: bool) -> None:
        problems = unmapped([("x.py:1", locator)], {"Smoke run"})
        assert (problems == []) is ok, problems
