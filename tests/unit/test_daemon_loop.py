"""DaemonLoop tick/settle/recover against a fake runner and a fake source.

The engine is replaced by an injectable Runner; the real DaemonStore and
StateStore run on a tmp db so persistence paths are exercised for real.
"""

from __future__ import annotations

import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from sbxloop import hostgit
from sbxloop.config import Config
from sbxloop.daemon import loop as loop_mod
from sbxloop.daemon.loop import DaemonLoop, RunHandle, day_window
from sbxloop.daemon.model import DaemonNotice, RunReport, TaskOutcome, WorkItem
from sbxloop.daemon.sources import IssueComment, IssueContext, LinkedIssue
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import (
    TERMINAL_RUN_STATES,
    Published,
    RunResult,
    TaskOutput,
    TaskRecord,
    TaskSpec,
)
from sbxloop.engine.store import StateStore
from sbxloop.errors import RunCancelledError, SbxError, StateError, WorkerError
from sbxloop.events import Event, EventBus
from tests.unit.test_hostgit import (
    git as git_cmd,
)
from tests.unit.test_hostgit import (
    make_repo,
    make_upstream_and_clone,
    push_upstream_commit,
)

PR_URL = "https://x/pull/9"


class FakeSource:
    name = "github"

    def __init__(self, items: list[WorkItem] | None = None, *, claim_ok: bool = True) -> None:
        self.items = items or []
        self.claim_ok = claim_ok
        self.calls: list[tuple[str, Any]] = []
        # What report_merged / report_blocked answer; a False keeps the debt.
        self.merged_ok = True
        self.blocked_ok = True

    def poll(self) -> list[WorkItem]:
        return list(self.items)

    def claim(self, item: WorkItem) -> bool:
        self.calls.append(("claim", item.item_id))
        if self.claim_ok:
            # The real source swaps the trigger label for in-progress, so a
            # claimed issue stops matching discovery until a human re-adds
            # the label (#600). Without that, every later poll would look
            # like a human asking to restart the item.
            self.items = [i for i in self.items if i.item_id != item.item_id]
        return self.claim_ok

    # Half-claims recovery asks about (#530): token -> whether the claim
    # comment landed on the source.
    settle: dict[str, bool] = {}  # noqa: RUF012 - per-test scripting

    def settle_claim(self, item: WorkItem) -> bool:
        self.calls.append(("settle", item.item_id))
        return bool(item.claim_token and self.settle.get(item.claim_token, False))

    def report_started(self, item: WorkItem, run_id: str) -> None:
        self.calls.append(("started", run_id))

    def report_retry(self, item: WorkItem, error: str, attempts_left: int) -> None:
        self.calls.append(("retry", attempts_left))

    def report_abandoned(self, item: WorkItem, error: str) -> None:
        self.calls.append(("abandoned", error))

    def report_cancelled(self, item: WorkItem, report: RunReport) -> None:
        self.calls.append(("cancelled", report))

    def report_requeued(self, item: WorkItem, by: str) -> None:
        self.calls.append(("requeued", by))

    def report_merged(self, item: WorkItem, pr_number: int | None, pr_url: str) -> bool:
        self.calls.append(("merged", (pr_number, pr_url)))
        return self.merged_ok

    def report_blocked(
        self, item: WorkItem, reason: str, pr_number: int | None, pr_url: str
    ) -> bool:
        self.calls.append(("blocked", (reason, pr_number, pr_url)))
        return self.blocked_ok

    completed_ok = True

    def report_completed(self, item: WorkItem, report: RunReport) -> bool:
        self.calls.append(("completed", report))
        return self.completed_ok


def gh_item(key: str = "1", **overrides: Any) -> WorkItem:
    fields: dict[str, Any] = {
        "item_id": f"gh:{key}",
        "source_key": key,
        "title": f"Do {key}",
        "url": f"https://x/issues/{key}",
    }
    fields.update(overrides)
    return WorkItem(**fields)


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Harness:
    """A DaemonLoop over real stores with a scripted runner."""

    def __init__(self, tmp_path: Path, config: Config | None = None) -> None:
        self.tmp_path = tmp_path
        self.config = config or Config.model_validate(
            {"home": str(tmp_path / "state"), "github": {"repo": "o/r"}}
        )
        self.store = StateStore(self.config.paths.state_db)
        self.dstore = DaemonStore(self.config.paths.state_db)
        self.clock = Clock()
        # per run: "merged" | "failed" | "blocked" | "completed" | "raise" | "die_mid_phase"
        self.outcomes: list[str] = []
        self.runs: list[tuple[str, bool]] = []
        self.run_configs: list[Config] = []  # the config each dispatch handed the runner
        self.source = FakeSource()
        self.loop = DaemonLoop(
            self.config,
            store=self.store,
            dstore=self.dstore,
            source=self.source,
            runner=self.runner,
            clock=self.clock,
        )

    def runner(
        self, item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
    ) -> RunResult:
        self.runs.append((run_id, resume))
        self.run_configs.append(cfg)
        kind = self.outcomes.pop(0) if self.outcomes else "merged"
        if kind == "raise":
            raise WorkerError("sandbox exploded")
        if kind == "die_mid_phase":
            # A run that died *inside* a phase: the row exists and is still
            # non-terminal, which is what a rejected decompose leaves behind.
            self.store.create_run(run_id, "outcome") if not resume else None
            self.store.set_run_state(run_id, "decomposing")
            raise WorkerError("decompose produced invalid output twice")
        if not resume:
            self.store.create_run(run_id, "outcome", kind=item.kind)
        if item.kind == "workload" and kind in ("completed", "held"):
            # The engine's shape for a published workload (#759): tasks
            # with outputs and one `Published` row per sink — or, `held`,
            # the same tasks parked at publishing with nothing delivered
            # (#760).
            self.store.save_tasks(run_id, [TaskSpec(id="t1", title="Answer")])
            (task,) = self.store.get_tasks(run_id)
            task.state, task.output = "done", TaskOutput(summary="the answer is 42")
            self.store.update_task(run_id, task)
            if kind == "held":
                self.store.set_run_state(run_id, "publishing")
            else:
                self.store.add_run_published(
                    run_id, Published(sink="chat", location="chat", tasks=["t1"])
                )
        reason = None
        if kind == "exhausted":
            # The engine's shape for a run one round short (#523): PR open,
            # the review budget spent past its limit, the budget recorded.
            self.store.set_run_pr(
                run_id, number=9, url=PR_URL, branch=f"sbxloop/{run_id}", head_sha="abc"
            )
            self.store.set_run_state(run_id, "reviewing")
            limit = cfg.landing.max_review_rounds + self.store.get_run(run_id).granted_rounds
            while self.store.get_run(run_id).review_rounds < limit:
                self.store.bump_run_counter(run_id, "review_rounds")
            self.store.set_run_exhausted(run_id, "review")
            reason = "review fix rounds exhausted (3 allowed by [landing] review_rounds): x"
            self.store.set_run_reason(run_id, reason)
            self.store.set_run_state(run_id, "failed")
            return RunResult(
                run_id=run_id,
                state="failed",
                pr_number=9,
                pr_url=PR_URL,
                reason=reason,
                exhausted="review",
            )
        if kind in ("merged", "blocked", "gated", "awaiting_review", "held_by_draft"):
            self.store.set_run_pr(
                run_id, number=9, url=PR_URL, branch=f"sbxloop/{run_id}", head_sha="abc"
            )
        if kind in ("awaiting_review", "held_by_draft"):
            # The engine's record of the park (#675): what the base wants —
            # or, `held_by_draft`, a person's draft hold (#677).
            draft = kind == "held_by_draft"
            self.store.append_event(
                Event(
                    ts=self.clock(),
                    run_id=run_id,
                    job_id=None,
                    type="run.awaiting_review",
                    data={
                        "pr": 9,
                        "approvals_required": 0 if draft else 2,
                        "code_owners": False,
                        "draft": draft,
                    },
                )
            )
            kind = "awaiting_review"
        if kind == "blocked":
            reason = "GitHub would not merge it: a protection rule wants an approval"
            self.store.set_run_reason(run_id, reason)
        state = cast(Any, kind)
        self.store.set_run_state(run_id, state)
        record = self.store.get_run(run_id)
        return RunResult(
            run_id=run_id,
            state=state,
            pr_number=record.pr_number,
            pr_url=record.pr_url,
            reason=reason,
        )


class RecordingFrontend:
    def __init__(self) -> None:
        self.seen: list[str] = []
        self.notices: list[DaemonNotice] = []
        self.finished: list[tuple[WorkItem, RunReport]] = []

    def daemon_notice(self, notice: DaemonNotice) -> None:
        self.notices.append(notice)
        self.seen.append(notice.text)

    def run_started(self, *a: Any) -> None: ...

    def run_finished(self, item: WorkItem, report: RunReport) -> None:
        self.finished.append((item, report))


