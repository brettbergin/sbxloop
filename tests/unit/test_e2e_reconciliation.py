"""End to end: a run with two review rounds merges fully reconciled (#520).

This is the whole of #520 in one run. A real :class:`LoopEngine` drives the
fake GitHub through two review rounds, and every assertion is made against
what the fake *recorded* — the calls a human reading the merged pull request
would see the effect of:

- round 1 posts one finding anchored to a line (it opens its own inline
  thread) and one with no line (it can only go in the review body);
- the fix round re-delivers and answers both in its report, in the
  ``addressed:`` / ``refuted:`` form :func:`sbxloop.engine.review.fix_brief`
  asks for;
- before round 2 runs, the loop replies in the inline finding's own thread
  and resolves it, and reports the body-only one in a single
  ``Reconciliation — round 1`` pull request comment;
- round 2 confirms the carried-over finding **in that thread** instead of
  restating it in a fresh review body;
- ``review.reconciled`` reaches the bus with its counts, per round;
- and the merge only happens because the gate found no loop thread left
  unresolved-and-unanswered — the same gate blocks when one is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.landing import unreconciled_threads
from sbxloop.engine.model import RunResult
from sbxloop.engine.store import StateStore
from sbxloop.events import HostEventTypes
from tests.conftest import FakeSbx
from tests.fakes.fake_github import FakeGithub
from tests.unit.test_engine import FILES_BUILD, Harness, task, taskgraph


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


# Round 1's two findings. Deliberately on different paths: a report line is
# matched to a finding by path first, so two findings sharing a path would
# make the parse ambiguous rather than exercise it.
INLINE_FINDING = {
    "path": "hello.txt",
    "line": 1,
    "body": "hello.txt must greet with hello, not hi",
    "severity": "major",
    # #521: a major finding without a repro is sent back once.
    "repro": "cat hello.txt prints 'hi'; expected 'hello'",
}
BODY_FINDING = {
    "path": "docs/greeting.md",
    "line": None,
    "body": "the greeting is not documented anywhere",
    "severity": "major",
    # #521: a major finding without a repro is sent back once.
    "repro": "grep -r greeting docs/ finds nothing; expected a docs/greeting.md entry",
}


def review_round(verdict: str, summary: str, **extra: Any) -> dict[str, Any]:
    return {"json": {"verdict": verdict, "summary": summary, **extra}}


ROUND_1 = review_round("request_changes", "two problems", findings=[INLINE_FINDING, BODY_FINDING])

# The fixer changes one thing and refuses the other, with a reason, and says
# so per finding in the closing brief.
FIX_ROUND = {
    "text": (
        "Greeting corrected; the docs point is not a problem.\n\n"
        "addressed: hello.txt:1 — say hello, not hi\n"
        "refuted: docs/greeting.md — a one-line fixture needs no documentation"
    ),
    "files": {"hello.txt": "hello\n"},
}

# Round 2 raises nothing new: it only rules on what round 1 carried over.
ROUND_2 = review_round(
    "approve",
    "the carried-over findings are settled",
    findings=[],
    confirmations=[
        {
            "anchor": "hello.txt:1",
            "status": "confirmed_fixed",
            "note": "greets with hello now",
        }
    ],
)

RUN_ID_MARKER = "sbxloop:reconciled"


class TestTwoRoundRunIsReconciledOnThePr:
    @pytest.fixture
    def run(self, harness: Harness) -> tuple[Harness, FakeGithub, LoopEngine, RunResult]:
        fake = FakeGithub()
        harness.script([taskgraph(task("t1")), FILES_BUILD, ROUND_1, FIX_ROUND, ROUND_2])
        engine = harness.pipeline(fake)
        result = engine.start("write hello.txt")
        assert result.state == "merged", result.state
        return harness, fake, engine, result

    def _reconciled(self, harness: Harness) -> list[dict[str, Any]]:
        return [e.data for e in harness.events if e.type == HostEventTypes.REVIEW_RECONCILED]

    # -- the shape of the run ------------------------------------------------

    def test_two_review_rounds_ran_and_the_pr_merged(
        self, run: tuple[Harness, FakeGithub, LoopEngine, RunResult]
    ) -> None:
        _, fake, engine, result = run
        assert [event for event, _, _ in fake.reviews] == ["REQUEST_CHANGES", "APPROVE"]
        # One fix round re-delivered between them: the head the replies cite.
        assert fake.head_sha == "commit2"
        assert fake.merges == [(7, "squash", "commit2")]
        run_row = engine.store.get_run(result.run_id)
        assert (run_row.state, run_row.last_verdict) == ("merged", "approve")

    # -- step 1/3: the inline finding is answered in its own thread ----------

    def test_the_inline_finding_is_replied_to_and_its_thread_resolved(
        self, run: tuple[Harness, FakeGithub, LoopEngine, RunResult]
    ) -> None:
        _, fake, _, result = run
        (thread,) = fake.threads
        assert thread.anchor == "hello.txt:1"
        # Root comment (round 1) → the loop's answer → round 2's confirmation.
        bodies = [c.body for c in thread.comments]
        assert len(bodies) == 3
        assert bodies[0].startswith("[major] hello.txt must greet with hello")
        assert bodies[1].startswith("**addressed in commit2**: say hello, not hi")
        assert f"{RUN_ID_MARKER} run={result.run_id} round=1" in bodies[1]
        assert thread.is_resolved

        # Every reply went to that thread's root comment, none anywhere else.
        assert {comment_id for comment_id, _ in fake.replies} == {thread.root_comment_id}

    # -- step 3: the body-only finding is answered in a PR comment -----------

    def test_the_body_only_finding_is_reconciled_in_a_pr_comment(
        self, run: tuple[Harness, FakeGithub, LoopEngine, RunResult]
    ) -> None:
        _, fake, _, result = run
        (comment,) = fake.issue_comments
        assert comment.startswith("## Reconciliation — round 1")
        assert "commit2" in comment
        assert "docs/greeting.md" in comment
        assert "**refuted**" in comment
        assert "a one-line fixture needs no documentation" in comment
        assert f"{RUN_ID_MARKER} run={result.run_id} round=1" in comment
        # The finding that *did* get a thread is answered there, not here.
        assert "say hello, not hi" not in comment

    # -- step 4: round 2 confirms in-thread, not in a new review body --------

    def test_round_two_confirms_the_carried_finding_in_its_thread(
        self, run: tuple[Harness, FakeGithub, LoopEngine, RunResult]
    ) -> None:
        _, fake, _, result = run
        (thread,) = fake.threads
        confirmation = thread.comments[-1].body
        assert confirmation.startswith("**confirmed fixed** (review round 2)")
        assert "greets with hello now" in confirmation
        assert f"sbxloop:confirmed run={result.run_id} round=2" in confirmation

        # ... and round 2's own body restates neither finding, and posts no
        # inline comment of its own.
        _, body, comments = fake.reviews[1]
        assert comments == []
        assert "must greet with hello" not in body
        assert "not documented anywhere" not in body
        assert body.startswith(
            "**Review verdict: approve** (round 2)\n\nthe carried-over findings are settled"
        )

    # -- step 3: the event, with its counts ----------------------------------

    def test_review_reconciled_events_carry_the_counts(
        self, run: tuple[Harness, FakeGithub, LoopEngine, RunResult]
    ) -> None:
        harness, _, _, _ = run
        first, second = self._reconciled(harness)
        assert (first["round"], first["addressed"], first["refuted"]) == (1, 1, 1)
        assert first["unanswered"] == 0
        assert (first["replied"], first["resolved"], first["body_only"]) == (1, 1, 1)
        # Round 2's confirmation of a carried finding is reconciliation too.
        assert (second["round"], second["confirmations"]) == (2, 1)
        assert (second["replied"], second["resolved"]) == (1, 1)

    # -- step 6: what a resumed run would read back --------------------------

    def test_thread_identity_and_statuses_survive_a_reopen(
        self, run: tuple[Harness, FakeGithub, LoopEngine, RunResult]
    ) -> None:
        harness, fake, _, result = run
        reopened = StateStore(harness.state_dir / "state.db")
        try:
            posted = reopened.posted_findings(result.run_id)
            statuses = reopened.reconciliations(result.run_id, 1)
            rows = [
                r
                for r in reopened.phase_attempts(result.run_id)
                if r["phase"] == "build" and r["task_id"] == "fix-1"
            ]
        finally:
            reopened.close()

        assert [(p.round, p.anchor, p.body_only) for p in posted] == [
            (1, "hello.txt:1", False),
            (1, "docs/greeting.md:0", True),
        ]
        assert posted[0].comment_id == fake.threads[0].root_comment_id
        assert posted[0].thread_node_id == fake.threads[0].node_id
        assert statuses["hello.txt:1"] == "addressed"
        assert json.loads(rows[-1]["output_json"])["reconciled"] == [
            {
                "anchor": "hello.txt:1",
                "status": "addressed",
                "note": "say hello, not hi",
                "test": "",
            },
            {
                "anchor": "docs/greeting.md:0",
                "status": "refuted",
                "note": "a one-line fixture needs no documentation",
                "test": "",
            },
        ]

    # -- step 5: the merge gate saw nothing left open ------------------------

    def test_the_merge_gate_found_no_unreconciled_thread(
        self, run: tuple[Harness, FakeGithub, LoopEngine, RunResult]
    ) -> None:
        _, fake, _, _ = run
        loop_open, human_open = unreconciled_threads(fake.threads, login=fake.user_login)
        assert (loop_open, human_open) == ([], [])
        assert all(t.is_resolved for t in fake.threads)

    def test_the_same_gate_would_have_blocked_an_unanswered_thread(
        self, run: tuple[Harness, FakeGithub, LoopEngine, RunResult]
    ) -> None:
        """The counterfactual, so the merge above is not vacuously green:
        strip the loop's answers off the thread and the gate names it."""
        _, fake, _, _ = run
        bare = [t._replace(comments=t.comments[:1], is_resolved=False) for t in fake.threads]
        loop_open, human_open = unreconciled_threads(bare, login=fake.user_login)
        assert (loop_open, human_open) == (["hello.txt:1"], [])

    # -- the whole story, top to bottom --------------------------------------

    def test_every_finding_of_round_one_has_a_readable_fate(
        self, run: tuple[Harness, FakeGithub, LoopEngine, RunResult]
    ) -> None:
        """A human scrolling the merged PR can name what happened to each of
        round 1's findings without opening the run's logs."""
        _, fake, _, _ = run
        transcript = "\n".join(
            [
                *(f"REVIEW {body}" for _, body, _ in fake.reviews),
                *(f"THREAD {c.body}" for t in fake.threads for c in t.comments),
                *(f"COMMENT {c}" for c in fake.issue_comments),
            ]
        )
        # The anchored finding: raised, addressed in a named commit, confirmed.
        assert "must greet with hello, not hi" in transcript
        assert "**addressed in commit2**" in transcript
        assert "**confirmed fixed**" in transcript
        # The body-only finding: raised in the body, refuted with a reason.
        assert "the greeting is not documented anywhere" in transcript
        assert "a one-line fixture needs no documentation" in transcript


