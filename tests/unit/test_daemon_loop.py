"""DaemonLoop tick/settle/recover against a fake runner and fake sources.

The engine is replaced by an injectable Runner; the real DaemonStore and
StateStore run on a tmp db so persistence paths are exercised for real.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from sbxloop import hostgit
from sbxloop.config import Config
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import RunResult, TaskRecord, TaskSpec
from sbxloop.engine.store import StateStore
from sbxloop.errors import RunCancelledError, SbxError, StateError, WorkerError
from sbxloop.events import Event, EventBus
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
        # The run itself stays resumable, and no fresh attempt is scheduled.
        assert h.store.get_run(h.runs[0][0]).state == "running"
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
