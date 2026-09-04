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
from sbxloop.engine.checks import PolicyFor, check_policy_reader, no_policy
from sbxloop.engine.landing import (
    AwaitingReview,
    Blocked,
    CiTimeout,
    Closed,
    Landed,
    NeedsFix,
    UpdateState,
    allowed_merge_methods,
    blocked_reason,
    human_objection,
    human_objections,
    land,
    poll_checks,
    resolve_merge_method,
)
from sbxloop.errors import GithubOpsError
from sbxloop.gh.ops import ChecksVerdict, FailedCheck, MergeOutcome, QueueEntry, QueueState
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
        ).verdict

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
        ).verdict
        assert verdict == NO_CHECKS
        assert rec.waits == ["ci", "ci", "ci"]  # 0, 30, 60 -> 90 settles
        assert len(fake.checks_calls) == 4

    def test_a_policy_reader_is_asked_on_every_poll(self) -> None:
        """#674: a base whose rules cannot be read learns its required
        checks from the PR's rollup, which grows as checks report — so a
        required check that turns up mid-poll is waited on."""
        fake = FakeGithub()
        fake.protection_forbidden = True
        fake.rollup_required = ("ci", "lint")
        # `docs` pends throughout and is never waited on; `lint` is absent,
        # then pending, then green — and gates the whole way.
        fake.checks = [
            ChecksVerdict("pending", 2, ("docs",), (), ("ci",)),
            ChecksVerdict("pending", 3, ("docs", "lint"), (), ("ci",)),
            ChecksVerdict("pending", 3, ("docs",), (), ("ci", "lint")),
        ]
        rec = Recorder(FakeClock(), per_tick=1.0)
        policy_for = check_policy_reader(fake, REPO, "main", cfg=cfg(), number=7)
        judged = poll_checks(
            fake,
            REPO,
            fake.head_sha,
            cfg=cfg(),
            tick=rec.tick,
            emit=rec.emit_checks,
            clock=rec.clock,
            policy_for=policy_for,
        )
        assert judged.state == "green" and judged.gating == ("ci", "lint")
        assert judged.source == "pr-rollup"
        assert rec.waits == ["ci", "ci"], "lint absent, then pending, then green"
        assert fake.rollup_calls == 3, "the rollup is re-read on every poll"

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


class PagedFake(FakeGithub):
    """FakeGithub whose review and review-comment lists are served in real
    100-entry pages, so the landing readers' page walk is exercised."""

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        base, _, query = path.partition("?")
        if method == "GET" and (base.endswith("/reviews") or base.endswith("/comments")):
            self.raw_calls.append((method, path, body))
            page = int(query.rsplit("page=", 1)[1]) if "page=" in query else 1
            rows = self.reviews_payload if base.endswith("/reviews") else self.comments_payload
            return list(rows[(page - 1) * 100 : page * 100])
        return super().raw(method, path, body)


