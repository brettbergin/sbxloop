"""Repair a completed review's response without repeating its investigation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sbxloop.config import Config
from sbxloop.engine.issue_lookup import IssueLookup
from sbxloop.engine.phases import PhaseRunner
from sbxloop.engine.review import ReviewFinding, ReviewGuard, ReviewVerdict
from sbxloop.engine.store import StateStore
from sbxloop.errors import InvalidOutputTwice
from sbxloop.provider import ProviderHeldError
from sbxloop_worker.protocol import JobRequest, JobResult
from tests.fakes.fake_github import FakeGithub

MAJOR = {
    "path": "src/certificate.py",
    "line": 12,
    "body": "The certificate parser accepts an expired certificate.",
    "severity": "major",
    "repro": "Parse a certificate that expired yesterday: accepted; expected rejection.",
}
MINOR = {
    "path": "src/display.py",
    "line": 21,
    "body": "The label has inconsistent capitalization.",
    "severity": "minor",
}
FOLLOWUP = {
    "title": "The existing status view omits retry timing",
    "body": "The status view predates this change and hides the next retry time.",
    "path": "src/status.py",
    "lookup_id": "existing-receipt",
    "decision": "tracked",
    "existing_issue": 9,
}


def verdict(*findings: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "verdict": "request_changes",
        "summary": "The certificate defect blocks; the label is a minor note.",
        "findings": list(findings),
        **extra,
    }


def reply(output: Any, *, session_id: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "output_json": output,
        "output_text": json.dumps(output),
        "session_id": session_id,
        **extra,
    }


class ScriptedAgent:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.jobs: list[JobRequest] = []
        self.handlers: list[Any] = []

    def submit(
        self, job: JobRequest, *, agent: str | None = None, tool_handler: Any = None
    ) -> JobResult:
        self.jobs.append(job)
        self.handlers.append(tool_handler)
        assert self.responses, "the review exceeded its bounded response-repair budget"
        return JobResult.model_validate(
            {"job_id": job.job_id, "status": "ok", **self.responses.pop(0)}
        )


def runner(agent: ScriptedAgent, workspace: Path, config: Config | None = None) -> PhaseRunner:
    return PhaseRunner(
        agent,  # type: ignore[arg-type]
        config or Config(),
        "r1",
        "Validate certificates and display the result",
        workdir="/workspace/project",
        workspace=workspace,
    )


def review(phases: PhaseRunner) -> ReviewVerdict:
    return phases.review(
        diff="+unique_original_diff_marker",
        pr_number=1,
        round=1,
        tasks=[],
        history="",
        refuted=set(),
    )


def contains_json(text: str, expected: Any) -> bool:
    """Compare embedded JSON semantically, independent of prompt formatting."""
    decoder = json.JSONDecoder()
    for start in re.finditer(r"[\[{]", text):
        try:
            value, _ = decoder.raw_decode(text[start.start() :])
        except json.JSONDecodeError:
            continue
        if value == expected:
            return True
    return False


def without_descriptions(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: without_descriptions(item) for key, item in value.items() if key != "description"
        }
    if isinstance(value, list):
        return [without_descriptions(item) for item in value]
    return value


def assert_response_only(job: JobRequest) -> None:
    assert job.permission_mode == "read_only"
    assert job.available_tools == []
    assert job.host_tools == []
    assert job.mcp_servers == []
    assert job.system_preset is False
    assert job.system_message
    assert "you have real tool" not in job.system_message.lower()
    assert job.prompt is not None
    assert "unique_original_diff_marker" not in job.prompt
    assert contains_json(job.prompt, without_descriptions(ReviewVerdict.model_json_schema()))
    assert "#521" not in job.prompt and "#517" not in job.prompt and "#520" not in job.prompt


def test_missing_severity_then_extra_fields_repairs_without_losing_findings(tmp_path: Path) -> None:
    missing = {key: value for key, value in MINOR.items() if key != "severity"}
    first = verdict(MAJOR, missing, followups=[FOLLOWUP])
    second = verdict(
        *[
            {**finding, "category": "correctness", "short_summary": "Preserve this finding"}
            for finding in (MAJOR, MINOR)
        ],
        followups=[FOLLOWUP],
    )
    final = verdict(MAJOR, MINOR, followups=[FOLLOWUP])
    agent = ScriptedAgent(
        [
            reply(first, session_id="first-review", usage={"input_tokens": 100}, turns=8),
            reply(second, session_id="first-repair", usage={"input_tokens": 20}, turns=1),
            reply(final, session_id="second-repair", usage={"input_tokens": 30}, turns=1),
        ]
    )
    phases = runner(agent, tmp_path)

    result = review(phases)

    assert result == ReviewVerdict.model_validate(final)
    assert len(result.findings) == 2
    assert result.findings[0].model_dump() == ReviewFinding.model_validate(MAJOR).model_dump()
    assert result.findings[1].severity == "minor"
    assert result.followups[0].lookup_id == FOLLOWUP["lookup_id"]
    initial, first_repair, second_repair = agent.jobs
    assert initial.available_tools is None
    assert initial.system_preset is True
    assert initial.resume_session_id is None
    assert initial.prompt is not None and "unique_original_diff_marker" in initial.prompt
    assert_response_only(first_repair)
    assert_response_only(second_repair)
    assert first_repair.resume_session_id == "first-review"
    assert second_repair.resume_session_id == "first-repair"
    assert contains_json(first_repair.prompt or "", first)
    assert contains_json(second_repair.prompt or "", second)
    assert "carry no `repro`" in (first_repair.prompt or "")
    assert "Extra inputs are not permitted" in (second_repair.prompt or "")
    spend = phases.drain_spend()
    assert spend.usage is not None and spend.usage.input_tokens == 150
    assert spend.turns == 10
    assert phases.drain_spend().usage is None


def test_repair_without_a_session_id_still_receives_the_complete_response(tmp_path: Path) -> None:
    first = verdict({**MAJOR, "category": "correctness"})
    agent = ScriptedAgent([reply(first), reply(verdict(MAJOR))])

    result = review(runner(agent, tmp_path))

    assert result.findings[0].body == MAJOR["body"]
    assert len(agent.jobs) == 2
    repaired = agent.jobs[1]
    assert_response_only(repaired)
    assert repaired.resume_session_id is None
    assert contains_json(repaired.prompt or "", first)


@pytest.mark.parametrize("session_id", [None, "review-session"])
def test_refutation_evidence_reaches_corrections_without_another_review(
    tmp_path: Path, session_id: str | None
) -> None:
    history = (
        "### Round 1 — request_changes\n\n"
        "refuted: src/certificate.py:12 — the caller rejects expired certificates "
        "before invoking the parser."
    )
    approved = {
        "verdict": "approve",
        "summary": "The existing refutation resolves this.",
        "findings": [],
    }
    original = verdict(MAJOR)
    agent = ScriptedAgent([reply(original, session_id=session_id), reply(approved)])
    phases = runner(agent, tmp_path)

    result = phases.review(
        diff="+unique_original_diff_marker",
        pr_number=1,
        round=2,
        tasks=[],
        history=history,
        refuted={"src/certificate.py:12"},
    )

    assert result.verdict == "approve"
    initial, correction = agent.jobs
    assert history in (initial.prompt or "")
    assert_response_only(correction)
    assert correction.resume_session_id == session_id
    assert history in (correction.prompt or "")
    assert "already refuted" in (correction.prompt or "")
    assert contains_json(correction.prompt or "", original)
    assert "# Review the pull request" not in (correction.prompt or "")


def test_repair_has_no_skill_lookup_or_configured_mcp_tools(tmp_path: Path) -> None:
    config = Config.model_validate(
        {
            "mcp": [
                {
                    "name": "reference",
                    "transport": "http",
                    "url": "https://reference.example.com/mcp",
                    "hosts": ["reference.example.com"],
                    "roles": ["critic"],
                }
            ]
        }
    )
    first = verdict({**MAJOR, "category": "correctness"}, followups=[FOLLOWUP])
    agent = ScriptedAgent([reply(first), reply(verdict(MAJOR, followups=[FOLLOWUP]))])
    phases = runner(agent, tmp_path, config)
    store = StateStore(tmp_path / "state.db")
    try:
        phases.issue_lookup = IssueLookup(FakeGithub(), "o/r", "r1", store)
        result = review(phases)
    finally:
        store.close()

    initial, repair = agent.jobs
    names = {tool.name for tool in initial.host_tools}
    assert "lookup_followup" in names
    assert len(names) >= 2  # The normal review also gets its skill tool.
    assert initial.mcp_servers
    assert agent.handlers[0] is not None
    assert_response_only(repair)
    assert agent.handlers[1] is None
    assert result.followups[0].lookup_id == FOLLOWUP["lookup_id"]


def test_three_invalid_responses_exhaust_the_repair_budget(tmp_path: Path) -> None:
    invalid = verdict({**MAJOR, "category": "correctness"})
    agent = ScriptedAgent([reply(invalid), reply(invalid), reply(invalid)])

    with pytest.raises(InvalidOutputTwice, match="invalid"):
        review(runner(agent, tmp_path))

    assert len(agent.jobs) == 3
    assert not agent.responses
    for job in agent.jobs[1:]:
        assert_response_only(job)


@pytest.mark.parametrize("during_repair", [False, True])
def test_provider_failure_bypasses_response_repair(tmp_path: Path, during_repair: bool) -> None:
    failure = {
        "status": "error",
        "error": {
            "type": "ProviderFailure",
            "message": "Quota exhausted",
            "provider": {"backend": "claude", "category": "quota", "reason": "Quota exhausted"},
        },
    }
    responses = [failure]
    if during_repair:
        responses.insert(0, reply(verdict({**MAJOR, "category": "correctness"})))
    agent = ScriptedAgent(responses)

    with pytest.raises(ProviderHeldError):
        review(runner(agent, tmp_path))

    assert len(agent.jobs) == (2 if during_repair else 1)


def test_other_json_phases_keep_their_two_attempt_limit(tmp_path: Path) -> None:
    agent = ScriptedAgent([reply({"unexpected": True}), reply({"unexpected": True})])
    phases = runner(agent, tmp_path)

    with pytest.raises(InvalidOutputTwice):
        phases.steer("continue", tasks=[], task=None)

    assert len(agent.jobs) == 2
    assert all(job.available_tools is None for job in agent.jobs)
    assert all(job.system_preset for job in agent.jobs)


def test_valid_review_does_not_start_a_repair(tmp_path: Path) -> None:
    agent = ScriptedAgent([reply(verdict(MAJOR, MINOR))])

    result = review(runner(agent, tmp_path))

    assert result.verdict == "request_changes"
    assert len(agent.jobs) == 1
    assert agent.jobs[0].available_tools is None


def test_strict_review_schema_and_legacy_severity_default_remain_intact() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReviewVerdict.model_validate(verdict({**MAJOR, "category": "correctness"}))
    assert ReviewFinding(path="src/legacy.py", body="An older stored finding").severity == "major"


@pytest.mark.parametrize(
    "changed_findings",
    [
        [],
        [{**MAJOR, "severity": "minor"}],
        [{**MAJOR, "repro": "A different reproduction"}],
        [{**MAJOR, "body": "A different problem at the same line"}],
        [MAJOR],
    ],
    ids=["drop", "downgrade", "replace-evidence", "replace-finding", "approve-unresolved"],
)
def test_repair_cannot_erase_a_reproduced_major_finding(
    tmp_path: Path, changed_findings: list[dict[str, Any]]
) -> None:
    original = verdict({**MAJOR, "category": "correctness"})
    changed = {**verdict(*changed_findings), "verdict": "approve"}
    agent = ScriptedAgent([reply(original), reply(changed), reply(changed)])

    with pytest.raises(InvalidOutputTwice):
        review(runner(agent, tmp_path))

    assert len(agent.jobs) == 3


def test_repair_guard_allows_removing_a_refuted_finding() -> None:
    original = verdict({**MAJOR, "category": "correctness"})
    repaired = ReviewVerdict(verdict="approve", summary="The existing refutation resolves this.")

    ReviewGuard({"src/certificate.py:12"}).check_repair(original, repaired)


def test_repair_guard_preserves_a_minor_finding_without_reproduction() -> None:
    original = {**verdict(MINOR), "verdict": "approve"}
    repaired = ReviewVerdict(verdict="approve", summary="There is no blocker.")

    with pytest.raises(ValueError, match="finding"):
        ReviewGuard(set()).check_repair(original, repaired)


def test_repair_guard_allows_assigning_severity_to_an_incomplete_finding() -> None:
    missing = {key: value for key, value in MINOR.items() if key != "severity"}
    original = verdict(MAJOR, missing)
    repaired = ReviewVerdict.model_validate(verdict(MAJOR, MINOR))

    ReviewGuard(set()).check_repair(original, repaired)


def test_repair_guard_keeps_distinct_findings_at_the_same_anchor() -> None:
    second = {**MAJOR, "body": "Another problem at the same location"}
    original = verdict(MAJOR, second)
    repaired = ReviewVerdict.model_validate(verdict(MAJOR))

    with pytest.raises(ValueError, match="finding"):
        ReviewGuard(set()).check_repair(original, repaired)


def test_repair_guard_does_not_promote_an_original_approval_to_a_blocker() -> None:
    original = {**verdict(MAJOR), "verdict": "approve"}
    repaired = ReviewVerdict.model_validate(original)

    ReviewGuard(set()).check_repair(original, repaired)


def test_repair_guard_does_not_treat_an_unparseable_response_as_evidence() -> None:
    repaired = ReviewVerdict.model_validate(verdict(MAJOR))
    guard = ReviewGuard(set())

    guard.check_repair(None, repaired)
    guard.check_repair({"findings": [None, {"path": ["not", "a", "string"]}]}, repaired)
