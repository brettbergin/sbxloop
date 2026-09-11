"""The byte-identity gate for ``tool`` runs.

A tool run's promise is that nothing in it is a model's: the chronology is
the command's, the checks', the sinks' — and the same every time. This test
drives the canonical tool scripts (a scan that passes and publishes, a
command that fails, a check that fails) and compares the ordered trail each
leaves against ``tests/fixtures/tool_run_trail/<scenario>.json``, the way
``test_code_run_trail.py`` holds a code run.

The fixture is a recording, not a derivation: regenerate it only on
purpose, with ``pytest --update-trail``, and read the diff before you
commit it. A change here is either a bug in the PR or a deliberate change
to what a tool run does, and the review should know which."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import FakeSbx
from tests.unit.test_code_run_trail import trail
from tests.unit.test_engine import Harness
from tests.unit.test_engine_tool import WRITE_REPORTS, tool

FIXTURES = Path(__file__).parent.parent / "fixtures" / "tool_run_trail"


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


def scenario_scan_published(harness: Harness) -> str:
    harness.script([])
    result = harness.engine().start(
        "scan the tree",
        kind="tool",
        tasks=[tool(WRITE_REPORTS, hosts=["index.example"])],
    )
    assert result.state == "completed", result.reason
    return result.run_id


def scenario_command_failed(harness: Harness) -> str:
    harness.script([])
    result = harness.engine().start("scan the tree", kind="tool", tasks=[tool("exit 3")])
    assert result.state == "failed"
    return result.run_id


def scenario_check_failed(harness: Harness) -> str:
    harness.script([])
    result = harness.engine().start(
        "scan the tree",
        kind="tool",
        tasks=[tool(WRITE_REPORTS, checks=["grep -q nothing-here out/report.md"])],
    )
    assert result.state == "failed"
    return result.run_id


SCENARIOS = {
    "scan_published": scenario_scan_published,
    "command_failed": scenario_command_failed,
    "check_failed": scenario_check_failed,
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_tool_run_trail_matches_the_recording(
    harness: Harness, name: str, request: pytest.FixtureRequest
) -> None:
    run_id = SCENARIOS[name](harness)
    actual = trail(harness, run_id)
    fixture = FIXTURES / f"{name}.json"
    if request.config.getoption("--update-trail"):
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
    assert fixture.is_file(), f"{fixture} is missing; record it with --update-trail"
    expected = json.loads(fixture.read_text())
    assert actual == expected, (
        f"the {name} tool run's trail changed; if that is deliberate, re-record "
        "with `pytest tests/unit/test_tool_run_trail.py --update-trail` and review the diff"
    )


def test_no_agent_session_appears_in_any_trail(harness: Harness) -> None:
    """Whatever else the fixtures hold, none of them may hold a model turn."""
    for name in sorted(SCENARIOS):
        recorded = json.loads((FIXTURES / f"{name}.json").read_text())
        types = {entry["type"] for entry in recorded["events"]}
        assert not any(t.startswith(("agent.", "judge.", "steer.")) for t in types), (name, types)
        assert "phase.end" in types