# An approving review may still carry a `nit`: `prompts/review.md` invites
# them, and every finding with a line gets its own inline thread. No fix
# round follows an approval, so unless the approving round answers its own
# non-blocking findings the merge gate blocks on a thread nothing in the
# pipeline can ever reconcile.
NIT_FINDING = {
    "path": "hello.txt",
    "line": 1,
    "body": "a trailing newline would be tidier",
    "severity": "nit",
}
APPROVE_WITH_NIT = review_round("approve", "no problems worth blocking on", findings=[NIT_FINDING])


class TestApproveWithANonBlockingFinding:
    @pytest.fixture
    def run(self, harness: Harness) -> tuple[Harness, FakeGithub, RunResult]:
        fake = FakeGithub()
        harness.script([taskgraph(task("t1")), FILES_BUILD, APPROVE_WITH_NIT])
        engine = harness.pipeline(fake)
        result = engine.start("write hello.txt")
        return harness, fake, result

    def test_the_run_merges(self, run: tuple[Harness, FakeGithub, RunResult]) -> None:
        _, fake, result = run
        assert result.state == "merged", result.reason
        assert fake.merges and fake.merges[0][0] == 7

    def test_the_nit_thread_is_answered_and_resolved(
        self, run: tuple[Harness, FakeGithub, RunResult]
    ) -> None:
        _, fake, _ = run
        loop_open, human_open = unreconciled_threads(fake.threads, login=fake.user_login)
        assert (loop_open, human_open) == ([], [])
        assert all(t.is_resolved for t in fake.threads)
        bodies = [c.body for t in fake.threads for c in t.comments]
        assert any("**noted, not blocking**" in b for b in bodies)

    def test_it_reports_the_reconciliation(
        self, run: tuple[Harness, FakeGithub, RunResult]
    ) -> None:
        harness, _, _ = run
        events = [e.data for e in harness.events if e.type == HostEventTypes.REVIEW_RECONCILED]
        assert events and events[-1]["noted"] == 1
        assert events[-1]["replied"] == 1
        assert events[-1]["resolved"] == 1


