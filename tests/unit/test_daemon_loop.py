"""DaemonLoop tick/settle/recover against a fake runner and fake sources.

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
from sbxloop.daemon.loop import DaemonLoop, RunHandle, day_window
from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.daemon.sources import GitHubLabels, PrSnapshot
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import TERMINAL_RUN_STATES, RunResult, TaskRecord, TaskSpec
from sbxloop.engine.store import StateStore
from sbxloop.errors import RunCancelledError, SbxError, StateError, WorkerError
from sbxloop.events import Event, EventBus
from sbxloop.gh.ops import ChecksVerdict, MergeOutcome, SubmittedReview
from tests.fakes.github_errors import field_error
from tests.unit.test_hostgit import make_repo, make_upstream_and_clone, push_upstream_commit


class FakeSource:
    name = "inbox"

    def __init__(self, items: list[WorkItem] | None = None, *, claim_ok: bool = True) -> None:
        self.items = items or []
        self.claim_ok = claim_ok
        self.calls: list[tuple[str, Any]] = []

    def poll(self) -> list[WorkItem]:
        return list(self.items)

    def claim(self, item: WorkItem) -> bool:
        self.calls.append(("claim", item.item_id))
        return self.claim_ok

    def report_started(self, item: WorkItem, run_id: str) -> None:
        self.calls.append(("started", run_id))

    def report_success(self, item: WorkItem, report: RunReport) -> None:
        self.calls.append(("success", report))

    def report_delivery_failed(self, item: WorkItem, report: RunReport) -> None:
        self.calls.append(("delivery_failed", report))

    def report_retry(self, item: WorkItem, error: str, attempts_left: int) -> None:
        self.calls.append(("retry", attempts_left))

    def report_abandoned(self, item: WorkItem, error: str) -> None:
        self.calls.append(("abandoned", error))

    def report_cancelled(self, item: WorkItem, report: RunReport) -> None:
        self.calls.append(("cancelled", report))

    def report_requeued(self, item: WorkItem, by: str) -> None:
        self.calls.append(("requeued", by))

    def file_backlog(self, title: str, body: str, origin_run_id: str, *, trigger: bool) -> str:
        self.calls.append(("backlog", title))
        return f"inbox:{title}"


def inbox_item(key: str = "a.md") -> WorkItem:
    return WorkItem(item_id=f"inbox:{key}", source="inbox", source_key=key, title=f"Do {key}")


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Harness:
    """A DaemonLoop over real stores with a scripted runner."""

    def __init__(self, tmp_path: Path, config: Config | None = None) -> None:
        self.tmp_path = tmp_path
        self.config = config or Config.model_validate({"state_dir": str(tmp_path / "state")})
        self.store = StateStore(self.config.state_dir / "state.db")
        self.dstore = DaemonStore(self.config.state_dir / "state.db")
        self.clock = Clock()
        self.outcomes: list[
            str
        ] = []  # scripted per run: "completed"|"failed"|"raise"|"deliver_fail"
        self.runs: list[tuple[str, bool]] = []
        self.run_configs: list[Config] = []  # the config each dispatch handed the runner
        self.source = FakeSource()
        self.loop = DaemonLoop(
            self.config,
            store=self.store,
            dstore=self.dstore,
            sources=[self.source],
            runner=self.runner,
            clock=self.clock,
        )

    def runner(
        self, item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
    ) -> RunResult:
        self.runs.append((run_id, resume))
        self.run_configs.append(cfg)
        kind = self.outcomes.pop(0) if self.outcomes else "completed"
        if kind == "raise":
            raise WorkerError("sandbox exploded")
        state = "failed" if kind == "failed" else "completed"
        self.store.create_run(run_id, "outcome") if not resume else None
        self.store.set_run_state(run_id, state)
        if kind == "deliver_fail":
            self.store.append_event(
                Event.now(
                    "run.deliver", run_id, repo="o/r", error=field_error("empty_repo_ref_409")
                )
            )
        elif kind == "completed":
            self.store.append_event(Event.now("run.report", run_id, repo="o/r", issue=5, url="u5"))
            self.store.append_event(
                Event.now("run.deliver", run_id, repo="o/r", pr=9, url="https://x/pull/9")
            )
        return RunResult(run_id=run_id, state=state)


class TestTick:
    def test_one_item_success_end_to_end(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
        result = h.loop.tick()
        assert result.dispatched == "inbox:a.md" and result.outcome == "done"
        assert h.dstore.get("inbox:a.md").state == "done"  # type: ignore[union-attr]
        kinds = [c[0] for c in h.source.calls]
        assert kinds == ["claim", "started", "success"]
        report = h.source.calls[-1][1]
        assert report.delivery == (9, "https://x/pull/9") and report.tracking_issue == (5, "u5")
        # ledger row closed; a second tick finds nothing new
        assert h.dstore.runs_started_since(0) == 1
        assert h.loop.tick().idle_reason == "no_work"

    def test_success_report_carries_the_filed_backlog_refs(self, tmp_path: Path) -> None:
        """An audit's deliverable is what it filed: the refs ride on the
        RunReport so the source can name them in the closing comment."""
        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
        h.loop._collect_backlog = lambda run_id, source: ["inbox:finding-a", "inbox:finding-b"]  # type: ignore[method-assign]
        front = RecordingFrontend()
        h.loop.frontend = front  # type: ignore[assignment]
        assert h.loop.tick().outcome == "done"
        report = h.source.calls[-1][1]
        assert report.filed == ("inbox:finding-a", "inbox:finding-b")
        # One notice per settle names them (no separate "filed N backlog item(s)" line).
        done = [t for t in front.seen if t.startswith("✅ ")]
        assert done == [
            "✅ inbox:a.md done (no tasks ran) · PR https://x/pull/9"
            " · filed `inbox:finding-a`, `inbox:finding-b`"
        ]
        assert not any(t.startswith("filed ") for t in front.seen)

    def test_daily_cap_blocks_dispatch(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "daemon": {"max_runs_per_day": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [inbox_item("a.md"), inbox_item("b.md")]
        assert h.loop.tick().outcome == "done"
        second = h.loop.tick()
        assert second.idle_reason == "daily_cap" and second.dispatched is None
        assert h.dstore.get("inbox:b.md").state == "queued"  # type: ignore[union-attr]
        # the calendar day rolls (local midnight passes) → dispatch resumes
        h.clock.t = day_window(h.clock.t, cfg.daemon.run_cap_timezone)[1]
        assert h.loop.tick().outcome == "done"

    def test_retry_then_abandon_at_cap(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "daemon": {"max_attempts_per_item": 2, "retry_backoff_s": 10},
            }
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [inbox_item()]
        h.outcomes = ["raise", "failed"]
        assert h.loop.tick().outcome == "retry"
        assert h.dstore.get("inbox:a.md").state == "queued"  # type: ignore[union-attr]
        assert ("retry", 1) in h.source.calls
        # backoff not elapsed → no dispatch, and the idle reason says so
        reason = h.loop.tick().idle_reason
        assert reason is not None and reason.startswith("backoff (1 queued")
        h.clock.t += 11
        assert h.loop.tick().outcome == "abandoned"
        assert h.dstore.get("inbox:a.md").state == "abandoned"  # type: ignore[union-attr]
        assert any(c[0] == "abandoned" for c in h.source.calls)
        # claim happened once: retries reuse the claimed item
        assert [c for c in h.source.calls if c[0] == "claim"] == [("claim", "inbox:a.md")]

    def test_claim_failure_abandons_without_running(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
        h.source.claim_ok = False
        assert h.loop.tick().outcome == "abandoned"
        assert h.runs == []
        assert h.dstore.get("inbox:a.md").state == "abandoned"  # type: ignore[union-attr]

    def test_delivery_failure_is_terminal_not_retried(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
        h.outcomes = ["deliver_fail"]
        assert h.loop.tick().outcome == "delivery_failed"
        assert h.dstore.get("inbox:a.md").state == "abandoned"  # type: ignore[union-attr]
        assert any(c[0] == "delivery_failed" for c in h.source.calls)
        assert not any(c[0] == "retry" for c in h.source.calls)

    def test_circuit_breaker_opens_cools_and_resets(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "daemon": {
                    "max_attempts_per_item": 1,
                    "max_consecutive_failures": 2,
                    "breaker_cooldown_s": 100,
                    "max_runs_per_day": 100,
                },
            }
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [inbox_item("a.md"), inbox_item("b.md"), inbox_item("c.md")]
        h.outcomes = ["raise", "raise", "completed"]
        assert h.loop.tick().outcome == "abandoned"
        assert h.loop.tick().outcome == "abandoned"
        assert h.loop.tick().idle_reason == "breaker"
        h.clock.t += 101
        assert h.loop.tick().outcome == "done"  # half-open let one through; success
        assert h.loop.status()["breaker_open"] is False

    def test_paused_loop_idles(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
        h.loop.pause()
        assert h.loop.tick().idle_reason == "paused"
        h.loop.unpause()
        assert h.loop.tick().outcome == "done"


class TestOutcomeAndConfig:
    def test_outcome_text_embeds_source_and_backlog_note(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": {"backlog": "github"},
            }
        )
        h = Harness(tmp_path, cfg)
        gh = WorkItem(
            item_id="gh:4",
            source="github",
            source_key="4",
            title="Fix it",
            body="details",
            url="https://x/4",
        )
        text = h.loop.outcome_text(gh)
        assert text.startswith("Fix it\n\ndetails")
        assert "GitHub issue #4 in o/r (https://x/4)" in text
        assert ".sbxloop/backlog/" in text
        off = Config.model_validate({"state_dir": str(tmp_path / "state")})
        assert ".sbxloop/backlog/" not in Harness(tmp_path, off).loop.outcome_text(inbox_item())

    def test_item_config_forces_delivery_for_github_items_only(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repo": "o/r", "create_repo": True},
                "keep_on_failure": True,
            }
        )
        h = Harness(tmp_path, cfg)
        gh = h.loop._item_config(
            WorkItem(item_id="gh:1", source="github", source_key="1", title="x")
        )
        assert gh.github.report and gh.github.deliver and gh.github.deliver_draft
        assert gh.github.create_repo is False and gh.keep_on_failure is False
        inbox = h.loop._item_config(inbox_item())
        assert inbox.github.deliver is False and inbox.keep_on_failure is False

    def test_audit_items_carry_the_audit_contract_and_never_deliver(self, tmp_path: Path) -> None:
        """The discovery lane: an audit's output is the issues it files. Its
        tree is deliberately unchanged, so delivery would only raise
        "nothing to deliver" and mis-settle it as failed — it is forced off;
        the outcome text swaps the backlog note for the audit contract."""
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": {"backlog": "github"},
            }
        )
        h = Harness(tmp_path, cfg)
        audit = WorkItem(
            item_id="gh:9",
            source="github",
            source_key="9",
            kind="audit",
            title="Audit X",
            body="look at Y",
            url="https://x/9",
        )
        conf = h.loop._item_config(audit)
        assert conf.github.deliver is False and conf.github.report is True
        text = h.loop.outcome_text(audit)
        assert "This is an AUDIT" in text and ".sbxloop/backlog/" in text
        assert "**Evidence**" in text and "empty result is a valid" in text
        assert "OUT OF SCOPE" not in text  # the patch-lane note is not appended twice
        # a patch item is unchanged
        patch = h.loop._item_config(
            WorkItem(item_id="gh:1", source="github", source_key="1", title="x")
        )
        assert patch.github.deliver is True

    def test_audit_contract_forbids_suite_verify_and_failed_audit_still_files(
        self, tmp_path: Path
    ) -> None:
        """Field failure rakvqn6fr: the planner gave an audit `uv run pytest`
        as its verify command — an audit chartered to find a failing test —
        so it failed by construction and its findings were lost."""
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": {"backlog": "github", "max_attempts_per_item": 1},
            }
        )
        h = Harness(tmp_path, cfg)
        audit = WorkItem(item_id="gh:9", source="github", source_key="9", kind="audit", title="A")
        text = h.loop.outcome_text(audit)
        assert "need NO verify_commands" in text and "Never the project's test suite" in text
        # a failed audit run still gets its findings collected
        h.source.items = [audit]
        h.source.name = "github"  # type: ignore[misc]
        h.outcomes = ["failed"]
        h.loop._collect_backlog = lambda run_id, source: ["gh:50"]  # type: ignore[method-assign]
        front = RecordingFrontend()
        h.loop.frontend = front  # type: ignore[assignment]
        assert h.loop.tick().outcome == "abandoned"
        assert (
            "🔎 gh:9 failed but its findings were filed · [#50](https://github.com/o/r/issues/50)"
            in front.seen
        )

    def test_item_config_skips_tracking_issue_when_disabled(self, tmp_path: Path) -> None:
        """tracking_issue=false (#251): the source issue already is the
        tracker, so no per-run issue — delivery stays forced on."""
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repo": "o/r", "report": True},
                "daemon": {"tracking_issue": False},
            }
        )
        gh = Harness(tmp_path, cfg).loop._item_config(
            WorkItem(item_id="gh:1", source="github", source_key="1", title="x")
        )
        assert gh.github.report is False and gh.github.deliver is True


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
            h.store.set_run_state(run_id, "running")  # mid-flight → resumable
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
                "state_dir": str(tmp_path / "state"),
                "daemon": {"max_attempts_per_item": 3, "max_consecutive_failures": 1},
            }
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [inbox_item()]
        self._run_until_cancelled(h, requester="Discord user `brett`")
        item = h.dstore.get("inbox:a.md")
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
            {"state_dir": str(tmp_path / "state"), "daemon": {"retry_backoff_s": 900}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [inbox_item()]
        self._run_until_cancelled(h, retry=True)
        item = h.dstore.get("inbox:a.md")
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
        h.source.items = [inbox_item()]
        self._run_until_cancelled(h, requester="Discord user `brett`", retry=True)
        item = h.dstore.get("inbox:a.md")
        assert item is not None and item.state == "queued"
        record = h.store.get_run(h.runs[0][0])
        assert record.state == "cancelled"
        assert record.reason is not None and "Discord user `brett`" in record.reason

    def test_cancel_appends_chronology_event(self, tmp_path: Path) -> None:
        """Reconciliation chronology is append-only: existing events keep
        their order and a `run.cancelled` event is added."""
        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
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
        h.source.items = [inbox_item()]
        started = threading.Event()
        release = threading.Event()

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            h.runs.append((run_id, resume))
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "running")
            started.set()
            assert release.wait(5)
            raise RunCancelledError("cancelled")

        h.loop._runner = runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        h.loop.abandon_item("inbox:a.md", "operator says stop")
        release.set()
        t.join(5)
        item = h.dstore.get("inbox:a.md")
        assert item is not None and item.state == "abandoned"
        assert h.store.get_run(h.runs[0][0]).state in TERMINAL_RUN_STATES

    def test_requeue_of_pinned_dead_run_ends_run_terminal(self, tmp_path: Path) -> None:
        """A pinned run that is not in flight is dead; requeue must not leave
        it `running`."""
        h = Harness(tmp_path)
        h.dstore.upsert_new(inbox_item(), h.clock())
        h.dstore.mark_claimed("inbox:a.md", h.clock())
        h.dstore.mark_running("inbox:a.md", "rdead", h.clock())
        h.store.create_run("rdead", "x")
        h.store.set_run_state("rdead", "running")
        h.loop.requeue_item("inbox:a.md")
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
        h.source.items = [inbox_item()]
        self._run_until_cancelled(h, error=WorkerError)
        item = h.dstore.get("inbox:a.md")
        assert item is not None and item.state == "queued" and item.attempts == 1
        assert h.loop._consecutive_failures == 1
        assert [c[0] for c in h.source.calls] == ["claim", "started", "retry"]

    def test_stale_cancel_never_taints_a_later_run(self, tmp_path: Path) -> None:
        """A cancel that lands after the engine already finished settles that
        run normally and must not carry over to the next item."""
        h = Harness(tmp_path)
        h.source.items = [inbox_item("a.md"), inbox_item("b.md")]
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
        assert h.dstore.get("inbox:a.md").state == "done"  # type: ignore[union-attr]
        h.loop._runner = h.runner
        h.outcomes = ["raise"]
        assert h.loop.tick().outcome == "retry"  # b.md fails as a failure, not a cancel
        assert not any(c[0] == "cancelled" for c in h.source.calls)

    def test_retry_settled_item_is_attributed(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "daemon": {"max_attempts_per_item": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [inbox_item()]
        h.outcomes = ["raise"]
        assert h.loop.tick().outcome == "abandoned"
        with pytest.raises(KeyError):
            h.loop.retry_item("inbox:nope")
        h.loop.retry_item("inbox:a.md", "Discord user `brett`")
        item = h.dstore.get("inbox:a.md")
        assert item is not None and item.state == "queued" and item.attempts == 0
        assert item.last_error == "re-queued by Discord user `brett`"
        assert ("requeued", "Discord user `brett`") in h.source.calls
        assert h.loop.tick().outcome == "done"

    def test_retry_of_cancelled_item_runs_fresh(self, tmp_path: Path) -> None:
        """#246 + #229: `!sbx retry` is the way back from a cancel — attempts
        reset, run unpinned so the next tick starts over, not resumes."""
        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
        h.dstore.upsert_new(inbox_item(), 1.0)
        h.dstore.mark_running("inbox:a.md", "r1", 1.0)
        h.dstore.mark_cancelled("inbox:a.md", "cancelled by op", 2.0)
        with pytest.raises(ValueError, match="use retry"):
            h.loop.requeue_item("inbox:a.md")
        got = h.loop.retry_item("inbox:a.md", "op")
        assert got.state == "queued" and got.attempts == 0 and got.run_id is None


class TestShutdownAndRecovery:
    def test_shutdown_mid_run_leaves_item_running(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
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
        assert h.loop.current is not None and h.loop.current.item.item_id == "inbox:a.md"
        h.loop.request_stop()
        release.set()
        t.join(5)
        assert h.dstore.get("inbox:a.md").state == "running"  # type: ignore[union-attr]
        assert h.loop.current is None

    def test_genuine_failure_after_stop_requested_still_settles(self, tmp_path: Path) -> None:
        """Review: stop-requested + any exception was treated as 'interrupted',
        masking real failures and leaving the item running. Only a run whose
        persisted state is still resumable is interrupted; a run that
        actually failed settles as a failure."""
        cfg = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "daemon": {"max_attempts_per_item": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [inbox_item()]
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
        item = h.dstore.get("inbox:a.md")
        assert item is not None and item.state == "abandoned"  # settled, not left running
        assert any(c[0] == "abandoned" for c in h.source.calls)

    def test_cancel_at_boundary_after_stop_is_interrupted(self, tmp_path: Path) -> None:
        """The genuine shutdown case: the run stays resumable (non-terminal
        persisted state) so the item is left running for recovery."""
        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
        started = threading.Event()

        def cancelled_runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            started.set()
            while not h.loop.stopping:
                time.sleep(0.01)
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "running")  # still resumable
            raise StateError("run cancelled at phase boundary")

        h.loop._runner = cancelled_runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        h.loop.request_stop()
        t.join(5)
        assert h.dstore.get("inbox:a.md").state == "running"  # type: ignore[union-attr]

    def test_recover_completed_run_settles(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_done", now=2.0)
        h.store.create_run("r_done", "x")
        h.store.set_run_state("r_done", "completed")
        h.store.append_event(
            Event.now("run.deliver", "r_done", repo="o/r", pr=3, url="https://x/pull/3")
        )
        h.loop.recover()
        assert h.dstore.get("inbox:a.md").state == "done"  # type: ignore[union-attr]
        assert any(c[0] == "success" for c in h.source.calls)
        assert h.runs == []  # nothing re-ran

    def test_recover_nonterminal_run_resumes_same_attempt(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_live", now=2.0)
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "running")
        h.outcomes = ["completed"]
        h.loop.recover()
        # Recovery only queues the resume (run pinned); the tick runs it.
        pending = h.dstore.get("inbox:a.md")
        assert pending is not None and pending.state == "queued" and pending.run_id == "r_live"
        assert h.runs == []
        assert h.loop.tick().outcome == "done"
        assert h.runs == [("r_live", True)]  # resumed, not restarted
        item = h.dstore.get("inbox:a.md")
        assert item is not None and item.state == "done" and item.attempts == 1
        # A resume is the same attempt but a fresh engine wall clock: the
        # daily cap sees it (#254/#234).
        assert h.dstore.runs_started_since(0) == 2
        assert h.loop.status()["resumes_today"] == 1

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
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_live", now=2.0)
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "running")
        h.outcomes = ["completed"]
        h.loop.recover()
        h.loop.tick()
        agent, gh = "sbxloop-r_live-agent", "sbxloop-r_live-github"
        assert [c for c in calls if c[0] == "rm"] == [("rm", agent), ("rm", gh)]
        secret_calls = [c[1] for c in calls if c[0] == "secret_rm"]
        # Agent: the Copilot custom secret (host+env — sbx rejects env-only
        # selection); github: the built-in service secret.
        assert secret_calls == [
            {"host": "api.github.com", "env": "COPILOT_GITHUB_TOKEN", "sandbox": agent},
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
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_live", now=2.0)
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "running")
        h.outcomes = ["completed"]
        h.loop.recover()
        h.loop.tick()
        assert len(calls) == 2
        assert h.runs == [("r_live", True)]

    @staticmethod
    def _interrupted(h: Harness, run_id: str = "r_live") -> None:
        now = h.clock()
        h.dstore.upsert_new(inbox_item(), now=now)
        h.dstore.mark_claimed("inbox:a.md", now=now)
        h.dstore.mark_running("inbox:a.md", run_id, now=now)
        h.store.create_run(run_id, "x")
        h.store.set_run_state(run_id, "running")

    def test_recovered_resume_waits_behind_pause_breaker_and_cap(self, tmp_path: Path) -> None:
        """recover() used to dispatch resumes directly, skipping every
        guardrail tick() enforces (#254): a daemon restarting into an open
        breaker, a spent cap, or an operator pause resumed anyway."""
        cfg = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "daemon": {"max_runs_per_day": 1}}
        )
        h = Harness(tmp_path, cfg)
        self._interrupted(h)
        h.outcomes = ["completed"]
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
                "state_dir": str(tmp_path / "state"),
                "daemon": {"max_resumes_per_item": 1, "max_attempts_per_item": 2},
            }
        )
        h = Harness(tmp_path, cfg)
        self._interrupted(h)
        h.dstore.mark_resuming("inbox:a.md", "r_live", now=h.clock())  # one resume spent
        h.loop.recover()
        result = h.loop.tick()
        assert result.outcome == "retry" and h.runs == []  # not resumed
        item = h.dstore.get("inbox:a.md")
        assert item is not None and item.state == "queued" and item.run_id is None
        assert any(c[0] == "retry" for c in h.source.calls)
        # The next tick (past the retry backoff) is a FRESH dispatch,
        # counting as attempt 2.
        h.clock.t += cfg.daemon.retry_backoff_s + 1
        h.outcomes = ["completed"]
        assert h.loop.tick().outcome == "done"
        assert len(h.runs) == 1 and h.runs[0][1] is False
        assert h.dstore.get("inbox:a.md").attempts == 2  # type: ignore[union-attr]

    def test_zero_resume_budget_never_resumes(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "daemon": {"max_resumes_per_item": 0}}
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
            {"state_dir": str(tmp_path / "state"), "daemon": {"max_consecutive_failures": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [inbox_item("a.md")]
        h.outcomes = ["failed"]
        assert h.loop.tick().outcome == "retry"
        assert h.loop.status()["breaker_open"] is True
        # A new loop over the same store (a restarted daemon) sees it.
        again = DaemonLoop(
            cfg,
            store=h.store,
            dstore=h.dstore,
            sources=[h.source],
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

        flaky = Flaky([inbox_item()])
        h.loop.sources = [flaky]
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
        assert polls == 4 and result.dispatched == "inbox:a.md"
        assert h.loop._source_failures == {}

    def test_recover_failed_run_takes_failure_path(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "daemon": {"max_attempts_per_item": 3}}
        )
        h = Harness(tmp_path, cfg)
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_dead", now=2.0)
        h.store.create_run("r_dead", "x")
        h.store.set_run_state("r_dead", "failed")
        h.loop.recover()
        assert h.dstore.get("inbox:a.md").state == "queued"  # type: ignore[union-attr]
        assert any(c[0] == "retry" for c in h.source.calls)

    def test_recover_claimed_but_unstarted_requeues(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.set_state("inbox:a.md", "running", now=1.5)
        h.loop.recover()
        got = h.dstore.get("inbox:a.md")
        assert got is not None and got.state == "queued" and got.claimed is True
        # and the next tick runs it WITHOUT re-claiming
        h.outcomes = ["completed"]
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
            h.store.set_run_state(run_id, "running")
            raise StateError("run cancelled at phase boundary")

        return started, runner

    def test_cli_abandon_of_running_item_cancels_run_and_reports(self, tmp_path: Path) -> None:
        """Field (#227/#228): the only way to abandon a spiraling item was
        poking DaemonStore from Python — and even then the settle path would
        have overwritten the row. An operator's row-level abandon must cancel
        the in-flight run, win over the run's own outcome, and reach the
        source exactly once without tripping the breaker."""
        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
        started, runner = self._blocking_runner(h)
        h.loop._runner = runner
        results: list[Any] = []
        t = threading.Thread(target=lambda: results.append(h.loop.tick()))
        t.start()
        assert started.wait(5)
        run_id = h.loop.current.run_id  # type: ignore[union-attr]
        # another process: only the row changes
        h.dstore.abandon("inbox:a.md", "operator: doomed plan", h.clock())
        t.join(10)
        assert results and results[0].outcome == "abandoned"
        item = h.dstore.get("inbox:a.md")
        assert item is not None and item.state == "abandoned" and item.run_id == run_id
        assert [c for c in h.source.calls if c[0] == "abandoned"] == [
            ("abandoned", "operator: doomed plan")
        ]
        assert h.loop._consecutive_failures == 0
        # ledger closed as abandoned, and recovery leaves the item alone
        h.loop.recover()
        assert h.dstore.get("inbox:a.md").state == "abandoned"  # type: ignore[union-attr]

    def test_cli_requeue_of_running_item_cancels_and_next_tick_starts_fresh(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
        started, runner = self._blocking_runner(h)
        h.loop._runner = runner
        results: list[Any] = []
        t = threading.Thread(target=lambda: results.append(h.loop.tick()))
        t.start()
        assert started.wait(5)
        first_run = h.loop.current.run_id  # type: ignore[union-attr]
        h.dstore.requeue("inbox:a.md", h.clock())
        t.join(10)
        assert results and results[0].outcome == "requeued"
        item = h.dstore.get("inbox:a.md")
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
        h.source.items = [inbox_item()]
        started, runner = self._blocking_runner(h)
        h.loop._runner = runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        got = h.loop.abandon_item("inbox:a.md", "operator says stop")
        assert got.state == "abandoned"
        t.join(10)
        assert [c for c in h.source.calls if c[0] == "abandoned"] == [
            ("abandoned", "operator says stop")
        ]

    def test_loop_abandon_queued_item_reports_immediately(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(inbox_item(), now=1.0)
        got = h.loop.abandon_item("inbox:a.md")
        assert got.state == "abandoned" and got.last_error == "abandoned by operator"
        assert h.source.calls == [("abandoned", "abandoned by operator")]
        assert h.loop.tick().idle_kind == "no_work"

    def test_loop_retry_then_dispatch_is_a_fresh_plan(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "daemon": {"max_attempts_per_item": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [inbox_item()]
        h.outcomes = ["failed"]
        assert h.loop.tick().outcome == "abandoned"
        first_run = h.runs[0][0]
        with pytest.raises(ValueError):
            h.loop.requeue_item("inbox:a.md")  # abandoned items need retry, not requeue
        got = h.loop.retry_item("inbox:a.md")
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
        h.source.items = [inbox_item("a.md"), inbox_item("b.md")]
        started = threading.Event()
        release = threading.Event()

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            h.runs.append((run_id, resume))
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "running")
            started.set()
            assert release.wait(5)
            raise StateError("run cancelled at phase boundary")

        h.loop._runner = runner
        results: list[Any] = []
        t = threading.Thread(target=lambda: results.append(h.loop.tick()))
        t.start()
        assert started.wait(5)
        assert h.loop.cancel_current("discord op", retry=True) is True
        h.dstore.abandon("inbox:a.md", "operator: doomed plan", h.clock())
        release.set()
        t.join(10)
        assert results and results[0].outcome == "abandoned"
        a = h.dstore.get("inbox:a.md")
        assert a is not None and a.state == "abandoned" and a.attempts == 1
        assert [c[0] for c in h.source.calls if c[0] in ("abandoned", "cancelled")] == ["abandoned"]
        # b.md's failure settles as a failure: the stale cancel is gone.
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
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_dead", now=2.0)
        h.dstore.finish_ledger("r_dead", "interrupted", 3.0)  # clean shutdown
        h.store.create_run("r_dead", "x")
        h.store.set_run_state("r_dead", "running")
        h.dstore.abandon("inbox:a.md", "operator: doomed plan", 4.0)  # CLI, no daemon
        h.loop.recover()
        assert h.source.calls == [("abandoned", "operator: doomed plan")]
        assert removed == ["sbxloop-r_dead-agent", "sbxloop-r_dead-github"]
        row = h.dstore._conn.execute(
            "SELECT result FROM daemon_runs WHERE run_id = 'r_dead'"
        ).fetchone()
        assert row["result"] == "abandoned"
        item = h.dstore.get("inbox:a.md")
        assert item is not None and item.state == "abandoned" and item.run_id == "r_dead"
        assert h.loop.tick().idle_kind == "no_work"
        h.loop.recover()  # idempotent: the ledger is closed, nothing to report
        assert len(h.source.calls) == 1

    def test_recover_closes_run_of_item_requeued_offline(self, tmp_path: Path) -> None:
        """Same, for `sbxloop daemon requeue` with no daemon: the crashed
        run's ledger row is still open; recovery closes it (no source
        report — requeue is not a verdict) and the item is dispatched fresh."""
        h = Harness(tmp_path)
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_dead", now=2.0)  # crash: ledger open
        h.store.create_run("r_dead", "x")
        h.store.set_run_state("r_dead", "running")
        h.dstore.requeue("inbox:a.md", 4.0)
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
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_live", now=2.0)
        h.dstore.finish_ledger("r_live", "interrupted", 3.0)
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "running")
        h.loop.recover()
        h.loop.tick()
        assert h.runs == [("r_live", True)]

    def test_loop_requeue_stale_pinned_item_closes_ledger(self, tmp_path: Path) -> None:
        """A dead process left the item running with a pinned run; requeue
        (daemon idle) must unpin it so recovery does not resume it."""
        h = Harness(tmp_path)
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_old", now=2.0)
        h.store.create_run("r_old", "x")
        h.store.set_run_state("r_old", "running")
        got = h.loop.requeue_item("inbox:a.md")
        assert got.state == "queued" and got.run_id is None
        h.loop.recover()  # nothing running any more
        assert h.runs == []
        # attempts were kept, so the usual retry backoff still applies
        assert h.loop.tick().idle_kind == "backoff"
        h.clock.t += 10_000
        assert h.loop.tick().outcome == "done"
        assert h.runs[0][0] != "r_old" and h.runs[0][1] is False

    def test_cli_retry_of_abandoned_item_reaches_the_source_before_dispatch(
        self, tmp_path: Path
    ) -> None:
        """A row-only `sbxloop daemon retry` (other process) cannot call
        report_requeued, so the inbox file would stay under failed/ and the
        GitHub issue would keep its failed label. The row carries the debt;
        the next tick pays it before the fresh dispatch — once."""
        cfg = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "daemon": {"max_attempts_per_item": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.source.items = [inbox_item()]
        h.outcomes = ["failed"]
        assert h.loop.tick().outcome == "abandoned"
        h.dstore.retry("inbox:a.md", h.clock(), "re-queued by operator (CLI)")  # CLI, row only
        assert h.loop.tick().outcome == "done"
        kinds = [c[0] for c in h.source.calls]
        assert kinds == ["claim", "started", "abandoned", "requeued", "started", "success"]
        assert h.source.calls[3] == ("requeued", "operator (CLI)")
        assert h.dstore.get("inbox:a.md").pending_report is None  # type: ignore[union-attr]

    def test_cli_abandon_of_queued_item_reaches_the_source(self, tmp_path: Path) -> None:
        """An item abandoned from the CLI while merely queued has no run in
        flight to override and no ledger row for recovery to find; the tick
        sweep still delivers the abandon (paused or not), exactly once."""
        h = Harness(tmp_path)
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.abandon("inbox:a.md", "not worth it", 2.0)  # CLI, row only
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
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_dead", now=2.0)
        h.dstore.finish_ledger("r_dead", "interrupted", 3.0)
        h.dstore.mark_resume_pending("inbox:a.md", 4.0)
        h.loop.abandon_item("inbox:a.md", "never mind")
        assert removed == ["sbxloop-r_dead-agent", "sbxloop-r_dead-github"]
        assert h.source.calls == [("abandoned", "never mind")]
        removed.clear()
        h.dstore.retry("inbox:a.md", 5.0)
        h.dstore.mark_running("inbox:a.md", "r_dead2", now=6.0)
        h.dstore.finish_ledger("r_dead2", "interrupted", 7.0)
        h.dstore.mark_resume_pending("inbox:a.md", 8.0)
        h.loop.requeue_item("inbox:a.md")  # same for an unpin
        assert removed == ["sbxloop-r_dead2-agent", "sbxloop-r_dead2-github"]
        assert h.dstore.unsettled_runs() == []


class RecordingFrontend:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def daemon_event(self, text: str) -> None:
        self.seen.append(text)

    def run_started(self, *a: Any) -> None: ...
    def run_finished(self, *a: Any) -> None: ...


class TestWorkspacePosture:
    """#255: unattended runs answer the dirty-tree question by config and
    start from a fetch-refreshed checkout. Real git repos in tmp_path (the
    hostgit test helpers) so the fast-forward is exercised for real."""

    @staticmethod
    def _config(tmp_path: Path, workspace: Path, **daemon: Any) -> Config:
        return Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "sandbox": {"workspace": str(workspace)},
                "daemon": daemon,
            }
        )

    def test_git_checkout_workspace_gets_clone_isolation_by_default(self, tmp_path: Path) -> None:
        h = Harness(tmp_path, self._config(tmp_path, make_repo(tmp_path)))
        assert h.config.sandbox.workspace_isolation == "auto"
        assert h.loop._item_config(inbox_item()).sandbox.workspace_isolation == "clone"

    def test_dispatch_hands_the_runner_clone_isolation(self, tmp_path: Path) -> None:
        """The override must reach the config the runner actually receives —
        `_item_config` being right is worthless if dispatch passes `self.config`."""
        h = Harness(tmp_path, self._config(tmp_path, make_repo(tmp_path)))
        h.source.items = [inbox_item()]
        assert h.loop.tick().outcome == "done"
        assert [c.sandbox.workspace_isolation for c in h.run_configs] == ["clone"]
        assert h.config.sandbox.workspace_isolation == "auto"  # operator config untouched

    def test_daemon_isolation_knob_governs_daemon_runs(self, tmp_path: Path) -> None:
        cfg = self._config(tmp_path, make_repo(tmp_path), workspace_isolation="in-place")
        h = Harness(tmp_path, cfg)
        assert h.loop._item_config(inbox_item()).sandbox.workspace_isolation == "in-place"

    def test_plain_directory_workspace_is_not_forced_to_clone(self, tmp_path: Path) -> None:
        """`clone` on a non-git dir is a provisioning error; a plain dir must
        keep `auto`'s in-place fallback or every daemon run would fail."""
        plain = tmp_path / "plain"
        plain.mkdir()
        h = Harness(tmp_path, self._config(tmp_path, plain))
        assert h.loop._item_config(inbox_item()).sandbox.workspace_isolation == "auto"

    def test_no_workspace_leaves_sandbox_config_alone(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        assert h.loop._item_config(inbox_item()).sandbox == h.config.sandbox

    def test_fresh_dispatch_fast_forwards_the_checkout(self, tmp_path: Path) -> None:
        upstream, checkout = make_upstream_and_clone(tmp_path)
        h = Harness(tmp_path, self._config(tmp_path, checkout))
        h.source.items = [inbox_item()]
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
        h.source.items = [inbox_item()]
        push_upstream_commit(tmp_path, upstream)
        assert h.loop.tick().outcome == "done"
        assert hostgit.head_commit(checkout) == stale

    def test_in_place_daemon_runs_are_not_refreshed(self, tmp_path: Path) -> None:
        upstream, checkout = make_upstream_and_clone(tmp_path)
        stale = hostgit.head_commit(checkout)
        cfg = self._config(tmp_path, checkout, workspace_isolation="in-place")
        h = Harness(tmp_path, cfg)
        h.source.items = [inbox_item()]
        push_upstream_commit(tmp_path, upstream)
        assert h.loop.tick().outcome == "done"
        assert hostgit.head_commit(checkout) == stale

    def test_failed_fetch_warns_and_still_runs(self, tmp_path: Path) -> None:
        """Network down must not fail every issue: warn, run from local HEAD."""
        upstream, checkout = make_upstream_and_clone(tmp_path)
        shutil.rmtree(upstream)
        h = Harness(tmp_path, self._config(tmp_path, checkout))
        h.source.items = [inbox_item()]
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
        h.dstore.upsert_new(inbox_item(), h.clock())
        h.dstore.mark_claimed("inbox:a.md", h.clock())
        h.dstore.mark_running("inbox:a.md", "rres", h.clock())
        h.store.create_run("rres", "outcome")
        h.store.set_run_state("rres", "running")
        push_upstream_commit(tmp_path, upstream)
        h.loop.recover()  # queues the resume with the run pinned; tick runs it
        assert h.loop.tick().outcome == "done"
        assert h.runs == [("rres", True)]
        assert hostgit.head_commit(checkout) == stale


class TestLogging:
    """The journal answers the questions it could not before: what was
    dispatched and how long it took, why the daemon is idle (once, not per
    tick), what shutdown interrupted, and what a report could not read."""

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
        h.source.items = [inbox_item()]
        with caplog.at_level(logging.INFO):
            assert h.loop.tick().outcome == "done"
        (dispatch,) = self._events(caplog, "run.dispatch")
        assert "'item': 'inbox:a.md'" in dispatch and "'source': 'inbox'" in dispatch
        assert "'resume': False" in dispatch and "'attempt': 1" in dispatch
        (finished,) = self._events(caplog, "run.finished")
        assert "'outcome': 'completed'" in finished and "'duration_s'" in finished
        assert self._events(caplog, "item.claimed")
        (done,) = self._events(caplog, "run.done")
        assert "'pr': 'https://x/pull/9'" in done

    def test_run_thread_records_carry_the_bound_run_id(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from sbxloop.log import get_logger

        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
        seen: list[str] = []

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            get_logger("sbxloop.test.inside").info("inside.run")
            seen.append(run_id)
            h.store.create_run(run_id, "outcome")
            h.store.set_run_state(run_id, "completed")
            return RunResult(run_id=run_id, state="completed")

        h.loop._runner = runner
        with caplog.at_level(logging.INFO):
            h.loop.tick()
        (inside,) = [r.getMessage() for r in caplog.records if r.name == "sbxloop.test.inside"]
        assert f"'run': '{seen[0]}'" in inside and "'item': 'inbox:a.md'" in inside

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
        h.source.items = [inbox_item()]
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
        assert "'item': 'inbox:a.md'" in interrupted and "'resumable': True" in interrupted
        (finished,) = self._events(caplog, "run.finished")
        assert "'outcome': 'interrupted'" in finished

    def test_report_mining_failure_is_a_warning_not_silence(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from sbxloop.errors import StateError

        h = Harness(tmp_path)

        def broken_events(*a: Any, **k: Any) -> Any:
            raise StateError("events table gone")

        h.store.events = broken_events  # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING):
            report = h.loop._report("r-x", None)
        assert report.delivery is None
        (warned,) = self._events(caplog, "run.report_events_unreadable")
        assert "'run': 'r-x'" in warned


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
                "state_dir": str(tmp_path / "state"),
                "daemon": {"max_runs_per_day": 1, "run_cap_timezone": tz},
            }
        )
        h = Harness(tmp_path, cfg)
        # A known absolute instant so calendar boundaries are deterministic.
        h.clock.t = datetime(2024, 3, 5, 12, 0, tzinfo=UTC).timestamp()
        h.source.items = [inbox_item("a.md"), inbox_item("b.md")]
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
            {"state_dir": str(tmp_path / "state"), "daemon": {"max_runs_per_day": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.clock.t = datetime(2024, 3, 5, 0, 30, tzinfo=UTC).timestamp()
        h.source.items = [inbox_item("a.md"), inbox_item("b.md")]
        assert h.loop.tick().outcome == "done"
        h.clock.t += 23 * 3600  # 23:30 the same day
        assert h.loop.tick().idle_reason == "daily_cap"
        h.clock.t += 1800  # 00:00 the next day
        assert h.loop.tick().outcome == "done"

    def test_run_just_before_boundary_does_not_free_a_slot_early(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "daemon": {"max_runs_per_day": 1}}
        )
        h = Harness(tmp_path, cfg)
        h.clock.t = datetime(2024, 3, 5, 23, 30, tzinfo=UTC).timestamp()
        h.source.items = [inbox_item("a.md"), inbox_item("b.md")]
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
        h.source.items = [*h.source.items, inbox_item("c.md")]
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
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_cx", now=2.0)
        h.store.create_run("r_cx", "x")
        h.store.set_run_state("r_cx", "running")
        h.store.append_event(Event.now("run.started", "r_cx"))
        before = len(list(h.store.events("r_cx")))
        h.dstore.mark_cancelled(
            "inbox:a.md", "cancelled by Discord user brett.bergin (via concierge)", now=3.0
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
        assert events[-1].data["previous_state"] == "running"

    def test_recover_reconciles_orphan_run_failed(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        for run_id, state in (("r_orph", "running"), ("r_dec", "decomposing")):
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, state)
        h.loop.recover()
        for run_id in ("r_orph", "r_dec"):
            record = h.store.get_run(run_id)
            assert record.state == "failed"
            assert record.reason is not None and "orphaned" in record.reason
            assert [e.type for _, e in h.store.events(run_id)] == ["run.reconciled"]

    def test_recover_leaves_live_and_resume_pending_runs(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        # Interrupted run: recover() queues it for resume and must not close it.
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_resume", now=2.0)
        h.store.create_run("r_resume", "x")
        h.store.set_run_state("r_resume", "running")
        # Genuinely in-flight run in this process.
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "running")
        h.loop._current = RunHandle(inbox_item("b.md"), "r_live", cast(Any, None), EventBus())
        h.loop.recover()
        assert h.store.get_run("r_resume").state == "running"
        assert h.store.get_run("r_live").state == "running"
        pending = h.dstore.get("inbox:a.md")
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
            {"state_dir": str(tmp_path / "state"), "daemon": {"run_stale_after_s": 3600.0}}
        )
        return Harness(tmp_path, config)

    def test_stale_run_reconciled_fresh_one_left_alone(self, tmp_path: Path) -> None:
        h = self._stale_harness(tmp_path)
        now = h.clock.t
        for run_id in ("r_stale", "r_fresh"):
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "running")
        self._age(h, "r_stale", now - 7200.0)
        self._age(h, "r_fresh", now - 60.0)
        h.loop.tick()
        stale = h.store.get_run("r_stale")
        assert stale.state == "failed"
        assert stale.reason is not None and "stale" in stale.reason
        assert [e.type for _, e in h.store.events("r_stale")] == ["run.reconciled"]
        assert h.store.get_run("r_fresh").state == "running"
        assert [e.type for _, e in h.store.events("r_fresh")] == []

    def test_sweep_runs_while_paused(self, tmp_path: Path) -> None:
        h = self._stale_harness(tmp_path)
        h.loop._paused = True
        h.store.create_run("r_stale", "x")
        h.store.set_run_state("r_stale", "decomposing")
        self._age(h, "r_stale", h.clock.t - 99999.0)
        assert h.loop.tick().idle_kind == "paused"
        assert h.store.get_run("r_stale").state == "failed"

    def test_current_run_is_never_stale(self, tmp_path: Path) -> None:
        h = self._stale_harness(tmp_path)
        for run_id in ("r_live", "r_other"):
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "running")
            self._age(h, run_id, h.clock.t - 99999.0)
        h.loop._current = RunHandle(inbox_item("b.md"), "r_live", cast(Any, None), EventBus())
        h.loop._reconcile_stale_runs(h.clock.t)
        # A live run means the daemon is working: nothing is swept.
        for run_id in ("r_live", "r_other"):
            assert h.store.get_run(run_id).state == "running"
            assert [e.type for _, e in h.store.events(run_id)] == []

    def test_zero_threshold_disables_the_sweep(self, tmp_path: Path) -> None:
        config = Config.model_validate(
            {"state_dir": str(tmp_path / "state"), "daemon": {"run_stale_after_s": 0}}
        )
        h = Harness(tmp_path, config)
        h.store.create_run("r_stale", "x")
        h.store.set_run_state("r_stale", "running")
        self._age(h, "r_stale", h.clock.t - 10_000_000.0)
        h.loop.tick()
        assert h.store.get_run("r_stale").state == "running"

    def test_stale_run_with_cancelled_item_keeps_operator_reason(self, tmp_path: Path) -> None:
        h = self._stale_harness(tmp_path)
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_cx", now=2.0)
        h.store.create_run("r_cx", "x")
        h.store.set_run_state("r_cx", "running")
        h.dstore.mark_cancelled(
            "inbox:a.md", "cancelled by Discord user brett.bergin (via concierge)", now=3.0
        )
        self._age(h, "r_cx", h.clock.t - 7200.0)
        h.loop.tick()
        record = h.store.get_run("r_cx")
        assert record.state == "cancelled"
        assert record.reason is not None and "brett.bergin" in record.reason

    def test_resume_pending_stale_run_is_left_for_tick(self, tmp_path: Path) -> None:
        h = self._stale_harness(tmp_path)
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_resume", now=2.0)
        h.store.create_run("r_resume", "x")
        h.store.set_run_state("r_resume", "running")
        h.dstore.mark_resume_pending("inbox:a.md", now=3.0)
        self._age(h, "r_resume", h.clock.t - 7200.0)
        h.loop._reconcile_stale_runs(h.clock.t)
        assert h.store.get_run("r_resume").state == "running"


class TestReviewGoesToThePr:
    """A review item's findings belong on the pull request, not in the
    tracker. Filing them as issues is what produced #392-#396 for one PR.
    """

    def _reviewed(self, tmp_path: Path) -> tuple[Harness, list[tuple[int, str]]]:
        h = Harness(tmp_path)
        h.source.name = "github"  # type: ignore[misc]
        item = WorkItem(
            item_id="gh:391", source="github", source_key="391", kind="audit", title="R"
        )
        h.source.items = [item]
        # This item IS the review of PR #7, as recorded when it was filed.
        h.dstore.record_review("rorigin01", 7, "gh:391", 0.0)
        posted: list[tuple[int, str]] = []

        def post_review(run: object, pr_number: int, origin_run_id: str) -> SubmittedReview:
            posted.append((pr_number, origin_run_id))
            return SubmittedReview("https://gh/r/1", "REQUEST_CHANGES")

        h.source.post_review = post_review  # type: ignore[attr-defined]
        return h, posted

    def test_a_review_item_posts_and_files_nothing(self, tmp_path: Path) -> None:
        h, posted = self._reviewed(tmp_path)
        h.loop._collect_backlog = lambda run_id, source: ["should:not:be:filed"]  # type: ignore[method-assign]
        assert h.loop.tick().outcome == "done"
        assert posted == [(7, "rorigin01")]
        report = h.source.calls[-1][1]
        assert report.filed == (), "a review must not file issues for its findings"

    def test_an_ordinary_item_is_untouched(self, tmp_path: Path) -> None:
        """No review recorded for this item, so the backlog lane runs as
        before — the change must not reach the ordinary path."""
        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
        h.loop._collect_backlog = lambda run_id, source: ["inbox:finding-a"]  # type: ignore[method-assign]
        assert h.loop.tick().outcome == "done"
        assert h.source.calls[-1][1].filed == ("inbox:finding-a",)


class ReviewingSource(FakeSource):
    """A github-shaped source that files reviews and answers PR state."""

    name = "github"

    def __init__(self) -> None:
        super().__init__()
        self.checks = ChecksVerdict("green", 2, (), ())
        self.review_state = "NONE"
        self.merged = False
        self.pr_open = True
        self.merged_ok = True  # what report_merged answers
        self.head_sha = ""  # the branch head each poll observes
        self.feedback = ""  # what pr_review_feedback answers
        self.polls = 0
        self.merge_polls = 0
        self.reviews: list[int] = []
        # The landing stage's view. Defaults describe a PR GitHub would merge
        # right now, so a test only spells out the state it is about.
        self.draft = False
        self.node_id = "PR_node9"
        self.mergeable: bool | None = True
        self.mergeable_state = "clean"
        self.undraft_ok = True
        self.update_ok = True
        self.merge_outcome = MergeOutcome(True, "merged01", "ok")
        self.merges: list[tuple[int, str, str]] = []
        self.updates: list[tuple[int, str]] = []
        self.deleted: list[str] = []
        self.labels = GitHubLabels(
            "sbxloop:run", "sbxloop:in-progress", "sbxloop:failed", "sbxloop:backlog"
        )

    def file_review(self, item: WorkItem, pr_number: int, pr_url: str, run_id: str) -> str:
        self.reviews.append(pr_number)
        self.calls.append(("file_review", pr_number))
        return f"gh:{pr_number + 100}"

    def pr_state(self, pr_number: int) -> PrSnapshot:
        self.polls += 1
        state = "open" if self.pr_open else "closed"
        if self.merged or not self.pr_open:
            return PrSnapshot(ChecksVerdict("pending", 0, (), ()), "NONE", self.merged, state)
        return PrSnapshot(
            self.checks,
            self.review_state,
            False,
            state,
            head_sha=self.head_sha,
            draft=self.draft,
            node_id=self.node_id,
            mergeable=self.mergeable,
            mergeable_state=self.mergeable_state,
        )

    def pr_merge_state(self, pr_number: int) -> tuple[bool, str]:
        self.merge_polls += 1
        return self.merged, "open" if self.pr_open else "closed"

    def pr_review_feedback(self, pr_number: int) -> str:
        self.calls.append(("review_feedback", pr_number))
        return self.feedback

    def report_merged(self, item: WorkItem, pr_number: int, pr_url: str) -> bool:
        self.calls.append(("merged", pr_number))
        return self.merged_ok

    def report_pr_closed(self, item: WorkItem, pr_number: int, pr_url: str) -> bool:
        self.calls.append(("pr_closed", pr_number))
        return True

    def pr_ready_for_review(self, node_id: str) -> bool:
        self.calls.append(("undraft", node_id))
        if not self.undraft_ok:
            return False
        self.draft = False
        self.mergeable_state = "clean"
        return True

    def pr_update_branch(self, pr_number: int, *, expected_head_sha: str = "") -> bool:
        self.updates.append((pr_number, expected_head_sha))
        return self.update_ok

    def pr_merge(self, pr_number: int, *, method: str = "squash", sha: str = "") -> MergeOutcome:
        self.merges.append((pr_number, method, sha))
        if self.merge_outcome.merged:
            self.merged = True
        return self.merge_outcome

    def branch_delete(self, branch: str) -> None:
        self.deleted.append(branch)


class TestAcceptanceGate:
    """A delivered PR is not done until it is green and its review is
    satisfied. Settling on "a PR exists" is how #389 was marked done with
    `mdformat` and `security` failing.

    The gates run cheapest-first: CI is GitHub's compute and costs nothing,
    so it decides before a review run is spent — reviewing a red PR burns a
    whole run on work that has to change anyway.
    """

    def _delivered(self, tmp_path: Path, **daemon: object) -> tuple[Harness, ReviewingSource]:
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": dict({"await_review": True}, **daemon),
            }
        )
        h = Harness(tmp_path, cfg)
        source = ReviewingSource()
        h.source = source
        h.loop.sources = [source]
        source.items = [WorkItem(item_id="gh:1", source="github", source_key="1", title="Do it")]
        assert h.loop.tick().outcome == "reviewing"
        return h, source

    def test_first_poll_baselines_the_delivered_head(self, tmp_path: Path) -> None:
        h, source = self._delivered(tmp_path)
        source.head_sha = "ours0001"
        h.loop.tick()
        assert h.dstore.pr_state("gh:1").delivered_head == "ours0001"  # type: ignore[union-attr]

    def test_a_foreign_push_hands_the_pr_over(self, tmp_path: Path) -> None:
        """#412: a fix round delivers by force-updating the branch, so a head
        the daemon did not deliver means continuing would replace a human's
        commits — whoever pushes last wins. Hand it over instead."""
        h, source = self._delivered(tmp_path)
        source.head_sha = "ours0001"
        h.loop.tick()  # baselines
        source.head_sha = "theirs002"
        source.checks = ChecksVerdict("red", 2, (), ("lint",))  # would queue a fix round
        h.loop.tick()
        item = h.dstore.get("gh:1")
        assert item.state == "abandoned", "handed to a human, not retried"  # type: ignore[union-attr]
        assert "taken over" in (item.last_error or "")  # type: ignore[union-attr]
        state = h.dstore.pr_state("gh:1")
        assert state is not None and state.fix_brief is None, "no fix round was queued"

    def test_a_review_is_not_refiled_after_its_charter_dies(self, tmp_path: Path) -> None:
        """#442: abandoning (or losing) the review charter must not silently
        re-derive 'this PR needs a review' and file the same charter under a
        new number. Green checks become the whole bar instead."""
        h, source = self._delivered(tmp_path)
        h.loop.tick()  # green + unreviewed: files the review charter (gh:109)
        assert source.reviews == [9]
        # The charter runs as its own item and dies without posting a verdict.
        h.dstore.upsert_new(
            WorkItem(item_id="gh:109", source="github", source_key="109", title="review"),
            now=5.0,
        )
        h.dstore.mark_failed("gh:109", "abandoned by operator", 6.0, requeue=False)
        h.loop.tick()  # clears the dead in-flight marker
        h.loop.tick()  # green + still unreviewed — but the charter was already filed
        assert source.reviews == [9], "the charter was re-filed"
        assert h.dstore.get("gh:1").state == "done"  # type: ignore[union-attr]

    def test_a_moot_review_charter_settles_without_a_run(self, tmp_path: Path) -> None:
        """#442: a review charter whose PR already merged reviews nothing —
        the field case burned four runs auditing a merged PR."""
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": {"await_review": True},
            }
        )
        h = Harness(tmp_path, cfg)
        source = ReviewingSource()
        h.source = source
        h.loop.sources = [source]
        h.dstore.record_review("rorig0001", 9, "gh:200", 1.0)
        source.merged = True
        source.items = [
            WorkItem(item_id="gh:200", source="github", source_key="200", title="review: PR #9")
        ]
        result = h.loop.tick()
        assert result.outcome == "done"
        assert h.runs == [], "the charter must not spend an engine run"
        assert h.dstore.get("gh:200").state == "done"  # type: ignore[union-attr]
        assert any(kind == "success" for kind, _ in source.calls)

    def test_a_delivered_item_waits_and_spends_no_review_yet(self, tmp_path: Path) -> None:
        """CI first: nothing is reviewed until the build has reported."""
        h, source = self._delivered(tmp_path)
        assert h.dstore.get("gh:1").state == "reviewing"  # type: ignore[union-attr]
        assert not any(kind == "success" for kind, _ in source.calls)
        assert source.reviews == []  # no review run spent on an unchecked PR

    def test_pending_checks_keep_waiting_and_review_nothing(self, tmp_path: Path) -> None:
        h, source = self._delivered(tmp_path)
        source.checks = ChecksVerdict("pending", 2, ("test",), ())
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "reviewing"  # type: ignore[union-attr]
        assert source.reviews == []

    def test_green_files_the_review_once(self, tmp_path: Path) -> None:
        h, source = self._delivered(tmp_path)
        h.loop.tick()
        assert len(source.reviews) == 1
        # A review in flight is not re-filed on the next poll.
        h.loop.tick()
        assert len(source.reviews) == 1
        assert h.dstore.get("gh:1").state == "reviewing"  # type: ignore[union-attr]

    def test_green_and_reviewed_and_approved_is_accepted(self, tmp_path: Path) -> None:
        h, source = self._delivered(tmp_path)
        h.loop.tick()  # files the review
        h.dstore.review_settled("gh:1", gates=True, verdict="APPROVE")
        source.review_state = "APPROVED"
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "done"  # type: ignore[union-attr]
        assert any(kind == "success" for kind, _ in source.calls)

    def test_red_checks_queue_a_fix_round_without_reviewing(self, tmp_path: Path) -> None:
        """The saving that matters: a red PR costs a fix run, not a review
        run followed by a fix run."""
        h, source = self._delivered(tmp_path)
        source.checks = ChecksVerdict("red", 2, (), ("mdformat",))
        h.loop.tick()
        item = h.dstore.get("gh:1")
        assert item.state == "queued"  # type: ignore[union-attr]
        assert item.run_id is None, "a fix is a fresh run, not a resume"  # type: ignore[union-attr]
        assert source.reviews == []
        brief = h.dstore.pr_state("gh:1").fix_brief  # type: ignore[union-attr]
        assert "mdformat" in brief and "#9" in brief

    def test_changes_requested_queues_a_fix_round(self, tmp_path: Path) -> None:
        h, source = self._delivered(tmp_path)
        h.loop.tick()  # files the review
        h.dstore.review_settled("gh:1", gates=True, verdict="REQUEST_CHANGES")
        source.review_state = "CHANGES_REQUESTED"
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "queued"  # type: ignore[union-attr]
        assert "requested changes" in h.dstore.pr_state("gh:1").fix_brief  # type: ignore[union-attr]

    def test_a_review_fix_round_quotes_the_objections(self, tmp_path: Path) -> None:
        """The fix agent's sandbox has no GitHub credential (#437): whatever
        the daemon quotes into the brief is all the reviewer feedback the
        round will ever see."""
        h, source = self._delivered(tmp_path)
        h.loop.tick()  # files the review
        h.dstore.review_settled("gh:1", gates=True, verdict="REQUEST_CHANGES")
        source.review_state = "CHANGES_REQUESTED"
        source.feedback = "- `a.py:9`: off by one"
        h.loop.tick()
        brief = h.dstore.pr_state("gh:1").fix_brief  # type: ignore[union-attr]
        assert "- `a.py:9`: off by one" in brief

    def test_a_non_gating_approval_still_accepts(self, tmp_path: Path) -> None:
        """A review the repo would only accept as a COMMENT never produces an
        approval on GitHub; waiting for one would strand every item. Our own
        record of what it asked for is the answer."""
        h, source = self._delivered(tmp_path)
        h.loop.tick()
        h.dstore.review_settled("gh:1", gates=False, verdict="APPROVE")
        source.review_state = "NONE"
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "done"  # type: ignore[union-attr]

    def test_a_gating_review_does_wait_for_its_approval(self, tmp_path: Path) -> None:
        h, source = self._delivered(tmp_path)
        h.loop.tick()
        h.dstore.review_settled("gh:1", gates=True, verdict="REQUEST_CHANGES")
        source.review_state = "NONE"
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "reviewing"  # type: ignore[union-attr]

    def test_the_round_budget_hands_it_to_a_human(self, tmp_path: Path) -> None:
        """A round is a real run, so the budget is spend, not patience.
        Nothing may spin: past it the PR is left open for a human."""
        h, source = self._delivered(tmp_path, review_rounds=1)
        source.checks = ChecksVerdict("red", 1, (), ("security",))
        front = RecordingFrontend()
        h.loop.frontend = front  # type: ignore[assignment]
        h.loop.tick()  # round 1: queued for a fix
        assert h.dstore.get("gh:1").state == "queued"  # type: ignore[union-attr]
        h.dstore.mark_reviewing("gh:1", h.clock.t)  # pretend the fix delivered
        h.loop.tick()  # round 2: over budget
        item = h.dstore.get("gh:1")
        # ``abandoned``: terminal, no retry, run kept pinned for forensics.
        assert item.state == "abandoned"  # type: ignore[union-attr]
        assert "#9" in (item.last_error or "")  # type: ignore[union-attr]
        assert any("not accepted" in t for t in front.seen)

    def test_an_item_with_no_delivery_record_is_not_stranded(self, tmp_path: Path) -> None:
        h, _ = self._delivered(tmp_path)
        h.dstore._conn.execute("DELETE FROM daemon_pr_state WHERE item_id = 'gh:1'")
        h.dstore._conn.commit()
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "done"  # type: ignore[union-attr]

    def test_a_non_gating_request_for_changes_is_still_honoured(self, tmp_path: Path) -> None:
        """PR #406: the review explained at length that the feature did not
        work outside its own tests, but it could only be posted as a COMMENT
        — so GitHub recorded no verdict, and the gate read "cannot block" as
        "satisfied" and would have accepted the PR anyway. What the review
        *asked for* is the only answer there is when GitHub holds none."""
        h, source = self._delivered(tmp_path)
        h.loop.tick()
        h.dstore.review_settled("gh:1", gates=False, verdict="REQUEST_CHANGES")
        source.review_state = "NONE"  # a COMMENT leaves no verdict on GitHub
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "queued"  # type: ignore[union-attr]
        assert "requested changes" in h.dstore.pr_state("gh:1").fix_brief  # type: ignore[union-attr]

    def test_the_waiting_item_is_settled_not_the_review_charter(self, tmp_path: Path) -> None:
        """The review runs as its own work item, so the charter's id is not
        the id of the item waiting on the PR. Settling against the wrong one
        updated no row and left the waiter marked "review in flight" for
        ever — gh:335 sat parked on PR #406 with its review already posted."""
        h, _ = self._delivered(tmp_path)
        h.loop.tick()
        state = h.dstore.pr_state("gh:1")
        assert state is not None and state.review_in_flight
        assert h.dstore.awaiting_review(state.review_ref or "") == "gh:1"
        assert h.dstore.awaiting_review("gh:not-a-review") is None

    def test_a_review_run_that_never_settles_does_not_park_the_item(self, tmp_path: Path) -> None:
        """An in-flight flag nothing will ever clear is a silent park, which
        is the one outcome this loop must not produce. The dead charter is
        NOT re-filed (#442 — re-deriving it undid operator abandons and
        burned runs re-reviewing the same PR): green checks become the whole
        bar, the same as a deployment with no reviewer."""
        h, source = self._delivered(tmp_path)
        h.loop.tick()
        ref = h.dstore.pr_state("gh:1").review_ref or "gh:x"  # type: ignore[union-attr]
        h.dstore.upsert_new(
            WorkItem(item_id=ref, source="github", source_key="x", title="review"), h.clock.t
        )
        h.dstore.mark_failed(ref, "review run died", h.clock.t, requeue=False)
        h.loop.tick()  # notices and clears the stale marker
        assert not h.dstore.pr_state("gh:1").review_in_flight  # type: ignore[union-attr]
        h.loop.tick()  # the gate resumes — and accepts on green, no re-file
        assert len(source.reviews) == 1
        assert h.dstore.get("gh:1").state == "done"  # type: ignore[union-attr]

    def test_the_gate_can_be_turned_off(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": {"await_review": False},
            }
        )
        h = Harness(tmp_path, cfg)
        source = ReviewingSource()
        h.source = source
        h.loop.sources = [source]
        source.items = [WorkItem(item_id="gh:1", source="github", source_key="1", title="Do it")]
        assert h.loop.tick().outcome == "done"
        assert source.polls == 0

    def test_a_poll_that_keeps_failing_hands_the_item_over(self, tmp_path: Path) -> None:
        """The one outcome an unattended daemon must never produce is a
        silently parked item. A poll that cannot reach GitHub still costs a
        round, so a persistent failure ends the way any unaccepted PR does:
        handed to a human, out loud."""
        h, source = self._delivered(tmp_path, review_rounds=1)

        def boom(pr_number: int) -> PrSnapshot:
            raise RuntimeError("github is down")

        source.pr_state = boom  # type: ignore[assignment]
        front = RecordingFrontend()
        h.loop.frontend = front  # type: ignore[assignment]
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "reviewing"  # type: ignore[union-attr]
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "abandoned"  # type: ignore[union-attr]
        assert any("could not read PR" in t for t in front.seen)

    def test_one_failed_poll_does_not_give_up(self, tmp_path: Path) -> None:
        """A blip is not a verdict: the item keeps waiting and recovers when
        the next poll succeeds."""
        h, source = self._delivered(tmp_path)
        calls = {"n": 0}
        real = source.pr_state

        def flaky(pr_number: int) -> PrSnapshot:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return real(pr_number)

        source.pr_state = flaky  # type: ignore[assignment]
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "reviewing"  # type: ignore[union-attr]
        h.loop.tick()  # recovers: files the review
        assert source.reviews == [9]

    def test_a_merged_pr_is_accepted_over_every_other_gate(self, tmp_path: Path) -> None:
        """A human merging the PR IS the acceptance: no review is filed on a
        merged PR, and the issue settles through report_merged right away —
        waiting for an approval on a merged PR is how items wedge forever."""
        h, source = self._delivered(tmp_path)
        source.merged = True
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "done"  # type: ignore[union-attr]
        assert source.reviews == []
        assert ("merged", 9) in source.calls
        assert not any(kind == "success" for kind, _ in source.calls)
        assert h.dstore.pr_state("gh:1").settled  # type: ignore[union-attr]

    def test_a_pr_closed_unmerged_during_review_abandons_the_item(self, tmp_path: Path) -> None:
        """A human closing the PR without merging is a rejection no gate
        below can override: handed over, issue marked failed but open."""
        h, source = self._delivered(tmp_path)
        source.pr_open = False
        h.loop.tick()
        item = h.dstore.get("gh:1")
        assert item.state == "abandoned"  # type: ignore[union-attr]
        assert "without being merged" in (item.last_error or "")  # type: ignore[union-attr]
        assert ("pr_closed", 9) in source.calls
        assert source.reviews == []


