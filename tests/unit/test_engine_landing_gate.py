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

import pytest

from sbxloop.config import LandingConfig
from sbxloop.engine.landing import (
    ACK_CAP,
    Blocked,
    Gated,
    Landed,
    UpdateState,
    land,
    resolve_login,
    unreconciled_threads,
)
from sbxloop.engine.reconcile import acknowledge_human_threads
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import PaginationError, ReviewThread, ThreadComment
from tests.fakes.fake_github import FakeGithub, human_review

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
    bot: bool = False,
) -> ReviewThread:
    """``bot`` marks the root comment's author as a GitHub App."""
    return ReviewThread(
        node_id=node_id,
        is_resolved=resolved,
        path=path,
        line=line,
        comments=tuple(
            ThreadComment(index + 1, login, body, is_bot=bot and index == 0)
            for index, (login, body) in enumerate(comments)
        ),
    )


def run_land(fake: FakeGithub, **over: Any) -> Any:
    cfg = LandingConfig.model_validate(
        {"ci_poll_interval_s": 1.0, "ci_settle_s": 0, "ci_timeout_s": 600.0}
    )
    answered: Container[str] = frozenset()
    kwargs: dict[str, Any] = {
        "cfg": cfg,
        "branch": None,
        "node_id": str(fake.pr["node_id"]),
        "login": LOGIN,
        "update": UpdateState(),
        "on_update": lambda state: None,
        "tick": lambda waiting: None,
        "emit": lambda type, **data: None,
        "clock": _clock(),
        "answered": answered,
    }
    kwargs.update(over)
    return land(fake, REPO, fake.number, **kwargs)


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
        assert fake.merges == [], "no merge method is resolved for a PR that will not merge"


def gate_ack(fake: FakeGithub, run_id: str = "rgate1234") -> Any:
    """The real landing-time ack, bound the way the engine binds it."""
    return lambda threads: acknowledge_human_threads(
        fake, REPO, fake.number, run_id=run_id, login=LOGIN, threads=threads
    )


class TestManyThreads:
    """#614 acceptance: one unanswered human thread among 150 blocks the
    merge and is named."""

    def test_one_unanswered_thread_in_150_blocks_and_is_named(self) -> None:
        fake = FakeGithub()
        fake.threads = [
            thread(
                node_id=f"PRRT_{i}",
                path="a.py",
                line=i,
                resolved=True,
                comments=((LOGIN, "[minor] x"), (LOGIN, "addressed")),
            )
            for i in range(149)
        ]
        fake.threads.insert(
            120, thread(node_id="PRRT_H", path="deep.py", line=7, comments=((HUMAN, "why?"),))
        )
        outcome = run_land(fake)
        assert isinstance(outcome, Blocked)
        assert outcome.why == "1 human review threads have no reply: deep.py:7"
        assert fake.merges == []


class TestThreadReadRetry:
    """A transient listing failure self-heals; a persistent one blocks
    with the attempt count — merging blind stays forbidden (#520)."""

    def test_a_one_off_502_self_heals(self) -> None:
        fake = FakeGithub()
        fake.fail_once["pr_review_threads"] = GithubOpsError("boom", http_status=502)
        assert isinstance(run_land(fake), Landed)

    def test_a_persistent_failure_blocks_with_the_attempt_count(self) -> None:
        fake = FakeGithub()
        fake.fail_always["pr_review_threads"] = GithubOpsError("boom", http_status=502)
        outcome = run_land(fake)
        assert isinstance(outcome, Blocked)
        assert "could not be read" in outcome.why
        assert "3 attempts" in outcome.why
        assert fake.merges == []

    def test_an_unread_page_blocks_at_once_without_retrying(self) -> None:
        """A list longer than the walk follows does not get shorter on
        retry (#614): block with the thread's name, no waiting."""
        fake = FakeGithub()
        fake.fail_always["pr_review_threads"] = PaginationError(
            "review thread deep.py:42 (PRRT_9) has more comments than were read; "
            "it cannot be judged reconciled"
        )
        waits: list[str] = []
        outcome = run_land(fake, tick=waits.append)
        assert isinstance(outcome, Blocked)
        assert outcome.why.startswith(
            "its review threads were not all read: review thread deep.py:42"
        )
        assert waits == []
        assert fake.merges == []

    def test_the_gate_waits_one_tick_between_attempts(self) -> None:
        fake = FakeGithub()
        fake.fail_always["pr_review_threads"] = GithubOpsError("boom", http_status=502)
        waits: list[str] = []
        run_land(fake, tick=waits.append)
        assert waits == ["threads", "threads"]