class TestTick:
    def test_one_item_merges_end_to_end(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        result = h.loop.tick()
        assert result.dispatched == "gh:issue:1" and result.outcome == "done"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "done" and item.pending_report is None
        kinds = [c[0] for c in h.source.calls]
        assert kinds == ["claim", "started", "merged"]
        assert h.source.calls[-1][1] == (9, PR_URL)
        # ledger row closed; a second tick finds nothing new
        assert h.dstore.runs_started_since(0) == 1
        assert h.loop.tick().idle_reason == "no_work"

    def test_daily_cap_blocks_dispatch(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"max_runs_per_day": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [gh_item("1"), gh_item("2")]
        assert h.loop.tick().outcome == "done"
        second = h.loop.tick()
        assert second.idle_reason == "daily_cap" and second.dispatched is None
        assert h.dstore.get("gh:issue:2").state == "queued"  # type: ignore[union-attr]
        # the calendar day rolls (local midnight passes) → dispatch resumes
        h.clock.t = day_window(h.clock.t, cfg.daemon.run_cap_timezone)[1]
        assert h.loop.tick().outcome == "done"

    def test_retry_then_give_up_at_cap(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "daemon": {"max_attempts_per_item": 2, "retry_backoff_s": 10},
            }
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [gh_item()]
        h.outcomes = ["raise", "failed"]
        assert h.loop.tick().outcome == "retry"
        assert h.dstore.get("gh:issue:1").state == "queued"  # type: ignore[union-attr]
        assert ("retry", 1) in h.source.calls
        # backoff not elapsed → no dispatch, and the idle reason says so
        reason = h.loop.tick().idle_reason
        assert reason is not None and reason.startswith("backoff (1 queued")
        h.clock.t += 11
        assert h.loop.tick().outcome == "failed"
        assert h.dstore.get("gh:issue:1").state == "failed"  # type: ignore[union-attr]
        assert any(c[0] == "abandoned" for c in h.source.calls)
        # claim happened once: retries reuse the claimed item
        assert [c for c in h.source.calls if c[0] == "claim"] == [("claim", "gh:issue:1")]

    def test_claim_failure_forgets_the_item_without_running(self, tmp_path: Path) -> None:
        """A claim that is not ours — lost race, closed issue, label gone —
        leaves no row at all (#530): a terminal `failed` row is what
        discovery dedups against, which made a lost race permanent. The
        next poll re-creates the row if the trigger label is still there."""
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        h.source.claim_ok = False
        assert h.loop.tick().outcome == "failed"
        assert h.runs == []
        assert h.dstore.get("gh:issue:1") is None
        # Still triggered on the source: rediscovered and claimed on the next poll.
        h.source.claim_ok = True
        assert h.loop.tick().outcome == "done"
        assert [c for c in h.source.calls if c[0] == "claim"] == [
            ("claim", "gh:issue:1"),
            ("claim", "gh:issue:1"),
        ]

    def test_an_engine_failure_at_delivery_is_a_failed_attempt(self, tmp_path: Path) -> None:
        """Delivery is a stage of the run now: an error there propagates
        like any infra failure, and the item is retried (the resumed run
        re-delivers)."""
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        h.outcomes = ["raise"]
        assert h.loop.tick().outcome == "retry"
        assert h.dstore.get("gh:issue:1").state == "queued"  # type: ignore[union-attr]
        assert any(c[0] == "retry" for c in h.source.calls)

    def test_circuit_breaker_opens_cools_and_resets(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "daemon": {
                    "max_attempts_per_item": 1,
                    "max_consecutive_failures": 2,
                    "breaker_cooldown_s": 100,
                    "max_runs_per_day": 100,
                },
            }
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [gh_item("1"), gh_item("2"), gh_item("3")]
        h.outcomes = ["raise", "raise", "merged"]
        assert h.loop.tick().outcome == "failed"
        assert h.loop.tick().outcome == "failed"
        assert h.loop.tick().idle_reason == "breaker"
        h.clock.t += 101
        assert h.loop.tick().outcome == "done"  # half-open let one through; success
        assert h.loop.status()["breaker_open"] is False

    def test_paused_loop_idles(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        h.loop.pause()
        assert h.loop.tick().idle_reason == "paused"
        h.loop.unpause()
        assert h.loop.tick().outcome == "done"


class TestGuardrailsAreDaemonWide:
    """The daily run cap, the circuit breaker and one-run-at-a-time belong to
    the daemon, not to a repository: with several repos registered, items from
    *different* repositories share one budget (#511)."""

    @staticmethod
    def _multi_repo(tmp_path: Path, daemon: dict[str, Any]) -> Config:
        return Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repos": [{"repo": "o/a"}, {"repo": "o/b"}]},
                "daemon": daemon,
            }
        )

    def test_daily_cap_counts_runs_across_repositories(self, tmp_path: Path) -> None:
        cfg = self._multi_repo(tmp_path, {"max_runs_per_day": 1})
        h = Harness(tmp_path, cfg)
        h.source.items = [
            gh_item("1", item_id="gh:o/a:issue:1", repo="o/a"),
            gh_item("2", item_id="gh:o/b:issue:2", repo="o/b"),
        ]
        assert h.loop.tick().outcome == "done"
        # The second item is a different repository — the cap does not reset.
        second = h.loop.tick()
        assert second.idle_reason == "daily_cap" and second.dispatched is None
        assert h.dstore.get("gh:o/b:issue:2").state == "queued"  # type: ignore[union-attr]
        h.clock.t = day_window(h.clock.t, cfg.daemon.run_cap_timezone)[1]
        assert h.loop.tick().dispatched == "gh:o/b:issue:2"

    def test_breaker_counts_failures_across_repositories(self, tmp_path: Path) -> None:
        cfg = self._multi_repo(
            tmp_path,
            {
                "max_attempts_per_item": 1,
                "max_consecutive_failures": 2,
                "breaker_cooldown_s": 100,
                "max_runs_per_day": 100,
            },
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [
            gh_item("1", item_id="gh:o/a:issue:1", repo="o/a"),
            gh_item("2", item_id="gh:o/b:issue:2", repo="o/b"),
            gh_item("3", item_id="gh:o/a:issue:3", repo="o/a"),
        ]
        h.outcomes = ["raise", "raise", "merged"]
        # One failure in o/a plus one in o/b trips a breaker of 2.
        assert h.loop.tick().outcome == "failed"
        assert h.loop.tick().outcome == "failed"
        assert h.loop.tick().idle_reason == "breaker"
        assert h.loop.status()["consecutive_failures"] == 2
        h.clock.t += 101
        assert h.loop.tick().outcome == "done"
        assert h.loop.status()["breaker_open"] is False


class TestSettle:
    """How the run ended decides what happens to the item — and nothing else
    does: there are no lanes to feed."""

    def test_merged_closes_the_issue_and_resets_the_breaker(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.set_breaker(None, 2)
        h.loop._breaker_opened_at, h.loop._consecutive_failures = h.dstore.breaker()
        h.source.items = [gh_item()]
        front = RecordingFrontend()
        h.loop.frontend = front  # type: ignore[assignment]
        assert h.loop.tick().outcome == "done"
        assert h.loop._consecutive_failures == 0
        done = [n for n in front.notices if n.kind == "run.done"]
        assert len(done) == 1 and done[0].url == PR_URL and done[0].run_id == h.runs[0][0]
        assert "🎉 gh:issue:1 merged" in done[0].text
        (_, report) = front.finished[0]
        assert report.state == "merged" and report.pr == (9, PR_URL) and report.succeeded

    def test_blocked_hands_over_without_a_retry_or_a_breaker_count(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        h.outcomes = ["blocked"]
        front = RecordingFrontend()
        h.loop.frontend = front  # type: ignore[assignment]
        assert h.loop.tick().outcome == "blocked"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "blocked" and item.pending_report is None
        assert item.last_error is not None and "protection rule" in item.last_error
        assert h.loop._consecutive_failures == 0
        kinds = [c[0] for c in h.source.calls]
        assert kinds == ["claim", "started", "blocked"]
        reason, number, url = h.source.calls[-1][1]
        assert number == 9 and url == PR_URL and "protection rule" in reason
        blocked = [n for n in front.notices if n.kind == "run.blocked"]
        assert blocked and blocked[0].level == "error" and blocked[0].url == PR_URL
        # Not retried: the queue is empty.
        h.clock.t += 100_000
        assert h.loop.tick().idle_reason == "no_work"
        # ...but a human can retry it.
        h.loop.retry_item("gh:issue:1", "op")
        assert h.loop.tick().outcome == "done"

    def test_a_failed_close_stays_owed_and_is_retried_next_tick(self, tmp_path: Path) -> None:
        """The issue close is a debt until GitHub confirms it: a hiccup on the
        close must not leave the issue open with nobody to close it."""
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        h.source.merged_ok = False
        assert h.loop.tick().outcome == "done"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "done" and item.pending_report == "merged"
        assert [c[0] for c in h.source.calls].count("merged") == 1
        h.source.merged_ok = True
        h.loop.pause()  # the sweep runs before every gate
        assert h.loop.tick().idle_reason == "paused"
        assert [c[0] for c in h.source.calls].count("merged") == 2
        assert h.dstore.get("gh:issue:1").pending_report is None  # type: ignore[union-attr]
        h.loop.unpause()
        h.loop.tick()
        assert [c[0] for c in h.source.calls].count("merged") == 2  # paid exactly once

    def test_a_failed_blocked_report_is_retried_too(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        h.outcomes = ["blocked"]
        h.source.blocked_ok = False
        assert h.loop.tick().outcome == "blocked"
        assert h.dstore.get("gh:issue:1").pending_report == "blocked"  # type: ignore[union-attr]
        h.source.blocked_ok = True
        h.loop.tick()
        assert h.dstore.get("gh:issue:1").pending_report is None  # type: ignore[union-attr]
        assert [c[0] for c in h.source.calls].count("blocked") == 2

    def test_completed_with_a_repository_is_blocked_not_done(self, tmp_path: Path) -> None:
        """With a repository configured a run must end merged; `completed`
        means it stopped before landing and a human has to look."""
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        h.outcomes = ["completed"]
        assert h.loop.tick().outcome == "blocked"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "blocked"
        assert item.last_error == "run ended completed without landing"

    def test_completed_without_a_repository_is_done(self, tmp_path: Path) -> None:
        cfg = Config.model_validate({"home": str(tmp_path / "state")})
        h = Harness(tmp_path, cfg)
        h.source.items = [gh_item()]
        h.outcomes = ["completed"]
        assert h.loop.tick().outcome == "done"
        assert h.source.calls[-1] == ("merged", (None, ""))

    def test_a_failed_run_carries_its_reason(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"max_attempts_per_item": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [gh_item()]

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            h.runs.append((run_id, resume))
            h.store.create_run(run_id, "outcome")
            h.store.set_run_reason(run_id, "review fix rounds exhausted (3 allowed)")
            h.store.set_run_state(run_id, "failed")
            return RunResult(
                run_id=run_id, state="failed", reason="review fix rounds exhausted (3 allowed)"
            )

        h.loop._runner = runner
        assert h.loop.tick().outcome == "failed"
        assert h.source.calls[-1] == ("abandoned", "review fix rounds exhausted (3 allowed)")


class TestOutcomeAndConfig:
    def test_outcome_text_is_the_issue_plus_provenance(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        gh = gh_item("4", title="Fix it", body="details\n\n<!-- sbxloop-claim abc -->\n")
        text = h.loop.outcome_text(gh)
        assert text.startswith("Fix it\n\ndetails")
        assert "GitHub issue #4 in o/r (https://x/issues/4)" in text
        assert "sbxloop-claim" not in text
        assert "backlog" not in text and "AUDIT" not in text
        # A source with no discussion to offer changes nothing.
        assert "Discussion" not in text and "Linked" not in text


class ContextSource(FakeSource):
    """A source whose issues carry a discussion (#691)."""

    def __init__(self) -> None:
        super().__init__()
        self.context = IssueContext(
            comments=(
                IssueComment("alice", "2026-08-30", "Actually the repro is `make check`."),
                IssueComment("bob", "2026-08-31", "Keep the API shape from #9."),
                IssueComment("carol", "2026-09-01", "Agreed; ship it."),
            ),
            omitted=0,
            linked=(
                LinkedIssue(123, "The earlier fix", "closed", "We did it this way.", "issue"),
                LinkedIssue(9, "API reshape", "open", "", "pull request"),
            ),
        )
        self.asked: list[tuple[str, Any]] = []
        self.fail: Exception | None = None

    def issue_context(self, item: WorkItem, **kwargs: Any) -> IssueContext:
        self.asked.append((item.item_id, kwargs.get("own")))
        if self.fail is not None:
            raise self.fail
        return self.context


class TestOutcomeContext:
    """The outcome carries the issue's discussion and linked issues (#691):
    the substance of a real tracker's issue is in its comments."""

    def harness(self, tmp_path: Path, **budgets: Any) -> Harness:
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "github": {"repo": "o/r"}, "budgets": budgets}
        )
        h = Harness(tmp_path, cfg)
        h.source = ContextSource()
        h.loop.source = h.source
        return h

    def test_the_discussion_and_the_links_follow_the_body(self, tmp_path: Path) -> None:
        h = self.harness(tmp_path)
        text = h.loop.outcome_text(gh_item("4", title="Fix it", body="details"))
        assert text.startswith("Fix it\n\ndetails\n\n## Discussion (3 comments)\n\n")
        assert "**@alice** (2026-08-30): Actually the repro is `make check`." in text
        assert "**@carol** (2026-09-01): Agreed; ship it." in text
        assert "## Linked issues\n- #123 (closed) — The earlier fix: We did it this way.\n" in text
        assert "- #9 (open pull request) — API reshape\n" in text
        assert text.endswith(
            "---\nThis work item came from: GitHub issue #4 in o/r (https://x/issues/4)."
        )
        # Without a GitHub client the loop has no identity to hand the
        # source: it asks with the unknown one rather than not at all.
        assert h.source.asked == [("gh:issue:4", ("", None))]

    def test_the_loops_identity_is_handed_to_the_source(self, tmp_path: Path) -> None:
        """With a GitHub client the loop resolves who it is — the App slug
        here — so the source can leave its comments out; a client that
        cannot answer costs the run nothing but that exclusion."""

        class Provisioner:
            login: str | None = "sbxloop[bot]"

            def gh_bot_login(self, repo: str | None = None) -> str | None:
                return self.login

        class Ops:
            def raw(self, method: str, path: str, body: Any = None) -> Any:
                raise WorkerError("sandbox gone")

        class Github:
            provisioner = Provisioner()
            failures: list[str] = []  # noqa: RUF012 - per-test scripting

            def ops(self) -> Any:
                return Ops()

            def note_failure(self, exc: BaseException) -> bool:
                self.failures.append(str(exc))
                return False

        h = self.harness(tmp_path)
        h.loop.github = Github()  # type: ignore[assignment]
        h.loop.outcome_text(gh_item("4"))
        assert h.source.asked[-1] == ("gh:issue:4", ("sbxloop[bot]", True))
        assert Github.failures == []
        Github.provisioner.login = None
        text = h.loop.outcome_text(gh_item("4"))
        assert h.source.asked[-1] == ("gh:issue:4", ("", None))
        assert Github.failures == ["sandbox gone"]
        assert "**@alice**" in text

    def test_omitted_comments_are_counted(self, tmp_path: Path) -> None:
        h = self.harness(tmp_path)
        h.source.context = h.source.context._replace(omitted=7, linked=())
        text = h.loop.outcome_text(gh_item("4"))
        assert (
            "## Discussion (10 comments)\n\n(7 earlier comments omitted; the latest are shown)"
            in text
        )
        assert "Linked issues" not in text

    def test_the_budget_cuts_the_discussion_and_says_so(self, tmp_path: Path) -> None:
        h = self.harness(tmp_path, outcome_max_chars=1_000)
        h.source.context = h.source.context._replace(
            comments=tuple(IssueComment("alice", "2026-08-30", "x" * 300) for _ in range(6))
        )
        body = "b" * 200
        text = h.loop.outcome_text(gh_item("4", title="Fix it", body=body))
        assert len(text) <= 1_000
        assert text.startswith(f"Fix it\n\n{body}\n\n## Discussion (6 comments)")
        assert "(discussion clipped by [budgets] outcome_max_chars=1000: " in text
        assert "chars not shown — the issue on GitHub has the rest)" in text
        assert text.endswith(
            "This work item came from: GitHub issue #4 in o/r (https://x/issues/4)."
        )
        # Linked issues sit after the discussion, so they are what the cut takes first.
        assert "Linked issues" not in text

    def test_the_body_is_never_cut_even_past_the_budget(self, tmp_path: Path) -> None:
        h = self.harness(tmp_path, outcome_max_chars=1_000)
        body = "b" * 1_500
        text = h.loop.outcome_text(gh_item("4", title="Fix it", body=body))
        assert body in text
        assert "(discussion clipped by [budgets] outcome_max_chars=1000: " in text
        assert "**@alice**" not in text

    def test_an_unreadable_discussion_is_said_not_hidden(self, tmp_path: Path) -> None:
        h = self.harness(tmp_path)
        h.source.fail = SbxError("github op raw.api failed: HTTP 502")
        text = h.loop.outcome_text(gh_item("4", title="Fix it", body="details"))
        assert text.startswith(
            "Fix it\n\ndetails\n\n(The issue's comments could not be read — SbxError: "
        )
        assert "HTTP 502" in text and "the title and body above are the whole ask" in text
        assert text.endswith("(https://x/issues/4).")

    def test_item_config_pins_the_issue_and_nothing_else(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repo": "o/r", "create_repo": True},
                "keep_on_failure": True,
            }
        )
        h = Harness(tmp_path, cfg)
        gh = h.loop._item_config(gh_item("1"))
        assert gh.github.repo == "o/r" and gh.github.deliver_closes == 1
        assert gh.github.create_repo is False and gh.keep_on_failure is False
        assert gh.sandbox.continue_branch is None
        assert gh.landing == cfg.landing

    def test_deliver_closes_needs_a_numeric_key(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        assert h.loop._item_config(gh_item("x")).github.deliver_closes is None

    def test_dispatch_hands_the_runner_the_pinned_issue(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item("12")]
        assert h.loop.tick().outcome == "done"
        assert h.run_configs[0].github.deliver_closes == 12


class TestOperatorCancel:
    """#246: a Discord `!sbx cancel` was settled as a failed attempt — retried
    fresh after the backoff and counted toward the breaker. An operator
    cancel is a decision, not a failure."""

    @staticmethod
    def _run_until_cancelled(
        h: Harness,
        *,
        requester: str | None = None,
        retry: bool = False,
        error: type[Exception] = RunCancelledError,
    ) -> None:
        started = threading.Event()
        cancelled = threading.Event()

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            h.runs.append((run_id, resume))
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "building")  # mid-flight → resumable
            started.set()
            assert cancelled.wait(5)
            # By default: what the engine raises at the next boundary after
            # request_cancel. Tests may substitute an infra error to model a
            # failure racing with the cancel.
            raise error(f"run {run_id} interrupted; resume with `sbxloop resume {run_id}`")

        h.loop._runner = runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        assert h.loop.cancel_current(requester, retry=retry) is True
        cancelled.set()
        t.join(5)

    def test_cancel_settles_item_as_cancelled_not_failed(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "daemon": {"max_attempts_per_item": 3, "max_consecutive_failures": 1},
            }
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [gh_item()]
        self._run_until_cancelled(h, requester="Discord user `brett`")
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "cancelled"
        assert item.last_error == "cancelled by Discord user `brett`"
        # Not a failure: no retry report, no breaker count (limit is 1 here).
        kinds = [c[0] for c in h.source.calls]
        assert kinds == ["claim", "started", "cancelled"]
        assert h.loop.status()["breaker_open"] is False
        report = h.source.calls[-1][1]
        assert report.state == "cancelled" and report.cancelled_by == "Discord user `brett`"
        assert report.requeued is False
        # The run record itself ends terminal (#374) with the operator in its
        # reason — it stays resumable, but nothing reports it as active.
        record = h.store.get_run(h.runs[0][0])
        assert record.state == "cancelled"
        assert record.reason is not None and "Discord user `brett`" in record.reason
        h.clock.t += 100_000
        assert h.loop.tick().idle_reason == "no_work"
        assert len(h.runs) == 1

    def test_cancel_with_retry_requeues_without_backoff(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"retry_backoff_s": 900}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [gh_item()]
        self._run_until_cancelled(h, retry=True)
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued" and item.attempts == 0
        report = h.source.calls[-1][1]
        assert report.requeued is True and report.cancelled_by == "operator"
        # Eligible immediately: a human asked, so no failure backoff applies.
        h.loop._runner = h.runner
        assert h.loop.tick().outcome == "done"
        assert h.runs[1][1] is False  # a fresh run, not a resume

    def test_cancel_retry_leaves_run_cancelled_and_item_queued(self, tmp_path: Path) -> None:
        """#374: `cancel --retry` requeues the item but the run record must
        still end terminal."""
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        self._run_until_cancelled(h, requester="Discord user `brett`", retry=True)
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued"
        record = h.store.get_run(h.runs[0][0])
        assert record.state == "cancelled"
        assert record.reason is not None and "Discord user `brett`" in record.reason

    def test_cancel_appends_chronology_event(self, tmp_path: Path) -> None:
        """Reconciliation chronology is append-only: existing events keep
        their order and a `run.cancelled` event is added."""
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        self._run_until_cancelled(h, requester="Discord user `brett`")
        run_id = h.runs[0][0]
        events = [e for _, e in h.store.events(run_id)]
        assert events[-1].type == "run.cancelled"
        assert events[-1].data.get("by") == "Discord user `brett`"
        # append-only: nothing before the new event was rewritten
        assert [e.type for e in events[:-1]] == [
            e.type for e in events if e.type != "run.cancelled"
        ]

    def test_abandon_current_run_ends_run_terminal(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        started = threading.Event()
        release = threading.Event()

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            h.runs.append((run_id, resume))
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "building")
            started.set()
            assert release.wait(5)
            raise RunCancelledError("cancelled")

        h.loop._runner = runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        h.loop.abandon_item("gh:issue:1", "operator says stop")
        release.set()
        t.join(5)
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "failed"
        assert h.store.get_run(h.runs[0][0]).state in TERMINAL_RUN_STATES

    def test_requeue_of_pinned_dead_run_ends_run_terminal(self, tmp_path: Path) -> None:
        """A pinned run that is not in flight is dead; requeue must not leave
        it `building`."""
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), h.clock())
        h.dstore.mark_claimed("gh:issue:1", h.clock())
        h.dstore.mark_running("gh:issue:1", "rdead", h.clock())
        h.store.create_run("rdead", "x")
        h.store.set_run_state("rdead", "building")
        h.loop.requeue_item("gh:issue:1")
        record = h.store.get_run("rdead")
        assert record.state == "cancelled"
        assert record.reason is not None and "never resumed" in record.reason

    def test_infra_error_racing_a_cancel_is_still_a_failure(self, tmp_path: Path) -> None:
        """Review: an infra error is re-raised while the run is still resumable
        (engine keeps it resumable on purpose), so persisted state alone cannot
        tell it from a cancel that took effect. Only the engine's cancellation
        error settles as cancelled; a real failure that races the request keeps
        its retry/breaker accounting."""
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        self._run_until_cancelled(h, error=WorkerError)
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued" and item.attempts == 1
        assert h.loop._consecutive_failures == 1
        assert [c[0] for c in h.source.calls] == ["claim", "started", "retry"]

    def test_stale_cancel_never_taints_a_later_run(self, tmp_path: Path) -> None:
        """A cancel that lands after the engine already finished settles that
        run normally and must not carry over to the next item."""
        h = Harness(tmp_path)
        h.source.items = [gh_item("1"), gh_item("2")]
        started = threading.Event()
        release = threading.Event()

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            started.set()
            release.wait(5)
            return h.runner(item, cfg, run_id, bus, resume)

        h.loop._runner = runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        assert h.loop.cancel_current("late")
        release.set()
        t.join(5)
        assert h.dstore.get("gh:issue:1").state == "done"  # type: ignore[union-attr]
        h.loop._runner = h.runner
        h.outcomes = ["raise"]
        assert h.loop.tick().outcome == "retry"  # gh:issue:2 fails as a failure, not a cancel
        assert not any(c[0] == "cancelled" for c in h.source.calls)

    def test_retry_settled_item_is_attributed(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"max_attempts_per_item": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [gh_item()]
        h.outcomes = ["raise"]
        assert h.loop.tick().outcome == "failed"
        with pytest.raises(KeyError):
            h.loop.retry_item("gh:nope")
        h.loop.retry_item("gh:issue:1", "Discord user `brett`")
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued" and item.attempts == 0
        assert item.last_error == "re-queued by Discord user `brett`"
        assert ("requeued", "Discord user `brett`") in h.source.calls
        assert h.loop.tick().outcome == "done"

    def test_retry_of_cancelled_item_runs_fresh(self, tmp_path: Path) -> None:
        """#246 + #229: `!sbx retry` is the way back from a cancel — attempts
        reset, run unpinned so the next tick starts over, not resumes."""
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        h.dstore.upsert_new(gh_item(), 1.0)
        h.dstore.mark_running("gh:issue:1", "r1", 1.0)
        h.dstore.mark_cancelled("gh:issue:1", "cancelled by op", 2.0)
        with pytest.raises(ValueError, match="use retry"):
            h.loop.requeue_item("gh:issue:1")
        got = h.loop.retry_item("gh:issue:1", "op")
        assert got.state == "queued" and got.attempts == 0 and got.run_id is None

    def test_operator_controls_accept_legacy_ids(self, tmp_path: Path) -> None:
        """#508: an operator (or an old watch) may still say `gh:1`. The loop
        normalises at the boundary, so it resolves the same row and every id
        it hands back and logs is typed."""
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), 1.0)
        assert h.loop.requeue_item("gh:1").item_id == "gh:issue:1"
        h.dstore.mark_running("gh:issue:1", "r1", 1.0)
        h.dstore.mark_cancelled("gh:issue:1", "cancelled by op", 2.0)
        assert h.loop.retry_item("gh:1", "op").item_id == "gh:issue:1"
        assert h.loop.abandon_item("gh:1", "enough").item_id == "gh:issue:1"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "failed"


class TestShutdownAndRecovery:
    def test_shutdown_mid_run_leaves_item_running(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        started = threading.Event()
        release = threading.Event()

        def slow_runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            started.set()
            release.wait(5)
            raise WorkerError("cancelled at boundary")

        h.loop._runner = slow_runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        assert h.loop.current is not None and h.loop.current.item.item_id == "gh:issue:1"
        h.loop.request_stop()
        release.set()
        t.join(5)
        assert h.dstore.get("gh:issue:1").state == "running"  # type: ignore[union-attr]
        assert h.loop.current is None

    def test_genuine_failure_after_stop_requested_still_settles(self, tmp_path: Path) -> None:
        """Review: stop-requested + any exception was treated as 'interrupted',
        masking real failures and leaving the item running. Only a run whose
        persisted state is still resumable is interrupted; a run that
        actually failed settles as a failure."""
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"max_attempts_per_item": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [gh_item()]
        started = threading.Event()

        def failing_runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            started.set()
            # stop gets requested while we run...
            while not h.loop.stopping:
                time.sleep(0.01)
            # ...and then the run FAILS for real (terminal persisted state)
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "failed")
            raise WorkerError("worker exploded, not a cancel")

        h.loop._runner = failing_runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        h.loop.request_stop()
        t.join(5)
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "failed"  # settled, not left running
        assert any(c[0] == "abandoned" for c in h.source.calls)

    def test_cancel_at_boundary_after_stop_is_interrupted(self, tmp_path: Path) -> None:
        """The genuine shutdown case: the run stays resumable (non-terminal
        persisted state) so the item is left running for recovery."""
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        started = threading.Event()

        def cancelled_runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            started.set()
            while not h.loop.stopping:
                time.sleep(0.01)
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "awaiting_ci")  # still resumable
            raise StateError("run cancelled at a boundary")

        h.loop._runner = cancelled_runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        h.loop.request_stop()
        t.join(5)
        assert h.dstore.get("gh:issue:1").state == "running"  # type: ignore[union-attr]

    def test_recover_merged_run_settles(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_done", now=2.0)
        h.store.create_run("r_done", "x")
        h.store.set_run_pr("r_done", number=3, url="https://x/pull/3", branch="b", head_sha="s")
        h.store.set_run_state("r_done", "merged")
        h.loop.recover()
        assert h.dstore.get("gh:issue:1").state == "done"  # type: ignore[union-attr]
        assert ("merged", (3, "https://x/pull/3")) in h.source.calls
        assert h.runs == []  # nothing re-ran

    def test_recover_blocked_run_hands_over(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_blk", now=2.0)
        h.store.create_run("r_blk", "x")
        h.store.set_run_reason("r_blk", "its draft status could not be cleared")
        h.store.set_run_state("r_blk", "blocked")
        h.loop.recover()
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "blocked"
        assert item.last_error == "its draft status could not be cleared"
        assert any(c[0] == "blocked" for c in h.source.calls)

    def test_recover_nonterminal_run_resumes_same_attempt(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_live", now=2.0)
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "building")
        h.outcomes = ["merged"]
        h.loop.recover()
        # Recovery only queues the resume (run pinned); the tick runs it.
        pending = h.dstore.get("gh:issue:1")
        assert pending is not None and pending.state == "queued" and pending.run_id == "r_live"
        assert h.runs == []
        assert h.loop.tick().outcome == "done"
        assert h.runs == [("r_live", True)]  # resumed, not restarted
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "done" and item.attempts == 1
        # A resume is the same attempt but a fresh engine wall clock: the
        # daily cap sees it (#254/#234).
        assert h.dstore.runs_started_since(0) == 2
        assert h.loop.status()["resumes_today"] == 1

    def test_recover_resumes_a_run_interrupted_in_the_pipeline(self, tmp_path: Path) -> None:
        """A crash during a CI wait is a resume at that stage, not a fresh
        attempt: the item stays pinned to its run."""
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_ci", now=2.0)
        h.store.create_run("r_ci", "x")
        h.store.set_run_pr("r_ci", number=9, url=PR_URL, branch="b", head_sha="s")
        h.store.set_run_state("r_ci", "awaiting_ci")
        h.loop.recover()
        pending = h.dstore.get("gh:issue:1")
        assert pending is not None and pending.state == "queued" and pending.run_id == "r_ci"
        assert h.loop.tick().outcome == "done"
        assert h.runs == [("r_ci", True)]

    def test_recover_removes_stale_run_sandboxes_and_secrets_before_resume(
        self, tmp_path: Path
    ) -> None:
        """A killed process leaves its microVMs alive; resume re-provisions
        under the same names and sbx refuses an existing name (field
        failure r6pgvatsd). Removing the sandbox alone is not enough: the
        secret registrations survive ``sbx rm``, the re-provision cannot
        replace them, and the agent boots with the proxy sentinel — Copilot
        SDK 401 (field failure rgn9ccjam). Both must go."""
        h = Harness(tmp_path)
        calls: list[tuple[str, Any]] = []

        class FakeSbx:
            def stop(self, name: str) -> None:
                calls.append(("stop", name))

            def rm(self, name: str, **kwargs: Any) -> None:
                calls.append(("rm", name))

            def secret_rm(self, **kwargs: Any) -> bool:
                calls.append(("secret_rm", kwargs))
                return True

        h.loop.sbx = FakeSbx()  # type: ignore[assignment]
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_live", now=2.0)
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "building")
        h.outcomes = ["merged"]
        h.loop.recover()
        h.loop.tick()
        agent, gh = "sbxloop-r_live-agent", "sbxloop-r_live-github"
        assert [c for c in calls if c[0] == "rm"] == [("rm", agent), ("rm", gh)]
        secret_calls = [c[1] for c in calls if c[0] == "secret_rm"]
        # Agent: every backend's custom secret (host+env — sbx rejects env-only
        # selection), because prune has no config and the backend may have
        # changed since provisioning (#617); github: the built-in service secret.
        assert secret_calls == [
            {"host": "api.github.com", "env": "COPILOT_GITHUB_TOKEN", "sandbox": agent},
            {"host": "api.anthropic.com", "env": "ANTHROPIC_API_KEY", "sandbox": agent},
            {"service": "github", "sandbox": gh},
        ]
        assert h.runs == [("r_live", True)]

    def test_recover_clears_secrets_even_when_sandbox_is_gone(self, tmp_path: Path) -> None:
        """The common case: the sandbox was already torn down but its
        secret registration lingered (rollback race). Recovery still
        clears the registration so resume can provision cleanly."""
        h = Harness(tmp_path)
        calls: list[tuple[str, Any]] = []

        class FakeSbx:
            def stop(self, name: str) -> None:
                raise SbxError("no such sandbox")

            def rm(self, name: str, **kwargs: Any) -> None:
                raise SbxError("no such sandbox")

            def secret_rm(self, **kwargs: Any) -> bool:
                calls.append(("secret_rm", kwargs))
                return False

        h.loop.sbx = FakeSbx()  # type: ignore[assignment]
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_live", now=2.0)
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "building")
        h.outcomes = ["merged"]
        h.loop.recover()
        h.loop.tick()
        # One agent secret_rm per backend (#617) plus the github service secret.
        assert len(calls) == 3
        assert h.runs == [("r_live", True)]

    @staticmethod
    def _interrupted(h: Harness, run_id: str = "r_live") -> None:
        now = h.clock()
        h.dstore.upsert_new(gh_item(), now=now)
        h.dstore.mark_claimed("gh:issue:1", now=now)
        h.dstore.mark_running("gh:issue:1", run_id, now=now)
        h.store.create_run(run_id, "x")
        h.store.set_run_state(run_id, "building")

    def test_recovered_resume_waits_behind_pause_breaker_and_cap(self, tmp_path: Path) -> None:
        """recover() used to dispatch resumes directly, skipping every
        guardrail tick() enforces (#254): a daemon restarting into an open
        breaker, a spent cap, or an operator pause resumed anyway."""
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"max_runs_per_day": 1}}
        )
        h = Harness(tmp_path, cfg)
        self._interrupted(h)
        h.outcomes = ["merged"]
        h.loop.recover()
        h.loop.pause()
        assert h.loop.tick().idle_reason == "paused" and h.runs == []
        h.loop.unpause()
        h.dstore.set_breaker(h.clock(), 3)
        h.loop._breaker_opened_at, h.loop._consecutive_failures = h.dstore.breaker()
        assert h.loop.tick().idle_reason == "breaker" and h.runs == []
        h.dstore.set_breaker(None, 0)
        h.loop._breaker_opened_at = None
        # The interrupted run's own start already counts against the cap of 1.
        assert h.loop.tick().idle_reason == "daily_cap" and h.runs == []
        h.clock.t += 86400 + 1
        assert h.loop.tick().outcome == "done"
        assert h.runs == [("r_live", True)]

    def test_resume_budget_exhausted_settles_as_failed_attempt(self, tmp_path: Path) -> None:
        """A plan that keeps getting interrupted burns a fresh engine wall
        clock per resume while never touching the attempt cap (#234): past
        ``max_resumes_per_item`` the interrupted run is a failed attempt."""
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "daemon": {"max_resumes_per_item": 1, "max_attempts_per_item": 2},
            }
        )
        h = Harness(tmp_path, cfg)
        self._interrupted(h)
        h.dstore.mark_resuming("gh:issue:1", "r_live", now=h.clock())  # one resume spent
        h.loop.recover()
        result = h.loop.tick()
        assert result.outcome == "retry" and h.runs == []  # not resumed
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued" and item.run_id is None
        assert any(c[0] == "retry" for c in h.source.calls)
        # The next tick (past the retry backoff) is a FRESH dispatch,
        # counting as attempt 2.
        h.clock.t += cfg.daemon.retry_backoff_s + 1
        h.outcomes = ["merged"]
        assert h.loop.tick().outcome == "done"
        assert len(h.runs) == 1 and h.runs[0][1] is False
        assert h.dstore.get("gh:issue:1").attempts == 2  # type: ignore[union-attr]

    def test_zero_resume_budget_never_resumes(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"max_resumes_per_item": 0}}
        )
        h = Harness(tmp_path, cfg)
        self._interrupted(h)
        h.loop.recover()
        assert h.loop.tick().outcome == "retry" and h.runs == []

    def test_breaker_state_survives_restart(self, tmp_path: Path) -> None:
        """The breaker used to be instance state: a crash-restart loop
        reset it every time and the "pause dispatch" it promised never
        happened (#254)."""
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"max_consecutive_failures": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [gh_item("1")]
        h.outcomes = ["failed"]
        assert h.loop.tick().outcome == "retry"
        assert h.loop.status()["breaker_open"] is True
        # A new loop over the same store (a restarted daemon) sees it.
        again = DaemonLoop(
            cfg,
            store=h.store,
            dstore=h.dstore,
            source=h.source,
            runner=h.runner,
            clock=h.clock,
        )
        assert again.tick().idle_reason == "breaker"
        assert again.status()["consecutive_failures"] == 1
        h.clock.t += cfg.daemon.breaker_cooldown_s + 1
        assert again.tick().idle_reason != "breaker"


