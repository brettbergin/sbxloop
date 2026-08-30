"""Reconciling a review round back onto the pull request (#520 step 3).

`engine.reconcile.reconcile_round` is the deterministic step between a fix
round's re-delivery and the next review: reply on every finding's own
thread, resolve the ones the fixer addressed, gather the body-only ones
into a single round comment — and do none of it twice. These tests drive it
against the fake GitHub, so both the GitHub calls and the store records it
asks for are observable.
"""

from __future__ import annotations

from functools import partial

import pytest

from sbxloop.engine.landing import unreconciled_threads
from sbxloop.engine.reconcile import (
    BODY_COMMENT_KEY,
    ReconcileOutcome,
    body_comment,
    marker,
    reconcile_round,
    reply_body,
)
from sbxloop.engine.review import Reconciliation
from sbxloop.engine.store import PostedRecord, StateStore
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import ReviewComment
from tests.fakes.fake_github import FakeGithub

REPO = "o/r"
PR = 7
RUN = "rabc12345"
HEAD = "0123456789abcdef0123456789abcdef01234567"


def seed(gh: FakeGithub, *anchors: tuple[str, int]) -> list[PostedRecord]:
    """Post a review carrying one inline comment per anchor; return what the
    store would later hand reconciliation back."""
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


def recorder(sink: list[tuple[str, str, bool]]):
    def record(*, anchor: str, status: str, resolved: bool) -> None:
        sink.append((anchor, status, resolved))

    return record


class TestReplyRendering:
    def test_addressed_names_the_sha_and_carries_the_marker(self) -> None:
        text = reply_body("addressed", "took the lock first", head_sha=HEAD, run_id=RUN, round=2)
        assert text.startswith("**addressed in 0123456789ab**: took the lock first")
        assert marker(RUN, 2) in text

    def test_refuted_says_why_and_names_no_sha(self) -> None:
        text = reply_body("refuted", "the caller holds it", head_sha=HEAD, run_id=RUN, round=1)
        assert text.startswith("**refuted**: the caller holds it")
        assert HEAD[:12] not in text

    def test_unanswered_says_the_round_did_not_report(self) -> None:
        text = reply_body("unanswered", "", head_sha=HEAD, run_id=RUN, round=3)
        assert "not answered" in text
        assert "fix round 3" in text

    def test_body_comment_lists_every_anchor_with_its_status(self) -> None:
        text = body_comment(
            [
                ("a.py:1", Reconciliation("addressed", "rewrote it")),
                ("b.py:2", Reconciliation("refuted", "intentional")),
            ],
            head_sha=HEAD,
            run_id=RUN,
            round=2,
        )
        assert text.startswith("## Reconciliation — round 2")
        assert "- `a.py:1` — **addressed** — rewrote it" in text
        assert "- `b.py:2` — **refuted** — intentional" in text
        assert marker(RUN, 2) in text


