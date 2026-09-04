"""A PR that waits for a human's approving review (#675).

The daemon-side half: a run that ends ``awaiting_review`` parks its item
with a durable hold row, the configured reviewers are asked once, the
requester is pinged, and the review tick polls the PR — an approval lands
it with gh ops alone, a request for changes resumes the run for a fix
round, a closed PR settles, a wait past ``review_wait_s`` pauses until
``resume``. The landing-side half (``refused_by_base``) is pinned in
test_engine_landing.py.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.control import dispatch
from sbxloop.gh.ops import GithubOpsError
from tests.fakes.fake_github import BLOCKED_405, FakeGithub, human_review
from tests.unit.test_daemon_loop import PR_URL, Harness, RecordingFrontend, gh_item
from tests.unit.test_daemon_merge_gate import FakeDaemonGithub


def config(tmp_path: Path, **overrides: Any) -> Config:
    data: dict[str, Any] = {
        "state_dir": str(tmp_path / "state"),
        "github": {"repo": "o/r", "reviewers": ["alice", "o/reviewers"]},
        "landing": {"review_poll_interval_s": 600, "review_wait_s": 3600, **overrides},
    }
    return Config.model_validate(data)


def review_harness(tmp_path: Path, cfg: Config | None = None) -> tuple[Harness, FakeGithub]:
    h = Harness(tmp_path, cfg or config(tmp_path))
    h.loop.frontend = RecordingFrontend()
    fake = FakeGithub(number=9)
    fake.pr["html_url"] = PR_URL
    h.loop.github = FakeDaemonGithub(fake)  # type: ignore[assignment]
    return h, fake


def park(h: Harness, key: str = "1", **item_overrides: Any) -> str:
    """Dispatch one item that ends awaiting a review; returns its run id."""
    h.source.items = [gh_item(key, **item_overrides)]
    h.outcomes = ["awaiting_review"]
    result = h.loop.tick()
    assert result.outcome == "awaiting_review", result
    item = h.dstore.get(f"gh:issue:{key}")
    assert item is not None and item.run_id is not None
    return item.run_id


def landed(h: Harness, run_id: str) -> Any:
    """Wait for the landing thread an approval spawned; returns the hold."""
    deadline = time.time() + 10
    while time.time() < deadline:
        hold = h.dstore.review_hold_for(run_id)
        if hold is not None and hold.state != "approving":
            return hold
        time.sleep(0.05)
    raise AssertionError("the landing thread did not finish")


def notices(h: Harness, kind: str) -> list[Any]:
    front = h.loop.frontend
    assert isinstance(front, RecordingFrontend)
    return [n for n in front.notices if n.kind == kind]


class TestPark:
    def test_the_item_waits_with_a_hold_row_and_a_reset_breaker(self, tmp_path: Path) -> None:
        h, _fake = review_harness(tmp_path)
        run_id = park(h)
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "awaiting_review"
        assert item.run_id == run_id, "the run stays pinned to the item"
        hold = h.dstore.review_hold_for("gh:issue:1")
        assert hold is not None and hold.state == "open"
        assert hold.pr_number == 9 and hold.pr_url == PR_URL
        assert hold.approvals_required == 2, "from the run's own record of the park"
        assert hold.login == "sbxloop-bot", "the loop's identity, resolved once"
        assert h.loop.status()["breaker_open"] is False
        assert h.store.get_run(run_id).state == "awaiting_review"

    def test_the_source_hears_only_the_start(self, tmp_path: Path) -> None:
        # The issue is still in progress; the PR is the visible state.
        h, _fake = review_harness(tmp_path)
        park(h)
        assert [c[0] for c in h.source.calls] == ["claim", "started"]
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.pending_report is None

    def test_the_configured_reviewers_are_asked_once(self, tmp_path: Path) -> None:
        h, fake = review_harness(tmp_path)
        park(h)
        posts = [(m, p, b) for m, p, b in fake.raw_calls if p.endswith("/requested_reviewers")]
        assert len(posts) == 1
        assert posts[0][2] == {"reviewers": ["alice"], "team_reviewers": ["reviewers"]}
        assert fake.pr["requested_reviewers"] == [{"login": "alice"}]

    def test_the_requester_and_review_notify_are_pinged(self, tmp_path: Path) -> None:
        cfg = config(tmp_path, review_notify=["U9"])
        h, _fake = review_harness(tmp_path, cfg)
        park(h, requested_by="U123")
        (notice,) = notices(h, "run.awaiting_review")
        assert notice.mention_ids == ("U123", "U9")
        assert "2 approving reviews" in notice.text and "PR #9" in notice.text
        hold = h.dstore.review_hold_for("gh:issue:1")
        assert hold is not None and hold.notify_ids == ("U123", "U9")

    def test_a_per_repo_override_replaces_reviewers_and_notify(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {
                    "repo": "o/r",
                    "reviewers": ["alice"],
                    "repos": [{"repo": "o/r", "reviewers": ["bob"], "review_notify": ["U7"]}],
                },
                "landing": {"review_notify": ["U9"]},
            }
        )
        h, fake = review_harness(tmp_path, cfg)
        park(h)
        posts = [b for _m, p, b in fake.raw_calls if p.endswith("/requested_reviewers")]
        assert posts == [{"reviewers": ["bob"]}]
        (notice,) = notices(h, "run.awaiting_review")
        assert notice.mention_ids == ("U7",)

    def test_a_failed_review_request_does_not_break_the_park(self, tmp_path: Path) -> None:
        h, fake = review_harness(tmp_path)
        fake.fail_once["raw"] = GithubOpsError("boom")
        park(h)
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "awaiting_review"
        hold = h.dstore.review_hold_for("gh:issue:1")
        assert hold is not None and hold.state == "open"

    def test_a_waiting_item_is_never_dispatched(self, tmp_path: Path) -> None:
        h, _fake = review_harness(tmp_path)
        park(h)
        assert h.loop.tick().idle_reason == "no_work"

    def test_retry_points_at_resume(self, tmp_path: Path) -> None:
        h, _fake = review_harness(tmp_path)
        park(h)
        with pytest.raises(ValueError, match="resume gh:issue:1"):
            h.loop.retry_item("gh:issue:1")


class TestPoll:
    def parked(self, tmp_path: Path, **cfg: Any) -> tuple[Harness, FakeGithub, str]:
        c = config(tmp_path, **cfg)
        # No resume budget at all: a review fix round is not an interruption.
        c = c.model_copy(update={"daemon": c.daemon.model_copy(update={"max_resumes_per_item": 0})})
        h, fake = review_harness(tmp_path, c)
        run_id = park(h)
        return h, fake, run_id

    def test_nothing_polls_before_the_interval(self, tmp_path: Path) -> None:
        h, fake, _run_id = self.parked(tmp_path)
        before = len(fake.raw_calls)
        h.clock.t += 60
        h.loop.tick()
        assert len(fake.raw_calls) == before

    def test_a_poll_is_two_requests_and_reschedules(self, tmp_path: Path) -> None:
        h, fake, run_id = self.parked(tmp_path)
        h.clock.t += 600
        before = len(fake.raw_calls)
        h.loop.tick()
        # pr_get is a modelled op, not a raw call; the reviews read is raw.
        assert [p for _m, p, _b in fake.raw_calls[before:] if "/pulls/9/reviews" in p]
        hold = h.dstore.review_hold_for(run_id)
        assert hold is not None and hold.polls == 1 and hold.state == "open"
        assert hold.next_poll_at == h.clock.t + 600

    def test_enough_human_approvals_land_the_pr(self, tmp_path: Path) -> None:
        h, fake, run_id = self.parked(tmp_path)
        fake.reviews_payload = [
            human_review("alice", "APPROVED", "lgtm", id=1),
            human_review("sbxloop-bot", "APPROVED", "own", id=2),
            human_review("ci[bot]", "APPROVED", "", id=3, bot=True),
        ]
        h.clock.t += 600
        h.loop.tick()
        hold = h.dstore.review_hold_for(run_id)
        assert hold is not None and hold.state == "open", "one of two: own and bot do not count"
        fake.reviews_payload.append(human_review("bob", "APPROVED", "", id=4))
        h.clock.t += 600
        h.loop.tick()
        assert notices(h, "review.approved")
        landed(h, run_id)
        assert fake.merges, "landed through the shared ops box, no engine"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "done"
        assert h.store.get_run(run_id).state == "merged"
        fresh = h.dstore.review_hold_for(run_id)
        assert fresh is not None and fresh.state == "merged" and fresh.resolved_by == "alice, bob"
        assert any(c[0] == "merged" for c in h.source.calls)
        (done,) = notices(h, "run.done")
        assert "alice, bob" in done.text

    def test_a_request_for_changes_resumes_the_run_for_a_fix(self, tmp_path: Path) -> None:
        h, fake, run_id = self.parked(tmp_path)
        fake.reviews_payload = [
            human_review("alice", "APPROVED", "", id=1),
            human_review("bob", "CHANGES_REQUESTED", "no", id=2),
        ]
        h.clock.t += 600
        h.loop.pause()
        h.loop.tick()
        hold = h.dstore.review_hold_for(run_id)
        assert hold is not None and hold.state == "fixing"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued" and item.run_id == run_id
        (asked,) = notices(h, "review.changes_requested")
        assert "bob" in asked.text
        # The next dispatch resumes the pinned run — no resume-budget charge.
        h.loop.unpause()
        h.outcomes = ["merged"]
        result = h.loop.tick()
        assert result.dispatched == "gh:issue:1" and result.outcome == "done"
        assert h.runs[-1] == (run_id, True)
        assert not notices(h, "run.resume_budget_exhausted")
        assert notices(h, "run.review_resumed")
        fresh = h.dstore.review_hold_for(run_id)
        assert fresh is not None and fresh.state == "merged"

    def test_a_fix_round_that_parks_again_reopens_the_wait(self, tmp_path: Path) -> None:
        h, fake, run_id = self.parked(tmp_path)
        fake.reviews_payload = [human_review("bob", "CHANGES_REQUESTED", "no", id=2)]
        h.clock.t += 600
        h.outcomes = ["awaiting_review"]
        # One tick: the poll re-queues the item and the same tick resumes it.
        assert h.loop.tick().outcome == "awaiting_review"
        assert h.runs[-1] == (run_id, True)
        hold = h.dstore.review_hold_for(run_id)
        assert hold is not None and hold.state == "open" and hold.since_at == h.clock.t
        assert len([p for _m, p, _b in fake.raw_calls if p.endswith("/requested_reviewers")]) == 1

    def test_a_fix_round_that_ends_blocked_ends_the_wait(self, tmp_path: Path) -> None:
        h, fake, run_id = self.parked(tmp_path)
        fake.reviews_payload = [human_review("bob", "CHANGES_REQUESTED", "no", id=2)]
        h.clock.t += 600
        h.outcomes = ["blocked"]
        assert h.loop.tick().outcome == "blocked"
        hold = h.dstore.review_hold_for(run_id)
        assert hold is not None and hold.state == "dismissed"

    def test_a_pr_closed_by_hand_settles_the_item(self, tmp_path: Path) -> None:
        h, fake, run_id = self.parked(tmp_path)
        fake.pr["state"] = "closed"
        h.clock.t += 600
        h.loop.tick()
        hold = h.dstore.review_hold_for(run_id)
        assert hold is not None and hold.state == "dismissed"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "failed"
        assert notices(h, "review.dismissed")

    def test_a_pr_merged_by_hand_settles_done(self, tmp_path: Path) -> None:
        h, fake, run_id = self.parked(tmp_path)
        fake.pr["merged"] = True
        fake.pr["merge_commit_sha"] = "human42"
        h.clock.t += 600
        h.loop.tick()
        landed(h, run_id)
        assert fake.merges == []
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "done"

    def test_a_landing_github_refuses_puts_the_wait_back_up(self, tmp_path: Path) -> None:
        h, fake, run_id = self.parked(tmp_path)
        fake.reviews_payload = [
            human_review("alice", "APPROVED", "", id=1),
            human_review("bob", "APPROVED", "", id=2),
        ]
        fake.merge_outcomes = [BLOCKED_405]
        fake.protection = {"required_pull_request_reviews": {"required_approving_review_count": 2}}
        h.clock.t += 600
        h.loop.tick()
        fresh = landed(h, run_id)
        assert fresh.state == "open" and fresh.detail == "the base now wants 2 approving reviews"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "awaiting_review"
        assert notices(h, "review.merge_failed")

    def test_a_poll_that_fails_keeps_waiting(self, tmp_path: Path) -> None:
        h, fake, run_id = self.parked(tmp_path)
        fake.fail_once["pr_get"] = GithubOpsError("github down")
        h.clock.t += 600
        h.loop.tick()
        hold = h.dstore.review_hold_for(run_id)
        assert hold is not None and hold.state == "open" and hold.polls == 1
        github = h.loop.github
        assert github is not None and github.failures  # type: ignore[attr-defined]

    def test_the_poll_runs_while_the_daemon_is_paused(self, tmp_path: Path) -> None:
        h, fake, run_id = self.parked(tmp_path)
        fake.reviews_payload = [
            human_review("alice", "APPROVED", "", id=1),
            human_review("bob", "APPROVED", "", id=2),
        ]
        h.loop.pause()
        h.clock.t += 600
        assert h.loop.tick().idle_kind == "paused"
        assert landed(h, run_id).state == "merged"


class TestTimeoutAndResume:
    def test_the_wait_pauses_after_review_wait_s(self, tmp_path: Path) -> None:
        h, fake = review_harness(tmp_path)
        run_id = park(h, requested_by="U1")
        h.clock.t += 3600
        before = len(fake.raw_calls)
        h.loop.tick()
        assert len(fake.raw_calls) == before, "a paused wait polls nothing"
        hold = h.dstore.review_hold_for(run_id)
        assert hold is not None and hold.state == "paused"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "paused_review" and item.run_id == run_id
        (paused,) = notices(h, "run.review_paused")
        assert paused.mention_ids == ("U1",) and "resume gh:issue:1" in paused.text
        h.clock.t += 600
        h.loop.tick()
        assert len(fake.raw_calls) == before
        assert h.loop.tick().idle_reason == "no_work"

    def test_resume_re_arms_a_paused_wait(self, tmp_path: Path) -> None:
        h, fake = review_harness(tmp_path)
        run_id = park(h)
        h.clock.t += 3600
        h.loop.tick()
        text = h.loop.resume_review("gh:issue:1", by="brett")
        assert "PR #9" in text
        hold = h.dstore.review_hold_for(run_id)
        assert hold is not None and hold.state == "open" and hold.since_at == h.clock.t
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "awaiting_review"
        fake.reviews_payload = [
            human_review("alice", "APPROVED", "", id=1),
            human_review("bob", "APPROVED", "", id=2),
        ]
        h.loop.tick()  # due at once
        assert notices(h, "review.approved")
        assert landed(h, run_id).state == "merged"

    def test_resume_on_an_open_wait_polls_now(self, tmp_path: Path) -> None:
        h, fake = review_harness(tmp_path)
        run_id = park(h)
        h.clock.t += 60
        h.loop.resume_review(run_id)
        before = len(fake.raw_calls)
        h.loop.tick()
        assert len(fake.raw_calls) > before

    def test_resume_refusals_name_the_state(self, tmp_path: Path) -> None:
        h, _fake = review_harness(tmp_path)
        run_id = park(h)
        with pytest.raises(ValueError, match="not waiting for a review"):
            h.loop.resume_review("gh:issue:404")
        assert h.dstore.claim_review_hold(run_id, "approving")
        with pytest.raises(ValueError, match="already past the wait"):
            h.loop.resume_review(run_id)
        h.dstore.resolve_review_hold(run_id, "merged", "x", h.clock())
        with pytest.raises(ValueError, match="retry gh:issue:1"):
            h.loop.resume_review(run_id)

    def test_abandon_dismisses_the_wait(self, tmp_path: Path) -> None:
        h, _fake = review_harness(tmp_path)
        run_id = park(h)
        h.loop.abandon_item("gh:issue:1", "not this one")
        hold = h.dstore.review_hold_for(run_id)
        assert hold is not None and hold.state == "dismissed"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "failed"
        assert notices(h, "review.dismissed")
        h.clock.t += 600
        h.loop.tick()
        assert h.dstore.review_hold_for(run_id).state == "dismissed"  # type: ignore[union-attr]

    def test_abandon_works_on_a_paused_wait_too(self, tmp_path: Path) -> None:
        h, _fake = review_harness(tmp_path)
        park(h)
        h.clock.t += 3600
        h.loop.tick()
        item = h.loop.abandon_item("gh:issue:1")
        assert item.state == "failed"


class TestRecovery:
    def test_recover_re_parks_a_run_that_ended_awaiting_review(self, tmp_path: Path) -> None:
        h, _fake = review_harness(tmp_path)
        run_id = park(h)
        h.dstore.set_state("gh:issue:1", "running", h.clock())
        h.loop.recover()
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "awaiting_review"
        hold = h.dstore.review_hold_for(run_id)
        assert hold is not None and hold.state == "open"

    def test_boot_reopens_an_interrupted_landing(self, tmp_path: Path) -> None:
        h, _fake = review_harness(tmp_path)
        run_id = park(h)
        assert h.dstore.claim_review_hold(run_id, "approving")
        h.loop.recover()
        hold = h.dstore.review_hold_for(run_id)
        assert hold is not None and hold.state == "open"
        assert hold.detail is not None and "restart" in hold.detail


class _ResumeLoop:
    def __init__(self) -> None:
        self.resumed: list[tuple[str, str | None]] = []
        self.unpaused: list[str | None] = []

    def resume_review(self, target: str, by: str | None = None) -> str:
        self.resumed.append((target, by))
        if target == "gh:issue:404":
            raise ValueError("'gh:issue:404' is not waiting for a review")
        return "gh:issue:1 is waiting for a review on PR #9 again"

    def unpause(self, hold: str | None, by: str | None = None) -> list[str]:
        self.unpaused.append(hold)
        return []


class TestResumeCommand:
    def test_a_bare_target_resumes_the_review_wait(self) -> None:
        loop = _ResumeLoop()
        reply = dispatch(loop, "resume gh:issue:1", by="brett", via="test")
        assert reply.ok and "PR #9" in reply.text
        assert loop.resumed == [("gh:issue:1", "brett")] and loop.unpaused == []

    def test_the_daemon_holds_still_resume(self) -> None:
        loop = _ResumeLoop()
        assert dispatch(loop, "resume", via="test").ok
        assert dispatch(loop, "resume --hold x", via="test").ok
        assert dispatch(loop, "resume --all", via="test").ok
        assert loop.resumed == [] and len(loop.unpaused) == 3

    def test_a_refusal_comes_back_not_ok(self) -> None:
        reply = dispatch(_ResumeLoop(), "resume gh:issue:404", via="test")
        assert not reply.ok and "not waiting for a review" in reply.text