class TestSourceBackoff:
    def test_failing_source_is_backed_off_then_recovers(self, tmp_path: Path) -> None:
        """A source that raises every poll (GitHub outage, dead github
        sandbox) is polled with doubling delays, not every tick (#254)."""
        h = Harness(tmp_path)
        polls = 0

        class Flaky(FakeSource):
            def poll(self) -> list[WorkItem]:
                nonlocal polls
                polls += 1
                if polls <= 3:
                    raise WorkerError("github sandbox is gone")
                return list(self.items)

        flaky = Flaky([gh_item()])
        h.loop.source = flaky
        interval = h.config.daemon.poll_interval_s
        h.loop.tick()  # failure 1 -> next poll in 2*interval
        h.loop.tick()  # skipped
        assert polls == 1
        h.clock.t += 2 * interval
        h.loop.tick()  # failure 2 -> next poll in 4*interval
        h.clock.t += 2 * interval
        h.loop.tick()  # skipped
        assert polls == 2
        h.clock.t += 2 * interval
        h.loop.tick()  # failure 3
        h.clock.t += 8 * interval
        result = h.loop.tick()  # recovers and dispatches
        assert polls == 4 and result.dispatched == "gh:issue:1"
        assert h.loop._source_failures == 0

    def test_recover_failed_run_takes_failure_path(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"max_attempts_per_item": 3}}
        )
        h = Harness(tmp_path, cfg)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_dead", now=2.0)
        h.store.create_run("r_dead", "x")
        h.store.set_run_state("r_dead", "failed")
        h.loop.recover()
        assert h.dstore.get("gh:issue:1").state == "queued"  # type: ignore[union-attr]
        assert any(c[0] == "retry" for c in h.source.calls)

    def test_recover_claimed_but_unstarted_requeues(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.set_state("gh:issue:1", "running", now=1.5)
        h.loop.recover()
        got = h.dstore.get("gh:issue:1")
        assert got is not None and got.state == "queued" and got.claimed is True
        # and the next tick runs it WITHOUT re-claiming
        h.outcomes = ["merged"]
        h.loop.tick()
        assert not any(c[0] == "claim" for c in h.source.calls)


@pytest.fixture
def _quiet_tasks() -> list[TaskRecord]:
    return [TaskRecord(spec=TaskSpec(id="t1", title="T"), state="done")]


class TestOperatorItemControls:
    """#229: abandon / retry / requeue from the CLI (row-only, other process)
    and from Discord (in-process loop methods)."""

    def _blocking_runner(self, h: Harness) -> tuple[threading.Event, Any]:
        """A runner that waits until the engine's cancel flag is raised, then
        ends the way a cancelled engine does (non-terminal persisted state,
        StateError at the boundary)."""
        started = threading.Event()

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            started.set()
            engine = h.loop.current.engine  # type: ignore[union-attr]
            assert engine._cancel_event.wait(5), "cancel never requested"
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "building")
            raise StateError("run cancelled at phase boundary")

        return started, runner

    def test_cli_abandon_of_running_item_cancels_run_and_reports(self, tmp_path: Path) -> None:
        """Field (#227/#228): the only way to abandon a spiraling item was
        poking DaemonStore from Python — and even then the settle path would
        have overwritten the row. An operator's row-level abandon must cancel
        the in-flight run, win over the run's own outcome, and reach the
        source exactly once without tripping the breaker."""
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        started, runner = self._blocking_runner(h)
        h.loop._runner = runner
        results: list[Any] = []
        t = threading.Thread(target=lambda: results.append(h.loop.tick()))
        t.start()
        assert started.wait(5)
        run_id = h.loop.current.run_id  # type: ignore[union-attr]
        # another process: only the row changes
        h.dstore.abandon("gh:issue:1", "operator: doomed plan", h.clock())
        t.join(10)
        assert results and results[0].outcome == "failed"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "failed" and item.run_id == run_id
        assert [c for c in h.source.calls if c[0] == "abandoned"] == [
            ("abandoned", "operator: doomed plan")
        ]
        assert h.loop._consecutive_failures == 0
        # ledger closed as abandoned, and recovery leaves the item alone
        h.loop.recover()
        assert h.dstore.get("gh:issue:1").state == "failed"  # type: ignore[union-attr]

    def test_cli_requeue_of_running_item_cancels_and_next_tick_starts_fresh(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        started, runner = self._blocking_runner(h)
        h.loop._runner = runner
        results: list[Any] = []
        t = threading.Thread(target=lambda: results.append(h.loop.tick()))
        t.start()
        assert started.wait(5)
        first_run = h.loop.current.run_id  # type: ignore[union-attr]
        h.dstore.requeue("gh:issue:1", h.clock())
        t.join(10)
        assert results and results[0].outcome == "requeued"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued" and item.run_id is None
        assert item.attempts == 1  # requeue keeps the count
        assert not any(c[0] in ("abandoned", "retry") for c in h.source.calls)
        # next tick: a fresh run (not a resume of the first), no re-claim
        h.loop._runner = h.runner
        h.clock.t += 10_000  # past the retry backoff for attempts=1
        assert h.loop.tick().outcome == "done"
        assert h.runs and h.runs[-1][0] != first_run and h.runs[-1][1] is False
        assert [c[0] for c in h.source.calls].count("claim") == 1

    def test_loop_abandon_item_in_flight_defers_source_report_to_settle(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        started, runner = self._blocking_runner(h)
        h.loop._runner = runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        got = h.loop.abandon_item("gh:issue:1", "operator says stop")
        assert got.state == "failed"
        t.join(10)
        assert [c for c in h.source.calls if c[0] == "abandoned"] == [
            ("abandoned", "operator says stop")
        ]

    def test_loop_abandon_queued_item_reports_immediately(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        got = h.loop.abandon_item("gh:issue:1")
        assert got.state == "failed" and got.last_error == "abandoned by operator"
        assert h.source.calls == [("abandoned", "abandoned by operator")]
        assert h.loop.tick().idle_kind == "no_work"

    def test_loop_retry_then_dispatch_is_a_fresh_plan(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"max_attempts_per_item": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [gh_item()]
        h.outcomes = ["failed"]
        assert h.loop.tick().outcome == "failed"
        first_run = h.runs[0][0]
        with pytest.raises(ValueError):
            h.loop.requeue_item("gh:issue:1")  # failed items need retry, not requeue
        got = h.loop.retry_item("gh:issue:1")
        assert got.state == "queued" and got.attempts == 0 and got.run_id is None
        assert h.loop.tick().outcome == "done"
        assert h.runs[-1][0] != first_run and h.runs[-1][1] is False
        assert [c[0] for c in h.source.calls].count("claim") == 1  # claim kept across retry

    def test_row_override_outranks_pending_cancel(self, tmp_path: Path) -> None:
        """`!sbx cancel` then, before the engine stops, `sbxloop daemon
        abandon` from another shell: the row already states the item's
        fate, so the abandon wins (no 'cancelled' report, no retry) and the
        consumed CancelRequest cannot leak onto the next run."""
        h = Harness(tmp_path)
        h.source.items = [gh_item("1"), gh_item("2")]
        started = threading.Event()
        release = threading.Event()

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            h.runs.append((run_id, resume))
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "building")
            started.set()
            assert release.wait(5)
            raise StateError("run cancelled at phase boundary")

        h.loop._runner = runner
        results: list[Any] = []
        t = threading.Thread(target=lambda: results.append(h.loop.tick()))
        t.start()
        assert started.wait(5)
        assert h.loop.cancel_current("discord op", retry=True) is True
        h.dstore.abandon("gh:issue:1", "operator: doomed plan", h.clock())
        release.set()
        t.join(10)
        assert results and results[0].outcome == "failed"
        a = h.dstore.get("gh:issue:1")
        assert a is not None and a.state == "failed" and a.attempts == 1
        assert [c[0] for c in h.source.calls if c[0] in ("abandoned", "cancelled")] == ["abandoned"]
        # gh:issue:2's failure settles as a failure: the stale cancel is gone.
        h.loop._runner = h.runner
        h.outcomes = ["raise"]
        assert h.loop.tick().outcome == "retry"
        assert h.loop._cancel_request is None
        assert not any(c[0] == "cancelled" for c in h.source.calls)

    def test_recover_reports_item_abandoned_offline(self, tmp_path: Path) -> None:
        """Field scenario of #229: the daemon is *not* running, the item is
        left running/pinned, `sbxloop daemon abandon` flips the row only.
        The next daemon start must report the abandon to the source, close
        the run's ledger and remove the dead run's sandboxes — once."""
        h = Harness(tmp_path)
        removed: list[str] = []

        class FakeSbx:
            def stop(self, name: str) -> None: ...

            def rm(self, name: str, **kwargs: Any) -> None:
                removed.append(name)

            def secret_rm(self, **kwargs: Any) -> bool:
                return True

        h.loop.sbx = FakeSbx()  # type: ignore[assignment]
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_dead", now=2.0)
        h.dstore.finish_ledger("r_dead", "interrupted", 3.0)  # clean shutdown
        h.store.create_run("r_dead", "x")
        h.store.set_run_state("r_dead", "building")
        h.dstore.abandon("gh:issue:1", "operator: doomed plan", 4.0)  # CLI, no daemon
        h.loop.recover()
        assert h.source.calls == [("abandoned", "operator: doomed plan")]
        assert removed == ["sbxloop-r_dead-agent", "sbxloop-r_dead-github"]
        row = h.dstore._conn.execute(
            "SELECT result FROM daemon_runs WHERE run_id = 'r_dead'"
        ).fetchone()
        assert row["result"] == "abandoned"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "failed" and item.run_id == "r_dead"
        assert h.loop.tick().idle_kind == "no_work"
        h.loop.recover()  # idempotent: the ledger is closed, nothing to report
        assert len(h.source.calls) == 1

    def test_recover_closes_run_of_item_requeued_offline(self, tmp_path: Path) -> None:
        """Same, for `sbxloop daemon requeue` with no daemon: the crashed
        run's ledger row is still open; recovery closes it (no source
        report — requeue is not a verdict) and the item is dispatched fresh."""
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_dead", now=2.0)  # crash: ledger open
        h.store.create_run("r_dead", "x")
        h.store.set_run_state("r_dead", "building")
        h.dstore.requeue("gh:issue:1", 4.0)
        h.loop.recover()
        row = h.dstore._conn.execute(
            "SELECT result FROM daemon_runs WHERE run_id = 'r_dead'"
        ).fetchone()
        assert row["result"] == "requeued"
        assert not any(c[0] in ("abandoned", "cancelled") for c in h.source.calls)
        h.clock.t += 10_000
        assert h.loop.tick().outcome == "done"
        assert h.runs == [(h.runs[0][0], False)] and h.runs[0][0] != "r_dead"

    def test_recover_leaves_pending_resume_alone(self, tmp_path: Path) -> None:
        """A queued item still pinned to its interrupted run is a pending
        resume, not an offline requeue."""
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_live", now=2.0)
        h.dstore.finish_ledger("r_live", "interrupted", 3.0)
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "building")
        h.loop.recover()
        h.loop.tick()
        assert h.runs == [("r_live", True)]

    def test_loop_requeue_stale_pinned_item_closes_ledger(self, tmp_path: Path) -> None:
        """A dead process left the item running with a pinned run; requeue
        (daemon idle) must unpin it so recovery does not resume it."""
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_old", now=2.0)
        h.store.create_run("r_old", "x")
        h.store.set_run_state("r_old", "building")
        got = h.loop.requeue_item("gh:issue:1")
        assert got.state == "queued" and got.run_id is None
        h.loop.recover()  # nothing running any more
        assert h.runs == []
        # attempts were kept, so the usual retry backoff still applies
        assert h.loop.tick().idle_kind == "backoff"
        h.clock.t += 10_000
        assert h.loop.tick().outcome == "done"
        assert h.runs[0][0] != "r_old" and h.runs[0][1] is False

    def test_cli_retry_of_failed_item_reaches_the_source_before_dispatch(
        self, tmp_path: Path
    ) -> None:
        """A row-only `sbxloop daemon retry` (other process) cannot call
        report_requeued, so the GitHub issue would keep its failed label.
        The row carries the debt; the next tick pays it before the fresh
        dispatch — once."""
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"max_attempts_per_item": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [gh_item()]
        h.outcomes = ["failed"]
        assert h.loop.tick().outcome == "failed"
        h.dstore.retry("gh:issue:1", h.clock(), "re-queued by operator (CLI)")  # CLI, row only
        assert h.loop.tick().outcome == "done"
        kinds = [c[0] for c in h.source.calls]
        assert kinds == ["claim", "started", "abandoned", "requeued", "started", "merged"]
        assert h.source.calls[3] == ("requeued", "operator (CLI)")
        assert h.dstore.get("gh:issue:1").pending_report is None  # type: ignore[union-attr]

    def test_cli_abandon_of_queued_item_reaches_the_source(self, tmp_path: Path) -> None:
        """An item abandoned from the CLI while merely queued has no run in
        flight to override and no ledger row for recovery to find; the tick
        sweep still delivers the abandon (paused or not), exactly once."""
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.abandon("gh:issue:1", "not worth it", 2.0)  # CLI, row only
        h.loop.pause()
        assert h.loop.tick().idle_kind == "paused"
        assert h.source.calls == [("abandoned", "not worth it")]
        h.loop.unpause()
        assert h.loop.tick().idle_kind == "no_work"
        h.loop.recover()
        assert len(h.source.calls) == 1

    def test_loop_abandon_of_pending_resume_removes_its_sandboxes(self, tmp_path: Path) -> None:
        """A Discord abandon of an item queued for resume closes the dead
        run's ledger — after which recovery no longer sees it — so its
        microVMs and secrets must go now or leak for good."""
        h = Harness(tmp_path)
        removed: list[str] = []

        class FakeSbx:
            def stop(self, name: str) -> None: ...

            def rm(self, name: str, **kwargs: Any) -> None:
                removed.append(name)

            def secret_rm(self, **kwargs: Any) -> bool:
                return True

        h.loop.sbx = FakeSbx()  # type: ignore[assignment]
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_dead", now=2.0)
        h.dstore.finish_ledger("r_dead", "interrupted", 3.0)
        h.dstore.mark_resume_pending("gh:issue:1", 4.0)
        h.loop.abandon_item("gh:issue:1", "never mind")
        assert removed == ["sbxloop-r_dead-agent", "sbxloop-r_dead-github"]
        assert h.source.calls == [("abandoned", "never mind")]
        removed.clear()
        h.dstore.retry("gh:issue:1", 5.0)
        h.dstore.mark_running("gh:issue:1", "r_dead2", now=6.0)
        h.dstore.finish_ledger("r_dead2", "interrupted", 7.0)
        h.dstore.mark_resume_pending("gh:issue:1", 8.0)
        h.loop.requeue_item("gh:issue:1")  # same for an unpin
        assert removed == ["sbxloop-r_dead2-agent", "sbxloop-r_dead2-github"]
        assert h.dstore.unsettled_runs() == []


class TestWorkspacePosture:
    """#255: unattended runs answer the dirty-tree question by config and
    start from a fetch-refreshed checkout. Real git repos in tmp_path (the
    hostgit test helpers) so the fast-forward is exercised for real."""

    @staticmethod
    def _config(tmp_path: Path, workspace: Path, **daemon: Any) -> Config:
        return Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "sandbox": {"workspace": str(workspace)},
                "daemon": daemon,
            }
        )

    def test_git_checkout_workspace_gets_clone_isolation_by_default(self, tmp_path: Path) -> None:
        h = Harness(tmp_path, self._config(tmp_path, make_repo(tmp_path)))
        assert h.config.sandbox.workspace_isolation == "auto"
        assert h.loop._item_config(gh_item()).sandbox.workspace_isolation == "clone"

    def test_dispatch_hands_the_runner_clone_isolation(self, tmp_path: Path) -> None:
        """The override must reach the config the runner actually receives —
        `_item_config` being right is worthless if dispatch passes `self.config`."""
        h = Harness(tmp_path, self._config(tmp_path, make_repo(tmp_path)))
        h.source.items = [gh_item()]
        assert h.loop.tick().outcome == "done"
        assert [c.sandbox.workspace_isolation for c in h.run_configs] == ["clone"]
        assert h.config.sandbox.workspace_isolation == "auto"  # operator config untouched

    def test_daemon_isolation_knob_governs_daemon_runs(self, tmp_path: Path) -> None:
        cfg = self._config(tmp_path, make_repo(tmp_path), workspace_isolation="in-place")
        h = Harness(tmp_path, cfg)
        assert h.loop._item_config(gh_item()).sandbox.workspace_isolation == "in-place"

    def test_plain_directory_workspace_is_not_forced_to_clone(self, tmp_path: Path) -> None:
        """`clone` on a non-git dir is a provisioning error; a plain dir must
        keep `auto`'s in-place fallback or every daemon run would fail."""
        plain = tmp_path / "plain"
        plain.mkdir()
        h = Harness(tmp_path, self._config(tmp_path, plain))
        assert h.loop._item_config(gh_item()).sandbox.workspace_isolation == "auto"

    def test_no_workspace_leaves_sandbox_config_alone(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        assert h.loop._item_config(gh_item()).sandbox == h.config.sandbox

    def test_fresh_dispatch_fast_forwards_the_checkout(self, tmp_path: Path) -> None:
        upstream, checkout = make_upstream_and_clone(tmp_path)
        h = Harness(tmp_path, self._config(tmp_path, checkout))
        h.source.items = [gh_item()]
        new_sha = push_upstream_commit(tmp_path, upstream)
        front = RecordingFrontend()
        h.loop.frontend = front
        assert h.loop.tick().outcome == "done"
        assert hostgit.head_commit(checkout) == new_sha
        assert any("refreshed workspace" in t and "fast-forwarded main" in t for t in front.seen)

    def test_refresh_disabled_leaves_checkout_stale(self, tmp_path: Path) -> None:
        upstream, checkout = make_upstream_and_clone(tmp_path)
        stale = hostgit.head_commit(checkout)
        h = Harness(tmp_path, self._config(tmp_path, checkout, refresh_workspace=False))
        h.source.items = [gh_item()]
        push_upstream_commit(tmp_path, upstream)
        assert h.loop.tick().outcome == "done"
        assert hostgit.head_commit(checkout) == stale

    def test_in_place_daemon_runs_are_not_refreshed(self, tmp_path: Path) -> None:
        upstream, checkout = make_upstream_and_clone(tmp_path)
        stale = hostgit.head_commit(checkout)
        cfg = self._config(tmp_path, checkout, workspace_isolation="in-place")
        h = Harness(tmp_path, cfg)
        h.source.items = [gh_item()]
        push_upstream_commit(tmp_path, upstream)
        assert h.loop.tick().outcome == "done"
        assert hostgit.head_commit(checkout) == stale

    def test_failed_fetch_warns_and_still_runs(self, tmp_path: Path) -> None:
        """Network down must not fail every issue: warn, run from local HEAD."""
        upstream, checkout = make_upstream_and_clone(tmp_path)
        shutil.rmtree(upstream)
        h = Harness(tmp_path, self._config(tmp_path, checkout))
        h.source.items = [gh_item()]
        front = RecordingFrontend()
        h.loop.frontend = front
        assert h.loop.tick().outcome == "done"
        assert len(h.runs) == 1
        assert any("workspace refresh failed" in t for t in front.seen)

    def test_resume_does_not_refresh(self, tmp_path: Path) -> None:
        """A resumed run is pinned to its existing clone; the source is
        left alone (moving it would change nothing for that run)."""
        upstream, checkout = make_upstream_and_clone(tmp_path)
        stale = hostgit.head_commit(checkout)
        h = Harness(tmp_path, self._config(tmp_path, checkout))
        h.dstore.upsert_new(gh_item(), h.clock())
        h.dstore.mark_claimed("gh:issue:1", h.clock())
        h.dstore.mark_running("gh:issue:1", "rres", h.clock())
        h.store.create_run("rres", "outcome")
        h.store.set_run_state("rres", "building")
        push_upstream_commit(tmp_path, upstream)
        h.loop.recover()  # queues the resume with the run pinned; tick runs it
        assert h.loop.tick().outcome == "done"
        assert h.runs == [("rres", True)]
        assert hostgit.head_commit(checkout) == stale


class TestLogging:
    """The journal answers the questions it could not before: what was
    dispatched and how long it took, why the daemon is idle (once, not per
    tick), and what shutdown interrupted."""

    @staticmethod
    def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == "sbxloop.daemon.loop" and f"'event': '{name}'" in r.getMessage()
        ]

    def test_dispatch_and_finish_are_logged_with_ids_and_duration(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        with caplog.at_level(logging.INFO):
            assert h.loop.tick().outcome == "done"
        (dispatch,) = self._events(caplog, "run.dispatch")
        assert "'item': 'gh:issue:1'" in dispatch
        assert "'resume': False" in dispatch and "'attempt': 1" in dispatch
        (finished,) = self._events(caplog, "run.finished")
        assert "'outcome': 'merged'" in finished and "'duration_s'" in finished
        assert self._events(caplog, "item.claimed")
        (done,) = self._events(caplog, "run.done")
        assert "'pr': 9" in done

    def test_run_thread_records_carry_the_bound_run_id(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from sbxloop.log import get_logger

        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        seen: list[str] = []

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            get_logger("sbxloop.test.inside").info("inside.run")
            seen.append(run_id)
            return h.runner(item, cfg, run_id, bus, resume)

        h.loop._runner = runner
        with caplog.at_level(logging.INFO):
            h.loop.tick()
        (inside,) = [r.getMessage() for r in caplog.records if r.name == "sbxloop.test.inside"]
        assert f"'run': '{seen[0]}'" in inside and "'item': 'gh:issue:1'" in inside

    def test_idle_reason_logged_on_change_not_every_tick(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        h = Harness(tmp_path)
        h.loop.pause()
        with caplog.at_level(logging.INFO):
            for _ in range(3):
                h.loop._log_tick(h.loop.tick(), 0.01)
            h.loop.unpause()
            h.loop._log_tick(h.loop.tick(), 0.01)
        idles = self._events(caplog, "daemon.idle")
        assert len(idles) == 2
        assert "'idle': 'paused'" in idles[0]
        assert "'idle': 'no_work'" in idles[1]

    def test_shutdown_interruption_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        started = threading.Event()
        release = threading.Event()

        def slow_runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            started.set()
            release.wait(5)
            raise WorkerError("cancelled at boundary")

        h.loop._runner = slow_runner
        with caplog.at_level(logging.INFO):
            t = threading.Thread(target=h.loop.tick)
            t.start()
            assert started.wait(5)
            h.loop.request_stop()
            release.set()
            t.join(5)
        (interrupted,) = self._events(caplog, "run.interrupted")
        assert "'item': 'gh:issue:1'" in interrupted and "'resumable': True" in interrupted
        (finished,) = self._events(caplog, "run.finished")
        assert "'outcome': 'interrupted'" in finished

    def test_report_reads_the_run_record_not_the_events(self, tmp_path: Path) -> None:
        """The PR lives on the engine's run row; a report never mines the
        chronology for it."""
        h = Harness(tmp_path)
        h.store.create_run("r-x", "outcome")
        h.store.set_run_pr("r-x", number=4, url="https://x/pull/4", branch="b", head_sha="s")
        h.store.set_run_reason("r-x", "why")
        report = h.loop.report_for("r-x")
        assert report.pr == (4, "https://x/pull/4") and report.branch == "b"
        assert report.reason == "why" and report.task_summary == "no tasks ran"
        assert h.loop.report_for("r-unknown").pr is None


class TestRunCapDayWindow:
    """The run cap is a wall-clock calendar day in a configured timezone,
    not a trailing 24h window (#: operators saw ``10/10`` and then runs
    starting minutes later as old runs aged out)."""

    def test_day_window_utc_boundaries(self) -> None:
        # 2024-03-05T12:00:00Z
        noon = datetime(2024, 3, 5, 12, 0, tzinfo=UTC).timestamp()
        start, nxt = day_window(noon, "UTC")
        assert start == datetime(2024, 3, 5, 0, 0, tzinfo=UTC).timestamp()
        assert nxt == datetime(2024, 3, 6, 0, 0, tzinfo=UTC).timestamp()

    def test_day_window_at_exact_midnight_starts_the_new_day(self) -> None:
        midnight = datetime(2024, 3, 5, 0, 0, tzinfo=UTC).timestamp()
        start, nxt = day_window(midnight, "UTC")
        assert start == midnight  # 00:00 belongs to the day it opens
        assert nxt == midnight + 86400
        # one microsecond earlier is still the previous day
        prev_start, prev_next = day_window(midnight - 0.001, "UTC")
        assert prev_next == midnight and prev_start == midnight - 86400

    def test_day_window_honors_non_utc_zone(self) -> None:
        noon = datetime(2024, 3, 5, 12, 0, tzinfo=UTC).timestamp()
        start, nxt = day_window(noon, "America/New_York")
        # EST is UTC-5 in early March 2024 → local midnight is 05:00Z
        assert start == datetime(2024, 3, 5, 5, 0, tzinfo=UTC).timestamp()
        assert nxt == datetime(2024, 3, 6, 5, 0, tzinfo=UTC).timestamp()
        assert start != day_window(noon, "UTC")[0]

    def test_day_window_spans_dst_transition(self) -> None:
        """A US spring-forward day is 23h long; the next boundary is still
        local midnight, not start+86400."""
        noon = datetime(2024, 3, 10, 16, 0, tzinfo=UTC).timestamp()
        start, nxt = day_window(noon, "America/New_York")
        assert nxt - start == 23 * 3600

    @staticmethod
    def _capped_harness(tmp_path: Path, tz: str = "UTC") -> Harness:
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "daemon": {"max_runs_per_day": 1, "run_cap_timezone": tz},
            }
        )
        h = Harness(tmp_path, cfg)
        # A known absolute instant so calendar boundaries are deterministic.
        h.clock.t = datetime(2024, 3, 5, 12, 0, tzinfo=UTC).timestamp()
        h.source.items = [gh_item("1"), gh_item("2")]
        return h

    def test_cap_reached_mid_day_blocks_then_resets_at_boundary(self, tmp_path: Path) -> None:
        h = self._capped_harness(tmp_path)
        assert h.loop.tick().outcome == "done"
        assert h.loop.tick().idle_reason == "daily_cap"
        # Just before the boundary: still capped.
        h.clock.t = datetime(2024, 3, 5, 23, 59, 59, tzinfo=UTC).timestamp()
        assert h.loop.tick().idle_reason == "daily_cap"
        # Crossing local midnight resets the count.
        h.clock.t = datetime(2024, 3, 6, 0, 0, tzinfo=UTC).timestamp()
        assert h.loop.tick().outcome == "done"

    def test_not_a_trailing_24h_window(self, tmp_path: Path) -> None:
        """23h after a 00:30 run it is still the same calendar day, so the
        slot is not freed — a rolling window would have released it."""
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"max_runs_per_day": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.clock.t = datetime(2024, 3, 5, 0, 30, tzinfo=UTC).timestamp()
        h.source.items = [gh_item("1"), gh_item("2")]
        assert h.loop.tick().outcome == "done"
        h.clock.t += 23 * 3600  # 23:30 the same day
        assert h.loop.tick().idle_reason == "daily_cap"
        h.clock.t += 1800  # 00:00 the next day
        assert h.loop.tick().outcome == "done"

    def test_run_just_before_boundary_does_not_free_a_slot_early(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"max_runs_per_day": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.clock.t = datetime(2024, 3, 5, 23, 30, tzinfo=UTC).timestamp()
        h.source.items = [gh_item("1"), gh_item("2")]
        assert h.loop.tick().outcome == "done"
        assert h.loop.tick().idle_reason == "daily_cap"
        # 30 minutes later the day has rolled even though 24h have not passed.
        h.clock.t += 1801
        assert h.loop.tick().outcome == "done"

    def test_timezone_setting_is_honored(self, tmp_path: Path) -> None:
        """With the cap day pinned to New York, 00:00 UTC does not reset —
        05:00 UTC (local midnight) does."""
        h = self._capped_harness(tmp_path, tz="America/New_York")
        assert h.loop.tick().outcome == "done"
        h.clock.t = datetime(2024, 3, 6, 0, 0, tzinfo=UTC).timestamp()
        assert h.loop.tick().idle_reason == "daily_cap"
        h.clock.t = datetime(2024, 3, 6, 5, 0, tzinfo=UTC).timestamp()
        assert h.loop.tick().outcome == "done"

    def test_status_agrees_with_the_gate_and_reports_the_reset(self, tmp_path: Path) -> None:
        h = self._capped_harness(tmp_path, tz="America/New_York")
        assert h.loop.tick().outcome == "done"
        status = h.loop.status()
        assert status["runs_today"] == 1
        assert status["max_runs_per_day"] == 1
        assert status["run_cap_timezone"] == "America/New_York"
        assert status["runs_today_resets_at"] == day_window(h.clock(), "America/New_York")[1]
        assert h.loop.tick().idle_reason == "daily_cap"
        # One second before the reset instant the count still stands...
        h.clock.t = status["runs_today_resets_at"] - 1
        assert h.loop.status()["runs_today"] == 1
        # ...and at the reset instant the counter is visibly back to zero.
        h.clock.t = status["runs_today_resets_at"]
        assert h.loop.status()["runs_today"] == 0
        assert h.loop.status()["runs_today_resets_at"] == status["runs_today_resets_at"] + 86400

    def test_day_window_fall_back_day_is_25h(self) -> None:
        """A US fall-back day is 25h long — the boundary is local midnight,
        not start+86400."""
        noon = datetime(2024, 11, 3, 16, 0, tzinfo=UTC).timestamp()
        start, nxt = day_window(noon, "America/New_York")
        assert nxt - start == 25 * 3600

    def test_day_window_boundary_is_idempotent(self) -> None:
        """The next boundary is itself the start of its own day, so the
        windows tile the timeline without gaps or overlap."""
        for tz in ("UTC", "America/New_York", "Asia/Tokyo"):
            _, nxt = day_window(datetime(2024, 3, 5, 12, 0, tzinfo=UTC).timestamp(), tz)
            assert day_window(nxt, tz)[0] == nxt

    def test_run_at_exactly_midnight_counts_toward_the_new_day(self, tmp_path: Path) -> None:
        """A run started at 23:59:59 belongs to the old day; one started at
        exactly 00:00:00 spends the new day's single slot."""
        h = self._capped_harness(tmp_path)
        h.clock.t = datetime(2024, 3, 5, 23, 59, 59, tzinfo=UTC).timestamp()
        assert h.loop.tick().outcome == "done"
        assert h.loop.tick().idle_reason == "daily_cap"
        # Exactly local midnight: the old run aged out, this one takes the slot.
        h.clock.t = datetime(2024, 3, 6, 0, 0, 0, tzinfo=UTC).timestamp()
        assert h.loop.status()["runs_today"] == 0
        assert h.loop.tick().outcome == "done"
        assert h.loop.status()["runs_today"] == 1
        # ...so the new day is now full, one second in.
        h.source.items = [*h.source.items, gh_item("3")]
        h.clock.t += 1
        assert h.loop.tick().idle_reason == "daily_cap"

    def test_positive_offset_timezone_resets_before_utc_midnight(self, tmp_path: Path) -> None:
        """Tokyo is UTC+9, so its local midnight is 15:00Z the day before —
        the cap resets then, not at 00:00Z."""
        h = self._capped_harness(tmp_path, tz="Asia/Tokyo")
        h.clock.t = datetime(2024, 3, 5, 16, 0, tzinfo=UTC).timestamp()  # 2024-03-06 01:00 JST
        assert h.loop.tick().outcome == "done"
        assert h.loop.tick().idle_reason == "daily_cap"
        # 00:00Z is still the same Tokyo day (09:00 JST) — no reset.
        h.clock.t = datetime(2024, 3, 6, 0, 0, tzinfo=UTC).timestamp()
        assert h.loop.tick().idle_reason == "daily_cap"
        # 15:00Z is the next Tokyo midnight — reset.
        h.clock.t = datetime(2024, 3, 6, 15, 0, tzinfo=UTC).timestamp()
        assert h.loop.tick().outcome == "done"


class TestOrphanRunReconciliation:
    """#374: a dead process left run rows non-terminal forever."""

    def test_recover_reconciles_cancelled_item_run(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_cx", now=2.0)
        h.store.create_run("r_cx", "x")
        h.store.set_run_state("r_cx", "building")
        h.store.append_event(Event.now("run.started", "r_cx"))
        before = len(list(h.store.events("r_cx")))
        h.dstore.mark_cancelled(
            "gh:issue:1", "cancelled by Discord user brett.bergin (via concierge)", now=3.0
        )
        h.loop.recover()
        record = h.store.get_run("r_cx")
        assert record.state == "cancelled"
        assert record.reason is not None
        assert "work item cancelled" in record.reason
        assert "brett.bergin" in record.reason
        events = [e for _, e in h.store.events("r_cx")]
        assert len(events) == before + 1
        assert events[-1].type == "run.reconciled"
        assert events[-1].data["previous_state"] == "building"

    def test_a_run_that_dies_mid_phase_does_not_stay_in_flight(self, tmp_path: Path) -> None:
        """The `runs` row is written only by the run loop, so a run that died
        inside a phase used to be left `decomposing` until the stale sweep
        timed it out — six hours in the field (runs rv2y1a8ke, rq826h546 of
        item gh:issue:478), during which `list_runs` and every active-run count
        disagreed with reality.

        Both settles close it now: the retry, whose item drops its run pin
        and is dispatched fresh, and the give-up, whose item is terminal.
        Neither can ever be resumed, so leaving the row open said something
        untrue about the daemon.
        """
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        h.outcomes = ["die_mid_phase", "die_mid_phase"]
        assert h.loop.tick().outcome == "retry"
        first = h.runs[0][0]
        record = h.store.get_run(first)
        assert record.state == "failed", "a requeued item is dispatched fresh, never resumed"
        assert record.reason is not None and "decompose produced invalid output" in record.reason
        assert "run.reconciled" in [e.type for _, e in h.store.events(first)]
        h.clock.t += h.loop.config.daemon.retry_backoff_s + 1
        assert h.loop.tick().outcome == "failed"
        second = h.runs[1][0]
        assert h.store.get_run(second).state == "failed"
        assert h.store.non_terminal_runs() == [], "no phantom left for the sweep to find"

    def test_recover_reconciles_orphan_run_failed(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        for run_id, state in (("r_orph", "building"), ("r_dec", "decomposing")):
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, state)  # type: ignore[arg-type]
        h.loop.recover()
        for run_id in ("r_orph", "r_dec"):
            record = h.store.get_run(run_id)
            assert record.state == "failed"
            assert record.reason is not None and "orphaned" in record.reason
            assert [e.type for _, e in h.store.events(run_id)] == ["run.reconciled"]

    def test_recover_leaves_live_and_resume_pending_runs(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        # Interrupted run: recover() queues it for resume and must not close it.
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_resume", now=2.0)
        h.store.create_run("r_resume", "x")
        h.store.set_run_state("r_resume", "building")
        # Genuinely in-flight run in this process.
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "building")
        h.loop._current = RunHandle(gh_item("2"), "r_live", cast(Any, None), EventBus())
        h.loop.recover()
        assert h.store.get_run("r_resume").state == "building"
        assert h.store.get_run("r_live").state == "building"
        pending = h.dstore.get("gh:issue:1")
        assert pending is not None and pending.state == "queued" and pending.run_id == "r_resume"
        for run_id in ("r_resume", "r_live"):
            assert [e.type for _, e in h.store.events(run_id)] == []


class TestStaleRunReconciliation:
    """#374 safety net: a run nothing is executing goes stale and is closed."""

    @staticmethod
    def _age(h: Harness, run_id: str, updated_at: float) -> None:
        h.store._conn.execute(
            "UPDATE runs SET updated_at = ? WHERE run_id = ?", (updated_at, run_id)
        )
        h.store._conn.commit()

    @staticmethod
    def _stale_harness(tmp_path: Path) -> Harness:
        config = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"run_stale_after_s": 3600.0}}
        )
        return Harness(tmp_path, config)

    def test_stale_run_reconciled_fresh_one_left_alone(self, tmp_path: Path) -> None:
        h = self._stale_harness(tmp_path)
        now = h.clock.t
        for run_id in ("r_stale", "r_fresh"):
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "building")
        self._age(h, "r_stale", now - 7200.0)
        self._age(h, "r_fresh", now - 60.0)
        h.loop.tick()
        stale = h.store.get_run("r_stale")
        assert stale.state == "failed"
        assert stale.reason is not None and "stale" in stale.reason
        assert [e.type for _, e in h.store.events("r_stale")] == ["run.reconciled"]
        assert h.store.get_run("r_fresh").state == "building"
        assert [e.type for _, e in h.store.events("r_fresh")] == []

    def test_sweep_runs_while_paused(self, tmp_path: Path) -> None:
        h = self._stale_harness(tmp_path)
        h.loop.pause()
        h.store.create_run("r_stale", "x")
        h.store.set_run_state("r_stale", "decomposing")
        self._age(h, "r_stale", h.clock.t - 99999.0)
        assert h.loop.tick().idle_kind == "paused"
        assert h.store.get_run("r_stale").state == "failed"

    def test_current_run_is_never_stale(self, tmp_path: Path) -> None:
        h = self._stale_harness(tmp_path)
        for run_id in ("r_live", "r_other"):
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "awaiting_ci")
            self._age(h, run_id, h.clock.t - 99999.0)
        h.loop._current = RunHandle(gh_item("2"), "r_live", cast(Any, None), EventBus())
        h.loop._reconcile_stale_runs(h.clock.t)
        # A live run means the daemon is working: nothing is swept.
        for run_id in ("r_live", "r_other"):
            assert h.store.get_run(run_id).state == "awaiting_ci"
            assert [e.type for _, e in h.store.events(run_id)] == []

    def test_zero_threshold_disables_the_sweep(self, tmp_path: Path) -> None:
        config = Config.model_validate(
            {"home": str(tmp_path / "state"), "daemon": {"run_stale_after_s": 0}}
        )
        h = Harness(tmp_path, config)
        h.store.create_run("r_stale", "x")
        h.store.set_run_state("r_stale", "building")
        self._age(h, "r_stale", h.clock.t - 10_000_000.0)
        h.loop.tick()
        assert h.store.get_run("r_stale").state == "building"

    def test_stale_run_with_cancelled_item_keeps_operator_reason(self, tmp_path: Path) -> None:
        h = self._stale_harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_cx", now=2.0)
        h.store.create_run("r_cx", "x")
        h.store.set_run_state("r_cx", "building")
        h.dstore.mark_cancelled(
            "gh:issue:1", "cancelled by Discord user brett.bergin (via concierge)", now=3.0
        )
        self._age(h, "r_cx", h.clock.t - 7200.0)
        h.loop.tick()
        record = h.store.get_run("r_cx")
        assert record.state == "cancelled"
        assert record.reason is not None and "brett.bergin" in record.reason

    def test_resume_pending_stale_run_is_left_for_tick(self, tmp_path: Path) -> None:
        h = self._stale_harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_resume", now=2.0)
        h.store.create_run("r_resume", "x")
        h.store.set_run_state("r_resume", "building")
        h.dstore.mark_resume_pending("gh:issue:1", now=3.0)
        self._age(h, "r_resume", h.clock.t - 7200.0)
        h.loop._reconcile_stale_runs(h.clock.t)
        assert h.store.get_run("r_resume").state == "building"