class TestObjectionsAcrossPages:
    """#614 acceptance: a standing CHANGES_REQUESTED past the first page of
    reviews is still an objection, and inline comments on the second page
    are still things to answer."""

    def test_a_changes_requested_on_the_second_page_is_detected(self) -> None:
        fake = PagedFake()
        fake.reviews_payload = [
            *(human_review(f"drive-by-{i}", "COMMENTED", "lgtm-ish", id=i) for i in range(134)),
            human_review("alice", "CHANGES_REQUESTED", "please no", id=135),
            *(human_review(f"drive-by-{i}", "COMMENTED", "", id=i) for i in range(136, 150)),
        ]
        assert human_objection(fake, REPO, 7, login=LOGIN) is True
        standing = human_objections(fake, REPO, 7, login=LOGIN)
        assert [o.login for o in standing] == ["alice"]
        assert standing[0].body == "please no"
        pages = [p.rsplit("page=", 1)[1] for m, p, _ in fake.raw_calls if "/reviews?" in p]
        assert pages == ["1", "2", "1", "2"], "each reader walked to the second page"

    def test_a_first_page_only_read_would_have_missed_it(self) -> None:
        """The failure this guards against, stated as the pre-#614 read."""
        fake = PagedFake()
        fake.reviews_payload = [
            *(human_review(f"drive-by-{i}", "COMMENTED", "", id=i) for i in range(134)),
            human_review("alice", "CHANGES_REQUESTED", "please no", id=135),
        ]
        first_page = fake.raw("GET", f"/repos/{REPO}/pulls/7/reviews?per_page=100&page=1")
        assert not any(r["state"] == "CHANGES_REQUESTED" for r in first_page)

    def test_inline_comments_on_the_second_page_are_objections(self) -> None:
        fake = PagedFake()
        fake.reviews_payload = [human_review("alice", "CHANGES_REQUESTED", "", id=41)]
        fake.comments_payload = [
            *(human_comment("bob", f"nit {i}", path="z.py", line=i, id=i) for i in range(100)),
            human_comment("alice", "this one matters", path="a.py", line=3, id=900),
        ]
        standing = human_objections(fake, REPO, 7, login=LOGIN)
        assert [o.body for o in standing] == ["", "this one matters"]


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

    def test_an_objection_knows_whether_a_bot_raised_it(self) -> None:
        """REST says ``user.type == "Bot"`` for a GitHub App; the objection
        carries it so landing can tell a machine's review from a person's
        (#613) and #622 can read it without a second lookup."""
        fake = FakeGithub()
        fake.reviews_payload = [
            human_review("coderabbitai[bot]", "CHANGES_REQUESTED", "nits", id=41, bot=True),
            human_review("alice", "CHANGES_REQUESTED", "no", id=42),
        ]
        fake.comments_payload = [
            human_comment("coderabbitai[bot]", "here", path="a.py", line=3, id=91, bot=True),
            human_comment("alice", "and here", path="a.py", line=9, id=92),
        ]
        found = {o.key: o.is_bot for o in human_objections(fake, REPO, 7, login=LOGIN)}
        assert found == {
            "human:review:41": True,
            "human:comment:91": True,
            "human:review:42": False,
            "human:comment:92": False,
        }


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
        policy_for: PolicyFor = no_policy,
        bot_round_spent: bool = False,
        settle_from: float | None = None,
        own_draft: bool = True,
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
            policy_for=policy_for,
            bot_round_spent=bot_round_spent,
            settle_from=settle_from,
            own_draft=own_draft,
        )

    def against_base(self, *, advisory_spent: Container[str] = frozenset()) -> PolicyFor:
        """Judge the head against the base the fake serves (#611)."""
        return check_policy_reader(
            self.fake, REPO, "main", cfg=self.cfg, advisory_spent=advisory_spent
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

    def test_a_draft_the_loop_did_not_make_is_a_persons_hold(self) -> None:
        """#677: a parked landing re-entering (`own_draft=False`) finds the
        PR a draft — a person converted it back. Not the loop's to clear:
        it parks until they mark it ready."""
        fake = FakeGithub(draft=True)
        lp = Landing(fake)
        outcome = lp.run(own_draft=False)
        assert outcome == AwaitingReview(0, 0, False, "commit0", draft=True)
        assert "marked ready for review" in outcome.wanted
        assert fake.ready_calls == [] and fake.merges == []
        assert ("land.held_by_draft", {"pr": 7, "head": "commit0"}) in lp.rec.events

    def test_a_draft_after_the_loops_own_undraft_is_a_persons_hold(self) -> None:
        """The loop's own draft is cleared once; someone converting the PR
        back between polls is a hold, not a second un-draft."""
        fake = FakeGithub(draft=True)
        fake.pr["mergeable_state"] = "draft"
        lp = Landing(fake)

        def redraft(n: int) -> None:
            fake.pr["mergeable_state"] = "clean"
            fake.pr["draft"] = True

        lp.rec.on_tick.append(redraft)
        outcome = lp.run()
        assert isinstance(outcome, AwaitingReview) and outcome.draft
        assert fake.ready_calls == ["PR_node7"], "un-drafted once, never over a person"
        assert fake.merges == []

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
        assert isinstance(outcome, Blocked)
        # A bare 405 is rewritten to name what the base's rules are known to
        # want (#620), GitHub's own words kept.
        assert BLOCKED_405.reason in outcome.why
        assert "protection could not be read" in outcome.why
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


class TestSettleAtLanding:
    """#633: a head with nothing reported on it is not trusted on sight.
    A resume at landing and the gate-approve path enter with no CI poll
    behind them; an update-branch makes a head no poll has seen. Each
    waits out ``ci_settle_s`` before "no CI" means no CI."""

    def test_a_fresh_entry_waits_out_the_settle_window(self) -> None:
        fake = FakeGithub()
        fake.checks = [NO_CHECKS]
        lp = Landing(fake, per_tick=30.0, ci_settle_s=90.0)
        assert isinstance(lp.run(), Landed)
        # the landing's read, then the poller's at +0, +30, +60, +90 ->
        # settled; the merge follows
        assert lp.rec.waits == ["ci", "ci", "ci"]
        assert fake.checks_calls == ["commit0"] * 5
        assert fake.merges == [(7, "squash", "commit0")]

    def test_ci_that_turns_up_during_the_settle_is_judged(self) -> None:
        fake = FakeGithub()
        fake.checks = [NO_CHECKS, NO_CHECKS, RED]
        lp = Landing(fake, per_tick=30.0, ci_settle_s=90.0)
        outcome = lp.run()
        assert isinstance(outcome, NeedsFix) and outcome.kind == "ci"
        assert fake.merges == []
        assert ("land.settled", {"pr": 7, "head": "commit0", "checks": RED.total}) in lp.rec.events

    def test_a_caller_that_already_settled_the_head_pays_nothing(self) -> None:
        """The CI stage settled this head from its delivery; landing in the
        same drive reads the checks once and merges."""
        fake = FakeGithub()
        fake.checks = [NO_CHECKS]
        lp = Landing(fake, per_tick=30.0, ci_settle_s=90.0)
        assert isinstance(lp.run(settle_from=lp.clock.t - 90.0), Landed)
        assert lp.rec.waits == []
        assert fake.checks_calls == ["commit0"]

    def test_an_update_branch_head_settles_on_its_own_clock(self) -> None:
        """The entry head was settled by the caller; the head the update
        makes is new to everyone and waits its own window."""
        fake = FakeGithub()
        fake.checks = [NO_CHECKS]
        fake.merge_outcomes = [STALE_409, MERGED]
        lp = Landing(fake, per_tick=30.0, ci_settle_s=90.0)
        moved = False

        def move(n: int) -> None:
            nonlocal moved
            if not moved:
                moved = True
                fake._move_head("commit1")

        lp.rec.on_tick.append(move)
        assert isinstance(lp.run(settle_from=lp.clock.t - 90.0), Landed)
        assert lp.rec.waits == ["merge", "ci", "ci", "ci"]
        assert fake.merges == [(7, "squash", "commit0"), (7, "squash", "commit1")]

    def test_a_settle_that_outlives_the_timeout_is_blocked(self) -> None:
        fake = FakeGithub()
        fake.checks = [NO_CHECKS]
        lp = Landing(fake, per_tick=100.0, ci_settle_s=1000.0, ci_timeout_s=250.0)
        outcome = lp.run()
        assert isinstance(outcome, Blocked) and "ci_timeout_s=250s" in outcome.why
        assert fake.merges == []

    def test_settle_off_trusts_the_first_read(self) -> None:
        fake = FakeGithub()
        fake.checks = [NO_CHECKS]
        lp = Landing(fake, per_tick=30.0)  # ci_settle_s = 0
        assert isinstance(lp.run(), Landed)
        assert lp.rec.waits == []


def red(*names: str, passed: tuple[str, ...] = ()) -> ChecksVerdict:
    return ChecksVerdict("red", len(names) + len(passed), (), names, passed)


class TestLandAgainstTheBase:
    """#611: a red the PR did not cause is merged over and named; one it
    did cause is fixed — for as many rounds as it gates, one if it does
    not."""

    def test_a_red_already_red_on_the_base_is_merged_over_and_named(self) -> None:
        fake = FakeGithub()
        fake.checks_by_sha["base123"] = red("flaky", passed=("ci",))
        fake.checks = [red("flaky", passed=("ci",))]
        lp = Landing(fake)
        assert lp.run(policy_for=lp.against_base()) == Landed("merge0001", by_human=False)
        assert fake.issue_comments == [
            "Merged with checks still red that this pull request did not cause:\n"
            "\n"
            "- `flaky` — already red on base123, the commit this PR is built on"
        ]
        (checks,) = [d for t, d in lp.rec.events if t == "landing.checks"]
        assert checks["state"] == "green"
        assert checks["preexisting"] == ["flaky"]
        assert checks["source"] == "all"
        assert checks["baseline_sha"] == "base123"

    def test_the_merged_over_comment_is_posted_once_across_a_stale_retry(self) -> None:
        fake = FakeGithub()
        fake.checks_by_sha["base123"] = red("flaky")
        fake.checks = [red("flaky")]
        fake.merge_outcomes = [STALE_409, MERGED]
        lp = Landing(fake)
        lp.rec.on_tick.append(lambda n: fake._move_head("commit1"))
        assert isinstance(lp.run(policy_for=lp.against_base()), Landed)
        assert len(fake.merges) == 2
        assert len(fake.issue_comments) == 1

    def test_a_refused_comment_does_not_stop_the_merge(self) -> None:
        fake = FakeGithub()
        fake.checks_by_sha["base123"] = red("flaky")
        fake.checks = [red("flaky")]
        fake.fail_once["pr_issue_comment"] = GithubOpsError("locked", http_status=403)
        lp = Landing(fake)
        assert isinstance(lp.run(policy_for=lp.against_base()), Landed)

    def test_a_red_the_pr_caused_goes_back_for_a_fix_with_only_its_logs(self) -> None:
        fake = FakeGithub()
        fake.checks_by_sha["base123"] = red("flaky", passed=("ci",))
        fake.checks = [red("ci", "flaky")]
        fake.failed_logs = [
            FailedCheck("ci", "failure", "boom", "https://x"),
            FailedCheck("flaky", "failure", "still flaky", "https://y"),
        ]
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, NeedsFix)
        assert outcome.why == "1 check(s) failed: ci; already red on the base: flaky"
        assert [c.name for c in outcome.failed_checks] == ["ci"]
        assert outcome.checks is not None and outcome.checks.preexisting == ("flaky",)
        assert fake.issue_comments == []

    def test_a_required_check_red_on_the_base_is_still_fixed(self) -> None:
        fake = FakeGithub()
        fake.protection = {"required_status_checks": {"contexts": ["ci"]}}
        fake.checks_by_sha["base123"] = red("ci")
        fake.checks = [red("ci")]
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, NeedsFix)
        assert outcome.checks is not None and not outcome.checks.advisory_only
        assert fake.merges == []

    def test_an_advisory_regression_gets_one_round_then_is_merged_over(self) -> None:
        fake = FakeGithub()
        fake.protection = {"required_status_checks": {"contexts": ["ci"]}}
        fake.checks = [red("lint", passed=("ci",))]
        lp = Landing(fake)
        first = lp.run(policy_for=lp.against_base())
        assert isinstance(first, NeedsFix)
        assert first.checks is not None and first.checks.advisory_only
        assert first.checks.fix == ("lint",)

        second = lp.run(policy_for=lp.against_base(advisory_spent={"lint"}))
        assert isinstance(second, Landed)
        assert fake.issue_comments == [
            "Merged with checks still red that this pull request did not cause:\n"
            "\n"
            "- `lint` — went red on this PR but is not required by the base branch; "
            "one fix round did not clear it"
        ]

    def test_only_gating_checks_are_waited_on(self) -> None:
        fake = FakeGithub()
        fake.protection = {"required_status_checks": {"contexts": ["ci"]}}
        fake.checks = [ChecksVerdict("pending", 2, ("slow-e2e",), (), ("ci",))]
        lp = Landing(fake)
        assert isinstance(lp.run(policy_for=lp.against_base()), Landed)
        assert lp.rec.waits == []

    def test_a_declared_check_absent_from_the_head_is_waited_on(self) -> None:
        fake = FakeGithub()
        fake.protection = {"required_status_checks": {"contexts": ["ci", "docs"]}}
        fake.checks = [
            ChecksVerdict("green", 1, (), (), ("ci",)),
            ChecksVerdict("green", 2, (), (), ("ci", "docs")),
        ]
        lp = Landing(fake)
        assert isinstance(lp.run(policy_for=lp.against_base()), Landed)
        assert lp.rec.waits == ["ci"]

    def test_ignored_checks_never_block(self) -> None:
        fake = FakeGithub()
        fake.checks = [red("codecov/patch", passed=("ci",))]
        lp = Landing(fake, ignore_checks=["codecov/*"])
        assert isinstance(lp.run(policy_for=lp.against_base()), Landed)
        assert fake.issue_comments == []
        (checks,) = [d for t, d in lp.rec.events if t == "landing.checks"]
        assert checks["ignored"] == ["codecov/patch"]

    def test_a_clean_landing_says_nothing_extra(self) -> None:
        fake = FakeGithub()
        lp = Landing(fake)
        assert isinstance(lp.run(policy_for=lp.against_base()), Landed)
        assert [t for t, _ in lp.rec.events if t == "landing.checks"] == []
        assert fake.issue_comments == []


