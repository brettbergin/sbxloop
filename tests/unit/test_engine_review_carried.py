"""Round *n+1*'s verdict on a carried-over finding goes in its thread (#520 t6).

A later review round used to restate every earlier finding in a fresh review
body while the finding's own conversation stayed open. These tests pin the
split: `split_carried` separates the reviewer's anchor-keyed verdicts on old
findings from its genuinely new ones, and `post_confirmations` puts the
former in the thread the earlier round opened — resolving it only when the
round says the problem is fixed.
"""

from __future__ import annotations

from sbxloop.engine.reconcile import (
    confirm_body,
    confirm_marker,
    post_confirmations,
)
from sbxloop.engine.review import (
    CarriedVerdict,
    ReviewFinding,
    ReviewRound,
    ReviewVerdict,
    prior_findings,
    review_body,
    split_carried,
)
from sbxloop.engine.store import PostedRecord
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import ReviewComment
from tests.fakes.fake_github import FakeGithub

REPO = "o/r"
PR = 11
RUN = "rcarried01"


def seed(gh: FakeGithub, *anchors: tuple[str, int]) -> list[PostedRecord]:
    """Post round 1's review with one inline comment per anchor."""
    submitted = gh.pr_review_create(
        REPO,
        PR,
        "COMMENT",
        "round 1",
        [ReviewComment(path=path, line=line, body="[major] fix it") for path, line in anchors],
    )
    return [
        PostedRecord(1, p.anchor, p.comment_id, p.thread_node_id, submitted.review_id)
        for p in submitted.posted
    ]


def round_one(*findings: ReviewFinding) -> list[ReviewRound]:
    verdict = ReviewVerdict(verdict="request_changes", summary="round 1", findings=list(findings))
    return [ReviewRound(1, verdict, "addressed: a.py:1 — took the lock")]


class TestSplitCarried:
    def test_first_round_review_is_untouched(self) -> None:
        verdict = ReviewVerdict(
            verdict="request_changes",
            summary="s",
            findings=[ReviewFinding(path="a.py", line=1, body="bad")],
        )
        posting, carried = split_carried(verdict, prior_findings([]))
        assert carried == []
        assert posting is verdict

    def test_confirmations_on_known_anchors_leave_the_body_with_new_findings(self) -> None:
        prior = prior_findings(round_one(ReviewFinding(path="a.py", line=1, body="bad")))
        verdict = ReviewVerdict(
            verdict="request_changes",
            summary="round 2",
            findings=[ReviewFinding(path="new.py", line=9, body="fresh problem")],
            confirmations=[
                CarriedVerdict(anchor="a.py:1", status="confirmed_fixed", note="lock taken")
            ],
        )
        posting, carried = split_carried(verdict, prior)
        assert [c.anchor for c in carried] == ["a.py:1"]
        assert [f.anchor for f in posting.findings] == ["new.py:9"]
        body = review_body(posting, run_id=RUN, round=2)
        assert "a.py:1" not in body
        assert [c.path for c in posting.comments()] == ["new.py"]

    def test_a_refiled_old_finding_is_carried_not_a_new_body_finding(self) -> None:
        prior = prior_findings(round_one(ReviewFinding(path="a.py", line=1, body="bad")))
        verdict = ReviewVerdict(
            verdict="request_changes",
            summary="round 2",
            findings=[ReviewFinding(path="a.py", line=1, body="still wrong")],
        )
        posting, carried = split_carried(verdict, prior)
        assert posting.findings == []
        assert [(c.anchor, c.status, c.note) for c in carried] == [
            ("a.py:1", "still_open", "still wrong")
        ]

    def test_a_refiling_beats_a_confirmed_fixed_verdict_on_the_same_anchor(self) -> None:
        prior = prior_findings(round_one(ReviewFinding(path="a.py", line=1, body="bad")))
        verdict = ReviewVerdict(
            verdict="request_changes",
            summary="round 2",
            findings=[ReviewFinding(path="a.py", line=1, body="no, still wrong")],
            confirmations=[CarriedVerdict(anchor="a.py:1", status="confirmed_fixed")],
        )
        _, carried = split_carried(verdict, prior)
        assert [(c.anchor, c.status) for c in carried] == [("a.py:1", "still_open")]

    def test_still_open_carries_the_original_finding_forward(self) -> None:
        original = ReviewFinding(path="a.py", line=1, body="bad", severity="blocking")
        prior = prior_findings(round_one(original))
        verdict = ReviewVerdict(
            verdict="request_changes",
            summary="round 2",
            confirmations=[
                CarriedVerdict(anchor="a.py:1", status="still_open", note="cleanup still last")
            ],
        )
        forward = verdict.carried_forward(prior)
        assert [f.anchor for f in forward] == ["a.py:1"]
        assert forward[0].severity == "blocking"
        assert "cleanup still last" in forward[0].body
        assert forward[0].blocking

    def test_confirmed_fixed_carries_nothing_forward(self) -> None:
        prior = prior_findings(round_one(ReviewFinding(path="a.py", line=1, body="bad")))
        verdict = ReviewVerdict(
            verdict="approve",
            summary="all clear",
            confirmations=[CarriedVerdict(anchor="a.py:1", status="confirmed_fixed")],
        )
        assert verdict.carried_forward(prior) == []

    def test_request_changes_is_valid_on_a_still_open_carried_finding_alone(self) -> None:
        verdict = ReviewVerdict(
            verdict="request_changes",
            summary="the old problem is still there",
            confirmations=[CarriedVerdict(anchor="a.py:1", status="still_open", note="nope")],
        )
        assert verdict.still_open


