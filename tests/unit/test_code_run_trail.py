"""The byte-identity gate for ``code`` runs (#765, #754).

Every PR in the agentic-workloads stack promises that a plain ``code`` run
— no credentials, no workload — behaves exactly as it did at ``main``
before the stack started. This test drives the canonical pipeline scripts
(issue-to-merge, review rounds with a refutation, gate-red, review
exhaustion) and compares the ordered trail each leaves — the run states
and every event with its structural fields — against
``tests/fixtures/code_run_trail/<scenario>.json``.

The fixture is a recording, not a derivation: regenerate it only on
purpose, with ``pytest --update-trail``, and read the diff before you
commit it. A change here is either a bug in the PR or a deliberate change
to what a code run does, and the review should know which."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from sbxloop.events import Event
from tests.conftest import FakeSbx
from tests.fakes.fake_github import GREEN, FakeGithub
from tests.unit.test_engine import (
    BUILD,
    FILES_BUILD,
    REVIEW_OK,
    REVIEW_RC,
    Harness,
    task,
    taskgraph,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "code_run_trail"

# The fields that describe WHAT happened, never how long it took or under
# which identifiers: the trail is order and structure, not timing.
STABLE_KEYS = frozenset(
    {
        "state",
        "phase",
        "task_id",
        "status",
        "attempt",
        "kind",
        "agent",
        "role",
        "stage",
        "exhausted",
        "verdict",
        "action",
        "round",
        "pr",
        "name",
        "sandbox",
        "ok",
        "tool",
        "rc",
        "mounted",
        "delivery",
        "envs",
        "hosts",
        "method",
        "target",
        "backend",
        "model",
    }
)
# Chatter whose presence or count depends on scheduling, not on the run.
NOISE = re.compile(r"^(worker\.stdout|sandbox\.resources.*|agent\.message_delta|agent\.usage)$")


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


def _scrub(value: Any, run_id: str) -> Any:
    if isinstance(value, str):
        return value.replace(run_id, "<run>")
    if isinstance(value, list):
        return [_scrub(v, run_id) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, run_id) for k, v in sorted(value.items())}
    return value


def _entry(event: Event, run_id: str) -> dict[str, Any]:
    data = {k: _scrub(v, run_id) for k, v in event.data.items() if k in STABLE_KEYS}
    return {"type": event.type, **data}


def trail(harness: Harness, run_id: str) -> dict[str, Any]:
    """The run's chronology as the fixture records it. Sandbox events are
    kept as a sorted set: the agent and github sandboxes provision in
    parallel, so their interleaving is scheduling, not behaviour."""
    ordered: list[dict[str, Any]] = []
    sandbox: list[dict[str, Any]] = []
    for event in harness.events:
        if NOISE.match(event.type):
            continue
        entry = _entry(event, run_id)
        (sandbox if event.type.startswith("sandbox.") else ordered).append(entry)
    return {
        "states": harness.run_states(),
        "events": ordered,
        "sandbox_events": sorted(sandbox, key=lambda e: json.dumps(e, sort_keys=True)),
    }


def scenario_issue_to_merge(harness: Harness) -> str:
    fake = FakeGithub(draft=True)
    fake.checks = [GREEN]
    harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_RC, BUILD, REVIEW_OK])
    result = harness.pipeline(fake).start("write hello.txt")
    assert result.state == "merged"
    return result.run_id


def scenario_review_rounds(harness: Harness) -> str:
    fake = FakeGithub()
    refute = {"text": "Left as is.\n\nrefuted: hello.txt:1 — the greeting is specified as hi"}
    harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_RC, refute, REVIEW_RC, REVIEW_OK])
    result = harness.pipeline(fake).start("write hello.txt")
    assert result.state == "merged"
    return result.run_id


def scenario_gate_red(harness: Harness) -> str:
    gate = "grep -q green state.txt"
    fake = FakeGithub()
    harness.script(
        [
            taskgraph(task("t1", verify=[gate]), task("t2", deps=["t1"])),
            {"text": "t1", "files": {"state.txt": "green\n", "hello.txt": "hi\n"}},
            {"text": "t2 broke it", "files": {"state.txt": "red\n"}},
            {"text": "fixed", "files": {"state.txt": "green\n"}},
            REVIEW_OK,
        ]
    )
    result = harness.pipeline(fake, sandbox={"gate_command": gate}).start("keep the gate green")
    assert result.state == "merged"
    return result.run_id


def scenario_review_exhausted(harness: Harness) -> str:
    fake = FakeGithub()
    harness.script([taskgraph(task("t1")), FILES_BUILD, REVIEW_RC, BUILD, REVIEW_RC])
    result = harness.pipeline(fake, landing={"max_review_rounds": 1}).start("never good enough")
    assert result.state == "failed"
    return result.run_id


SCENARIOS = {
    "issue_to_merge": scenario_issue_to_merge,
    "review_rounds": scenario_review_rounds,
    "gate_red": scenario_gate_red,
    "review_exhausted": scenario_review_exhausted,
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_code_run_trail_matches_the_recording(
    harness: Harness, name: str, request: pytest.FixtureRequest
) -> None:
    run_id = SCENARIOS[name](harness)
    actual = trail(harness, run_id)
    fixture = FIXTURES / f"{name}.json"
    if request.config.getoption("--update-trail"):
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
        pytest.skip(f"recorded {fixture}")
    assert fixture.is_file(), f"{fixture} is missing; record it with --update-trail"
    expected = json.loads(fixture.read_text())
    assert actual["states"] == expected["states"]
    assert actual["sandbox_events"] == expected["sandbox_events"]
    assert actual["events"] == expected["events"]