# `ReviewVerdict._check` only constrains `request_changes`: an `approve`
# carrying a `major` is a valid verdict, and it opens a real inline thread
# too. Reconciliation must key on "no fix round follows", not on severity,
# or the merge deadlocks on a thread nothing can reach.
MAJOR_FINDING = {
    "path": "hello.txt",
    "line": 1,
    "body": "the greeting should end with a newline",
    "severity": "major",
    # #521: a major finding without a repro is sent back once.
    "repro": "tail -c1 hello.txt is not a newline; expected one",
}
APPROVE_WITH_MAJOR = review_round(
    "approve", "approving anyway; noting one thing", findings=[MAJOR_FINDING]
)


class TestApproveCarryingABlockingSeverityFinding:
    @pytest.fixture
    def run(self, harness: Harness) -> tuple[Harness, FakeGithub, RunResult]:
        fake = FakeGithub()
        harness.script([taskgraph(task("t1")), FILES_BUILD, APPROVE_WITH_MAJOR])
        engine = harness.pipeline(fake)
        result = engine.start("write hello.txt")
        return harness, fake, result

    def test_the_run_still_merges(self, run: tuple[Harness, FakeGithub, RunResult]) -> None:
        _, fake, result = run
        assert result.state == "merged", result.reason
        assert fake.merges and fake.merges[0][0] == 7

    def test_the_major_thread_is_answered_and_resolved(
        self, run: tuple[Harness, FakeGithub, RunResult]
    ) -> None:
        _, fake, _ = run
        loop_open, human_open = unreconciled_threads(fake.threads, login=fake.user_login)
        assert (loop_open, human_open) == ([], [])
        bodies = [c.body for t in fake.threads for c in t.comments]
        assert any("not held against the merge" in b and "`major`" in b for b in bodies)
