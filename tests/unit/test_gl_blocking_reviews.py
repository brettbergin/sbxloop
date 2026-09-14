"""A requested-changes state counts as a gate only when GitLab confirms it."""

import pytest

from sbxloop.vcs.protocol import Capability
from tests.fakes.fake_gitlab import FakeGitlab, gitlab_error


def ready(*, blocking: bool = True) -> FakeGitlab:
    fake = FakeGitlab()
    fake.seed_mr(1, source_branch="feature", head_sha="source")
    fake.blocking_reviews = blocking
    fake.enterprise = True
    return fake


def test_paid_request_changes_blocks_and_later_approval_releases_it() -> None:
    fake = ready()
    review = fake.pr_review_create(fake.repo, 1, "REQUEST_CHANGES", "fix this")
    assert review.event == "REQUEST_CHANGES" and review.gates_merge
    assert fake.capabilities()["request_changes_review"] is Capability.SUPPORTED
    assert fake.merge_requests[1]["detailed_merge_status"] == "requested_changes"
    assert fake.pr_review_state(fake.repo, 1, login=fake.user_login) == "CHANGES_REQUESTED"
    assert fake.pr_review_create(fake.repo, 1, "APPROVE", "fixed").event == "APPROVE"
    assert fake.merge_requests[1]["detailed_merge_status"] == "mergeable"
    assert fake.pr_review_state(fake.repo, 1, login=fake.user_login) == "APPROVED"


def test_free_tier_is_a_truthful_comment() -> None:
    fake = ready(blocking=False)
    review = fake.pr_review_create(fake.repo, 1, "REQUEST_CHANGES", "fix this")
    assert review.event == "COMMENT" and not review.gates_merge
    assert fake.capabilities()["request_changes_review"] is Capability.UNSUPPORTED
    assert not any(body and "mutation" in body.get("query", "") for _, _, body in fake.raw_calls)


@pytest.mark.parametrize("failure", ["schema", "permission", "unrecorded", "unreadable"])
def test_unconfirmed_request_changes_never_claims_a_merge_gate(failure: str) -> None:
    fake = ready()
    if failure == "schema":
        fake.graphql_errors = [{"message": "Field does not exist"}]
    elif failure == "permission":
        fake.request_changes_ok = False
    elif failure == "unrecorded":
        fake.request_changes_recorded = False
    else:
        fake.fail_always["change_requesters"] = gitlab_error(403, "Forbidden")
    review = fake.pr_review_create(fake.repo, 1, "REQUEST_CHANGES", "fix this")
    assert review.event == "COMMENT" and not review.gates_merge
    assert fake.capabilities()["request_changes_review"] is Capability.UNKNOWN
    assert "not confirmed" in fake.mr_notes_posted[-1][1]


def test_requester_readback_pages_past_other_reviewers() -> None:
    fake = ready()
    fake.change_requesters[1] = [f"reviewer-{i}" for i in range(101)]
    assert fake.pr_review_create(fake.repo, 1, "REQUEST_CHANGES", "fix this").gates_merge


def test_an_unread_project_has_no_assumed_license() -> None:
    assert FakeGitlab().capabilities()["request_changes_review"] is Capability.UNKNOWN


def test_ce_schema_without_licensed_fields_is_unsupported() -> None:
    fake = ready(blocking=False)
    fake.enterprise = False
    fake.graphql_errors = [{"message": "Field does not exist"}]
    review = fake.pr_review_create(fake.repo, 1, "REQUEST_CHANGES", "fix this")
    assert review.event == "COMMENT" and not review.gates_merge
    assert fake.capabilities()["request_changes_review"] is Capability.UNSUPPORTED