class TestReconcileRound:
    def test_addressed_thread_is_replied_to_and_resolved(self) -> None:
        gh = FakeGithub()
        posted = seed(gh, ("src/app.py", 12))
        records: list[tuple[str, str, bool]] = []
        out = reconcile_round(
            gh,
            REPO,
            PR,
            run_id=RUN,
            round=1,
            head_sha=HEAD,
            posted=posted,
            items={"src/app.py:12": Reconciliation("addressed", "took the lock first")},
            record=recorder(records),
        )
        assert out == ReconcileOutcome(
            round=1, addressed=1, replied=1, resolved=1, comment_url=None
        )
        assert len(gh.replies) == 1
        comment_id, body = gh.replies[0]
        assert comment_id == posted[0].comment_id
        assert body.startswith("**addressed in 0123456789ab**: took the lock first")
        assert gh.resolved == [posted[0].thread_node_id]
        assert records == [("src/app.py:12", "addressed", True)]

    def test_refuted_and_unanswered_are_replied_to_but_left_open(self) -> None:
        gh = FakeGithub()
        posted = seed(gh, ("a.py", 1), ("b.py", 2))
        out = reconcile_round(
            gh,
            REPO,
            PR,
            run_id=RUN,
            round=1,
            head_sha=HEAD,
            posted=posted,
            items={
                "a.py:1": Reconciliation("refuted", "the caller holds it"),
                "b.py:2": Reconciliation("unanswered", ""),
            },
        )
        assert (out.refuted, out.unanswered, out.replied, out.resolved) == (1, 1, 2, 0)
        assert gh.resolved == []
        assert not any(t.is_resolved for t in gh.threads)
        assert gh.replies[0][1].startswith("**refuted**: the caller holds it")
        assert "not answered" in gh.replies[1][1]

    def test_body_only_findings_get_one_round_comment(self) -> None:
        gh = FakeGithub()
        records: list[tuple[str, str, bool]] = []
        posted = [PostedRecord(1, "a.py:1"), PostedRecord(1, "b.py:2")]
        out = reconcile_round(
            gh,
            REPO,
            PR,
            run_id=RUN,
            round=2,
            head_sha=HEAD,
            posted=posted,
            items={
                "a.py:1": Reconciliation("addressed", "rewrote it"),
                "b.py:2": Reconciliation("refuted", "intentional"),
            },
            record=recorder(records),
        )
        assert out.body_only == 2
        assert out.replied == 0
        assert gh.replies == []
        assert len(gh.issue_comments) == 1
        text = gh.issue_comments[0]
        assert text.startswith("## Reconciliation — round 2")
        assert "- `a.py:1` — **addressed** — rewrote it" in text
        assert out.comment_url
        assert records == [(BODY_COMMENT_KEY, "posted", False)]

    def test_findings_the_round_did_not_reconcile_are_untouched(self) -> None:
        gh = FakeGithub()
        posted = seed(gh, ("a.py", 1))
        out = reconcile_round(
            gh,
            REPO,
            PR,
            run_id=RUN,
            round=1,
            head_sha=HEAD,
            posted=posted,
            items={"other.py:9": Reconciliation("addressed", "x")},
        )
        assert out.total == 0
        assert gh.replies == []
        assert not out.did_anything

    def test_a_failed_reply_does_not_resolve_or_record(self) -> None:
        gh = FakeGithub()
        posted = seed(gh, ("a.py", 1))
        gh.fail_once["pr_comment_reply"] = GithubOpsError("boom")
        records: list[tuple[str, str, bool]] = []
        out = reconcile_round(
            gh,
            REPO,
            PR,
            run_id=RUN,
            round=1,
            head_sha=HEAD,
            posted=posted,
            items={"a.py:1": Reconciliation("addressed", "done")},
            record=recorder(records),
        )
        assert (out.replied, out.resolved) == (0, 0)
        assert gh.resolved == []
        assert records == []


