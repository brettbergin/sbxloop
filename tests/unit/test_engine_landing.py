"""The CI wait and the merge as pure decision loops (engine.landing).

``poll_checks`` and ``land`` take a GithubOps and a ``tick`` callback and
return a decision; nothing here touches the engine, a sandbox or a store.
The semantics are the ones the daemon's landing stage used to own
(tests/unit/test_daemon_loop.py::TestLandingStage): the PR's own fate
first, then un-drafting, a human's objection, CI, mergeability, and only
then the merge — with 405 an answer (Blocked) and 409 a race (re-judge).
"""

from __future__ import annotations

from collections.abc import Container
from typing import Any

import pytest

from sbxloop.config import LandingConfig
from sbxloop.engine.landing import (
    Blocked,
    CiTimeout,
    Closed,
    Landed,
    NeedsFix,
    UpdateState,
    human_objection,
    human_objections,
    land,
    poll_checks,
)
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import ChecksVerdict, FailedCheck, MergeOutcome
from tests.fakes.fake_github import (
    BLOCKED_405,
    GREEN,
    MERGED,
    NO_CHECKS,
    PENDING,
    RED,
    STALE_409,
    FakeGithub,
    human_comment,
    human_review,
)

REPO = "o/r"
LOGIN = "sbxloop-bot"


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Recorder:
    """Records the waits the loop asked for and the events it emitted, and
    lets a test run a side effect on each tick (GitHub moving under us)."""

    def __init__(self, clock: FakeClock, *, per_tick: float = 0.0) -> None:
        self.clock = clock
        self.per_tick = per_tick
        self.waits: list[str] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.on_tick: list[Any] = []

    def tick(self, waiting: str) -> None:
        self.waits.append(waiting)
        self.clock.advance(self.per_tick)
        for fn in self.on_tick:
            fn(len(self.waits))

    def emit(self, type: str, **data: Any) -> None:
        self.events.append((type, data))

    def emit_checks(self, **data: Any) -> None:
        self.events.append(("ci.status", data))


def cfg(**over: Any) -> LandingConfig:
    base: dict[str, Any] = {"ci_poll_interval_s": 1.0, "ci_settle_s": 0, "ci_timeout_s": 600.0}
    return LandingConfig.model_validate(base | over)


class TestPollChecks:
    def _poll(self, fake: FakeGithub, rec: Recorder, **over: Any) -> ChecksVerdict:
        return poll_checks(
            fake,
            REPO,
            fake.head_sha,
            cfg=cfg(**over),
            tick=rec.tick,
            emit=rec.emit_checks,
            clock=rec.clock,
        )

    def test_green_with_check_runs_returns_at_once(self) -> None:
        fake = FakeGithub()
        fake.checks = [GREEN]
        rec = Recorder(FakeClock())
        assert self._poll(fake, rec) == GREEN
        assert rec.waits == []
        assert fake.checks_calls == ["commit0"]

    def test_red_returns_without_waiting_for_stragglers(self) -> None:
        fake = FakeGithub()
        fake.checks = [ChecksVerdict("red", 3, ("slow",), ("lint",))]
        rec = Recorder(FakeClock())
        assert self._poll(fake, rec).state == "red"
        assert rec.waits == []

    def test_pending_waits_then_settles_and_emits_on_change_only(self) -> None:
        fake = FakeGithub()
        fake.checks = [PENDING, PENDING, GREEN]
        rec = Recorder(FakeClock(), per_tick=1.0)
        assert self._poll(fake, rec) == GREEN
        assert rec.waits == ["ci", "ci"]
        # three polls, two distinct verdicts: one event each
        assert [e["state"] for _, e in rec.events] == ["pending", "green"]
        assert rec.events[0][1]["pending"] == ["ci"]
        assert rec.events[1][1]["waited_s"] == 2

    def test_no_check_runs_is_trusted_only_after_the_settle_window(self) -> None:
        """Actions registers its check runs seconds after a push; "nothing
        failed yet" must not read as success before then."""
        fake = FakeGithub()
        fake.checks = [NO_CHECKS]
        clock = FakeClock()
        rec = Recorder(clock, per_tick=30.0)
        delivered_at = clock.t
        verdict = poll_checks(
            fake,
            REPO,
            "commit0",
            cfg=cfg(ci_settle_s=90.0),
            tick=rec.tick,
            emit=rec.emit_checks,
            clock=clock,
            settle_from=delivered_at,
        )
        assert verdict == NO_CHECKS
        assert rec.waits == ["ci", "ci", "ci"]  # 0, 30, 60 -> 90 settles
        assert len(fake.checks_calls) == 4

    def test_settle_defaults_to_the_poll_start(self) -> None:
        fake = FakeGithub()
        fake.checks = [NO_CHECKS]
        clock = FakeClock()
        rec = Recorder(clock, per_tick=100.0)
        self._poll(fake, rec, ci_settle_s=90.0)
        assert rec.waits == ["ci"]

    def test_timeout_raises(self) -> None:
        fake = FakeGithub()
        fake.checks = [PENDING]
        rec = Recorder(FakeClock(), per_tick=100.0)
        with pytest.raises(CiTimeout, match="ci_timeout_s=250s"):
            self._poll(fake, rec, ci_timeout_s=250.0)
        assert rec.waits == ["ci", "ci", "ci"]


