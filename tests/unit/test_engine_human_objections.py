"""A human's changes-requested review, answered on the PR (#520 step 5).

Two rules this file exists to hold down:

* a human's thread gets a **reply** — the change made, or the fixer's
  reasoned explanation — and is **never resolved** by the loop: closing a
  reviewer's conversation is the reviewer's call;
* an objection already answered in this run never buys a second fix pass.
  Only its author can dismiss a ``CHANGES_REQUESTED``, so the same review
  stands on the next landing pass; without the record, that is
  ``max_ci_rounds`` spent re-fixing words already answered.
"""

from __future__ import annotations

import pytest

from sbxloop.engine.landing import HumanObjection
from sbxloop.engine.reconcile import (
    ack_body,
    ack_marker,
    acknowledge_human_threads,
    marker,
    reconcile_human,
)
from sbxloop.engine.store import StateStore
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import ReviewComment, ReviewThread, ThreadComment
from tests.fakes.fake_github import FakeGithub

REPO = "o/r"
PR = 7
RUN = "rabc12345"
HEAD = "0123456789abcdef0123456789abcdef01234567"

REPORT = """
Did the round.

addressed: a.py:3 — took the lock before the read
refuted: b.py:9 — the caller already holds it
"""


def inline(gh: FakeGithub, path: str, line: int) -> HumanObjection:
    """A human inline comment, with the thread GitHub would open for it."""
    submitted = gh.pr_review_create(
        REPO, PR, "COMMENT", "please fix", [ReviewComment(path=path, line=line, body="fix it")]
    )
    posted = submitted.posted[0]
    assert posted.comment_id is not None
    return HumanObjection(
        key=f"human:comment:{posted.comment_id}",
        login="alice",
        body="fix it",
        anchor=f"{path}:{line}",
        comment_id=posted.comment_id,
        thread_node_id=posted.thread_node_id,
    )


def recorder(sink: list[tuple[str, str]]):
    def record(*, key: str, status: str) -> None:
        sink.append((key, status))

    return record


def run(gh: FakeGithub, objections, *, report: str = REPORT, round: int = 1, done=None, sink=None):
    return reconcile_human(
        gh,
        REPO,
        PR,
        run_id=RUN,
        login=gh.user_login,
        round=round,
        head_sha=HEAD,
        objections=objections,
        report=report,
        done=done,
        record=recorder(sink) if sink is not None else None,
    )


class TestReplies:
    def test_an_addressed_objection_gets_a_reply_naming_the_sha(self) -> None:
        gh = FakeGithub()
        objection = inline(gh, "a.py", 3)
        outcome = run(gh, [objection])
        assert outcome.replied == 1
        ((comment_id, body),) = gh.replies
        assert comment_id == objection.comment_id
        assert "**addressed in 0123456789ab**: took the lock before the read" in body
        assert marker(RUN, 1) in body

    def test_a_refuted_objection_gets_the_reasoning(self) -> None:
        gh = FakeGithub()
        outcome = run(gh, [inline(gh, "b.py", 9)])
        assert outcome.replied == 1
        assert "**not changed**: the caller already holds it" in gh.replies[0][1]

    def test_an_objection_the_round_did_not_answer_says_so(self) -> None:
        gh = FakeGithub()
        outcome = run(gh, [inline(gh, "c.py", 1)])
        assert outcome.replied == 1
        body = gh.replies[0][1]
        assert "did not report specifically on this point" in body
        assert "fix it" in body

    def test_a_human_thread_is_never_resolved(self) -> None:
        gh = FakeGithub()
        run(gh, [inline(gh, "a.py", 3), inline(gh, "b.py", 9), inline(gh, "c.py", 1)])
        assert gh.resolved == []
        assert all(not t.is_resolved for t in gh.threads)

    def test_a_review_body_objection_is_answered_in_a_pr_comment(self) -> None:
        gh = FakeGithub()
        objection = HumanObjection(key="human:review:41", login="alice", body="rename foo")
        outcome = run(gh, [objection])
        assert outcome.replied == 0
        assert outcome.body_only == 1
        assert gh.replies == []
        assert gh.resolved == []
        text = gh.issue_comments[0]
        assert "review feedback" in text
        assert "`@alice` — **unanswered**" in text

    def test_a_failed_reply_is_not_recorded_as_answered(self) -> None:
        gh = FakeGithub()
        objection = inline(gh, "a.py", 3)
        gh.fail_once["pr_comment_reply"] = GithubOpsError("nope")
        sink: list[tuple[str, str]] = []
        outcome = run(gh, [objection], sink=sink)
        assert outcome.replied == 0
        assert outcome.answered == ()
        assert sink == []