class TestMergeWatch:
    """Acceptance is not the end of the story: the source issue settles —
    close plus completed label — when the PR actually lands. The watch
    sweeps accepted-but-unsettled rows in daemon_pr_state, throttled to one
    read per PR per interval, and survives restarts because the rows do.
    """

    def _accepted(self, tmp_path: Path, **daemon: object) -> tuple[Harness, ReviewingSource]:
        """One github patch item run to done (no review gate), watch armed."""
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": dict({"await_review": False}, **daemon),
            }
        )
        h = Harness(tmp_path, cfg)
        source = ReviewingSource()
        h.source = source
        h.loop.sources = [source]
        source.items = [WorkItem(item_id="gh:1", source="github", source_key="1", title="Do it")]
        assert h.loop.tick().outcome == "done"
        return h, source

    def test_an_open_pr_waits_and_the_watch_is_throttled(self, tmp_path: Path) -> None:
        h, source = self._accepted(tmp_path)
        h.loop.tick()  # first watch poll
        assert source.merge_polls == 1
        assert not any(kind in {"merged", "pr_closed"} for kind, _ in source.calls)
        assert not h.dstore.pr_state("gh:1").settled  # type: ignore[union-attr]
        h.loop.tick()  # inside the interval: no read spent
        assert source.merge_polls == 1
        h.clock.t += 301
        h.loop.tick()
        assert source.merge_polls == 2

    def test_a_merged_pr_settles_the_issue_once(self, tmp_path: Path) -> None:
        h, source = self._accepted(tmp_path)
        source.merged = True
        h.loop.tick()
        assert [c for c in source.calls if c[0] == "merged"] == [("merged", 9)]
        assert h.dstore.pr_state("gh:1").settled  # type: ignore[union-attr]
        assert h.dstore.get("gh:1").state == "done"  # type: ignore[union-attr]
        h.clock.t += 3600
        h.loop.tick()  # settled rows are not polled again
        assert source.merge_polls == 1

    def test_a_pr_closed_unmerged_abandons_the_item(self, tmp_path: Path) -> None:
        h, source = self._accepted(tmp_path)
        source.pr_open = False
        h.loop.tick()
        assert ("pr_closed", 9) in source.calls
        item = h.dstore.get("gh:1")
        assert item.state == "abandoned"  # type: ignore[union-attr]
        assert "without being merged" in (item.last_error or "")  # type: ignore[union-attr]
        assert h.dstore.pr_state("gh:1").settled  # type: ignore[union-attr]

    def test_a_failed_settle_is_retried_next_interval(self, tmp_path: Path) -> None:
        """report_merged returning False (a GitHub hiccup mid-settle) leaves
        the row unsettled so the close is retried, not recorded as done."""
        h, source = self._accepted(tmp_path)
        source.merged = True
        source.merged_ok = False
        h.loop.tick()
        assert not h.dstore.pr_state("gh:1").settled  # type: ignore[union-attr]
        source.merged_ok = True
        h.clock.t += 301
        h.loop.tick()
        assert h.dstore.pr_state("gh:1").settled  # type: ignore[union-attr]
        assert [c for c in source.calls if c[0] == "merged"] == [("merged", 9), ("merged", 9)]

    def test_a_restart_resumes_the_watch(self, tmp_path: Path) -> None:
        """The watch lives in daemon_pr_state, not in memory: a fresh loop
        over the same store picks up where the old one stopped."""
        h, source = self._accepted(tmp_path)
        h.loop.tick()
        assert source.merge_polls == 1
        loop2 = DaemonLoop(
            h.config,
            store=h.store,
            dstore=h.dstore,
            sources=[source],
            runner=h.runner,
            clock=h.clock,
        )
        source.merged = True
        h.clock.t += 301
        loop2.tick()
        assert h.dstore.pr_state("gh:1").settled  # type: ignore[union-attr]
        assert ("merged", 9) in source.calls

    def test_a_non_github_row_retires_without_a_read(self, tmp_path: Path) -> None:
        """Inbox work has no issue to settle: its row is retired silently,
        costing zero GitHub reads."""
        h, source = self._accepted(tmp_path)
        h.dstore.upsert_new(inbox_item(), h.clock.t)
        h.dstore.mark_done("inbox:a.md", h.clock.t)
        h.dstore.record_delivery("inbox:a.md", 7, "b", h.clock.t, url="u")
        h.loop.tick()
        assert h.dstore.pr_state("inbox:a.md").settled  # type: ignore[union-attr]
        assert source.merge_polls == 1  # only gh:1 was asked

    def test_deliver_closes_is_threaded_for_github_patch_items(self, tmp_path: Path) -> None:
        """The PR body carries `Closes #N` so GitHub links issue and PR and
        closes the issue on merge even when the daemon is down."""
        h, _ = self._accepted(tmp_path)
        assert h.run_configs[0].github.deliver_closes == 1

    def test_deliver_closes_is_not_set_for_inbox_items(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [inbox_item()]
        assert h.loop.tick().outcome == "done"
        assert h.run_configs[0].github.deliver_closes is None


class TestLandingStage:
    """The last step out of the loop: the daemon merges the PR itself.

    Reached only from the full acceptance bar — green checks AND a satisfied
    review. Everything weaker keeps handing the PR to a human, because
    merging is the one irreversible thing the loop does to a repository.
    """

    def _accepted(self, tmp_path: Path, **daemon: object) -> tuple[Harness, ReviewingSource]:
        """A delivered item polled all the way to green-and-approved."""
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": dict({"await_review": True, "auto_merge": True}, **daemon),
            }
        )
        h = Harness(tmp_path, cfg)
        source = ReviewingSource()
        h.source = source
        h.loop.sources = [source]
        source.items = [WorkItem(item_id="gh:1", source="github", source_key="1", title="Do it")]
        assert h.loop.tick().outcome == "reviewing"
        source.head_sha = "ours0001"
        h.loop.tick()  # green + unreviewed: baselines the head, files the review
        h.dstore.review_settled("gh:1", gates=True, verdict="APPROVE")
        source.review_state = "APPROVED"
        return h, source

    def test_auto_merge_off_leaves_the_pr_to_a_human(self, tmp_path: Path) -> None:
        """The flag is the whole feature: with it off the gate settles exactly
        as it always did, and nothing is merged."""
        h, source = self._accepted(tmp_path, auto_merge=False)
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "done"  # type: ignore[union-attr]
        assert source.merges == []
        assert any(kind == "success" for kind, _ in source.calls)

    def test_a_clean_pr_is_merged_and_its_issue_closed(self, tmp_path: Path) -> None:
        h, source = self._accepted(tmp_path)
        h.loop.tick()
        assert source.merges == [(9, "squash", "ours0001")], "the judged head rides on the merge"
        assert h.dstore.get("gh:1").state == "done"  # type: ignore[union-attr]
        assert ("merged", 9) in source.calls, "the issue settles as merged, not as delivered"

    def test_the_merge_method_is_the_operators(self, tmp_path: Path) -> None:
        h, source = self._accepted(tmp_path, merge_method="rebase")
        h.loop.tick()
        assert source.merges == [(9, "rebase", "ours0001")]

    def test_the_branch_is_tidied_away(self, tmp_path: Path) -> None:
        h, source = self._accepted(tmp_path)
        h.loop.tick()
        assert [b.startswith("sbxloop/") for b in source.deleted] == [True]

    def test_the_branch_is_kept_when_the_operator_says_so(self, tmp_path: Path) -> None:
        h, source = self._accepted(tmp_path, delete_branch_on_merge=False)
        h.loop.tick()
        assert source.deleted == []
        assert source.merges, "the merge itself still happened"

    def test_a_draft_is_undrafted_before_it_is_merged(self, tmp_path: Path) -> None:
        """And not in the same poll: GitHub reports a draft's mergeable_state
        as ``draft``, so what the PR's real merge state is only becomes
        readable once it is out of draft."""
        h, source = self._accepted(tmp_path)
        source.draft = True
        source.mergeable_state = "draft"
        h.loop.tick()
        assert ("undraft", "PR_node9") in source.calls
        assert source.merges == [], "un-drafting and merging are separate polls"
        assert h.dstore.get("gh:1").state == "reviewing"  # type: ignore[union-attr]
        h.loop.tick()
        assert source.merges == [(9, "squash", "ours0001")]

    def test_a_draft_that_will_not_clear_is_handed_over(self, tmp_path: Path) -> None:
        h, source = self._accepted(tmp_path)
        source.draft = True
        source.undraft_ok = False
        h.loop.tick()
        item = h.dstore.get("gh:1")
        assert item.state == "abandoned"  # type: ignore[union-attr]
        assert "draft status could not be cleared" in (item.last_error or "")  # type: ignore[union-attr]
        assert source.merges == []

    def test_unknown_mergeability_waits_rather_than_merging(self, tmp_path: Path) -> None:
        """GitHub computes mergeability asynchronously and says None while it
        is still thinking. That is not permission to merge."""
        h, source = self._accepted(tmp_path)
        source.mergeable = None
        h.loop.tick()
        assert source.merges == []
        assert h.dstore.get("gh:1").state == "reviewing"  # type: ignore[union-attr]

    def test_a_conflicted_pr_goes_back_for_a_fix_round(self, tmp_path: Path) -> None:
        """A re-delivery rebuilds the commit on the current base, so a real
        conflict is genuinely fixable — on the fix-round budget, because
        unlike every other landing step it costs a whole run."""
        h, source = self._accepted(tmp_path)
        source.mergeable = False
        source.mergeable_state = "dirty"
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "queued"  # type: ignore[union-attr]
        assert "conflicts with its base" in h.dstore.pr_state("gh:1").fix_brief  # type: ignore[union-attr]
        assert source.merges == []

    def test_a_behind_pr_updates_its_branch_instead_of_spending_a_run(self, tmp_path: Path) -> None:
        h, source = self._accepted(tmp_path)
        before = len(h.runs)
        source.mergeable_state = "behind"
        h.loop.tick()
        assert source.updates == [(9, "ours0001")], "the expected head guards the update"
        assert source.merges == []
        assert len(h.runs) == before, "keeping a branch current must not cost an engine run"
        state = h.dstore.pr_state("gh:1")
        assert state is not None and state.updates == 1 and state.landing

    def test_our_own_update_commit_is_not_read_as_a_takeover(self, tmp_path: Path) -> None:
        """#412's guard fails the item on a head it did not deliver, and an
        update-branch moves the head by design. Without the in-flight marker
        every PR that ever fell behind would be handed off."""
        h, source = self._accepted(tmp_path)
        source.mergeable_state = "behind"
        h.loop.tick()  # asks for the update
        source.head_sha = "updated1"  # its merge commit appears
        source.mergeable_state = "clean"
        h.loop.tick()
        item = h.dstore.get("gh:1")
        assert item.state == "done", "the update commit must not read as a takeover"  # type: ignore[union-attr]
        state = h.dstore.pr_state("gh:1")
        assert state is not None and not state.landing
        assert source.merges == [(9, "squash", "updated1")], "merged at the new head"

    def test_a_foreign_push_during_a_landing_still_hands_over(self, tmp_path: Path) -> None:
        """The marker only excuses ONE head move; once it is cleared the
        guard is back to its old self."""
        h, source = self._accepted(tmp_path)
        source.mergeable_state = "behind"
        h.loop.tick()  # asks for the update
        source.head_sha = "updated1"
        source.mergeable_state = "clean"
        source.merge_outcome = MergeOutcome(False, "", "head moved", stale=True)
        h.loop.tick()  # adopts the update commit, merge races and loses
        source.head_sha = "theirs02"
        h.loop.tick()
        item = h.dstore.get("gh:1")
        assert item.state == "abandoned"  # type: ignore[union-attr]
        assert "taken over" in (item.last_error or "")  # type: ignore[union-attr]

    def test_only_one_update_is_in_flight_at_a_time(self, tmp_path: Path) -> None:
        h, source = self._accepted(tmp_path)
        source.mergeable_state = "behind"
        h.loop.tick()
        h.loop.tick()  # head has not moved yet — do not ask again
        assert source.updates == [(9, "ours0001")]

    def test_update_attempts_are_bounded_then_handed_over(self, tmp_path: Path) -> None:
        """A base moving faster than CI finishes would update for ever."""
        h, source = self._accepted(tmp_path, merge_update_attempts=1)
        source.mergeable_state = "behind"
        h.loop.tick()  # update 1 of 1
        source.head_sha = "updated1"
        h.loop.tick()  # adopts it; still behind
        item = h.dstore.get("gh:1")
        assert item.state == "abandoned"  # type: ignore[union-attr]
        assert "behind its base" in (item.last_error or "")  # type: ignore[union-attr]
        assert len(source.updates) == 1

    def test_branch_updating_can_be_disabled_outright(self, tmp_path: Path) -> None:
        h, source = self._accepted(tmp_path, merge_update_attempts=0)
        source.mergeable_state = "behind"
        h.loop.tick()
        assert source.updates == []
        item = h.dstore.get("gh:1")
        assert item.state == "abandoned"  # type: ignore[union-attr]
        assert "branch updating is disabled" in (item.last_error or "")  # type: ignore[union-attr]

    def test_a_refused_update_is_not_charged_or_marked(self, tmp_path: Path) -> None:
        """GitHub refusing the update means the PR moved under us; the next
        poll re-reads it rather than this one inventing a verdict."""
        h, source = self._accepted(tmp_path)
        source.mergeable_state = "behind"
        source.update_ok = False
        h.loop.tick()
        state = h.dstore.pr_state("gh:1")
        assert state is not None and state.updates == 0 and not state.landing
        assert h.dstore.get("gh:1").state == "reviewing"  # type: ignore[union-attr]

    def test_a_blocked_merge_hands_over_with_the_pr_left_open(self, tmp_path: Path) -> None:
        """A protection rule wanting an approval this identity cannot give is
        not fixable by another round. Stop, and leave a PR a human can finish
        in one click."""
        h, source = self._accepted(tmp_path)
        source.merge_outcome = MergeOutcome(
            False, "", "Pull Request is not mergeable", blocked=True
        )
        h.loop.tick()
        item = h.dstore.get("gh:1")
        assert item.state == "abandoned"  # type: ignore[union-attr]
        assert "not mergeable" in (item.last_error or "")  # type: ignore[union-attr]
        assert source.deleted == [], "a PR that did not merge keeps its branch"

    def test_a_stale_merge_just_waits_for_the_next_poll(self, tmp_path: Path) -> None:
        h, source = self._accepted(tmp_path)
        source.merge_outcome = MergeOutcome(False, "", "Head branch was modified", stale=True)
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "reviewing"  # type: ignore[union-attr]
        state = h.dstore.pr_state("gh:1")
        assert state is not None and state.rounds == 0, "a race costs no budget"

    def test_a_merge_that_raises_costs_a_round(self, tmp_path: Path) -> None:
        """The one outcome an unattended daemon must never produce is silence:
        a persistently failing merge ends the way any unaccepted PR does."""

        def explode(pr_number: int, **kwargs: object) -> MergeOutcome:
            raise RuntimeError("github down")

        h, source = self._accepted(tmp_path, review_rounds=1)
        source.pr_merge = explode  # type: ignore[assignment]
        h.loop.tick()
        assert h.dstore.pr_state("gh:1").rounds == 1  # type: ignore[union-attr]
        h.loop.tick()
        assert h.dstore.get("gh:1").state == "abandoned"  # type: ignore[union-attr]

    def test_green_with_no_reviewer_is_never_merged(self, tmp_path: Path) -> None:
        """The bar for a merge is the full one. An acceptance that only ever
        meant "green CI, and nobody could review it" is handed to a human."""
        cfg = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": {"await_review": True, "auto_merge": True, "review_deliveries": False},
            }
        )
        h = Harness(tmp_path, cfg)
        source = ReviewingSource()
        h.source = source
        h.loop.sources = [source]
        source.items = [WorkItem(item_id="gh:1", source="github", source_key="1", title="Do it")]
        assert h.loop.tick().outcome == "reviewing"
        h.loop.tick()  # green, and no review can be filed
        assert h.dstore.get("gh:1").state == "done"  # type: ignore[union-attr]
        assert source.merges == [], "no reviewer means no merge"
