"""The code-run trail under a named-agent assignment (plan S-A4).

A run given the default assignment must leave exactly the recorded
``issue_to_merge`` trail. A run whose builder is a custom agent has its own
recording, ``issue_to_merge_custom_agent.json``: the agent's identity rides
in ``agent_slug``/``agent_name``, which are deliberately not stable trail
keys, so the recording pins that nothing else about the run moved.

Record the custom fixture on purpose only, and only it:
``pytest tests/unit/test_code_run_trail_assignment.py --update-trail``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sbxloop.agents.assignment import plan_assignment
from sbxloop.agents.registry import ConfigAgentRegistry
from tests.conftest import FakeSbx
from tests.fakes.fake_github import GREEN, FakeGithub
from tests.unit.test_code_run_trail import FIXTURES, STABLE_KEYS, trail
from tests.unit.test_engine import (
    BUILD,
    FILES_BUILD,
    REVIEW_OK,
    REVIEW_RC,
    Harness,
    task,
    taskgraph,
)

ADA = {
    "slug": "ada",
    "name": "Ada",
    "instructions": "Prefer the smallest diff that meets the ask.",
    "roles": ["builder"],
}


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


def issue_to_merge(harness: Harness, *, builder: str | None) -> str:
    fake = FakeGithub(draft=True)
    fake.checks = [GREEN]
    harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_RC, BUILD, REVIEW_OK])
    engine = harness.pipeline(fake, **({"agents": [ADA]} if builder else {}))
    assignment = plan_assignment(
        ConfigAgentRegistry(engine.config),
        kind="code",
        lead=None,
        requested={"builder": builder} if builder else {},
        channel_id=None,
    )
    assert assignment.is_default() is (builder is None)
    result = engine.start("write hello.txt", assignment=assignment)
    assert result.state == "merged"
    return result.run_id


def test_identity_keys_are_not_trail_keys() -> None:
    assert "agent_slug" not in STABLE_KEYS
    assert "agent_name" not in STABLE_KEYS


def test_the_default_assignment_leaves_the_recorded_trail(harness: Harness) -> None:
    run_id = issue_to_merge(harness, builder=None)
    expected = json.loads((FIXTURES / "issue_to_merge.json").read_text())
    actual = trail(harness, run_id)
    assert actual["states"] == expected["states"]
    assert actual["sandbox_events"] == expected["sandbox_events"]
    assert actual["events"] == expected["events"]


def test_a_custom_builder_leaves_its_recorded_trail(
    harness: Harness, request: pytest.FixtureRequest
) -> None:
    run_id = issue_to_merge(harness, builder="ada")
    actual = trail(harness, run_id)
    fixture = FIXTURES / "issue_to_merge_custom_agent.json"
    if request.config.getoption("--update-trail"):
        fixture.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
        pytest.skip(f"recorded {fixture}")
    assert fixture.is_file(), f"{fixture} is missing; record it with --update-trail"
    expected = json.loads(fixture.read_text())
    assert actual["states"] == expected["states"]
    assert actual["sandbox_events"] == expected["sandbox_events"]
    assert actual["events"] == expected["events"]
    # The identity is there, just outside the recording.
    builders = [
        e
        for e in harness.events
        if e.type.startswith("agent.") and e.data.get("agent") == "builder"
    ]
    assert builders and {e.data["agent_slug"] for e in builders} == {"ada"}
