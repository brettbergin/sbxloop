"""DaemonLoop: discover → claim → run → report, forever.

One item at a time, one fresh :class:`LoopEngine` per item (engines are
single-use: their cancel flag never clears), one shared daemon-owned
:class:`StateStore`, a fresh :class:`EventBus` per run (each engine adds
permanent subscribers to its bus). Spend guardrails — a rolling daily run
cap, a per-item attempt cap, a consecutive-failure circuit breaker — are
the daemon's only defense against a mislabeled issue in a fully autonomous
setup, so they are enforced in the tick, not left to configuration hope.

Shutdown is cooperative: a signal sets the stop flag, asks the in-flight
engine to cancel (honored at its next task boundary), and joins it briefly.
Interrupted runs are resumable by design, so the item stays ``running`` and
:meth:`recover` picks it up on the next start.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from sbxloop.config import Config, GithubConfig
from sbxloop.daemon.backlog import BACKLOG_INSTRUCTIONS, collect_backlog
from sbxloop.daemon.model import RunReport, TickResult, WorkItem
from sbxloop.daemon.sources import WorkSource
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.model import RESUMABLE_RUN_STATES, TERMINAL_RUN_STATES, RunResult
from sbxloop.engine.store import StateStore
from sbxloop.errors import SbxError, SbxloopError, StateError
from sbxloop.events import EventBus, HostEventTypes
from sbxloop.ids import new_run_id
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import sandbox_name
from sbxloop.sbx.sandbox import Sandbox

logger = logging.getLogger(__name__)

DAY_S = 86400.0


class Frontend(Protocol):
    """What a human-facing channel (Discord) sees of the loop's lifecycle.
    Every call is best-effort: the loop never depends on a frontend."""

    def daemon_event(self, text: str) -> None: ...
    def run_started(
        self, item: WorkItem, run_id: str, engine: LoopEngine, bus: EventBus
    ) -> None: ...
    def run_finished(self, item: WorkItem, report: RunReport) -> None: ...


class RunHandle:
    """The in-flight run: what shutdown and a frontend need to reach."""

    def __init__(self, item: WorkItem, run_id: str, engine: LoopEngine, bus: EventBus) -> None:
        self.item = item
        self.run_id = run_id
        self.engine = engine
        self.bus = bus


# (item, per-item config, run_id, bus, resume) -> RunResult. Injectable so
# the tick algorithm is testable without sandboxes; the default builds a
# fresh LoopEngine and calls start() or resume().
Runner = Callable[[WorkItem, Config, str, EventBus, bool], RunResult]


class DaemonLoop:
    def __init__(
        self,
        config: Config,
        *,
        store: StateStore,
        dstore: DaemonStore,
        sources: Sequence[WorkSource],
        sbx: SbxCLI | None = None,
        runner: Runner | None = None,
        clock: Callable[[], float] = time.time,
        frontend: Frontend | None = None,
        worker_python: str | None = None,
        install_workers: bool | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.dstore = dstore
        self.sources = list(sources)
        self.sbx = sbx
        self.clock = clock
        self.frontend = frontend
        # Engine construction knobs (tests/e2e point these at the host
        # interpreter and skip the install ladder, like the CLI does).
        self.worker_python = worker_python
        self.install_workers = install_workers
        self._runner = runner or self._default_runner
        self._stop = threading.Event()
        self._paused = False
        self._current: RunHandle | None = None
        self._current_lock = threading.Lock()
        self._consecutive_failures = 0
        self._breaker_opened_at: float | None = None
        self._last_cap_log = 0.0

    # -- external control ---------------------------------------------------------

    @property
    def current(self) -> RunHandle | None:
        return self._current

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def unpause(self) -> None:
        self._paused = False

    def request_stop(self) -> None:
        self._stop.set()

    def cancel_current(self) -> bool:
        with self._current_lock:
            handle = self._current
        if handle is None:
            return False
        handle.engine.request_cancel()
        return True

    def quiesce(self) -> None:
        """The cleanup registry's shutdown hook: stop claiming, ask the
        in-flight engine to cancel, wait briefly. Stable across runs — it
        looks up the current engine at call time."""
        self._stop.set()
        with self._current_lock:
            handle = self._current
        if handle is not None:
            handle.engine.request_cancel()
        thread = getattr(self, "_engine_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.config.daemon.shutdown_grace_s)

    def status(self) -> dict[str, Any]:
        now = self.clock()
        with self._current_lock:
            handle = self._current
        return {
            "current": {
                "item_id": handle.item.item_id,
                "run_id": handle.run_id,
                "title": handle.item.title,
            }
            if handle
            else None,
            "queued": len(self.dstore.queued()),
            "runs_today": self.dstore.runs_started_since(now - DAY_S),
            "max_runs_per_day": self.config.daemon.max_runs_per_day,
            "breaker_open": self._breaker_open(now),
            "paused": self._paused,
            "stopping": self._stop.is_set(),
        }

    # -- main loop --------------------------------------------------------------------

    def run_forever(self) -> None:
        self._notify("daemon started")
        try:
            while not self._stop.is_set():
                result = self.tick()
                if result.dispatched is None:
                    self._stop.wait(self.config.daemon.poll_interval_s)
        finally:
            self._notify("daemon stopped")

    def tick(self) -> TickResult:
        now = self.clock()
        if self._paused:
            return TickResult(idle_reason="paused")
        if self._breaker_open(now):
            return TickResult(idle_reason="breaker")
        started_today = self.dstore.runs_started_since(now - DAY_S)
        if started_today >= self.config.daemon.max_runs_per_day:
            if now - self._last_cap_log > 3600:
                self._last_cap_log = now
                cap = self.config.daemon.max_runs_per_day
                self._notify(
                    f"daily run cap reached ({started_today}/{cap}); idling until the window rolls"
                )
            return TickResult(idle_reason="daily_cap")
        discovered = self._discover(now)
        item = self.dstore.next_queued(now, self.config.daemon.retry_backoff_s)
        if item is None:
            # Say WHY there is nothing to run: a queue full of items sitting
            # in retry backoff reads as "no work" otherwise (field: --once
            # after a failed attempt printed no_work with no explanation).
            waiting = self.dstore.queued()
            if waiting:
                soonest = min(
                    max(0.0, w.attempts * self.config.daemon.retry_backoff_s - (now - w.updated_at))
                    for w in waiting
                )
                return TickResult(
                    discovered=discovered,
                    idle_reason=f"backoff ({len(waiting)} queued; next eligible in {soonest:.0f}s)",
                )
            return TickResult(discovered=discovered, idle_reason="no_work")
        source = self._source_for(item)
        if source is None:
            self.dstore.mark_failed(item.item_id, "no source for item", now, requeue=False)
            return TickResult(discovered=discovered, idle_reason="no_work")
        if not item.claimed:
            if not source.claim(item):
                self.dstore.mark_failed(item.item_id, "claim failed", now, requeue=False)
                self._notify(f"could not claim {item.item_id} ({item.title}); dropped")
                return TickResult(
                    discovered=discovered, dispatched=item.item_id, outcome="abandoned"
                )
            self.dstore.mark_claimed(item.item_id, now)
        outcome = self._dispatch(item, source, resume_run_id=None)
        return TickResult(discovered=discovered, dispatched=item.item_id, outcome=outcome)

    # -- discovery ---------------------------------------------------------------------

    def _discover(self, now: float) -> int:
        new = 0
        for source in self.sources:
            try:
                found = source.poll()
            except Exception:
                logger.warning("source %s poll failed", source.name, exc_info=True)
                continue
            for item in found:
                if self.dstore.upsert_new(item, now):
                    new += 1
                    self._notify(f"queued {item.item_id}: {item.title}")
        return new

    def _source_for(self, item: WorkItem) -> WorkSource | None:
        return next((s for s in self.sources if s.name == item.source), None)

    # -- dispatch ----------------------------------------------------------------------

    def _dispatch(self, item: WorkItem, source: WorkSource, *, resume_run_id: str | None) -> str:
        """Run one item (fresh, or resuming its interrupted run) and settle it."""
        now = self.clock()
        run_id = resume_run_id or new_run_id()
        if resume_run_id is None:
            self.dstore.mark_running(item.item_id, run_id, now)
            item = self.dstore.get(item.item_id) or item
            source.report_started(item, run_id)
        item_config = self._item_config(item)
        bus = EventBus()
        engine = LoopEngine(
            item_config,
            store=self.store,
            bus=bus,
            sbx=self.sbx,
            worker_python=self.worker_python,
            install_workers=self.install_workers,
        )
        handle = RunHandle(item, run_id, engine, bus)
        with self._current_lock:
            self._current = handle
        if self.frontend is not None:
            try:
                self.frontend.run_started(item, run_id, engine, bus)
            except Exception:
                logger.warning("frontend run_started failed", exc_info=True)

        result_box: dict[str, Any] = {}

        def target() -> None:
            try:
                result_box["result"] = self._runner(
                    item, item_config, run_id, bus, resume_run_id is not None
                )
            except BaseException as exc:
                result_box["error"] = exc

        thread = threading.Thread(target=target, name=f"sbxloop-daemon-run-{run_id}", daemon=True)
        self._engine_thread = thread
        thread.start()
        while thread.is_alive():
            thread.join(timeout=1.0)
        with self._current_lock:
            self._current = None

        if self._stop.is_set() and "result" not in result_box:
            # Interrupted by shutdown: leave the item running for recovery.
            self.dstore.finish_ledger(run_id, "interrupted", self.clock())
            return "interrupted"
        error = result_box.get("error")
        if error is not None and not isinstance(error, SbxloopError | StateError):
            logger.error("run %s crashed", run_id, exc_info=error)
        return self._settle(item, source, run_id, result_box.get("result"), error)

    def _settle(
        self,
        item: WorkItem,
        source: WorkSource,
        run_id: str,
        result: RunResult | None,
        error: BaseException | None,
    ) -> str:
        now = self.clock()
        report = self._report(run_id, result)
        if result is not None and report.succeeded:
            self._collect_backlog(run_id, source)
            self.dstore.mark_done(item.item_id, now)
            self.dstore.finish_ledger(run_id, "done", now)
            self._consecutive_failures = 0
            source.report_success(item, report)
            self._frontend_finished(item, report)
            self._notify(
                f"✅ {item.item_id} done ({report.task_summary})"
                + (f" · PR {report.delivery[1]}" if report.delivery else "")
            )
            return "done"
        if result is not None and result.state == "completed" and report.delivery_error:
            # Work done, PR failed: a human must look; retrying would redo the work.
            self._collect_backlog(run_id, source)
            self.dstore.mark_failed(
                item.item_id, f"delivery failed: {report.delivery_error}", now, requeue=False
            )
            self.dstore.finish_ledger(run_id, "delivery_failed", now)
            source.report_delivery_failed(item, report)
            self._frontend_finished(item, report)
            self._notify(f"⚠ {item.item_id} completed but delivery failed: {report.delivery_error}")
            return "delivery_failed"
        reason = str(error) if error is not None else f"run ended {report.state}"
        attempts_left = self.config.daemon.max_attempts_per_item - item.attempts
        self._consecutive_failures += 1
        self.dstore.finish_ledger(run_id, "failed", now)
        if attempts_left > 0:
            self.dstore.mark_failed(item.item_id, reason, now, requeue=True)
            source.report_retry(item, reason, attempts_left)
            self._notify(f"❌ {item.item_id} failed ({reason}); {attempts_left} attempt(s) left")
            outcome = "retry"
        else:
            self.dstore.mark_failed(item.item_id, reason, now, requeue=False)
            source.report_abandoned(item, reason)
            self._notify(f"❌ {item.item_id} abandoned after {item.attempts} attempt(s): {reason}")
            outcome = "abandoned"
        self._frontend_finished(item, report)
        if self._consecutive_failures >= self.config.daemon.max_consecutive_failures:
            self._breaker_opened_at = now
            self._notify(
                f"🛑 circuit breaker opened after {self._consecutive_failures} consecutive "
                f"failures; pausing dispatch for {self.config.daemon.breaker_cooldown_s:.0f}s"
            )
        return outcome

    def _breaker_open(self, now: float) -> bool:
        if self._breaker_opened_at is None:
            return False
        if now - self._breaker_opened_at >= self.config.daemon.breaker_cooldown_s:
            # Half-open: allow one item through; a success resets, a failure
            # re-opens via the counter.
            self._breaker_opened_at = None
            self._consecutive_failures = max(self._consecutive_failures - 1, 0)
            self._notify("circuit breaker half-open; allowing one item")
            return False
        return True

    # -- item -> run mapping ---------------------------------------------------------

    def _item_config(self, item: WorkItem) -> Config:
        gh = self.config.github
        if item.source == "github":
            gh = GithubConfig.model_validate(
                {
                    **gh.model_dump(),
                    "report": True,
                    "deliver": True,
                    "deliver_draft": self.config.daemon.deliver_draft,
                    "create_repo": False,
                }
            )
        return self.config.model_copy(update={"github": gh, "keep_on_failure": False})

    def outcome_text(self, item: WorkItem) -> str:
        parts = [item.title.strip()]
        if item.body.strip():
            parts.append(item.body.strip())
        if item.source == "github":
            origin = f"GitHub issue #{item.source_key} in {self.config.github.repo}"
            if item.url:
                origin += f" ({item.url})"
        else:
            origin = f"inbox file `{item.source_key}`"
        parts.append(f"---\nThis work item came from: {origin}.")
        if self.config.daemon.backlog != "off":
            parts.append(BACKLOG_INSTRUCTIONS)
        return "\n\n".join(parts)

    def _default_runner(
        self, item: WorkItem, item_config: Config, run_id: str, bus: EventBus, resume: bool
    ) -> RunResult:
        # _dispatch built the engine (so cancel_current and the frontend see
        # the one that is actually running); use it rather than a second one.
        handle = self._current
        assert handle is not None and handle.run_id == run_id
        engine = handle.engine
        if resume:
            return engine.resume(run_id)
        return engine.start(self.outcome_text(item), run_id=run_id)

    # -- reporting -----------------------------------------------------------------------

    def _report(self, run_id: str, result: RunResult | None) -> RunReport:
        try:
            record = self.store.get_run(run_id)
            state = record.state
            tasks = self.store.get_tasks(run_id)
        except SbxloopError:
            state = result.state if result is not None else "failed"
            tasks = result.tasks if result is not None else []
        done = sum(1 for t in tasks if t.state == "done")
        summary = f"{done}/{len(tasks)} tasks done" if tasks else "no tasks ran"
        tracking = delivery = None
        delivery_error = None
        try:
            for _seq, event in self.store.events(run_id, type_prefix="run."):
                data = event.data
                if event.type == HostEventTypes.RUN_REPORT and data.get("issue"):
                    tracking = (int(data["issue"]), str(data.get("url", "")))
                elif event.type == HostEventTypes.RUN_DELIVER:
                    if data.get("error"):
                        delivery_error = str(data["error"])
                    elif data.get("url"):
                        delivery = (int(data.get("pr", 0)), str(data["url"]))
                        delivery_error = None
        except SbxloopError:
            pass
        workspace = str(result.workspace) if result is not None and result.workspace else None
        return RunReport(run_id, state, summary, tracking, delivery, delivery_error, workspace)

    def _collect_backlog(self, run_id: str, source: WorkSource) -> None:
        mode = self.config.daemon.backlog
        if mode == "off":
            return
        target = next((s for s in self.sources if s.name == mode), None)
        if target is None:
            logger.warning("backlog mode %r but no such source is active", mode)
            return
        try:
            record = self.store.get_run(run_id)
            filed = collect_backlog(
                record,
                dstore=self.dstore,
                source=target,
                max_items=self.config.daemon.backlog_max_per_run,
                trigger=self.config.daemon.backlog_auto_trigger,
                now=self.clock(),
            )
        except SbxloopError:
            logger.warning("backlog collection failed for %s", run_id, exc_info=True)
            return
        if filed:
            self._notify(f"filed {len(filed)} backlog item(s) from {run_id}: {', '.join(filed)}")

    # -- recovery ------------------------------------------------------------------------

    def recover(self) -> None:
        """Reconcile items left ``running`` by a previous process."""
        for item in self.dstore.running_items():
            source = self._source_for(item)
            now = self.clock()
            if source is None:
                self.dstore.mark_failed(item.item_id, "no source on recovery", now, requeue=False)
                continue
            if item.run_id is None:
                self.dstore.mark_requeued_unstarted(item.item_id, now)
                self._notify(f"recovery: {item.item_id} re-queued (claimed, never started)")
                continue
            try:
                record = self.store.get_run(item.run_id)
            except SbxloopError:
                self.dstore.mark_requeued_unstarted(item.item_id, now)
                continue
            if record.state == "completed":
                self._notify(f"recovery: {item.run_id} had completed; settling {item.item_id}")
                self._settle(item, source, item.run_id, self._result_from_record(item.run_id), None)
            elif record.state in TERMINAL_RUN_STATES:
                self._notify(f"recovery: {item.run_id} ended {record.state}; applying failure path")
                self._settle(
                    item, source, item.run_id, None, StateError(f"run ended {record.state}")
                )
            elif record.state in RESUMABLE_RUN_STATES:
                last = self.store.last_event_ts(item.run_id)
                self._notify(
                    f"recovery: resuming {item.run_id} for {item.item_id} "
                    f"(last activity {self.clock() - last:.0f}s ago)"
                    if last
                    else f"recovery: resuming {item.run_id} for {item.item_id}"
                )
                # A dead process leaves its microVMs alive; resume
                # re-provisions under the same names and `sbx create` refuses
                # a name that exists (field: SIGKILL mid-run → 'sandbox
                # already exists' on the very next start).
                self._remove_stale_run_sandboxes(item.run_id)
                self._dispatch(item, source, resume_run_id=item.run_id)
            else:
                self.dstore.mark_requeued_unstarted(item.item_id, now)

    def _remove_stale_run_sandboxes(self, run_id: str) -> None:
        if self.sbx is None:
            return
        for role in ("agent", "github"):
            name = sandbox_name(run_id, role)
            try:
                Sandbox(self.sbx, name).rm()
                self._notify(f"recovery: removed stale sandbox {name}")
            except SbxError:
                pass  # not there — the common case

    def _result_from_record(self, run_id: str) -> RunResult:
        record = self.store.get_run(run_id)
        return RunResult(
            run_id=run_id,
            state=record.state,
            tasks=self.store.get_tasks(run_id),
            workspace=record.workspace,
            mounted=record.mounted,
        )

    # -- helpers ------------------------------------------------------------------------

    def _notify(self, text: str) -> None:
        logger.info("%s", text)
        if self.frontend is not None:
            try:
                self.frontend.daemon_event(text)
            except Exception:
                logger.debug("frontend daemon_event failed", exc_info=True)

    def _frontend_finished(self, item: WorkItem, report: RunReport) -> None:
        if self.frontend is not None:
            try:
                self.frontend.run_finished(item, report)
            except Exception:
                logger.warning("frontend run_finished failed", exc_info=True)