class TestConfirmBody:
    def test_confirmed_fixed_names_the_round_and_carries_a_marker(self) -> None:
        text = confirm_body(
            CarriedVerdict(anchor="a.py:1", status="confirmed_fixed", note="lock taken"),
            run_id=RUN,
            round=2,
        )
        assert text.startswith("**confirmed fixed** (review round 2): lock taken")
        assert confirm_marker(RUN, 2) in text

    def test_still_open_says_so(self) -> None:
        text = confirm_body(
            CarriedVerdict(anchor="a.py:1", status="still_open", note="cleanup still last"),
            run_id=RUN,
            round=3,
        )
        assert text.startswith("**still open** (review round 3): cleanup still last")


class TestPostConfirmations:
    def test_confirmed_fixed_replies_in_thread_and_resolves_it(self) -> None:
        gh = FakeGithub()
        posted = seed(gh, ("a.py", 1))
        outcome = post_confirmations(
            gh,
            REPO,
            PR,
            run_id=RUN,
            login=gh.user_login,
            round=2,
            items=[CarriedVerdict(anchor="a.py:1", status="confirmed_fixed", note="fixed")],
            posted=posted,
        )
        assert outcome.replied == 1 and outcome.resolved == 1 and outcome.confirmed == 1
        assert gh.replies and "confirmed fixed" in gh.replies[0][1]
        assert gh.resolved == [posted[0].thread_node_id]
        assert all(t.is_resolved for t in gh.threads)

    def test_still_open_replies_and_leaves_the_thread_open(self) -> None:
        gh = FakeGithub()
        posted = seed(gh, ("a.py", 1))
        outcome = post_confirmations(
            gh,
            REPO,
            PR,
            run_id=RUN,
            login=gh.user_login,
            round=2,
            items=[CarriedVerdict(anchor="a.py:1", status="still_open", note="nope")],
            posted=posted,
        )
        assert outcome.replied == 1 and outcome.resolved == 0 and outcome.still_open == 1
        assert gh.resolved == []
        assert not any(t.is_resolved for t in gh.threads)
        assert "still open" in gh.replies[0][1]

    def test_a_body_only_finding_has_no_thread_to_reply_in(self) -> None:
        gh = FakeGithub()
        outcome = post_confirmations(
            gh,
            REPO,
            PR,
            run_id=RUN,
            login=gh.user_login,
            round=2,
            items=[CarriedVerdict(anchor="a.py:0", status="confirmed_fixed")],
            posted=[PostedRecord(1, "a.py:0")],
        )
        assert outcome.body_only == 1 and outcome.replied == 0
        assert gh.replies == []

    def test_the_store_record_skips_an_anchor_already_confirmed(self) -> None:
        gh = FakeGithub()
        posted = seed(gh, ("a.py", 1))
        outcome = post_confirmations(
            gh,
            REPO,
            PR,
            run_id=RUN,
            login=gh.user_login,
            round=2,
            items=[CarriedVerdict(anchor="a.py:1", status="confirmed_fixed")],
            posted=posted,
            done={"a.py:1": "confirmed_fixed"},
        )
        assert outcome.skipped == 1 and gh.replies == []

    def test_an_existing_marked_reply_is_not_repeated_on_resume(self) -> None:
        gh = FakeGithub()
        posted = seed(gh, ("a.py", 1))
        item = CarriedVerdict(anchor="a.py:1", status="confirmed_fixed")
        post_confirmations(
            gh, REPO, PR, run_id=RUN, login=gh.user_login, round=2, items=[item], posted=posted
        )
        # The store record was lost between the reply and the write.
        again = post_confirmations(
            gh, REPO, PR, run_id=RUN, login=gh.user_login, round=2, items=[item], posted=posted
        )
        assert again.skipped == 1 and again.replied == 0
        assert len(gh.replies) == 1

    def test_a_reply_failure_does_not_resolve_or_record(self) -> None:
        gh = FakeGithub()
        posted = seed(gh, ("a.py", 1))
        gh.fail_once["pr_comment_reply"] = GithubOpsError("boom")
        seen: list[str] = []
        outcome = post_confirmations(
            gh,
            REPO,
            PR,
            run_id=RUN,
            login=gh.user_login,
            round=2,
            items=[CarriedVerdict(anchor="a.py:1", status="confirmed_fixed")],
            posted=posted,
            record=lambda *, anchor, status, resolved: seen.append(anchor),
        )
        assert outcome.replied == 0 and gh.resolved == [] and seen == []

    def test_the_record_callback_reports_each_reply(self) -> None:
        gh = FakeGithub()
        posted = seed(gh, ("a.py", 1), ("b.py", 2))
        seen: list[tuple[str, str, bool]] = []
        post_confirmations(
            gh,
            REPO,
            PR,
            run_id=RUN,
            login=gh.user_login,
            round=2,
            items=[
                CarriedVerdict(anchor="a.py:1", status="confirmed_fixed"),
                CarriedVerdict(anchor="b.py:2", status="still_open", note="no"),
            ],
            posted=posted,
            record=lambda *, anchor, status, resolved: seen.append((anchor, status, resolved)),
        )
        assert seen == [("a.py:1", "confirmed_fixed", True), ("b.py:2", "still_open", False)]
