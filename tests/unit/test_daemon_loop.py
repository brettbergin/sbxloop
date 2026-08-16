"""DaemonLoop tick/settle/recover against a fake runner and fake sources.

The engine is replaced by an injectable Runner; the real DaemonStore and
StateStore run on a tmp db so persistence paths are exercised for real.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import RunResult, TaskRecord, TaskSpec
from sbxloop.engine.store import StateStore
from sbxloop.errors import StateError, WorkerError
from sbxloop.events import Event, EventBus


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
        kind = self.outcomes.pop(0) if self.outcomes else "completed"
        if kind == "raise":
            raise WorkerError("sandbox exploded")
        state = "failed" if kind == "failed" else "completed"
        self.store.create_run(run_id, "outcome") if not resume else None
        self.store.set_run_state(run_id, state)
        if kind == "deliver_fail":
            self.store.append_event(
                Event.now("run.deliver", run_id, repo="o/r", error="HTTP 409 empty")
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
        # window rolls → dispatch resumes
        h.clock.t += 90_000
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
        assert h.runs == [("r_live", True)]  # resumed, not restarted
        item = h.dstore.get("inbox:a.md")
        assert item is not None and item.state == "done" and item.attempts == 1

    def test_recover_removes_stale_run_sandboxes_before_resume(self, tmp_path: Path) -> None:
        """A killed process leaves its microVMs alive; resume re-provisions
        under the same names and sbx refuses an existing name (field
        failure r6pgvatsd)."""
        h = Harness(tmp_path)
        removed: list[str] = []

        class FakeSbx:
            def rm(self, name: str, **kwargs: Any) -> None:
                removed.append(name)

        h.loop.sbx = FakeSbx()  # type: ignore[assignment]
        h.dstore.upsert_new(inbox_item(), now=1.0)
        h.dstore.mark_claimed("inbox:a.md", now=1.0)
        h.dstore.mark_running("inbox:a.md", "r_live", now=2.0)
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "running")
        h.outcomes = ["completed"]
        h.loop.recover()
        assert removed == ["sbxloop-r_live-agent", "sbxloop-r_live-github"]
        assert h.runs == [("r_live", True)]

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