class TestEmptyLoginGuard:
    """#569 x #536: an empty login must never classify — it read every
    loop thread as a human's and stranded reconciled App-auth runs."""

    def test_unreconciled_threads_refuses_an_empty_login(self) -> None:
        with pytest.raises(ValueError, match="loop's own login"):
            unreconciled_threads([thread()], login="")

    def test_threads_with_an_unknown_identity_block_with_the_real_reason(self) -> None:
        fake = FakeGithub()
        fake.threads = [thread()]
        outcome = run_land(fake, login="")
        assert isinstance(outcome, Blocked)
        assert "identity could not be resolved" in outcome.why
        assert fake.merges == []

    def test_no_threads_and_no_identity_still_merges(self) -> None:
        """Identity only matters when there is something to classify."""
        fake = FakeGithub()
        assert isinstance(run_land(fake, login=""), Landed)

    def test_standing_objections_with_an_unknown_identity_block_not_fix(self) -> None:
        """These "objections" may be the loop's own words — a fix round on
        them is budget burn, not autonomy."""
        fake = FakeGithub()
        fake.reviews_payload = [human_review("alice", "CHANGES_REQUESTED", "no")]
        outcome = run_land(fake, login="")
        assert isinstance(outcome, Blocked)
        assert "identity could not be resolved" in outcome.why


class TestHumanAck:
    """Landing answers human threads nothing else in the pipeline would
    ever speak to — the loop never waits on a human it never asked."""

    def test_a_human_thread_is_answered_then_the_pr_merges(self) -> None:
        fake = FakeGithub()
        fake.threads = [thread(comments=((HUMAN, "why this way?"),))]
        outcome = run_land(fake, ack=gate_ack(fake))
        assert isinstance(outcome, Landed)
        assert fake.replies and "does not hold up the merge" in fake.replies[0][1]
        assert fake.resolved == [], "a human's thread is never resolved"

    def test_the_ack_reaches_the_event_stream(self) -> None:
        fake = FakeGithub()
        fake.threads = [thread(comments=((HUMAN, "why?"),))]
        seen: list[tuple[str, dict[str, Any]]] = []
        run_land(fake, ack=gate_ack(fake), emit=lambda type, **data: seen.append((type, data)))
        assert ("land.human_ack", {"pr": 7, "acked": 1}) in seen

    def test_a_failed_ack_still_blocks_truthfully(self) -> None:
        fake = FakeGithub()
        fake.threads = [thread(path="x.py", line=1, comments=((HUMAN, "no"),))]
        fake.fail_always["pr_comment_reply"] = GithubOpsError("boom", http_status=502)
        outcome = run_land(fake, ack=gate_ack(fake))
        assert isinstance(outcome, Blocked)
        assert outcome.why == "1 human review threads have no reply: x.py:1"
        assert fake.merges == []

    def test_without_an_ack_the_gate_stays_read_only(self) -> None:
        fake = FakeGithub()
        fake.threads = [thread(path="x.py", line=1, comments=((HUMAN, "no"),))]
        outcome = run_land(fake)
        assert isinstance(outcome, Blocked)
        assert fake.replies == []

    def test_a_second_pass_posts_nothing_new(self) -> None:
        fake = FakeGithub()
        fake.threads = [thread(comments=((HUMAN, "why?"),))]
        assert isinstance(run_land(fake, ack=gate_ack(fake)), Landed)
        posted = len(fake.replies)
        fake.pr["merged"] = False  # run the landing pass again from the top
        assert isinstance(run_land(fake, ack=gate_ack(fake)), Landed)
        assert len(fake.replies) == posted