class TestNoRepeatWork:
    def test_an_objection_recorded_in_the_store_is_skipped(self) -> None:
        gh = FakeGithub()
        objection = inline(gh, "a.py", 3)
        outcome = run(gh, [objection], done={objection.key: "addressed"})
        assert (outcome.replied, outcome.skipped) == (0, 1)
        assert gh.replies == []

    def test_a_live_marker_stops_a_double_reply_after_a_lost_record(self) -> None:
        gh = FakeGithub()
        objection = inline(gh, "a.py", 3)
        first = run(gh, [objection])
        assert first.replied == 1
        # The record never landed (crash between reply and store write).
        sink: list[tuple[str, str]] = []
        second = run(gh, [objection], sink=sink)
        assert (second.replied, second.skipped) == (0, 1)
        assert len(gh.replies) == 1
        assert sink == [(objection.key, "addressed")]

    def test_answered_keys_come_back_for_the_caller_to_persist(self) -> None:
        gh = FakeGithub()
        one, two = inline(gh, "a.py", 3), inline(gh, "b.py", 9)
        outcome = run(gh, [one, two])
        assert set(outcome.answered) == {one.key, two.key}


class TestStoreRecord:
    @pytest.fixture
    def store(self, tmp_path) -> StateStore:
        return StateStore(tmp_path / "state.db")

    def test_answered_objections_round_trip(self, store: StateStore) -> None:
        assert store.answered_objections(RUN) == {}
        store.record_human_reply(RUN, "human:comment:91", "addressed")
        store.record_human_reply(RUN, "human:review:41", "unanswered")
        assert store.answered_objections(RUN) == {
            "human:comment:91": "addressed",
            "human:review:41": "unanswered",
        }
        # Idempotent, and kept apart from the review rounds' own records.
        store.record_human_reply(RUN, "human:comment:91", "addressed")
        assert len(store.answered_objections(RUN)) == 2
        assert store.reconciliations(RUN, 1) == {}


def ack_thread(*comments: tuple[str, str], node: str = "PRRT_9") -> ReviewThread:
    return ReviewThread(
        node_id=node,
        is_resolved=False,
        path="a.py",
        line=3,
        comments=tuple(
            ThreadComment(index + 1, login, body) for index, (login, body) in enumerate(comments)
        ),
    )


class TestAcknowledgeHumanThreads:
    """The landing-time ack: answer a human aside, never resolve their
    thread, never double-post — the loop never waits on a human it never
    asked."""

    LOGIN = "sbxloop-bot"

    def ack(self, gh: FakeGithub, threads: list[ReviewThread]) -> int:
        return acknowledge_human_threads(
            gh, REPO, PR, run_id=RUN, login=self.LOGIN, threads=threads
        )

    def test_replies_with_the_marker_and_the_override_lever(self) -> None:
        gh = FakeGithub()
        assert self.ack(gh, [ack_thread(("alice", "why this way?"))]) == 1
        ((comment_id, body),) = gh.replies
        assert comment_id == 1
        assert "does not hold up the merge" in body
        assert "requesting changes" in body
        assert ack_marker(RUN) in body

    def test_never_resolves_a_human_thread(self) -> None:
        gh = FakeGithub()
        self.ack(gh, [ack_thread(("alice", "why?"))])
        assert gh.resolved == []

    def test_skips_a_thread_the_loop_already_answered(self) -> None:
        gh = FakeGithub()
        answered = ack_thread(("alice", "why?"), (self.LOGIN, "**addressed**: because"))
        assert self.ack(gh, [answered]) == 0
        assert gh.replies == []

    def test_skips_a_marker_stamped_thread(self) -> None:
        """The marker is the resume-safe idempotency key — under either
        spelling of the loop's login (GraphQL drops an App's ``[bot]``)."""
        gh = FakeGithub()
        stamped = ack_thread(("alice", "why?"), (f"{self.LOGIN}[bot]", ack_body(run_id=RUN)))
        assert self.ack(gh, [stamped]) == 0

    def test_a_marker_quoted_by_someone_else_does_not_count(self) -> None:
        """Only the loop's own reply carries the marker (#618): a person
        quoting it back does not make the thread acknowledged."""
        gh = FakeGithub()
        quoted = ack_thread(("alice", "why?"), ("bob", f"> {ack_body(run_id=RUN)}\n\nstill why?"))
        assert self.ack(gh, [quoted]) == 1

    def test_skips_loop_threads_and_rootless_threads(self) -> None:
        gh = FakeGithub()
        own = ack_thread((self.LOGIN, "[minor] naming"))
        rootless = ReviewThread(
            node_id="PRRT_X",
            is_resolved=False,
            path="b.py",
            line=None,
            comments=(ThreadComment(None, "alice", "hm"),),
        )
        assert self.ack(gh, [own, rootless]) == 0
        assert gh.replies == []

    def test_skips_the_loops_own_thread_across_the_bot_suffix(self) -> None:
        """GraphQL spells the App bare; the resolved login carries [bot].
        The ack pass must recognise its own thread either way (field
        failure r9t8hnv33: it replied "noted — this comment did not arrive
        with a changes-requested review" to its own findings)."""
        gh = FakeGithub()
        own = ack_thread(("sbxloop", "[minor] naming"))
        replied = acknowledge_human_threads(
            gh, REPO, PR, run_id=RUN, login="sbxloop[bot]", threads=[own]
        )
        assert replied == 0
        assert gh.replies == []

    def test_a_failed_reply_is_not_counted(self) -> None:
        gh = FakeGithub()
        gh.fail_always["pr_comment_reply"] = GithubOpsError("boom", http_status=502)
        assert self.ack(gh, [ack_thread(("alice", "why?"))]) == 0