BOT = "coderabbitai[bot]"


class TestBotReviewers:
    """#613: a bot's CHANGES_REQUESTED is a signal worth one fix round and
    never an authority — only a person's review blocks a merge."""

    def test_a_bots_changes_requested_buys_one_dedicated_fix_round(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [
            human_review(BOT, "CHANGES_REQUESTED", "unused import", id=41, bot=True)
        ]
        fake.comments_payload = [
            human_comment(BOT, "drop this", path="a.py", line=3, id=91, bot=True)
        ]
        fake.feedback = "- a.py:3 drop this"
        outcome = Landing(fake).run()
        assert isinstance(outcome, NeedsFix)
        assert outcome.kind == "bot"
        assert outcome.why == "an automated reviewer requested changes on the pull request"
        assert outcome.objections == fake.feedback
        assert [o.key for o in outcome.human] == ["human:review:41", "human:comment:91"]
        assert all(o.is_bot for o in outcome.human)
        assert fake.merges == []

    def test_after_its_round_a_standing_bot_review_is_merged_over_and_named(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review(BOT, "CHANGES_REQUESTED", "still", id=41, bot=True)]
        lp = Landing(fake)
        assert isinstance(lp.run(bot_round_spent=True), Landed)
        assert fake.merges == [(7, "squash", "commit0")]
        assert len(fake.issue_comments) == 1
        comment = fake.issue_comments[0]
        assert f"`{BOT}`" in comment
        assert "bots do not dismiss their reviews" in comment
        assert [d for t, d in lp.rec.events if t == "land.bot_standing"] == [
            {"pr": 7, "reviewers": [BOT]}
        ]

    def test_a_bots_review_never_becomes_the_terminal_block(self) -> None:
        """Every objection answered, review still standing — a human's
        would hand over here; a bot's is merged over."""
        fake = FakeGithub()
        fake.reviews_payload = [human_review(BOT, "CHANGES_REQUESTED", "still", id=41, bot=True)]
        outcome = Landing(fake).run(answered={"human:review:41"}, bot_round_spent=True)
        assert isinstance(outcome, Landed)

    def test_an_unanswered_bot_objection_after_the_round_is_not_a_second_round(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review(BOT, "CHANGES_REQUESTED", "more", id=41, bot=True)]
        fake.comments_payload = [
            human_comment(BOT, "new nit", path="b.py", line=1, id=92, bot=True)
        ]
        assert isinstance(Landing(fake).run(bot_round_spent=True), Landed)

    def test_the_bot_comment_is_posted_once_across_polls(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review(BOT, "CHANGES_REQUESTED", "still", id=41, bot=True)]
        fake.checks = [PENDING, PENDING, GREEN]
        lp = Landing(fake)
        assert isinstance(lp.run(bot_round_spent=True), Landed)
        assert lp.rec.waits == ["ci", "ci"]
        assert len(fake.issue_comments) == 1
        assert len([t for t, _ in lp.rec.events if t == "land.bot_standing"]) == 1

    def test_a_refused_bot_comment_does_not_stop_the_merge(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review(BOT, "CHANGES_REQUESTED", "still", id=41, bot=True)]
        fake.fail_always["pr_issue_comment"] = GithubOpsError("nope", http_status=403)
        assert isinstance(Landing(fake).run(bot_round_spent=True), Landed)
        assert fake.merges == [(7, "squash", "commit0")]

    def test_a_person_standing_beside_a_bot_keeps_full_authority(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [
            human_review(BOT, "CHANGES_REQUESTED", "nits", id=41, bot=True),
            human_review("alice", "CHANGES_REQUESTED", "no", id=42),
        ]
        first = Landing(fake).run()
        assert isinstance(first, NeedsFix) and first.kind == "human"
        assert [o.key for o in first.human] == ["human:review:42"], "the bot rides no human round"
        after = Landing(fake).run(answered={"human:review:42"}, bot_round_spent=True)
        assert isinstance(after, Blocked)
        assert "only they can dismiss it" in after.why
        assert fake.merges == []

    def test_a_human_review_is_untouched_by_the_bot_round_state(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review("alice", "CHANGES_REQUESTED", "no", id=42)]
        outcome = Landing(fake).run(bot_round_spent=True)
        assert isinstance(outcome, NeedsFix) and outcome.kind == "human"

    def test_ignore_reviewers_treats_a_user_account_as_a_bot(self) -> None:
        """A bot reviewing from a personal token is a User to GitHub; the
        operator names it and it gets a bot's one round, not a person's
        veto."""
        fake = FakeGithub()
        fake.reviews_payload = [human_review("Review-Robot", "CHANGES_REQUESTED", "hm", id=41)]
        lp = Landing(fake, ignore_reviewers=["review-robot"])
        first = lp.run()
        assert isinstance(first, NeedsFix) and first.kind == "bot"
        assert isinstance(
            Landing(fake, ignore_reviewers=["review-robot"]).run(bot_round_spent=True), Landed
        )

    def test_there_is_no_reverse_list(self) -> None:
        """Nothing in config turns an App into a person."""
        fake = FakeGithub()
        fake.reviews_payload = [human_review(BOT, "CHANGES_REQUESTED", "x", id=41, bot=True)]
        outcome = Landing(fake, ignore_reviewers=[]).run()
        assert isinstance(outcome, NeedsFix) and outcome.kind == "bot"

    def test_a_bot_round_still_needs_the_loops_identity(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [human_review(BOT, "CHANGES_REQUESTED", "x", id=41, bot=True)]
        outcome = Landing(fake).run(login="")
        assert isinstance(outcome, Blocked)
        assert "identity could not be resolved" in outcome.why


def approval(*names: str, passed: tuple[str, ...] = ()) -> ChecksVerdict:
    return ChecksVerdict("pending", len(names) + len(passed), (), (), passed, names)


class TestWorkflowApproval:
    """#612: `action_required` is a maintainer's to clear — the loop neither
    waits it out nor spends a fix round on it, and says so."""

    def test_poll_returns_at_once_on_an_unapproved_workflow(self) -> None:
        fake = FakeGithub()
        fake.checks = [approval("ci")]
        rec = Recorder(FakeClock())
        verdict = poll_checks(
            fake,
            REPO,
            fake.head_sha,
            cfg=cfg(),
            tick=rec.tick,
            emit=rec.emit_checks,
            clock=rec.clock,
        )
        assert verdict.needs_approval == ("ci",)
        assert rec.waits == []

    def test_landing_hands_over_named_without_a_fix_round(self) -> None:
        fake = FakeGithub()
        fake.checks = [approval("ci")]
        lp = Landing(fake)
        outcome = lp.run()
        assert outcome == Blocked("check ci needs a maintainer to approve the workflow run")
        assert fake.merges == [] and lp.rec.waits == []
        (checks,) = [d for t, d in lp.rec.events if t == "landing.checks"]
        assert checks["needs_approval"] == ["ci"]

    def test_a_real_red_is_fixed_before_the_approval_is_named(self) -> None:
        fake = FakeGithub()
        fake.checks = [ChecksVerdict("red", 2, (), ("lint",), (), ("ci",))]
        fake.failed_logs = [FailedCheck("lint", "failure", "boom", "https://x")]
        outcome = Landing(fake).run()
        assert isinstance(outcome, NeedsFix)
        assert [c.name for c in outcome.failed_checks] == ["lint"]

    def test_an_unapproved_advisory_workflow_does_not_block(self) -> None:
        fake = FakeGithub()
        fake.protection = {"required_status_checks": {"contexts": ["ci"]}}
        fake.checks = [approval("optional-e2e", passed=("ci",))]
        lp = Landing(fake)
        assert isinstance(lp.run(policy_for=lp.against_base()), Landed)


class TestMergeMethod:
    """#620: `auto` takes what the repository allows; an explicit method it
    refuses is named, never swapped for another."""

    def test_allowed_methods_come_in_the_loops_order(self) -> None:
        payload = {
            "allow_squash_merge": False,
            "allow_merge_commit": True,
            "allow_rebase_merge": True,
        }
        assert allowed_merge_methods(payload) == ("merge", "rebase")
        assert allowed_merge_methods({"allow_squash_merge": True}) is None, "partial = unknown"
        assert allowed_merge_methods(None) is None

    def test_resolution(self) -> None:
        assert resolve_merge_method("auto", ("merge", "rebase"))[0] == "merge"
        assert resolve_merge_method("auto", None)[0] == "squash"
        assert resolve_merge_method("rebase", ("merge", "rebase"))[0] == "rebase"
        assert resolve_merge_method("rebase", None)[0] == "rebase"
        method, why = resolve_merge_method("auto", ())
        assert method is None and "allows no merge method" in why
        method, why = resolve_merge_method("squash", ("merge",))
        assert method is None
        assert '`[landing] merge_method = "squash"`' in why and "it allows: merge" in why

    def test_auto_takes_the_first_allowed_method(self) -> None:
        fake = FakeGithub()
        fake.repo_payload["allow_squash_merge"] = False
        lp = Landing(fake)
        assert isinstance(lp.run(), Landed)
        assert fake.merges == [(7, "merge", "commit0")]
        assert ("land.merge_method", {"pr": 7, "method": "merge", "configured": "auto"}) in (
            lp.rec.events
        )

    def test_auto_is_squash_by_default_as_before(self) -> None:
        lp = Landing(FakeGithub())
        lp.run()
        assert lp.fake.merges == [(7, "squash", "commit0")]

    def test_an_explicit_disallowed_method_blocks_and_never_substitutes(self) -> None:
        fake = FakeGithub()
        fake.repo_payload["allow_rebase_merge"] = False
        outcome = Landing(fake, merge_method="rebase").run()
        assert isinstance(outcome, Blocked)
        assert '`[landing] merge_method = "rebase"`' in outcome.why
        assert fake.merges == []

    def test_an_unreadable_repository_lets_the_merge_answer(self) -> None:
        fake = FakeGithub()
        fake.fail_always["repo_get"] = GithubOpsError("nope", http_status=500)
        lp = Landing(fake)
        assert isinstance(lp.run(), Landed)
        assert fake.merges == [(7, "squash", "commit0")]

    def test_the_method_is_resolved_once_per_landing(self) -> None:
        fake = FakeGithub()
        fake.merge_outcomes = [STALE_409, MERGED]
        lp = Landing(fake)
        lp.rec.on_tick.append(lambda n: fake._move_head("commit1"))
        assert isinstance(lp.run(), Landed)
        assert len([t for t, _ in lp.rec.events if t == "land.merge_method"]) == 1


class TestBlockedWithGreenChecks:
    """#620: GitHub says `blocked` with every check green — name what the
    base's rules want instead of PUTting and parsing a 405."""

    def test_a_blocked_state_is_reread_once_then_named(self) -> None:
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "blocked"
        fake.protection = {
            "required_pull_request_reviews": {"required_approving_review_count": 1},
            "required_signatures": {"enabled": True},
        }
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, Blocked)
        assert "requires an approving review" in outcome.why
        assert "requires signed commits" in outcome.why
        assert "merge_gate" not in outcome.why
        assert fake.merges == []
        assert lp.rec.waits == ["mergeability"], (
            "read again: the first state may predate the checks"
        )

    def test_a_blocked_state_whose_only_want_is_a_review_awaits_one(self) -> None:
        """#675: a base that wants nothing the loop cannot supply *except*
        an approving review is a wait for a person, not a block — with the
        standing human approvals counted (the loop's own excluded)."""
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "blocked"
        fake.protection = {"required_pull_request_reviews": {"required_approving_review_count": 2}}
        fake.reviews_payload = [
            human_review("alice", "APPROVED", "lgtm", id=90),
            human_review("sbxloop-bot", "APPROVED", "own", id=91),
            human_review("ci-bot", "APPROVED", "bot", id=92, bot=True),
        ]
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, AwaitingReview)
        assert outcome.approvals_required == 2
        assert outcome.approvals_have == 1
        assert not outcome.code_owners
        assert outcome.head == fake.pr["head"]["sha"]
        assert outcome.wanted == "2 approving reviews"
        assert fake.merges == []

    def test_a_code_owner_review_alone_is_also_a_wait(self) -> None:
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "blocked"
        fake.protection = {
            "required_pull_request_reviews": {
                "required_approving_review_count": 0,
                "require_code_owner_reviews": True,
            }
        }
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, AwaitingReview)
        assert outcome.code_owners and outcome.approvals_required == 0
        assert outcome.wanted == "an approving review from a code owner"

    def test_a_review_wait_still_blocks_on_the_last_push_rule(self) -> None:
        # The fatal rule stays fatal: no review can ever satisfy it.
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "blocked"
        fake.protection = {
            "required_pull_request_reviews": {
                "required_approving_review_count": 1,
                "require_last_push_approval": True,
            }
        }
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, Blocked)
        assert "approval of the last push" in outcome.why

    def test_a_blocked_state_that_clears_on_the_reread_merges(self) -> None:
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "blocked"
        lp = Landing(fake)
        lp.rec.on_tick.append(lambda n: fake.pr.__setitem__("mergeable_state", "clean"))
        assert isinstance(lp.run(), Landed)

    def test_unstable_is_merged_over(self) -> None:
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "unstable"
        assert isinstance(Landing(fake).run(), Landed)

    def test_the_reason_names_what_is_known(self) -> None:
        from sbxloop.gh.protection import BaseRequirements

        reviews = BaseRequirements((), 1, "protection")
        assert "requires an approving review" in blocked_reason(reviews, cfg())
        assert "merge_gate" not in blocked_reason(reviews, cfg(merge_gate="chat"))
        unknown = BaseRequirements(None, None, "unknown")
        assert "base's protection could not be read" in blocked_reason(unknown, cfg())
        # #674: which source, and why classic protection is the usual one.
        forbidden = BaseRequirements(None, None, "unknown", unread=("protection",))
        why = blocked_reason(forbidden, cfg())
        assert "base's classic branch protection could not be read" in why
        assert "(reading classic protection needs admin)" in why
        both = BaseRequirements(None, None, "unknown", unread=("protection", "rulesets"))
        assert "classic branch protection and rulesets could not be read" in blocked_reason(
            both, cfg()
        )
        # The rollup supplied the checks, but not the rule behind the refusal.
        rollup = BaseRequirements(("ci",), None, "pr-rollup", unread=("protection",))
        assert "classic branch protection could not be read" in blocked_reason(rollup, cfg())
        none = BaseRequirements((), 0, "protection")
        why = blocked_reason(none, cfg(), detail="Pull Request is not mergeable (HTTP 405)")
        assert "a rule it does not read" in why and why.endswith(
            "(GitHub: Pull Request is not mergeable (HTTP 405))"
        )

    def test_the_reason_lists_every_blocker_one_per_line(self) -> None:
        """#673: each rule of the base the loop cannot satisfy is its own
        line; the fatal last-push rule comes first. (The merge-gate hint that
        used to follow a review rule is gone with #675: a review wait is a
        wait, not something to configure around.)"""
        from sbxloop.gh.protection import BaseRequirements

        rules = BaseRequirements(
            (),
            2,
            "rulesets",
            code_owner_review=True,
            last_push_approval=True,
            signed_commits=True,
            merge_queue=True,
            required_deployments=("staging",),
        )
        why = blocked_reason(rules, cfg(merge_method="merge"), detail="HTTP 405")
        lines = why.split("\n")
        assert lines[0] == "the base branch's rules require what the loop cannot supply:"
        assert lines[1].startswith("- the base requires approval of the last push")
        assert lines[2].startswith("- the base requires 2 approving reviews")
        assert lines[3].startswith("- the base requires a review from a code owner")
        assert lines[4].startswith("- the base requires signed commits")
        assert lines[5].startswith("- the base requires a successful deployment to staging")
        # The merge queue is not on the list (#676): the loop enqueues.
        assert len(lines) == 6 and "merge_gate" not in why and "merge queue" not in why
        assert why.endswith("(GitHub: HTTP 405)")
        # Signing is the credential's to satisfy; a linear-history rule only
        # blocks a merge commit.
        signed = BaseRequirements((), 0, "rulesets", signed_commits=True, linear_history=True)
        assert blocked_reason(signed, cfg(merge_method="squash"), can_sign=True).startswith(
            "GitHub reports the pull request as blocked"
        )
        assert "linear history" in blocked_reason(signed, cfg(merge_method="merge"), can_sign=True)

    def test_a_blocked_outcome_carries_the_blockers(self) -> None:
        fake = FakeGithub()
        fake.pr["mergeable_state"] = "blocked"
        fake.rules = [
            {"type": "pull_request", "parameters": {"require_last_push_approval": True}},
            {
                "type": "required_deployments",
                "parameters": {"required_deployment_environments": ["staging"]},
            },
        ]
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, Blocked)
        assert len(outcome.blockers) == 2
        assert outcome.blockers[0].startswith("the base requires approval of the last push")
        assert outcome.blockers[1].startswith("the base requires a successful deployment")
        assert all(f"- {b}" in outcome.why for b in outcome.blockers)

    def test_a_405_on_the_merge_is_rewritten_with_the_bases_rules(self) -> None:
        fake = FakeGithub()
        fake.merge_outcomes = [BLOCKED_405]
        fake.protection = {"required_signatures": {"enabled": True}}
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, Blocked)
        assert "requires signed commits" in outcome.why
        assert outcome.why.endswith("(GitHub: Pull Request is not mergeable (HTTP 405))")

    def test_a_405_on_the_merge_for_a_review_alone_awaits_one(self) -> None:
        # The same #675 judgment on the other refusal path: a merge GitHub
        # refused with 405 when the base only wants a review.
        fake = FakeGithub()
        fake.merge_outcomes = [BLOCKED_405]
        fake.protection = {"required_pull_request_reviews": {"required_approving_review_count": 1}}
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, AwaitingReview)
        assert outcome.approvals_have == 0 and outcome.approvals_required == 1