class TestBotThreads:
    """#613: a bot's inline threads reach the loop through the one fix
    round its review buys, never through the reconciliation gate."""

    BOT = "coderabbitai[bot]"

    def test_a_bot_opened_thread_is_not_a_humans(self) -> None:
        opened = thread(comments=((self.BOT, "nit"),), bot=True)
        assert unreconciled_threads([opened], login=LOGIN) == ([], [])

    def test_an_ignored_reviewers_thread_is_a_bots(self) -> None:
        opened = thread(comments=(("Review-Robot", "nit"),))
        assert unreconciled_threads([opened], login=LOGIN) == ([], ["a.py:12"])
        assert unreconciled_threads([opened], login=LOGIN, ignore=["review-robot"]) == ([], [])

    def test_a_human_reply_inside_a_bot_thread_does_not_make_it_human(self) -> None:
        opened = thread(comments=((self.BOT, "nit"), (HUMAN, "agreed")), bot=True)
        assert unreconciled_threads([opened], login=LOGIN) == ([], [])

    def test_a_bot_thread_is_neither_acked_nor_a_block(self) -> None:
        fake = FakeGithub()
        fake.threads = [thread(comments=((self.BOT, "nit"),), bot=True)]
        assert isinstance(run_land(fake, ack=gate_ack(fake)), Landed)
        assert fake.replies == []

    def test_an_ignored_reviewers_thread_is_neither_acked_nor_a_block(self) -> None:
        fake = FakeGithub()
        fake.threads = [thread(comments=(("review-robot", "nit"),))]
        cfg = LandingConfig.model_validate(
            {"ci_poll_interval_s": 1.0, "ci_settle_s": 0, "ignore_reviewers": ["review-robot"]}
        )
        assert isinstance(run_land(fake, cfg=cfg, ack=gate_ack(fake)), Landed)
        assert fake.replies == []


class TestAckCap:
    """Human acknowledgments are capped per landing pass (#613): a burst of
    hundreds of threads is answered a page at a time, and the remainder
    blocks truthfully rather than being papered over."""

    def _threads(self, count: int) -> list[ReviewThread]:
        # Distinct root comment ids: the fake files a reply by the root it
        # answers, as GitHub does.
        return [
            ReviewThread(
                node_id=f"PRRT_{i}",
                is_resolved=False,
                path="a.py",
                line=i,
                comments=(ThreadComment(1000 + i, HUMAN, f"why {i}?"),),
            )
            for i in range(1, count + 1)
        ]

    def test_the_cap_is_twenty_five(self) -> None:
        assert ACK_CAP == 25

    def test_at_the_cap_everything_is_acked_and_the_pr_merges(self) -> None:
        fake = FakeGithub()
        fake.threads = self._threads(ACK_CAP)
        seen: list[tuple[str, dict[str, Any]]] = []
        outcome = run_land(
            fake, ack=gate_ack(fake), emit=lambda type, **data: seen.append((type, data))
        )
        assert isinstance(outcome, Landed)
        assert len(fake.replies) == ACK_CAP
        assert [t for t, _ in seen if t == "land.human_ack_capped"] == []

    def test_past_the_cap_the_rest_block_and_the_reason_says_so(self) -> None:
        fake = FakeGithub()
        fake.threads = self._threads(ACK_CAP + 3)
        seen: list[tuple[str, dict[str, Any]]] = []
        outcome = run_land(
            fake, ack=gate_ack(fake), emit=lambda type, **data: seen.append((type, data))
        )
        assert isinstance(outcome, Blocked)
        assert outcome.why == (
            f"3 human review threads have no reply (acknowledgments are capped at {ACK_CAP} "
            f"per landing pass): a.py:{ACK_CAP + 1}, a.py:{ACK_CAP + 2}, a.py:{ACK_CAP + 3}"
        )
        assert len(fake.replies) == ACK_CAP
        assert (
            "land.human_ack_capped",
            {"pr": 7, "acked": ACK_CAP, "remaining": 3, "cap": ACK_CAP},
        ) in seen
        assert fake.merges == []

    def test_the_next_pass_answers_the_rest(self) -> None:
        fake = FakeGithub()
        fake.threads = self._threads(ACK_CAP + 3)
        assert isinstance(run_land(fake, ack=gate_ack(fake)), Blocked)
        assert isinstance(run_land(fake, ack=gate_ack(fake)), Landed)
        assert len(fake.replies) == ACK_CAP + 3


