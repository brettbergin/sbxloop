"""Follow-up creation must have repository evidence, including on resume."""

import json
from pathlib import Path

import pytest

from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.followups import followup_key, followup_marker
from sbxloop.engine.issue_lookup import MAX_LOOKUPS, IssueLookup, LookupUnavailable
from sbxloop.engine.review import Followup, ReviewRound, ReviewVerdict, render_review_history
from sbxloop.engine.store import StateStore
from sbxloop.errors import GithubOpsError
from sbxloop_worker.protocol import HostToolCall
from tests.fakes.fake_github import FakeGithub


def test_an_earlier_runs_issue_prevents_refiling() -> None:
    github = FakeGithub()
    key = followup_key("The checks never run")
    github.existing_issues = [
        {
            "number": 12,
            "title": "The checks never run",
            "body": followup_marker("older-run", key),
            "html_url": "https://github.com/o/r/issues/12",
            "state": "open",
        }
    ]
    assert LoopEngine._filed_on_repo(github, "o/r", "follow-up", "new-run") == {
        key: "https://github.com/o/r/issues/12"
    }


def test_review_history_retains_prior_followups() -> None:
    verdict = ReviewVerdict(
        verdict="approve",
        summary="The change works.",
        followups=[Followup(title="Checks never run", body="The workflow omits them.")],
    )
    history = render_review_history([ReviewRound(1, verdict, "")])
    assert "Checks never run" in history
    assert "The workflow omits them." in history


def test_failed_or_malformed_marker_listing_is_not_an_empty_backlog() -> None:
    github = FakeGithub()
    github.fail_always["issue_list"] = GithubOpsError("unavailable")
    with pytest.raises(GithubOpsError):
        LoopEngine._filed_on_repo(github, "o/r", "follow-up", "r1")
    github.fail_always.clear()
    github.issue_list_payload = {"message": "unexpected response"}
    with pytest.raises(LookupUnavailable, match="malformed"):
        LoopEngine._filed_on_repo(github, "o/r", "follow-up", "r1")


def test_this_runs_marker_wins_over_a_previously_fixed_issue() -> None:
    github = FakeGithub()
    key = followup_key("Checks do not run")
    github.existing_issues = [
        {"body": followup_marker("old-run", key), "html_url": "https://github.com/o/r/issues/12"},
        {"body": followup_marker("r1", key), "html_url": "https://github.com/o/r/issues/13"},
    ]
    assert LoopEngine._filed_on_repo(github, "o/r", "follow-up", "r1")[key].endswith("/13")