class TestHumanObjection:
    def test_another_reviewers_standing_request_counts(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review("alice", "CHANGES_REQUESTED", "no")]
        assert human_objection(fake, REPO, 7, login=LOGIN) is True

    def test_the_loops_own_review_is_excluded_not_trusted(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review(LOGIN, "CHANGES_REQUESTED", "our own verdict")]
        assert human_objection(fake, REPO, 7, login=LOGIN) is False

    def test_a_later_approval_clears_the_objection(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [
            human_review("alice", "CHANGES_REQUESTED", "no"),
            human_review("alice", "APPROVED", "ok now"),
        ]
        assert human_objection(fake, REPO, 7, login=LOGIN) is False

    def test_malformed_payload_is_no_objection(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = ["not a review", {"state": "CHANGES_REQUESTED"}]  # type: ignore[list-item]
        # a nameless reviewer is "" which is not our login: it counts
        assert human_objection(fake, REPO, 7, login=LOGIN) is True
        fake.raw = lambda method, path, body=None: {"message": "boom"}  # type: ignore[method-assign]
        assert human_objection(fake, REPO, 7, login=LOGIN) is False


class TestHumanObjections:
    """Each thing a human asked for, as something the loop can answer."""

    def test_the_review_body_and_each_inline_comment_are_objections(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review("alice", "CHANGES_REQUESTED", "rename foo", id=41)]
        fake.comments_payload = [
            human_comment("alice", "and this", path="a.py", line=3, id=91),
            human_comment("bob", "nit from a non-objector", path="b.py", line=1, id=92),
        ]
        found = human_objections(fake, REPO, 7, login=LOGIN)
        assert [(o.key, o.anchor, o.comment_id) for o in found] == [
            ("human:review:41", "", None),
            ("human:comment:91", "a.py:3", 91),
        ]
        assert found[0].body == "rename foo"

    def test_nothing_stands_when_the_objection_was_cleared(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [
            human_review("alice", "CHANGES_REQUESTED", "no", id=1),
            human_review("alice", "APPROVED", "ok now", id=2),
        ]
        fake.comments_payload = [human_comment("alice", "old", path="a.py", line=3, id=91)]
        assert human_objections(fake, REPO, 7, login=LOGIN) == []

    def test_the_loops_own_review_is_not_an_objection(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review(LOGIN, "CHANGES_REQUESTED", "ours", id=1)]
        assert human_objections(fake, REPO, 7, login=LOGIN) == []


class Landing:
    """One ``land`` call with its bookkeeping in reach."""

    def __init__(self, fake: FakeGithub, *, per_tick: float = 1.0, **over: Any) -> None:
        self.fake = fake
        self.clock = FakeClock()
        self.rec = Recorder(self.clock, per_tick=per_tick)
        self.cfg = cfg(**over)
        self.update = UpdateState()
        self.persisted: list[tuple[int, str | None]] = []

    def run(
        self,
        *,
        branch: str | None = "sbxloop/r1",
        login: str = LOGIN,
        answered: Container[str] = frozenset(),
        review_posted: bool = True,
    ) -> Any:
        return land(
            self.fake,
            REPO,
            self.fake.number,
            cfg=self.cfg,
            branch=branch,
            node_id=str(self.fake.pr["node_id"]),
            login=login,
            update=self.update,
            on_update=lambda state: self.persisted.append((state.attempts, state.head)),
            tick=self.rec.tick,
            emit=self.rec.emit,
            clock=self.clock,
            answered=answered,
            review_posted=review_posted,
        )


class TestLand:
    def test_a_clean_pr_is_merged_at_the_judged_head(self) -> None:
        lp = Landing(FakeGithub())
        assert lp.run() == Landed("merge0001", by_human=False)
        assert lp.fake.merges == [(7, "squash", "commit0")], "the judged head rides on the merge"
        assert lp.fake.deleted_branches == ["sbxloop/r1"]
        assert lp.rec.waits == []

    def test_the_merge_method_is_the_operators(self) -> None:
        lp = Landing(FakeGithub(), merge_method="rebase")
        lp.run()
        assert lp.fake.merges == [(7, "rebase", "commit0")]

    def test_the_branch_is_kept_when_the_operator_says_so(self) -> None:
        lp = Landing(FakeGithub(), delete_branch_on_merge=False)
        assert isinstance(lp.run(), Landed)
        assert lp.fake.deleted_branches == []

    def test_no_branch_name_means_nothing_to_delete(self) -> None:
        lp = Landing(FakeGithub())
        assert isinstance(lp.run(branch=None), Landed)
        assert lp.fake.deleted_branches == []

    def test_a_failed_branch_delete_does_not_unmerge(self) -> None:
        fake = FakeGithub()
        fake.fail_once["branch_delete"] = GithubOpsError("protected", http_status=403)
        assert isinstance(Landing(fake).run(), Landed)

    def test_a_draft_is_undrafted_first_and_merged_on_the_next_read(self) -> None:
        """GitHub reports a draft's mergeable_state as `draft`, so the real
        merge state only becomes readable after a further poll."""
        fake = FakeGithub(draft=True)
        fake.pr["mergeable_state"] = "draft"
        lp = Landing(fake)
        lp.rec.on_tick.append(lambda n: fake.pr.__setitem__("mergeable_state", "clean"))
        assert isinstance(lp.run(), Landed)
        assert fake.ready_calls == ["PR_node7"]
        assert lp.rec.waits == ["undraft"]
        assert ("land.undraft", {"pr": 7}) in lp.rec.events
        assert fake.merges == [(7, "squash", "commit0")]

    def test_a_draft_that_will_not_clear_is_handed_over(self) -> None:
        fake = FakeGithub(draft=True)
        fake.undraft_ok = False
        outcome = Landing(fake).run()
        assert outcome == Blocked("its draft status could not be cleared")
        assert fake.merges == []

    def test_an_undraft_that_raises_is_handed_over_too(self) -> None:
        fake = FakeGithub(draft=True)
        fake.fail_once["pr_ready_for_review"] = GithubOpsError("graphql said no")
        assert isinstance(Landing(fake).run(), Blocked)

    def test_a_pr_a_human_already_merged_lands_without_a_merge_call(self) -> None:
        fake = FakeGithub()
        fake.pr["merged"] = True
        fake.pr["merge_commit_sha"] = "human123"
        assert Landing(fake).run() == Landed("human123", by_human=True)
        assert fake.merges == []

    def test_a_closed_pr_is_closed(self) -> None:
        fake = FakeGithub()
        fake.pr["state"] = "closed"
        outcome = Landing(fake).run()
        assert isinstance(outcome, Closed)
        assert "closed without being merged" in outcome.why

    def test_a_human_objection_goes_back_for_a_fix_round(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review("alice", "CHANGES_REQUESTED", "rename foo")]
        fake.feedback = "rename foo\n\n- `a.py:3`: and this"
        outcome = Landing(fake).run()
        assert isinstance(outcome, NeedsFix)
        assert outcome.kind == "human"
        assert outcome.why == "a reviewer requested changes on the pull request"
        assert outcome.objections == fake.feedback
        assert [o.login for o in outcome.human] == ["alice"]
        assert fake.merges == []

    def test_an_already_answered_objection_does_not_buy_another_fix_round(self) -> None:
        """Only its author can dismiss a CHANGES_REQUESTED, so the same
        review stands on the next landing pass. Once every objection in it
        has a loop reply, the run hands over rather than re-fixing (#520)."""
        fake = FakeGithub()
        fake.reviews_payload = [human_review("alice", "CHANGES_REQUESTED", "rename foo", id=41)]
        fake.comments_payload = [
            human_comment("alice", "and this", path="a.py", line=3, id=91),
        ]
        landing = Landing(fake)
        first = landing.run()
        assert isinstance(first, NeedsFix)
        answered = {o.key for o in first.human}
        assert answered == {"human:review:41", "human:comment:91"}

        outcome = Landing(fake).run(answered=answered)
        assert isinstance(outcome, Blocked)
        assert "still standing" in outcome.why
        assert fake.merges == []

    def test_a_partly_answered_objection_still_fixes_the_rest(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review("alice", "CHANGES_REQUESTED", "rename foo", id=41)]
        fake.comments_payload = [human_comment("alice", "and this", path="a.py", line=3, id=91)]
        outcome = Landing(fake).run(answered={"human:review:41"})
        assert isinstance(outcome, NeedsFix)
        assert [o.key for o in outcome.human] == ["human:comment:91"]

    def test_the_loops_own_request_changes_does_not_block_itself(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review(LOGIN, "CHANGES_REQUESTED", "round 1")]
        assert isinstance(Landing(fake).run(), Landed)

    def test_pending_checks_wait_then_merge(self) -> None:
        fake = FakeGithub()
        fake.checks = [PENDING, GREEN]
        lp = Landing(fake)
        assert isinstance(lp.run(), Landed)
        assert lp.rec.waits == ["ci"]

    def test_red_checks_go_back_for_a_fix_round_with_the_logs(self) -> None:
        fake = FakeGithub()
        fake.checks = [ChecksVerdict("red", 2, (), ("lint",))]
        fake.failed_logs = [FailedCheck("lint", "failure", "E501 line too long", "https://x")]
        outcome = Landing(fake).run()
        assert isinstance(outcome, NeedsFix)
        assert outcome.kind == "ci"
        assert outcome.why == "1 of 2 check(s) failed: lint"
        assert outcome.failed_checks == tuple(fake.failed_logs)

    def test_unknown_mergeability_waits_rather_than_merging(self) -> None:
        """GitHub computes mergeability asynchronously and says None while
        it is still thinking. That is not permission to merge."""
        fake = FakeGithub()
        fake.pr["mergeable"] = None
        lp = Landing(fake)
        lp.rec.on_tick.append(lambda n: fake.pr.__setitem__("mergeable", True))
        assert isinstance(lp.run(), Landed)
        assert lp.rec.waits == ["mergeability"]

    def test_a_conflicted_pr_goes_back_for_a_fix_round(self) -> None:
        fake = FakeGithub()
        fake.pr["mergeable"] = False
        fake.pr["mergeable_state"] = "dirty"
        outcome = Landing(fake).run()
        assert outcome == NeedsFix("conflict", "the pull request conflicts with its base branch")
        assert fake.merges == []

    def test_a_behind_pr_updates_its_branch_then_merges_at_the_new_head(self) -> None:
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "behind"
        lp = Landing(fake)
        assert isinstance(lp.run(), Landed)
        assert fake.updates == [(7, "commit0")], "the expected head guards the update"
        assert lp.persisted == [(1, "commit0")]
        assert lp.rec.waits == ["update"]
        assert ("land.update", {"pr": 7, "attempt": 1, "accepted": True}) in lp.rec.events
        assert fake.merges == [(7, "squash", "updated1")], "merged at the new head"

    def test_only_one_update_is_in_flight_at_a_time(self) -> None:
        """GitHub answers an update with 202 and no sha; until the head
        moves, asking again would spend the budget twice."""
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "behind"
        lp = Landing(fake, merge_update_attempts=3)
        # The update is accepted but its commit takes two polls to appear.
        original = fake.pr_update_branch

        def slow_update(repo: str, number: int, *, expected_head_sha: str = "") -> bool:
            fake.updates.append((number, expected_head_sha))
            return True

        fake.pr_update_branch = slow_update  # type: ignore[method-assign]

        def land_it(n: int) -> None:
            if n == 2:
                fake.pr_update_branch = original  # type: ignore[method-assign]
                fake._move_head("updated1")
                fake.pr["mergeable_state"] = "clean"

        lp.rec.on_tick.append(land_it)
        assert isinstance(lp.run(), Landed)
        assert fake.updates == [(7, "commit0")]
        assert lp.rec.waits == ["update", "update"]

    def test_a_resumed_update_marker_is_honoured(self) -> None:
        """The engine hands the persisted marker back in: an update requested
        at this head before the crash is not requested again."""
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "behind"
        lp = Landing(fake)
        lp.update = UpdateState(attempts=1, head="commit0")
        lp.rec.on_tick.append(lambda n: fake.pr.__setitem__("mergeable_state", "clean"))
        assert isinstance(lp.run(), Landed)
        assert fake.updates == []
        assert lp.persisted == []

    def test_a_landed_update_stops_being_recorded_as_in_flight(self) -> None:
        """Once the head moves past the sha we asked at, a still-behind PR
        gets a fresh update at the new head."""
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "behind"
        lp = Landing(fake, merge_update_attempts=3)
        # the fake's update lands at once but the base has moved on again
        lp.rec.on_tick.append(
            lambda n: fake.pr.__setitem__("mergeable_state", "behind" if n == 1 else "clean")
        )
        assert isinstance(lp.run(), Landed)
        assert fake.updates == [(7, "commit0"), (7, "updated1")]
        assert lp.persisted == [(1, "commit0"), (2, "updated1")]

    def test_update_attempts_are_bounded_then_handed_over(self) -> None:
        """A base moving faster than CI finishes would update for ever."""
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "behind"
        lp = Landing(fake, merge_update_attempts=1)
        lp.rec.on_tick.append(lambda n: fake.pr.__setitem__("mergeable_state", "behind"))
        outcome = lp.run()
        assert outcome == Blocked(
            "still behind its base after 1 update(s) (merge_update_attempts=1)"
        )
        assert len(fake.updates) == 1
        assert fake.merges == []

    def test_branch_updating_can_be_disabled_outright(self) -> None:
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "behind"
        outcome = Landing(fake, merge_update_attempts=0).run()
        assert isinstance(outcome, Blocked)
        assert "merge_update_attempts=0" in outcome.why
        assert fake.updates == []

    def test_a_refused_update_is_charged_but_not_marked(self) -> None:
        """422 means the PR moved under us; the next poll re-reads it. The
        attempt is spent (it bounds the loop) but no head marker is set, so
        the next poll may ask again."""
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "behind"
        fake.update_ok = False
        lp = Landing(fake, merge_update_attempts=2)
        outcome = lp.run()
        assert isinstance(outcome, Blocked)
        assert lp.persisted == [(1, None), (2, None)]
        assert fake.updates == [(7, "commit0"), (7, "commit0")]
        assert ("land.update", {"pr": 7, "attempt": 1, "accepted": False}) in lp.rec.events

    def test_a_blocked_merge_hands_over_with_the_pr_left_open(self) -> None:
        """A protection rule wanting an approval this identity cannot give is
        not fixable by another round: stop, leave a PR a human can finish."""
        fake = FakeGithub()
        fake.merge_outcomes = [BLOCKED_405]
        outcome = Landing(fake).run()
        assert outcome == Blocked(BLOCKED_405.reason)
        assert fake.deleted_branches == [], "a PR that did not merge keeps its branch"

    def test_a_stale_merge_re_judges_the_new_head(self) -> None:
        fake = FakeGithub()
        fake.merge_outcomes = [STALE_409, MERGED]
        lp = Landing(fake)
        lp.rec.on_tick.append(lambda n: fake._move_head("commit1"))
        assert isinstance(lp.run(), Landed)
        assert lp.rec.waits == ["merge"]
        assert fake.merges == [(7, "squash", "commit0"), (7, "squash", "commit1")]

    def test_a_merge_that_raises_propagates(self) -> None:
        """An infrastructure error is the engine's to handle (the run stays
        resumable at landing); the loop does not invent a verdict."""
        fake = FakeGithub()
        fake.fail_once["pr_merge"] = GithubOpsError("github down", http_status=502)
        with pytest.raises(GithubOpsError, match="github down"):
            Landing(fake).run()

    def test_a_merge_that_does_not_confirm_is_blocked(self) -> None:
        fake = FakeGithub()
        fake.merge_outcomes = [MergeOutcome(False, "", "merge was not confirmed: {}", blocked=True)]
        assert isinstance(Landing(fake).run(), Blocked)

    def test_a_landing_that_never_settles_is_blocked(self) -> None:
        fake = FakeGithub()
        fake.checks = [PENDING]
        lp = Landing(fake, per_tick=100.0, ci_timeout_s=250.0)
        outcome = lp.run()
        assert outcome == Blocked("landing did not settle within ci_timeout_s=250s")
        assert lp.rec.waits == ["ci", "ci", "ci"]

    def test_a_payload_without_a_head_is_an_error(self) -> None:
        fake = FakeGithub()
        fake.pr["head"] = {}
        with pytest.raises(GithubOpsError, match="no head sha"):
            Landing(fake).run()

    def test_gate_order_pr_fate_before_undraft_before_objection_before_ci(self) -> None:
        """Merged by a human outranks a draft flag; a draft outranks an
        objection; an objection outranks red CI."""
        fake = FakeGithub(draft=True)
        fake.pr["merged"] = True
        assert isinstance(Landing(fake).run(), Landed)
        assert fake.ready_calls == []

        fake = FakeGithub(draft=True)
        fake.undraft_ok = False
        fake.reviews_payload = [human_review("alice", "CHANGES_REQUESTED")]
        assert isinstance(Landing(fake).run(), Blocked)

        fake = FakeGithub()
        fake.reviews_payload = [human_review("alice", "CHANGES_REQUESTED")]
        fake.checks = [RED]
        outcome = Landing(fake).run()
        assert isinstance(outcome, NeedsFix) and outcome.kind == "human"
        assert fake.checks_calls == []