class TestOptInMergeGate:
    """`land(gate=True)` — the one permissible human gate, and only after
    every other bar."""

    def test_gate_on_parks_instead_of_merging(self) -> None:
        fake = FakeGithub()
        outcome = run_land(fake, gate=True)
        assert isinstance(outcome, Gated)
        assert outcome.head == "commit0"
        assert fake.merges == []

    def test_gate_off_merges_as_before(self) -> None:
        assert isinstance(run_land(FakeGithub()), Landed)

    def test_the_gate_runs_after_the_reconciliation_gate(self) -> None:
        """An unreconciled thread blocks; it never parks as approvable."""
        fake = FakeGithub()
        fake.threads = [thread(path="x.py", line=1)]
        outcome = run_land(fake, gate=True)
        assert isinstance(outcome, Blocked)

    def test_the_gate_still_updates_a_behind_branch_first(self) -> None:
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "behind"
        outcome = run_land(fake, gate=True)
        assert isinstance(outcome, Gated)
        assert fake.updates, "update-branch ran before the park"

    def test_a_standing_human_objection_still_outranks_the_gate(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review("alice", "CHANGES_REQUESTED", "no", id=41)]
        outcome = run_land(fake, gate=True)
        assert outcome.__class__.__name__ == "NeedsFix"


class TestResolveLogin:
    """The shared identity resolver (engine drive + daemon approve path)."""

    def test_a_bot_login_short_circuits_everything(self) -> None:
        fake = FakeGithub()
        assert resolve_login(fake, REPO, 7, bot_login="app[bot]") == "app[bot]"
        assert fake.raw_calls == [], "no doomed GET /user under App auth"

    def test_pat_mode_asks_get_user(self) -> None:
        fake = FakeGithub()
        assert resolve_login(fake, REPO, 7) == fake.user_login

    def test_a_403_falls_back_to_the_pr_author(self) -> None:
        fake = FakeGithub()
        fake.fail_user_lookup = GithubOpsError("HTTP 403", http_status=403)
        assert resolve_login(fake, REPO, 7) == fake.pr_author

    def test_every_source_dead_degrades_to_empty(self) -> None:
        fake = FakeGithub()
        fake.fail_user_lookup = GithubOpsError("HTTP 403", http_status=403)
        fake.pr["user"] = None
        assert resolve_login(fake, REPO, 7) == ""
        assert resolve_login(fake, REPO, None) == ""


class TestBotSuffixIdentity:
    """REST attributes the App as ``sbxloop[bot]``; GraphQL reports the
    same actor as bare ``sbxloop``. Field failure r9t8hnv33 / ry2t99za6 /
    ra2k5bv6z: with the resolved login carrying the suffix and the threads
    read via GraphQL, every loop thread classified as a human's, the loop
    ack-replied to its own findings, and reconciled PRs ended blocked on
    "human review threads have no reply"."""

    def test_logins_match_folds_the_suffix_and_case(self) -> None:
        from sbxloop.gh.ops import logins_match

        assert logins_match("sbxloop[bot]", "sbxloop")
        assert logins_match("sbxloop", "sbxloop[bot]")
        assert logins_match("SBXLoop[bot]", "sbxloop")
        assert not logins_match("sbxloop", "other")
        assert not logins_match("", "")
        assert not logins_match("sbxloop", "")

    def test_a_loop_thread_under_the_rest_login_is_loop_authored(self) -> None:
        replied = thread(
            comments=(("sbxloop", "[minor] leaks"), ("sbxloop", "**noted, not blocking**"))
        )
        assert unreconciled_threads([replied], login="sbxloop[bot]") == ([], [])

    def test_has_reply_from_crosses_the_suffix(self) -> None:
        answered = thread(comments=(("alice", "why?"), ("sbxloop", "**addressed**: done")))
        assert answered.has_reply_from("sbxloop[bot]")

    def test_the_pr_604_shape_merges_and_acks_nothing(self) -> None:
        """The exact field shape: the loop's own resolved finding threads
        (noted replies included), threads spelt bare by GraphQL, login
        spelt with the suffix — must merge, and the ack pass must not
        reply to the loop's own threads."""
        fake = FakeGithub()
        fake.threads = [
            thread(
                resolved=True,
                comments=(("sbxloop", "[minor] x"), ("sbxloop", "**noted, not blocking**")),
            ),
            thread(
                node_id="PRRT_2",
                path="b.py",
                line=4,
                resolved=True,
                comments=(("sbxloop", "[nit] y"), ("sbxloop", "**noted, not blocking**")),
            ),
        ]

        def ack(threads: Any) -> int:
            return acknowledge_human_threads(
                fake, REPO, fake.number, run_id="rfix12345", login="sbxloop[bot]", threads=threads
            )

        outcome = run_land(fake, login="sbxloop[bot]", ack=ack)
        assert isinstance(outcome, Landed)
        assert fake.replies == [], "no ack lands on the loop's own threads"