QUEUED = QueueState(
    merged=False, closed=False, entry=QueueEntry("MQE_1", "AWAITING_CHECKS", 1, "queue0")
)
QUEUE_MERGED = QueueState(merged=True, closed=False, entry=None, merge_sha="queued0001")
NOT_QUEUED = QueueState(merged=False, closed=False, entry=None)


class TestMergeQueue:
    """#676: a base that merges through a merge queue is entered, not
    merged — where the merge would happen, after every other bar."""

    def _fake(self) -> FakeGithub:
        fake = FakeGithub()
        fake.rules = [{"type": "merge_queue", "parameters": {"merge_method": "SQUASH"}}]
        return fake

    def test_a_queued_pr_lands_when_the_queue_merges_it(self) -> None:
        fake = self._fake()
        fake.queue_states = [NOT_QUEUED, QUEUED, QUEUED, QUEUE_MERGED]
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert outcome == Landed("queued0001", by_human=False)
        assert fake.merges == [], "PUT /merge is never sent on a merge-queue base"
        assert fake.enqueued == [("PR_node7", "commit0")], "the judged head guards the enqueue"
        assert lp.rec.waits == ["queue", "queue", "queue"]
        kinds = [t for t, _ in lp.rec.events]
        assert kinds.count("land.enqueued") == 1 and "land.dequeued" not in kinds
        enqueued = next(d for t, d in lp.rec.events if t == "land.enqueued")
        assert enqueued == {"pr": 7, "head": "commit0", "position": 1, "resumed": False}
        assert fake.deleted_branches == [], "the queue owns the branch's fate"

    def test_the_queue_is_entered_only_after_every_other_bar(self) -> None:
        """A review the base wants comes first: the wait is for the human,
        and the enqueue happens on the landing that follows their approval."""
        fake = self._fake()
        fake.pr["mergeable_state"] = "blocked"
        fake.rules.append(
            {"type": "pull_request", "parameters": {"required_approving_review_count": 1}}
        )
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, AwaitingReview)
        assert fake.enqueued == []

    def test_a_blocked_state_with_only_the_queue_left_is_enqueued(self) -> None:
        """Whether GitHub reports a queue-bound PR `blocked` before it is
        queued is field-unverified; either way the enqueue is GitHub's own
        verdict, so the loop asks rather than hands over."""
        fake = self._fake()
        fake.pr["mergeable_state"] = "blocked"
        fake.queue_states = [NOT_QUEUED, QUEUE_MERGED]
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert outcome == Landed("queued0001", by_human=False)
        assert len(fake.enqueued) == 1

    def test_a_refused_enqueue_blocks_with_githubs_words(self) -> None:
        fake = self._fake()
        fake.enqueue_ok = False
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, Blocked)
        assert "refused to enqueue" in outcome.why and "not mergeable" in outcome.why
        assert fake.merges == []

    def test_a_refused_enqueue_with_a_review_rule_left_is_the_rules_verdict(self) -> None:
        """The queue's 405: when the base's other rules say what GitHub
        wants, the outcome is theirs — a review wait here, GitHub's words
        riding along in the blocked case — not a bare block."""
        fake = self._fake()
        fake.enqueue_ok = False
        fake.rules = [
            {"type": "merge_queue"},
            {"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
        ]
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, AwaitingReview)
        assert outcome.approvals_required == 1 and fake.merges == []
        fake = self._fake()
        fake.enqueue_ok = False
        fake.rules = [{"type": "merge_queue"}, {"type": "required_signatures"}]
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, Blocked)
        assert "signed commits" in outcome.why and "not mergeable" in outcome.why

    def test_a_removal_with_red_checks_on_the_queue_commit_is_a_ci_round(self) -> None:
        """The queue's checks ran on its own merge-group commit; those reds
        are what the fix round gets, not the PR head's (green) ones."""
        fake = self._fake()
        removed = QueueState(
            merged=False, closed=False, entry=None, removals=1, removed_reason="CI_FAILED"
        )
        fake.queue_states = [NOT_QUEUED, QUEUED, removed]
        fake.failed_logs = [FailedCheck("integration", "failure", "boom", "https://x/logs")]
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, NeedsFix) and outcome.kind == "ci"
        assert "removed the pull request (CI_FAILED)" in outcome.why
        assert "integration" in outcome.why
        assert [c.name for c in outcome.failed_checks] == ["integration"]
        assert fake.failed_logs_calls[-1] == "queue0", "the queue's commit, not the PR head"
        dequeued = next(d for t, d in lp.rec.events if t == "land.dequeued")
        assert dequeued["reason"] == "CI_FAILED" and dequeued["failed"] == ["integration"]

    def test_a_removal_with_nothing_to_fix_blocks(self) -> None:
        """A human dequeued it, or the queue said something the loop cannot
        read: a fix round with nothing to fix is budget burn."""
        fake = self._fake()
        fake.queue_states = [NOT_QUEUED, QUEUED, NOT_QUEUED]
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, Blocked)
        assert "removed the pull request, with no failing check" in outcome.why
        # No removal event the loop did not already know of: no reason.
        assert "(" not in outcome.why.split(",")[0]

    def test_a_removal_from_before_this_landing_is_not_this_landings_reason(self) -> None:
        """The removal count seen at enqueue time is the baseline: an older
        removal's reason is not attributed to this one."""
        fake = self._fake()
        earlier = QueueState(
            merged=False, closed=False, entry=None, removals=1, removed_reason="MANUAL"
        )
        fake.queue_states = [earlier, QUEUED, earlier]
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, Blocked) and "MANUAL" not in outcome.why

    def test_a_pr_already_in_the_queue_is_watched_not_enqueued_twice(self) -> None:
        fake = self._fake()
        fake.queue_states = [QUEUED, QUEUE_MERGED]
        lp = Landing(fake)
        outcome = lp.run(policy_for=lp.against_base())
        assert outcome == Landed("queued0001", by_human=False)
        assert fake.enqueued == []
        enqueued = next(d for t, d in lp.rec.events if t == "land.enqueued")
        assert enqueued["resumed"] is True

    def test_a_pr_the_queue_already_merged_is_landed_by_human(self) -> None:
        fake = self._fake()
        fake.queue_states = [QUEUE_MERGED]
        lp = Landing(fake)
        assert lp.run(policy_for=lp.against_base()) == Landed("queued0001", by_human=True)

    def test_closed_while_queued(self) -> None:
        fake = self._fake()
        fake.queue_states = [NOT_QUEUED, QueueState(merged=False, closed=True, entry=None)]
        lp = Landing(fake)
        assert isinstance(lp.run(policy_for=lp.against_base()), Closed)

    def test_the_timeout_leaves_the_pr_in_the_queue(self) -> None:
        fake = self._fake()
        fake.queue_states = [NOT_QUEUED, QUEUED]
        lp = Landing(fake, per_tick=100.0, ci_timeout_s=250.0)
        outcome = lp.run(policy_for=lp.against_base())
        assert isinstance(outcome, Blocked)
        assert "still in the merge queue after ci_timeout_s=250s" in outcome.why
        assert lp.rec.waits == ["queue", "queue", "queue"]

    def test_a_405_naming_the_queue_enters_it_when_the_rules_were_unread(self) -> None:
        """The rules are the primary signal; GitHub's own refusal wording
        is the fallback when they could not be read."""
        fake = FakeGithub()
        fake.merge_outcomes = [
            MergeOutcome(
                False,
                "",
                "HTTP 405: This branch must be merged through the merge queue",
                blocked=True,
            )
        ]
        fake.queue_states = [NOT_QUEUED, QUEUE_MERGED]
        lp = Landing(fake)
        outcome = lp.run()
        assert outcome == Landed("queued0001", by_human=False)
        assert len(fake.merges) == 1 and len(fake.enqueued) == 1

    def test_a_plain_405_still_blocks(self) -> None:
        fake = FakeGithub()
        fake.merge_outcomes = [BLOCKED_405]
        lp = Landing(fake)
        assert isinstance(lp.run(), Blocked)
        assert fake.enqueued == []
