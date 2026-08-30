"""The merge gate: a pull request does not land unreconciled (#520 step 5).

``land`` used to merge whatever GitHub would take — including a PR whose
review findings were all still open on their threads, and (the #503 failure
mode) one carrying no review record at all because the post 422'd and the
engine logged it as a courtesy. These tests pin the two new preconditions:
every loop thread resolved-or-replied, every human thread replied, and the
approving review actually posted.
"""

from __future__ import annotations

from collections.abc import Container
from typing import Any

from sbxloop.config import LandingConfig
from sbxloop.engine.landing import Blocked, Landed, UpdateState, land, unreconciled_threads
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import ReviewThread, ThreadComment
from tests.fakes.fake_github import FakeGithub

REPO = "o/r"
LOGIN = "sbxloop-bot"
HUMAN = "brettbergin"


def thread(
    *,
    node_id: str = "PRRT_1",
    path: str = "a.py",
    line: int | None = 12,
    resolved: bool = False,
    comments: tuple[tuple[str, str], ...] = ((LOGIN, "[major] this leaks"),),
) -> ReviewThread:
    return ReviewThread(
        node_id=node_id,
        is_resolved=resolved,
        path=path,
        line=line,
        comments=tuple(
            ThreadComment(index + 1, login, body) for index, (login, body) in enumerate(comments)
        ),
    )


def run_land(fake: FakeGithub, **over: Any) -> Any:
    cfg = LandingConfig.model_validate(
        {"ci_poll_interval_s": 1.0, "ci_settle_s": 0, "ci_timeout_s": 600.0}
    )
    answered: Container[str] = frozenset()
    return land(
        fake,
        REPO,
        fake.number,
        cfg=cfg,
        branch=None,
        node_id=str(fake.pr["node_id"]),
        login=LOGIN,
        update=UpdateState(),
        on_update=lambda state: None,
        tick=lambda waiting: None,
        emit=lambda type, **data: None,
        clock=_clock(),
        answered=answered,
        **over,
    )


def _clock() -> Any:
    box = {"t": 1000.0}

    def now() -> float:
        box["t"] += 1.0
        return box["t"]

    return now


class TestUnreconciledThreads:
    def test_a_resolved_loop_thread_is_reconciled(self) -> None:
        assert unreconciled_threads([thread(resolved=True)], login=LOGIN) == ([], [])

    def test_an_open_loop_thread_with_a_loop_reply_is_refuted_not_pending(self) -> None:
        """Refuted findings deliberately stay open — a reply is the whole
        reconciliation, and demanding resolution would force the loop to
        close threads it disagrees with."""
        replied = thread(comments=((LOGIN, "[major] this leaks"), (LOGIN, "**refuted**: no")))
        assert unreconciled_threads([replied], login=LOGIN) == ([], [])

    def test_an_open_unanswered_loop_thread_is_reported_by_anchor(self) -> None:
        loop, human = unreconciled_threads([thread(path="b.py", line=3)], login=LOGIN)
        assert (loop, human) == (["b.py:3"], [])

    def test_a_human_thread_needs_a_reply_but_not_resolution(self) -> None:
        raw = thread(comments=((HUMAN, "please rename this"),))
        assert unreconciled_threads([raw], login=LOGIN) == ([], ["a.py:12"])
        answered = thread(comments=((HUMAN, "please rename this"), (LOGIN, "**addressed**: ok")))
        assert unreconciled_threads([answered], login=LOGIN) == ([], [])

    def test_a_resolved_human_thread_without_a_loop_reply_still_needs_one(self) -> None:
        raw = thread(resolved=True, comments=((HUMAN, "please rename"),))
        assert unreconciled_threads([raw], login=LOGIN) == ([], ["a.py:12"])

    def test_an_empty_thread_is_ignored(self) -> None:
        assert unreconciled_threads([thread(comments=())], login=LOGIN) == ([], [])

    def test_a_thread_with_no_line_is_named_by_path(self) -> None:
        loop, _ = unreconciled_threads([thread(path="c.py", line=None)], login=LOGIN)
        assert loop == ["c.py"]