class TestMultiRepoItemRouting:
    """An item from the second configured repository runs against THAT repo."""

    def _harness(self, tmp_path: Path) -> Harness:
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {
                    "repos": [
                        {"repo": "o/a"},
                        {"repo": "o/b", "deliver_base": "develop"},
                    ],
                    "deliver_base": "trunk",
                },
            }
        )
        return Harness(tmp_path, cfg)

    def test_item_config_is_narrowed_to_the_items_repo(self, tmp_path: Path) -> None:
        h = self._harness(tmp_path)
        cfg = h.loop._item_config(gh_item("5", item_id="gh:o/b:issue:5", repo="o/b"))
        assert cfg.github.repo == "o/b"
        assert [r.repo for r in cfg.github.repo_list()] == ["o/b"]
        # The per-repo base wins over the global default.
        assert cfg.github.deliver_base == "develop"

    def test_a_repoless_legacy_item_keeps_the_default(self, tmp_path: Path) -> None:
        h = self._harness(tmp_path)
        cfg = h.loop._item_config(gh_item("5"))
        assert cfg.github.repo == "o/a"
        assert cfg.github.deliver_base == "trunk"

    def test_the_repo_qualified_id_alone_routes_the_run(self, tmp_path: Path) -> None:
        h = self._harness(tmp_path)
        assert h.loop._item_repo(gh_item("5", item_id="gh:o/b:issue:5")) == "o/b"

    def test_provenance_names_the_items_repo(self, tmp_path: Path) -> None:
        h = self._harness(tmp_path)
        text = h.loop.outcome_text(gh_item("5", item_id="gh:o/b:issue:5", repo="o/b"))
        assert "GitHub issue #5 in o/b" in text

    def test_an_unknown_repo_falls_back_to_the_default(self, tmp_path: Path) -> None:
        h = self._harness(tmp_path)
        assert h.loop._item_repo(gh_item("5", repo="o/gone")) is None
        assert h.loop._item_config(gh_item("5", repo="o/gone")).github.repo == "o/a"


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