class TestIdempotency:
    def test_store_record_short_circuits_a_second_pass(self) -> None:
        gh = FakeGithub()
        posted = seed(gh, ("a.py", 1))
        items = {"a.py:1": Reconciliation("addressed", "done")}
        run = partial(
            reconcile_round,
            gh,
            REPO,
            PR,
            run_id=RUN,
            round=1,
            head_sha=HEAD,
            posted=posted,
            items=items,
        )
        run()
        again = run(done={"a.py:1": "addressed"})
        assert len(gh.replies) == 1
        assert (again.replied, again.skipped) == (0, 1)

    def test_live_marker_stops_a_double_reply_when_the_record_was_lost(self) -> None:
        """The resume window: the reply landed, the store record did not."""
        gh = FakeGithub()
        posted = seed(gh, ("a.py", 1))
        items = {"a.py:1": Reconciliation("addressed", "done")}
        run = partial(
            reconcile_round,
            gh,
            REPO,
            PR,
            run_id=RUN,
            round=1,
            head_sha=HEAD,
            posted=posted,
            items=items,
        )
        run()
        records: list[tuple[str, str, bool]] = []
        again = run(record=recorder(records))
        assert len(gh.replies) == 1
        assert again.replied == 0 and again.skipped == 1
        # It is recorded on the way past, so the third pass is free.
        assert records == [("a.py:1", "addressed", True)]

    def test_a_later_round_replies_again_on_the_same_thread(self) -> None:
        gh = FakeGithub()
        posted = seed(gh, ("a.py", 1))
        items = {"a.py:1": Reconciliation("addressed", "done")}
        reconcile_round(
            gh, REPO, PR, run_id=RUN, round=1, head_sha=HEAD, posted=posted, items=items
        )
        reconcile_round(
            gh, REPO, PR, run_id=RUN, round=2, head_sha=HEAD, posted=posted, items=items
        )
        assert len(gh.replies) == 2

    def test_body_only_comment_is_posted_once(self) -> None:
        gh = FakeGithub()
        posted = [PostedRecord(1, "a.py:1")]
        items = {"a.py:1": Reconciliation("refuted", "no")}
        run = partial(
            reconcile_round,
            gh,
            REPO,
            PR,
            run_id=RUN,
            round=1,
            head_sha=HEAD,
            posted=posted,
            items=items,
        )
        run()
        again = run(done={BODY_COMMENT_KEY: "posted"})
        assert len(gh.issue_comments) == 1
        assert again.comment_url is None

    def test_unreadable_threads_do_not_stop_the_replies(self) -> None:
        gh = FakeGithub()
        posted = seed(gh, ("a.py", 1))
        gh.fail_once["pr_review_threads"] = GithubOpsError("no graphql for you")
        out = reconcile_round(
            gh,
            REPO,
            PR,
            run_id=RUN,
            round=1,
            head_sha=HEAD,
            posted=posted,
            items={"a.py:1": Reconciliation("addressed", "done")},
        )
        assert out.replied == 1
        # The node id came from the store record, so it still resolved.
        assert gh.resolved == [posted[0].thread_node_id]

    def test_failed_capture_still_finds_its_live_thread_by_anchor(self) -> None:
        """`comment_id=None` can mean capture failed, not that no thread exists.

        The inline comment went out in the review POST body and is live on
        the PR; treating it as body-only leaves an unreplied loop thread and
        the merge gate deadlocks on it (#520).
        """
        gh = FakeGithub()
        seed(gh, ("a.py", 1))
        posted = [PostedRecord(1, "a.py:1", None, None)]
        records: list[tuple[str, str, bool]] = []
        out = reconcile_round(
            gh,
            REPO,
            PR,
            run_id=RUN,
            round=1,
            head_sha=HEAD,
            posted=posted,
            items={"a.py:1": Reconciliation("addressed", "took the lock")},
            record=recorder(records),
        )
        assert (out.replied, out.resolved, out.body_only) == (1, 1, 0)
        assert gh.issue_comments == []
        assert len(gh.replies) == 1
        assert gh.replies[0][1].startswith("**addressed in 0123456789ab**: took the lock")
        assert records == [("a.py:1", "addressed", True)]
        live = gh.pr_review_threads(REPO, PR)
        assert unreconciled_threads(live, login=gh.user_login) == ([], [])


class TestStoreRecords:
    @pytest.fixture
    def store(self, tmp_path) -> StateStore:  # type: ignore[no-untyped-def]
        return StateStore(tmp_path / "state.db")

    def test_records_are_per_round_and_idempotent(self, store: StateStore) -> None:
        store.create_run(RUN, "outcome")
        store.record_reconciliation(RUN, 1, "a.py:1", "addressed", resolved=True)
        store.record_reconciliation(RUN, 1, "a.py:1", "addressed", resolved=True)
        store.record_reconciliation(RUN, 2, "b.py:2", "refuted")
        assert store.reconciliations(RUN, 1) == {"a.py:1": "addressed"}
        assert store.reconciliations(RUN, 2) == {"b.py:2": "refuted"}
        assert store.reconciliations(RUN) == {"a.py:1": "addressed", "b.py:2": "refuted"}
        assert store.reconciliations("nobody") == {}
