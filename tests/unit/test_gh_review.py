"""Folding GitHub's review and check-run payloads into one verdict.

The loop gates a merge on these two answers, so the folds have to be exactly
right about the boundary cases: a PR whose checks have not reported yet has
not passed, a reviewer's newest verdict is the only one that counts, and a
conclusion nobody recognises is not permission to merge.

Pure over fixture payloads — no transport, no sandbox.
"""

from __future__ import annotations

from sbxloop.gh.ops import (
    ChecksVerdict,
    ReviewComment,
    SubmittedReview,
    fold_check_runs,
    fold_reviews,
    review_payload,
)


def runs(*entries: tuple[str, str | None]) -> dict[str, object]:
    return {"check_runs": [{"name": name, "conclusion": c} for name, c in entries]}


def review(login: str, state: str) -> dict[str, object]:
    return {"user": {"login": login}, "state": state}


class TestFoldCheckRuns:
    def test_all_successful_is_green(self) -> None:
        verdict = fold_check_runs(runs(("lint", "success"), ("test", "success")))
        assert verdict.state == "green"
        assert verdict.total == 2 and verdict.failed == ()

    def test_an_unreported_run_is_pending_not_green(self) -> None:
        """The whole point of the gate: "no failures so far" is not success,
        and settling a PR on it is how a red one gets marked done."""
        verdict = fold_check_runs(runs(("lint", "success"), ("test", None)))
        assert verdict.state == "pending"
        assert verdict.pending == ("test",)

    def test_red_beats_pending(self) -> None:
        """The build is already known broken; waiting on the stragglers only
        delays the fix."""
        verdict = fold_check_runs(runs(("lint", "failure"), ("test", None)))
        assert verdict.state == "red"
        assert verdict.failed == ("lint",) and verdict.pending == ("test",)

    def test_neutral_and_skipped_are_not_failures(self) -> None:
        """A skipped job is not a red build. Treating it as one would wedge
        the fix loop against something no commit can change."""
        assert fold_check_runs(runs(("a", "neutral"), ("b", "skipped"))).state == "green"

    def test_an_unknown_conclusion_fails_closed(self) -> None:
        """A conclusion nobody understands must not read as permission to
        merge — including one GitHub adds after this was written."""
        verdict = fold_check_runs(runs(("weird", "invented_by_github_later")))
        assert verdict.state == "red" and verdict.failed == ("weird",)

    def test_every_real_failure_conclusion_is_red(self) -> None:
        for conclusion in ("failure", "timed_out", "cancelled", "action_required", "stale"):
            assert fold_check_runs(runs(("job", conclusion))).state == "red", conclusion

    def test_a_repo_without_ci_is_green_not_deadlocked(self) -> None:
        """No checks at all must not hold the loop waiting for a report that
        will never arrive."""
        assert fold_check_runs({"check_runs": []}).state == "green"

    def test_a_malformed_payload_does_not_raise(self) -> None:
        assert fold_check_runs(None).state == "green"
        assert fold_check_runs({"check_runs": "nope"}).state == "green"
        assert (
            fold_check_runs({"check_runs": [None, {"name": "ok", "conclusion": "success"}]}).state
            == "green"
        )

    def test_summary_names_the_failures(self) -> None:
        verdict = fold_check_runs(runs(("lint", "failure"), ("mdformat", "failure")))
        assert "lint" in verdict.summary() and "mdformat" in verdict.summary()
        assert ChecksVerdict("green", 1, (), ()).summary() == "all 1 check(s) passed"


class TestFoldReviews:
    def test_only_the_latest_verdict_per_reviewer_counts(self) -> None:
        """GitHub keeps every review ever submitted; an APPROVE after a
        REQUEST_CHANGES clears it."""
        payload = [review("bot", "CHANGES_REQUESTED"), review("bot", "APPROVED")]
        assert fold_reviews(payload) == "APPROVED"

    def test_changes_requested_after_approval_stands(self) -> None:
        payload = [review("bot", "APPROVED"), review("bot", "CHANGES_REQUESTED")]
        assert fold_reviews(payload) == "CHANGES_REQUESTED"

    def test_one_reviewer_blocking_blocks(self) -> None:
        payload = [review("human", "APPROVED"), review("bot", "CHANGES_REQUESTED")]
        assert fold_reviews(payload) == "CHANGES_REQUESTED"

    def test_comment_reviews_never_change_the_verdict(self) -> None:
        """GitHub's own rule: a COMMENT review leaves the reviewer's standing
        verdict untouched."""
        payload = [review("bot", "CHANGES_REQUESTED"), review("bot", "COMMENTED")]
        assert fold_reviews(payload) == "CHANGES_REQUESTED"

    def test_a_dismissed_review_no_longer_stands(self) -> None:
        payload = [review("bot", "CHANGES_REQUESTED"), review("bot", "DISMISSED")]
        assert fold_reviews(payload) == "NONE"

    def test_login_narrows_the_fold_to_one_reviewer(self) -> None:
        """How the loop asks "did *my* review get satisfied?" without a
        human's approval answering on its behalf."""
        payload = [review("human", "APPROVED"), review("bot", "CHANGES_REQUESTED")]
        assert fold_reviews(payload, login="bot") == "CHANGES_REQUESTED"
        assert fold_reviews(payload, login="human") == "APPROVED"
        assert fold_reviews(payload, login="nobody") == "NONE"

    def test_no_reviews_is_none(self) -> None:
        assert fold_reviews([]) == "NONE"
        assert fold_reviews(None) == "NONE"
        assert fold_reviews([None, "nonsense"]) == "NONE"


class TestReviewPayload:
    def test_a_summary_review_omits_the_comments_key(self) -> None:
        assert review_payload("APPROVE", "looks good") == {
            "event": "APPROVE",
            "body": "looks good",
        }

    def test_inline_comments_carry_their_anchor(self) -> None:
        payload = review_payload(
            "REQUEST_CHANGES",
            "two problems",
            [ReviewComment(path="a.py", line=12, body="off by one")],
        )
        assert payload["event"] == "REQUEST_CHANGES"
        assert payload["comments"] == [
            {"path": "a.py", "line": 12, "side": "RIGHT", "body": "off by one"}
        ]

    def test_a_deleted_line_can_be_annotated_on_the_left(self) -> None:
        payload = review_payload(
            "COMMENT", "why?", [ReviewComment(path="a.py", line=3, body="?", side="LEFT")]
        )
        assert payload["comments"][0]["side"] == "LEFT"


class TestSubmittedReview:
    def test_only_approve_and_request_changes_gate_the_merge(self) -> None:
        """A COMMENT review gates nothing. A caller that assumed otherwise
        would wait forever for an approval nobody was asked to give."""
        assert SubmittedReview("u", "REQUEST_CHANGES").gates_merge
        assert SubmittedReview("u", "APPROVE").gates_merge
        assert not SubmittedReview("u", "COMMENT").gates_merge