@pytest.fixture
def lookup(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    store.create_run("r1", "ship the change")
    yield IssueLookup(FakeGithub(), "o/r", "r1", store)
    store.close()


def proposed() -> Followup:
    return Followup(title="The release omits checks", body="The workflow never runs tests.")


def looked_up(lookup: IssueLookup, **changes) -> Followup:
    followup = proposed()
    response = lookup.handle(
        HostToolCall(
            call_id="c1",
            name="lookup_followup",
            arguments={"followup": followup.model_dump(), "queries": ["workflow"]},
        )
    )
    assert response.ok, response.error
    return followup.model_copy(
        update={
            "lookup_id": json.loads(response.text)["lookup_id"],
            "decision": "new",
            "rationale": "No issue covers the missing checks.",
            **changes,
        }
    )


def existing(**changes):
    return {
        "number": 12,
        "title": "Test suite never gates releases",
        "body": "The workflow never runs tests.",
        "state": "open",
        "state_reason": None,
        "html_url": "https://github.com/o/r/issues/12",
        **changes,
    }


def test_issue_evidence_accepts_each_forges_issue_path():
    """The URL an issue lives at is the forge's to spell (#1017): GitHub
    and Gitea serve ``/<repo>/issues/<n>``, GitLab ``/<repo>/-/issues/<n>``;
    a path from another repository, or not an issue at all, is refused."""
    from sbxloop.engine.issue_lookup import issue_evidence

    assert issue_evidence(existing(), "o/r").url == "https://github.com/o/r/issues/12"
    on_gitlab = existing(html_url="https://gitlab.example.com/o/r/-/issues/12")
    assert issue_evidence(on_gitlab, "o/r").number == 12
    for wrong in (
        "https://gitlab.example.com/o/other/-/issues/12",
        "https://github.com/o/r/pull/12",
        "http://github.com/o/r/issues/12",
    ):
        with pytest.raises(LookupUnavailable):
            issue_evidence(existing(html_url=wrong), "o/r")


def test_no_lookup_cannot_authorize_creation(lookup):
    with pytest.raises(LookupUnavailable, match="no completed"):
        lookup.check(proposed())


def test_receipt_survives_resume_and_binds_the_proposal_and_repository(lookup):
    followup = looked_up(lookup)
    resumed = IssueLookup(lookup.ops, "o/r", "r1", lookup.store)
    assert resumed.check(followup) is None
    with pytest.raises(LookupUnavailable, match="no completed"):
        resumed.check(followup.model_copy(update={"body": "An unrelated problem"}))
    with pytest.raises(LookupUnavailable, match="no completed"):
        IssueLookup(lookup.ops, "elsewhere/r", "r1", lookup.store).check(followup)
    lookup.store.create_run("r2", "another run")
    with pytest.raises(LookupUnavailable, match="no completed"):
        IssueLookup(lookup.ops, "o/r", "r2", lookup.store).check(followup)


@pytest.mark.parametrize(
    "state, reason",
    [("open", None), ("closed", "not_planned"), ("closed", "completed"), ("closed", "duplicate")],
)
def test_differently_worded_human_issue_is_reused(lookup, state, reason):
    lookup.ops.existing_issues = [existing(state=state, state_reason=reason)]
    followup = looked_up(lookup, decision="tracked", existing_issue=12)
    assert lookup.check(followup) == "https://github.com/o/r/issues/12"
    assert lookup.ops.issues_created == []


def test_changed_evidence_requires_triage(lookup):
    followup = looked_up(lookup)
    lookup.ops.existing_issues = [existing()]
    with pytest.raises(LookupUnavailable, match="changed after review"):
        lookup.check(followup)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"items": [], "total_count": 0},
        {"items": [], "total_count": 1, "incomplete_results": False},
        {"items": [], "total_count": 0, "incomplete_results": True},
    ],
)
def test_incomplete_or_malformed_search_never_issues_a_receipt(lookup, payload):
    lookup.ops.issue_search_payload = payload
    response = lookup.handle(
        HostToolCall(
            call_id="c1",
            name="lookup_followup",
            arguments={
                "followup": proposed().model_dump(),
                "queries": ["workflow"],
            },
        )
    )
    assert not response.ok
    assert not lookup.store.phase_attempts("r1")


def test_failed_refresh_refuses_creation(lookup):
    followup = looked_up(lookup)
    lookup.ops.fail_always["issue_search"] = GithubOpsError("rate limited")
    with pytest.raises(GithubOpsError):
        lookup.check(followup)


@pytest.mark.parametrize("reason", ["not_planned", "duplicate"])
def test_an_uncompleted_issue_cannot_be_resurrected_as_a_regression(lookup, reason):
    lookup.ops.existing_issues = [existing(state="closed", state_reason=reason)]
    followup = looked_up(lookup, decision="regression", existing_issue=12, repro="It fails")
    with pytest.raises(LookupUnavailable, match="regression needs"):
        lookup.check(followup)


def test_regression_requires_a_completed_issue_and_reproduction(lookup):
    lookup.ops.existing_issues = [existing(state="closed", state_reason="completed")]
    followup = looked_up(lookup, decision="regression", existing_issue=12)
    with pytest.raises(LookupUnavailable, match="regression needs"):
        lookup.check(followup)
    assert (
        lookup.check(followup.model_copy(update={"repro": "New input reproduces the failure"}))
        is None
    )


def test_queries_cannot_escape_the_repository_and_calls_are_bounded(lookup):
    for _ in range(MAX_LOOKUPS):
        response = lookup.handle(
            HostToolCall(
                call_id="c1",
                name="lookup_followup",
                arguments={
                    "followup": proposed().model_dump(),
                    "queries": ["repo:other/private"],
                },
            )
        )
        assert not response.ok
    response = lookup.handle(
        HostToolCall(
            call_id="c1",
            name="lookup_followup",
            arguments={
                "followup": proposed().model_dump(),
                "queries": ["workflow"],
            },
        )
    )
    assert not response.ok and "budget" in response.error
    assert lookup.ops.raw_calls == []