class TestPerRepoWorkspaceRefresh:
    """#526: the pre-run fast-forward must touch the *claimed repo's*
    checkout — never "the" daemon-wide one, which with several repos is
    whichever repository that path happens to be."""

    @staticmethod
    def _two_repo_config(tmp_path: Path, ws_a: Path, ws_b: Path) -> Config:
        return Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {
                    "repos": [
                        {"repo": "o/a", "workspace": str(ws_a)},
                        {"repo": "o/b", "workspace": str(ws_b)},
                    ]
                },
            }
        )

    def test_refresh_touches_only_the_claimed_repos_checkout(self, tmp_path: Path) -> None:
        a_up, a_ws = make_upstream_and_clone(_mkdir(tmp_path / "a"))
        b_up, b_ws = make_upstream_and_clone(_mkdir(tmp_path / "b"))
        h = Harness(tmp_path, self._two_repo_config(tmp_path, a_ws, b_ws))
        a_stale = hostgit.head_commit(a_ws)
        push_upstream_commit(tmp_path / "a", a_up)
        b_new = push_upstream_commit(tmp_path / "b", b_up)

        h.loop._refresh_workspace("o/b")

        assert hostgit.head_commit(b_ws) == b_new
        assert hostgit.head_commit(a_ws) == a_stale

    def test_dispatch_refreshes_the_items_repo(self, tmp_path: Path) -> None:
        a_up, a_ws = make_upstream_and_clone(_mkdir(tmp_path / "a"))
        b_up, b_ws = make_upstream_and_clone(_mkdir(tmp_path / "b"))
        h = Harness(tmp_path, self._two_repo_config(tmp_path, a_ws, b_ws))
        a_stale = hostgit.head_commit(a_ws)
        push_upstream_commit(tmp_path / "a", a_up)
        b_new = push_upstream_commit(tmp_path / "b", b_up)
        h.source.items = [gh_item("7", item_id="gh:o/b:issue:7", repo="o/b")]

        assert h.loop.tick().outcome == "done"

        assert hostgit.head_commit(b_ws) == b_new
        assert hostgit.head_commit(a_ws) == a_stale

    def test_repo_without_a_workspace_is_skipped_not_an_error(self, tmp_path: Path) -> None:
        _a_up, a_ws = make_upstream_and_clone(_mkdir(tmp_path / "a"))
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {
                    "repos": [
                        {"repo": "o/a", "workspace": str(a_ws)},
                        {"repo": "o/b"},
                    ]
                },
            }
        )
        h = Harness(tmp_path, cfg)
        a_stale = hostgit.head_commit(a_ws)

        h.loop._refresh_workspace("o/b")  # no raise

        assert hostgit.head_commit(a_ws) == a_stale

    def test_origin_mismatch_is_refused(self, tmp_path: Path) -> None:
        """A checkout whose origin names another repo is left alone rather
        than fast-forwarded — the exact rvnbn7n2m shape."""
        upstream, checkout = make_upstream_and_clone(_mkdir(tmp_path / "a"))
        git_cmd("remote", "set-url", "origin", "https://github.com/o/a", cwd=checkout)
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repos": [{"repo": "o/b", "workspace": str(checkout)}]},
            }
        )
        h = Harness(tmp_path, cfg)
        front = RecordingFrontend()
        h.loop.frontend = front
        stale = hostgit.head_commit(checkout)
        push_upstream_commit(tmp_path / "a", upstream)

        h.loop._refresh_workspace("o/b")

        assert hostgit.head_commit(checkout) == stale
        # Refused outright, not attempted: a fetch against the foreign origin
        # would have surfaced as a refresh-failed warning instead.
        assert not any("workspace refresh" in t for t in front.seen)