class TestMergeGate:
    def test_a_fully_reconciled_pr_merges(self) -> None:
        fake = FakeGithub()
        fake.threads = [
            thread(node_id="PRRT_1", resolved=True),
            thread(
                node_id="PRRT_2",
                path="b.py",
                line=4,
                comments=((LOGIN, "[minor] naming"), (LOGIN, "**refuted**: intentional")),
            ),
            thread(
                node_id="PRRT_3",
                path="c.py",
                line=9,
                comments=((HUMAN, "rename"), (LOGIN, "**addressed** in abc123def456")),
            ),
        ]
        assert isinstance(run_land(fake), Landed)
        assert fake.merges == [(7, "squash", "commit0")]

    def test_a_pr_with_no_threads_merges(self) -> None:
        fake = FakeGithub()
        assert isinstance(run_land(fake), Landed)

    def test_an_unreconciled_loop_thread_blocks_and_is_named(self) -> None:
        fake = FakeGithub()
        fake.threads = [thread(path="x.py", line=1), thread(node_id="PRRT_2", path="y.py", line=2)]
        outcome = run_land(fake)
        assert isinstance(outcome, Blocked)
        assert outcome.why == "2 review threads unreconciled: x.py:1, y.py:2"
        assert fake.merges == [], "nothing merges while a finding is unanswered"

    def test_a_human_thread_without_a_reply_blocks(self) -> None:
        fake = FakeGithub()
        fake.threads = [thread(path="x.py", line=1, comments=((HUMAN, "no"),))]
        outcome = run_land(fake)
        assert isinstance(outcome, Blocked)
        assert outcome.why == "1 human review threads have no reply: x.py:1"
        assert fake.merges == []

    def test_a_failed_approving_review_post_blocks_the_merge(self) -> None:
        """Regression for #503: the review 422'd, the engine warned, and the
        loop merged 90 seconds later with no review on the PR at all."""
        fake = FakeGithub()
        outcome = run_land(fake, review_posted=False)
        assert outcome == Blocked("review record could not be posted")
        assert fake.merges == []

    def test_unreadable_threads_block_rather_than_merge_blind(self) -> None:
        fake = FakeGithub()
        fake.fail_once["pr_review_threads"] = GithubOpsError("boom", http_status=502)
        outcome = run_land(fake)
        assert isinstance(outcome, Blocked)
        assert "could not be read" in outcome.why
        assert fake.merges == []

    def test_the_gate_runs_after_ci_and_mergeability(self) -> None:
        """An unreconciled PR that is also unmergeable is a conflict first:
        the fix round that resolves the conflict is what will reconcile."""
        fake = FakeGithub()
        fake.pr["mergeable"] = False
        fake.threads = [thread()]
        outcome = run_land(fake)
        assert outcome.__class__.__name__ == "NeedsFix"

    def test_the_block_reason_reaches_the_event_stream(self) -> None:
        fake = FakeGithub()
        fake.threads = [thread(path="x.py", line=1)]
        seen: list[tuple[str, dict[str, Any]]] = []
        cfg = LandingConfig.model_validate(
            {"ci_poll_interval_s": 1.0, "ci_settle_s": 0, "ci_timeout_s": 600.0}
        )
        outcome = land(
            fake,
            REPO,
            fake.number,
            cfg=cfg,
            branch=None,
            node_id=str(fake.pr["node_id"]),
            login=LOGIN,
            update=UpdateState(),
            on_update=lambda state: None,
            tick=lambda waiting: None,
            emit=lambda type, **data: seen.append((type, data)),
            clock=_clock(),
        )
        assert isinstance(outcome, Blocked)
        assert seen == [("land.unreconciled", {"pr": 7, "why": outcome.why})]