class TestPauseHolds:
    """Pause is a set of named holds (#534): the operator's pause and a
    deploy's hold coexist, each side releases only its own, and the daemon
    idles while any hold stands."""

    def test_bare_pause_and_resume_act_on_the_operator_hold(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        assert not h.loop.paused and h.loop.holds == []
        assert h.loop.pause() == ["operator"]
        assert h.loop.paused and h.loop.status()["holds"] == ["operator"]
        assert h.loop.tick().idle_kind == "paused"
        assert h.loop.unpause() == []
        assert not h.loop.paused and h.loop.tick().idle_kind == "no_work"

    def test_each_side_releases_only_its_own_hold(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.loop.pause()  # the operator
        assert h.loop.pause("deploy-1") == ["deploy-1", "operator"]
        # The deploy releasing its hold does not resume a daemon the operator paused.
        assert h.loop.unpause("deploy-1") == ["operator"]
        assert h.loop.paused and h.loop.tick().idle_kind == "paused"
        # And the operator's bare resume does not release a deploy's hold.
        h.loop.pause("deploy-2")
        assert h.loop.unpause() == ["deploy-2"]
        assert h.loop.paused
        # Releasing a hold nobody holds is a no-op, not an error.
        assert h.loop.unpause("deploy-1") == ["deploy-2"]
        # `resume --all` is the override for a hold whose owner died.
        assert h.loop.unpause(None) == []
        assert not h.loop.paused

    def test_hold_names_are_validated(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        for bad in ("", "has space", "-leading", "a" * 65, "x`y`"):
            with pytest.raises(ValueError):
                h.loop.pause(bad)
        assert h.loop.holds == []

    def test_transitions_are_narrated_once(self, tmp_path: Path) -> None:
        """Discord sees who is holding the daemon and when a hold goes, but
        a repeated `pause` of the same hold is not a second notice."""
        h = Harness(tmp_path)
        front = RecordingFrontend()
        h.loop.frontend = front
        h.loop.pause("deploy-7", by="github-actions")
        h.loop.pause("deploy-7", by="github-actions")
        h.loop.pause(by="brett")
        h.loop.unpause("deploy-7")
        h.loop.unpause("deploy-7")  # already released: silent
        h.loop.unpause()
        kinds = [(n.kind, n.text) for n in front.notices]
        assert [k for k, _ in kinds] == [
            "daemon.paused",
            "daemon.paused",
            "daemon.resumed",
            "daemon.resumed",
        ]
        assert kinds[0][1] == "paused by deploy-7 (github-actions); holds: deploy-7"
        assert kinds[1][1] == "paused by operator (brett); holds: deploy-7, operator"
        assert kinds[2][1] == "hold released: deploy-7; still paused by operator"
        assert kinds[3][1] == "hold released: operator; claiming again"

    def test_status_reports_the_claim_in_progress(self, tmp_path: Path) -> None:
        """A claim is not a run, but it is not idle either (#530): the deploy
        pipeline reads `claiming <item>` as busy and waits."""
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        seen: list[tuple[str | None, str]] = []
        real_claim = h.source.claim

        def claim(item: WorkItem) -> bool:
            from sbxloop.daemon.control import dispatch

            status = h.loop.status()
            seen.append((status["claiming"], dispatch(h.loop, "status").text))
            return real_claim(item)

        h.source.claim = claim  # type: ignore[method-assign]
        assert h.loop.status()["claiming"] is None
        assert h.loop.tick().outcome == "done"
        assert seen[0][0] == "gh:issue:1"
        assert "**current:** claiming gh:issue:1" in seen[0][1]
        assert h.loop.status()["claiming"] is None

    def test_claiming_is_cleared_when_the_claim_fails(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        h.source.claim_ok = False
        assert h.loop.tick().outcome == "failed"
        assert h.loop.status()["claiming"] is None


class TestDeployChoreography:
    """The deploy pipeline's steps (`.github/workflows/deploy.yml`), played
    against the loop: take a hold, wait for idle, snapshot the *other*
    holds right before the restart, restart (holds are in-memory: a fresh
    loop has none), re-take the snapshot, release the deploy's own hold.
    Covers the two 2026-08-29 pause/restore races (#534): an operator pause
    issued before the deploy (18:36) and during its wait (21:10) both
    survive it."""

    HOLD = "deploy-123"

    def _status(self, loop: DaemonLoop) -> dict[str, Any]:
        from sbxloop.daemon.control import dispatch

        reply = dispatch(loop, "status")
        assert reply.status is not None
        lines = {
            line.split(":**", 1)[0].strip("*"): line.split(":**", 1)[1].strip()
            for line in reply.text.splitlines()
            if ":**" in line
        }
        return {"text": lines, "raw": reply.status}

    def _deploy(self, h: Harness, *, operator_pauses_during_wait: bool = False) -> DaemonLoop:
        from sbxloop.daemon.control import dispatch

        loop = h.loop
        # Take the deploy hold.
        assert dispatch(loop, f"pause --hold {self.HOLD}", by="github-actions").ok
        # Wait for idle: a run in flight shows on `current:`; the hold stops
        # the next claim, so once idle it stays idle.
        while self._status(loop)["text"]["current"] != "idle":
            loop.tick()
        if operator_pauses_during_wait:
            assert dispatch(loop, "pause", by="brett").ok
        # Snapshot the holds immediately before the restart, minus our own.
        holds = self._status(loop)["text"]["holds"]
        keep = [x.strip() for x in holds.split(",") if x.strip() not in ("none", self.HOLD)]
        # Restart: a new process has no holds at all.
        again = DaemonLoop(
            h.config,
            store=h.store,
            dstore=h.dstore,
            source=h.source,
            runner=h.runner,
            clock=h.clock,
        )
        for hold in keep:
            assert dispatch(again, f"pause --hold {hold}", by="github-actions").ok
        # Release our hold: already gone with the restart; a no-op.
        assert dispatch(again, f"resume --hold {self.HOLD}", by="github-actions").ok
        return again

    def test_deploy_waits_out_the_run_and_the_daemon_claims_again(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item("1"), gh_item("2")]
        assert h.loop.tick().outcome == "done"  # run 1 in flight… and done (scripted runner)
        again = self._deploy(h)
        assert again.holds == [] and not again.paused
        assert again.tick().outcome == "done"  # gh:issue:2 was not claimed under the hold
        assert [c for c in h.source.calls if c[0] == "claim"] == [
            ("claim", "gh:issue:1"),
            ("claim", "gh:issue:2"),
        ]

    def test_operator_pause_before_the_deploy_survives_it(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item("1")]
        h.loop.pause(by="brett")
        again = self._deploy(h)
        assert again.holds == ["operator"] and again.paused
        assert again.tick().idle_kind == "paused"
        assert [c for c in h.source.calls if c[0] == "claim"] == []

    def test_operator_pause_during_the_wait_survives_it(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item("1")]
        again = self._deploy(h, operator_pauses_during_wait=True)
        assert again.holds == ["operator"] and again.paused
        assert again.tick().idle_kind == "paused"

    def test_a_deploy_that_never_restarts_leaves_the_daemon_as_it_was(self, tmp_path: Path) -> None:
        """Timed out waiting, or failed before the upgrade: the `always()`
        release step is the only thing that runs, and it must not resume a
        daemon the operator paused."""
        from sbxloop.daemon.control import dispatch

        h = Harness(tmp_path)
        h.loop.pause(by="brett")
        dispatch(h.loop, f"pause --hold {self.HOLD}")
        reply = dispatch(h.loop, f"resume --hold {self.HOLD}")
        assert reply.ok and "still paused by `operator`" in reply.text
        assert h.loop.holds == ["operator"]
        # And without an operator pause the release resumes claiming.
        h.loop.unpause()
        dispatch(h.loop, f"pause --hold {self.HOLD}")
        assert dispatch(h.loop, f"resume --hold {self.HOLD}").text == "resumed."
        assert not h.loop.paused


class TestExhaustedRuns:
    """A run that exhausts a fix-round budget resumes its own PR (#523)
    instead of being retried from scratch with a second PR."""

    def _harness(self, tmp_path: Path, **overrides: Any) -> Harness:
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": {"max_attempts_per_item": 2, "retry_backoff_s": 100},
                **overrides,
            }
        )
        return Harness(tmp_path, cfg)

    def test_first_exhaustion_schedules_a_resume_of_the_same_run(self, tmp_path: Path) -> None:
        h = self._harness(tmp_path)
        front = RecordingFrontend()
        h.loop.frontend = front
        h.source.items = [gh_item()]
        h.outcomes = ["exhausted", "merged"]
        assert h.loop.tick().outcome == "retry"
        (run_id, _) = h.runs[0]
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued"
        assert item.run_id == run_id, "the run stays pinned: the retry is a resume"
        assert item.not_before == h.clock() + 100
        assert item.attempts == 1, "a scheduled continuation is not a second attempt"
        assert h.loop._consecutive_failures == 0, "nor a breaker count"
        run = h.store.get_run(run_id)
        assert run.granted_rounds == 2 and run.exhausted is None and run.state == "failed"
        # The issue and Discord hear what will happen, not a generic failure.
        retry = [c for c in h.source.calls if c[0] == "retry"]
        assert retry == [("retry", 1)]
        (notice,) = [n for n in front.notices if n.kind == "run.exhausted"]
        assert "resuming the same run in 2 min with 2 more round(s)" in notice.text
        assert f"grant-rounds {run_id} N" in notice.text and notice.url == PR_URL
        assert not any(n.kind == "run.failed" for n in front.notices)
        # Backoff not elapsed: nothing dispatches, and the idle reason says so.
        reason = h.loop.tick().idle_reason
        assert reason is not None and reason.startswith("backoff (1 queued; next eligible in 100s")
        h.clock.t += 101
        assert h.loop.tick().outcome == "done"
        assert h.runs == [(run_id, False), (run_id, True)], "same run, resumed"
        assert h.dstore.get("gh:issue:1").state == "done"  # type: ignore[union-attr]
        assert h.dstore.runs_started_since(0) == 2, "the daily cap counts the resume"
        resuming = [n for n in front.notices if n.kind == "run.resuming"]
        assert resuming and "2 granted fix round(s)" in resuming[0].text

    def test_the_granted_resume_ignores_the_resume_budget(self, tmp_path: Path) -> None:
        """The resume budget bounds crash-resume churn; a granted
        continuation is bounded by the grant, and an operator's grant must
        never be refused for it."""
        h = self._harness(tmp_path, daemon={"max_resumes_per_item": 0, "retry_backoff_s": 1})
        h.source.items = [gh_item()]
        h.outcomes = ["exhausted", "merged"]
        assert h.loop.tick().outcome == "retry"
        h.clock.t += 2
        assert h.loop.tick().outcome == "done"
        assert h.runs[1][1] is True

    def test_second_exhaustion_hands_over_with_the_run_pinned(self, tmp_path: Path) -> None:
        h = self._harness(tmp_path, daemon={"retry_backoff_s": 1})
        front = RecordingFrontend()
        h.loop.frontend = front
        h.source.items = [gh_item()]
        h.outcomes = ["exhausted", "exhausted"]
        assert h.loop.tick().outcome == "retry"
        h.clock.t += 2
        assert h.loop.tick().outcome == "failed"
        (run_id, _) = h.runs[0]
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "failed" and item.run_id == run_id
        assert item.last_error is not None and "2 already granted" in item.last_error
        assert h.source.calls[-1][0] == "abandoned"
        assert h.loop._consecutive_failures == 1
        notices = [n for n in front.notices if n.kind == "run.exhausted"]
        assert len(notices) == 2 and notices[1].level == "error"
        assert "handed over" in notices[1].text and f"grant-rounds {run_id} N" in notices[1].text
        # No fresh run was ever started for it: one run id throughout.
        assert {r for r, _ in h.runs} == {run_id}

    def test_retry_rounds_zero_hands_over_at_once(self, tmp_path: Path) -> None:
        h = self._harness(tmp_path, landing={"retry_rounds": 0})
        h.source.items = [gh_item()]
        h.outcomes = ["exhausted"]
        assert h.loop.tick().outcome == "failed"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "failed" and item.run_id == h.runs[0][0]
        assert "retry_rounds = 0" in (item.last_error or "")
        assert h.store.get_run(h.runs[0][0]).granted_rounds == 0

    def test_grant_rounds_resumes_a_handed_over_item_now(self, tmp_path: Path) -> None:
        h = self._harness(tmp_path, landing={"retry_rounds": 0})
        front = RecordingFrontend()
        h.loop.frontend = front
        h.source.items = [gh_item()]
        h.outcomes = ["exhausted", "merged"]
        assert h.loop.tick().outcome == "failed"
        (run_id, _) = h.runs[0]
        fresh = h.loop.grant_rounds(run_id, 3, by="brett")
        assert fresh.state == "queued" and fresh.run_id == run_id and fresh.not_before is None
        run = h.store.get_run(run_id)
        assert run.granted_rounds == 3 and run.exhausted is None
        # The source drops the failed label and re-claims, attributed.
        assert h.source.calls[-1] == ("requeued", "brett")
        (granted,) = [n for n in front.notices if n.kind == "run.rounds_granted"]
        assert "brett granted" in granted.text and "3 more fix round(s)" in granted.text
        assert h.loop.tick().outcome == "done"
        assert h.runs == [(run_id, False), (run_id, True)]

    def test_grant_rounds_during_the_backoff_skips_it(self, tmp_path: Path) -> None:
        h = self._harness(tmp_path)
        h.source.items = [gh_item()]
        h.outcomes = ["exhausted", "merged"]
        assert h.loop.tick().outcome == "retry"
        (run_id, _) = h.runs[0]
        assert h.loop.tick().idle_kind == "backoff"
        fresh = h.loop.grant_rounds(run_id, 1)
        assert fresh.not_before is None and fresh.pending_report is None
        assert h.store.get_run(run_id).granted_rounds == 3  # 2 automatic + 1
        assert not any(c[0] == "requeued" for c in h.source.calls)
        assert h.loop.tick().outcome == "done"

    def test_grant_rounds_refuses_anything_but_an_exhausted_run(self, tmp_path: Path) -> None:
        h = self._harness(tmp_path)
        h.source.items = [gh_item("1"), gh_item("2")]
        h.outcomes = ["merged", "failed"]
        assert h.loop.tick().outcome == "done"
        assert h.loop.tick().outcome == "retry"
        merged_run, failed_run = h.runs[0][0], h.runs[1][0]
        with pytest.raises(ValueError, match="is merged"):
            h.loop.grant_rounds(merged_run, 1)
        with pytest.raises(ValueError, match="not the pinned run"):
            h.loop.grant_rounds(failed_run, 1)  # a plain failure unpins its run
        with pytest.raises(ValueError, match="unknown run"):
            h.loop.grant_rounds("r_nope", 1)
        with pytest.raises(ValueError, match="at least 1"):
            h.loop.grant_rounds(merged_run, 0)

    def test_grant_rounds_refuses_the_run_in_flight(self, tmp_path: Path) -> None:
        h = self._harness(tmp_path)
        h.source.items = [gh_item()]
        h.outcomes = ["exhausted", "exhausted"]
        assert h.loop.tick().outcome == "retry"
        (run_id, _) = h.runs[0]
        # Fake "in flight": the item is running on that run.
        h.dstore.mark_resuming("gh:issue:1", run_id, h.clock())
        with pytest.raises(ValueError, match="is running"):
            h.loop.grant_rounds(run_id, 1)

    def test_a_scheduled_resume_survives_a_daemon_restart(self, tmp_path: Path) -> None:
        """The pin and the clock live in the store: a new process neither
        reconciles the run away nor resumes it early."""
        h = self._harness(tmp_path)
        h.source.items = [gh_item()]
        h.outcomes = ["exhausted"]
        assert h.loop.tick().outcome == "retry"
        (run_id, _) = h.runs[0]
        again = DaemonLoop(
            h.config,
            store=h.store,
            dstore=h.dstore,
            source=h.source,
            runner=h.runner,
            clock=h.clock,
        )
        again.recover()
        assert h.store.get_run(run_id).state == "failed"
        assert again.tick().idle_kind == "backoff"
        h.clock.t += 101
        h.outcomes = ["merged"]
        assert again.tick().outcome == "done"
        assert h.runs[-1] == (run_id, True)


class TestClaimProtocol:
    """#530: the claim is persisted before its side effects, signals are
    held while it runs, and a half-claim is settled on the next start."""

    def test_the_token_is_persisted_before_the_source_is_asked(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        seen: list[tuple[str | None, bool]] = []
        real_claim = h.source.claim

        def claim(item: WorkItem) -> bool:
            row = h.dstore.get(item.item_id)
            seen.append((item.claim_token, bool(row and row.claim_token == item.claim_token)))
            return real_claim(item)

        h.source.claim = claim  # type: ignore[method-assign]
        assert h.loop.tick().outcome == "done"
        (token, persisted) = seen[0]
        assert token and len(token) == 32 and persisted
        done = h.dstore.get("gh:issue:1")
        assert done is not None and done.claimed and done.claim_token == token

    def test_recovery_finishes_a_half_claim_whose_comment_landed(self, tmp_path: Path) -> None:
        """The deploy-restart sequence of 2026-08-29 21:10Z: the old process
        posted its claim comment and died before persisting the claim."""
        h = Harness(tmp_path)
        now = h.clock()
        h.dstore.upsert_new(gh_item(), now=now)
        h.dstore.mark_claiming("gh:issue:1", "a" * 32, now)  # ... then kill -9
        h.source.settle = {"a" * 32: True}
        front = RecordingFrontend()
        again = DaemonLoop(
            h.config,
            store=h.store,
            dstore=h.dstore,
            source=h.source,
            runner=h.runner,
            clock=h.clock,
            frontend=front,
        )
        again.recover()
        row = h.dstore.get("gh:issue:1")
        assert row is not None and row.claimed and row.state == "queued"
        (notice,) = [n for n in front.notices if n.kind == "recovery.claim_settled"]
        assert "the claim is finished" in notice.text
        # Dispatches without claiming again — the claim is complete.
        assert again.tick().outcome == "done"
        assert [c for c in h.source.calls if c[0] in ("claim", "settle")] == [
            ("settle", "gh:issue:1")
        ]

    def test_recovery_clears_a_half_claim_that_never_reached_the_source(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        now = h.clock()
        h.dstore.upsert_new(gh_item(), now=now)
        h.dstore.mark_claiming("gh:issue:1", "b" * 32, now)  # died before the POST
        h.source.items = [gh_item()]
        again = DaemonLoop(
            h.config,
            store=h.store,
            dstore=h.dstore,
            source=h.source,
            runner=h.runner,
            clock=h.clock,
        )
        again.recover()
        row = h.dstore.get("gh:issue:1")
        assert row is not None and not row.claimed and row.claim_token is None
        # Claimed from scratch on the next tick, under a fresh token.
        assert again.tick().outcome == "done"
        assert [c for c in h.source.calls if c[0] in ("claim", "settle")] == [
            ("settle", "gh:issue:1"),
            ("claim", "gh:issue:1"),
        ]
        assert h.dstore.get("gh:issue:1").claim_token != "b" * 32  # type: ignore[union-attr]

    def test_a_signal_during_the_claim_is_held_until_it_completes(self, tmp_path: Path) -> None:
        """SIGTERM mid-claim used to unwind the tick between the comment and
        the persist. It is now delivered after the claim is persisted."""
        import os
        import signal

        from sbxloop.daemon.loop import defer_signals

        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        delivered: list[int] = []
        previous = signal.signal(signal.SIGTERM, lambda signum, frame: delivered.append(signum))
        try:
            real_claim = h.source.claim

            def claim(item: WorkItem) -> bool:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.05)  # let the signal land while the claim is in flight
                assert delivered == [], "the handler must not have run yet"
                return real_claim(item)

            h.source.claim = claim  # type: ignore[method-assign]
            assert h.loop.tick().outcome == "done"
            assert delivered == [signal.SIGTERM], "delivered once, after the claim"
            assert h.dstore.get("gh:issue:1").claimed  # type: ignore[union-attr]
            # Outside the block the handler is the one we installed.
            with defer_signals():
                pass
            assert signal.getsignal(signal.SIGTERM) is not None
        finally:
            signal.signal(signal.SIGTERM, previous)


class TestRepoHealthSurface:
    """#516: the loop exposes the source's per-repo health and narrates
    suspension/recovery/resume as daemon notices."""

    class HealthySource(FakeSource):
        def __init__(self) -> None:
            super().__init__()
            from sbxloop.daemon.sources import RepoHealth

            self.health = [RepoHealth("o/a"), RepoHealth("o/b", 4, None, True, "gone", 1.0)]

        @property
        def repo_health(self) -> list[Any]:
            return list(self.health)

        def resume_repo(self, repo: str) -> Any:
            from sbxloop.daemon.sources import RepoHealth

            if repo != "o/b":
                raise KeyError(f"unknown repository {repo!r}")
            self.health[1] = RepoHealth("o/b")
            return self.health[1]

    def test_status_carries_repo_health_and_resume_narrates(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        source = self.HealthySource()
        h.loop.source = source
        front = RecordingFrontend()
        h.loop.frontend = front
        repos = h.loop.status()["repos"]
        assert [(r["repo"], r["state"]) for r in repos] == [("o/a", "ok"), ("o/b", "suspended")]
        assert repos[1]["reason"] == "gone"
        got = h.loop.resume_repo("o/b", by="brett")
        assert got["state"] == "ok"
        (notice,) = [n for n in front.notices if n.kind == "source.repo_resumed"]
        assert notice.text == "brett resumed polling of o/b"
        with pytest.raises(KeyError):
            h.loop.resume_repo("o/zzz")
        # The source's own transitions arrive as notices too.
        h.loop.source_notice("source.repo_suspended", "o/a", "🚫 repository o/a suspended")
        (suspended,) = [n for n in front.notices if n.kind == "source.repo_suspended"]
        assert suspended.level == "error"

    def test_a_single_repo_daemon_has_no_repo_health(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        assert h.loop.status()["repos"] == []
        with pytest.raises(ValueError, match="only a multi-repository daemon"):
            h.loop.resume_repo("o/a")


class TestWorkloadReport:
    """#757: the report the frontends get for a workload carries each task's
    output and the judge's verdict, and the same closing line the engine's
    RunResult does, read from the same store rows."""

    @staticmethod
    def _seed(h: Harness, run_id: str = "w1") -> None:
        h.store.create_run(run_id, "count the lines", kind="workload")
        h.store.set_run_title(run_id, "Count the lines")
        h.store.save_tasks(
            run_id,
            [
                TaskSpec(id="t1", title="Fetch"),
                TaskSpec(id="t2", title="Count"),
                TaskSpec(id="t3", title="Report"),
            ],
        )
        t1, t2, t3 = h.store.get_tasks(run_id)
        t1.state, t1.output = "done", TaskOutput(summary="fetched 3 files", files=["a", "b", "c"])
        t2.state, t2.output = "failed", TaskOutput(summary="counts written", files=["x"])
        t3.state = "skipped"
        for t in (t1, t2, t3):
            h.store.update_task(run_id, t)
        h.store.record_phase(
            run_id,
            "judge",
            task_id="t1",
            attempt=1,
            status="ok",
            output_json='{"passed": true, "unmet": [], "notes": ""}',
            started_at=1.0,
        )
        for attempt, row in enumerate(
            (
                '{"passed": true, "unmet": []}',
                '{"passed": false, "unmet": ["`summary.csv` has one row per file", "sorted"]}',
            ),
            start=1,
        ):
            h.store.record_phase(
                run_id,
                "judge",
                task_id="t2",
                attempt=attempt,
                status="ok",
                output_json=row,
                started_at=float(attempt),
            )
        h.store.set_run_state(run_id, "completed")

    def test_report_carries_outputs_verdicts_and_the_closing_line(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        self._seed(h)
        report = h.loop._report("w1", None)
        assert report.kind == "workload" and report.state == "completed"
        assert report.task_summary == "1/3 tasks done"
        assert report.pr is None and report.branch is None
        assert report.outputs == (
            TaskOutcome("t1", "Fetch", "done", "fetched 3 files", 3, "passed"),
            TaskOutcome(
                "t2",
                "Count",
                "failed",
                "counts written",
                1,
                "failed — unmet: `summary.csv` has one row per file (+1 more)",
            ),
            TaskOutcome("t3", "Report", "skipped", "", 0, None),
        )
        assert report.summary == (
            "Count the lines — 1/3 task(s) passed the judge\n"
            "t1: fetched 3 files (3 files)\n"
            "t2: counts written (1 file)"
        )

    def test_a_degraded_judge_row_reads_as_failed_closed(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        self._seed(h)
        h.store.record_phase(
            "w1",
            "judge",
            task_id="t2",
            attempt=3,
            status="failed",
            output_json='{"passed": false, "unmet": [], "degraded": true}',
            started_at=3.0,
        )
        outcomes = {o.task_id: o.verdict for o in h.loop._report("w1", None).outputs}
        assert outcomes["t2"] == "no usable verdict — failed closed"
        h.store.record_phase(
            "w1", "judge", task_id="t2", attempt=4, status="ok", output_json="{", started_at=4.0
        )
        assert {o.task_id: o.verdict for o in h.loop._report("w1", None).outputs}["t2"] == "failed"

    def test_a_code_run_report_has_no_outputs(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item()]
        assert h.loop.tick().outcome == "done"
        run_id = h.runs[0][0]
        report = h.loop._report(run_id, None)
        assert report.kind == "code" and report.outputs == () and report.summary is None

    def test_result_from_record_keeps_the_kind_and_the_summary(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        self._seed(h)
        result = h.loop._result_from_record("w1")
        assert result.kind == "workload" and result.state == "completed"
        assert result.summary == h.loop._report("w1", None).summary
        assert [tid for tid, _ in result.outputs] == ["t1", "t2"]


class TestWorkloadIntake:
    """#760: a workload item runs the operator persona and settles as
    `completed` — no PR to land, whatever `[github]` says — and a chat ask
    is an item with no issue behind it."""

    def test_a_workload_item_dispatches_its_kind_and_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h = Harness(tmp_path)
        seen: list[tuple[Any, ...]] = []

        class Engine:
            def __init__(self, *a: Any, **kw: Any) -> None: ...

            def start(self, outcome: str, **kw: Any) -> RunResult:
                seen.append((outcome, kw.get("kind"), kw.get("profile"), kw.get("repo")))
                run_id, kind = kw["run_id"], kw["kind"]
                h.store.create_run(run_id, outcome, kind=kind)
                state = "completed" if kind == "workload" else "merged"
                if kind == "code":
                    h.store.set_run_pr(
                        run_id, number=9, url=PR_URL, branch=f"sbxloop/{run_id}", head_sha="abc"
                    )
                h.store.set_run_state(run_id, state)
                return RunResult(run_id=run_id, state=state, kind=kind)

        monkeypatch.setattr(loop_mod, "LoopEngine", Engine)
        h.loop._runner = h.loop._default_runner  # type: ignore[method-assign]
        h.source.items = [gh_item("6", kind="workload", profile="research")]
        assert h.loop.tick().outcome == "done"
        # a legacy `gh:<n>` id carries no repository of its own
        assert seen == [(h.loop.outcome_text(gh_item("6")), "workload", "research", None)]
        h.source.items = [gh_item("7", item_id="gh:o/r:7", repo="o/r")]
        assert h.loop.tick().outcome == "done"
        assert seen[-1][1:] == ("code", None, "o/r")

    def test_a_completed_workload_is_done_with_a_repository_configured(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        front = RecordingFrontend()
        h.loop.frontend = front  # type: ignore[assignment]
        h.source.items = [gh_item("6", kind="workload")]
        h.outcomes = ["completed"]
        assert h.loop.tick().outcome == "done"
        item = h.dstore.get("gh:issue:6")
        assert item is not None and item.state == "done" and item.pending_report is None
        kinds = [c[0] for c in h.source.calls]
        assert kinds == ["claim", "started", "completed"]
        report = h.source.calls[-1][1]
        assert report.kind == "workload" and report.state == "completed"
        assert "the answer is 42" in (report.summary or "")
        assert [p.sink for p in report.published] == ["chat"]
        (notice,) = [n for n in front.notices if n.kind == "run.done"]
        assert notice.text.startswith("✅ gh:issue:6 completed (")
        assert "the answer is 42" in notice.text
        assert front.finished[0][1].state == "completed"

    def test_a_completed_report_the_source_cannot_take_stays_pending(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [gh_item("6", kind="workload")]
        h.outcomes = ["completed"]
        h.source.completed_ok = False
        assert h.loop.tick().outcome == "done"
        item = h.dstore.get("gh:issue:6")
        assert item is not None and item.state == "done"
        assert item.pending_report == "completed"
        h.source.completed_ok = True
        h.loop.tick()
        assert h.dstore.get("gh:issue:6").pending_report is None  # type: ignore[union-attr]
        assert [c[0] for c in h.source.calls].count("completed") == 2

    def test_a_workload_pins_the_asking_issue_as_its_result_issue(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        cfg = h.loop._item_config(gh_item("12", kind="workload"))
        assert cfg.workload.result_issue == 12
        # No pull request will close the issue: the completed report does.
        assert cfg.github.deliver_closes is None
        code = h.loop._item_config(gh_item("12"))
        assert code.workload.result_issue is None and code.github.deliver_closes == 12

    def test_a_chat_item_has_no_issue_and_no_repository(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        item = WorkItem(
            item_id="chat:9001",
            source_key="9001",
            title="Summarise the deploys",
            body="Summarise the deploys\n\nOne paragraph per day.",
            kind="workload",
            profile="research",
            requested_by="777",
        )
        cfg = h.loop._item_config(item)
        assert cfg.workload.result_issue is None and cfg.github.deliver_closes is None
        text = h.loop.outcome_text(item)
        assert text == (
            "Summarise the deploys\n\nOne paragraph per day.\n\n"
            "---\nThis work item came from: a chat ask by <@777>."
        )
        anonymous = h.loop.outcome_text(item.model_copy(update={"requested_by": None}))
        assert anonymous.endswith("a chat ask by an operator.")

    def test_a_chat_item_runs_to_done_without_github(self, tmp_path: Path) -> None:
        cfg = Config.model_validate({"home": str(tmp_path / "state")})
        h = Harness(tmp_path, cfg)
        h.source.name = "chat"
        h.source.items = [
            WorkItem(item_id="chat:9001", source_key="9001", title="ask", kind="workload")
        ]
        h.outcomes = ["completed"]
        assert h.loop.tick().outcome == "done"
        item = h.dstore.get("chat:9001")
        assert item is not None and item.state == "done" and item.pending_report is None
        assert [c[0] for c in h.source.calls] == ["claim", "started", "completed"]
        assert h.loop.status()["source"] == "chat"

    def test_the_status_names_the_source(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        assert h.loop.status()["source"] == "github"
