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
Interrupted runs are resumable by design, so the item stays ``running``;
:meth:`recover` re-queues it with the run pinned on the next start and the
tick resumes it through the same guardrails as any dispatch.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple, Protocol

from sbxloop.config import Config, GithubConfig
from sbxloop.daemon.backlog import BACKLOG_INSTRUCTIONS, collect_backlog
from sbxloop.daemon.model import RunReport, TickOutcome, TickResult, WorkItem
from sbxloop.daemon.sources import WorkSource
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.model import RESUMABLE_RUN_STATES, TERMINAL_RUN_STATES, RunResult
from sbxloop.engine.store import StateStore
from sbxloop.errors import RunCancelledError, SbxError, SbxloopError, StateError
from sbxloop.events import EventBus, HostEventTypes
from sbxloop.gc import DAY_S, format_bytes, prune_run_dirs
from sbxloop.ids import new_run_id
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import sandbox_name
from sbxloop.sbx.prune import remove_run_sandbox, remove_run_sandbox_secrets

logger = logging.getLogger(__name__)


class Frontend(Protocol):
    """What a human-facing channel (Discord) sees of the loop's lifecycle.
    Every call is best-effort: the loop never depends on a frontend."""

    def daemon_event(self, text: str) -> None: ...
    def run_started(
        self, item: WorkItem, run_id: str, engine: LoopEngine, bus: EventBus
    ) -> None: ...
    def run_finished(self, item: WorkItem, report: RunReport) -> None: ...


class CancelRequest(NamedTuple):
    """An operator's ``!sbx cancel`` for one specific run. Recorded so the
    settle step can tell it from a failure: the engine surfaces both as an
    exception at the next boundary (field: a Discord cancel was settled as
    a failed attempt, re-run fresh after the backoff and counted toward the
    breaker — #246)."""

    run_id: str
    requester: str
    retry: bool


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
        self._cancel_request: CancelRequest | None = None
        # Breaker state lives in the store: a crash-restart loop must not
        # reset it (#254). These attributes are the write-through cache.
        self._breaker_opened_at, self._consecutive_failures = self.dstore.breaker()
        self._last_cap_log = 0.0
        # Per-source poll backoff: consecutive failures and the earliest
        # next poll, so a source that is down (GitHub outage, dead github
        # sandbox) is not hammered every tick.
        self._source_failures: dict[str, int] = {}
        self._source_next_poll: dict[str, float] = {}
        self._last_gc: float | None = None

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

    def cancel_current(self, requester: str | None = None, *, retry: bool = False) -> bool:
        """Operator cancel of the in-flight run. The engine stops at its next
        boundary and the item settles as *cancelled* — no retry, no breaker
        count — unless ``retry`` asks for a fresh run. Recorded under the
        current lock so the request can never be attributed to a later run."""
        with self._current_lock:
            handle = self._current
            if handle is None:
                return False
            self._cancel_request = CancelRequest(handle.run_id, requester or "operator", retry)
        handle.engine.request_cancel()
        return True

    def _take_cancel(self, run_id: str) -> CancelRequest | None:
        """The cancel request for ``run_id``, consumed. Any other pending
        request is stale (its run is gone) and dropped."""
        with self._current_lock:
            request, self._cancel_request = self._cancel_request, None
        return request if request is not None and request.run_id == run_id else None

    # -- operator item controls (#229) --------------------------------------------

    def abandon_item(self, item_id: str, reason: str | None = None) -> WorkItem:
        """Give up on an item deliberately. If it is the run in flight, the
        engine is asked to cancel and the settle path reports the abandon
        (so the source hears about it exactly once, after the run is really
        down); otherwise the source is told right here."""
        why = reason or "abandoned by operator"
        now = self.clock()
        fresh = self.dstore.abandon(item_id, why, now)
        if self._cancel_if_current(item_id):
            self._notify(f"abandoning {item_id}: cancelling its run {fresh.run_id}")
            return fresh
        if fresh.run_id is not None:
            # A pinned run that is not the one in flight is dead (a pending
            # resume): its microVMs and secrets would otherwise outlive the
            # ledger row that recovery uses to find them.
            self._close_dead_run(fresh.run_id, "abandoned", now)
        self._deliver_report(fresh)
        return fresh

    def retry_item(self, item_id: str, by: str | None = None) -> WorkItem:
        """Put a settled (abandoned/cancelled) item back in the queue with a
        clean slate at a human's request — fresh plan, not a resume — and
        tell the source who did it (GitHub: re-claim, drop the failed
        label)."""
        who = by or "operator"
        fresh = self.dstore.retry(item_id, self.clock(), f"re-queued by {who}")
        self._deliver_report(fresh, by=who)
        return fresh

    def requeue_item(self, item_id: str) -> WorkItem:
        """Drop an item's pinned run so its next dispatch starts fresh
        (attempts intact). Cancels the run if it is the one in flight."""
        before = self.dstore.get(item_id)
        pinned = before.run_id if before is not None else None
        now = self.clock()
        fresh = self.dstore.requeue(item_id, now)
        if self._cancel_if_current(item_id):
            self._notify(f"requeue: {item_id} — cancelling its run {pinned}")
        elif pinned is not None:
            self._close_dead_run(pinned, "requeued", now)
            self._notify(f"requeue: {item_id} unpinned from {pinned}")
        else:
            self._notify(f"requeue: {item_id} re-queued")
        return fresh

    def _close_dead_run(self, run_id: str, result: str, now: float) -> None:
        """A pinned run that will never be resumed: drop its sandboxes and
        secrets first (so an interruption here leaves the ledger open for
        recovery to finish the job), then close its ledger row."""
        self._remove_stale_run_sandboxes(run_id)
        self.dstore.finish_ledger(run_id, result, now)

    def _deliver_pending_reports(self) -> None:
        """Tell the sources about operator decisions they have not heard:
        ``sbxloop daemon abandon|retry`` runs in another process and can
        only flip the row, so an abandoned-while-queued item or a retried
        one would otherwise stay in the inbox's ``pending/``/``failed/`` (or
        keep GitHub's trigger/failed label) forever. Runs at the top of
        every tick and after recovery; the in-flight item is not here (its
        settle path delivers, once the run is really down)."""
        for item in self.dstore.pending_reports():
            self._deliver_report(item)

    def _deliver_report(self, item: WorkItem, *, by: str | None = None) -> None:
        """Deliver ``item.pending_report`` to its source, exactly once: the
        debt is taken atomically first, so a Discord command on the bridge
        thread and the tick sweep on the loop thread cannot both report."""
        kind = item.pending_report
        source = self._source_for(item)
        if kind is None or source is None:
            # No source (not configured this start): nothing to tell, and
            # the debt stays on the row for a start that has one.
            return
        if not self.dstore.take_pending_report(item.item_id):
            return
        if kind == "abandoned":
            why = item.last_error or "abandoned by operator"
            source.report_abandoned(item, why)
            self._notify(f"❌ {item.item_id} abandoned by operator: {why}")
        else:
            # A row-only retry records who asked as its last_error.
            who = by or (item.last_error or "").removeprefix("re-queued by ") or "operator"
            source.report_requeued(item, who)
            self._notify(f"↻ {item.item_id} re-queued by {who} (attempts reset)")

    def _cancel_if_current(self, item_id: str) -> bool:
        with self._current_lock:
            handle = self._current
        if handle is None or handle.item.item_id != item_id:
            return False
        handle.engine.request_cancel()
        return True

    def _operator_override(self, item_id: str, run_id: str) -> WorkItem | None:
        """The item row is the daemon's only channel from a CLI running in
        another process: an item that is no longer ``running`` on *this*
        run was abandoned/requeued by an operator. Returns the fresh row
        when so."""
        fresh = self.dstore.get(item_id)
        if fresh is None or (fresh.state == "running" and fresh.run_id == run_id):
            return None
        return fresh

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
            "resumes_today": self.dstore.resumes_since(now - DAY_S),
            "max_runs_per_day": self.config.daemon.max_runs_per_day,
            "breaker_open": self._breaker_open(now),
            "consecutive_failures": self._consecutive_failures,
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
        # Before the gates: an operator decision made from another process
        # reaches its source even while paused or with the breaker open.
        self._deliver_pending_reports()
        self._maybe_gc(now)
        if self._paused:
            return TickResult(idle_kind="paused")
        if self._breaker_open(now):
            return TickResult(idle_kind="breaker")
        started_today = self.dstore.runs_started_since(now - DAY_S)
        if started_today >= self.config.daemon.max_runs_per_day:
            if now - self._last_cap_log > 3600:
                self._last_cap_log = now
                cap = self.config.daemon.max_runs_per_day
                self._notify(
                    f"daily run cap reached ({started_today}/{cap}); idling until the window rolls"
                )
            return TickResult(idle_kind="daily_cap")
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
                    idle_kind="backoff",
                    idle_detail=f"{len(waiting)} queued; next eligible in {soonest:.0f}s",
                )
            return TickResult(discovered=discovered, idle_kind="no_work")
        source = self._source_for(item)
        if source is None:
            self.dstore.mark_failed(item.item_id, "no source for item", now, requeue=False)
            return TickResult(discovered=discovered, idle_kind="no_work")
        if not item.claimed:
            if not source.claim(item):
                self.dstore.mark_failed(item.item_id, "claim failed", now, requeue=False)
                self._notify(f"could not claim {item.item_id} ({item.title}); dropped")
                return TickResult(
                    discovered=discovered, dispatched=item.item_id, outcome="abandoned"
                )
            self.dstore.mark_claimed(item.item_id, now)
        if item.run_id is not None:
            outcome = self._resume(item, source, now)
        else:
            outcome = self._dispatch(item, source, resume_run_id=None)
        return TickResult(discovered=discovered, dispatched=item.item_id, outcome=outcome)

    def _resume(self, item: WorkItem, source: WorkSource, now: float) -> TickOutcome:
        """Resume the run recovery pinned on a queued item — or, past the
        per-item resume budget, settle that run as a failed attempt so a
        plan that keeps getting interrupted cannot burn engine wall clock
        forever (#234)."""
        run_id = item.run_id
        assert run_id is not None
        resumes = self.dstore.resumes_for_item(item.item_id)
        budget = self.config.daemon.max_resumes_per_item
        if resumes >= budget:
            self._notify(
                f"{item.item_id}: {run_id} interrupted again after {resumes} resume(s) "
                f"(budget {budget}); settling as a failed attempt"
            )
            return self._settle(
                item,
                source,
                run_id,
                None,
                StateError(f"run {run_id} interrupted; resume budget ({budget}) exhausted"),
            )
        # A dead process leaves its microVMs alive; resume re-provisions
        # under the same names and `sbx create` refuses a name that exists
        # (field: SIGKILL mid-run → 'sandbox already exists' on the very
        # next start).
        self._remove_stale_run_sandboxes(run_id)
        self._notify(f"resuming {run_id} for {item.item_id} (resume {resumes + 1}/{budget})")
        return self._dispatch(item, source, resume_run_id=run_id)

    # -- state-dir retention -------------------------------------------------------

    def _maybe_gc(self, now: float) -> None:
        """Sweep runs/<id>/ on the first tick after start and once a day
        thereafter. Runs before the pause/breaker checks: retention is
        housekeeping, not dispatch, and a paused daemon still fills the disk."""
        if self._last_gc is not None and now - self._last_gc < DAY_S:
            return
        self._last_gc = now
        self.gc(now)

    def gc(self, now: float | None = None) -> None:
        """One retention sweep (see :mod:`sbxloop.gc`); never raises — a
        failed sweep must not take the daemon down with it."""
        days = self.config.daemon.prune_runs_after_days
        if days <= 0:
            return
        now = self.clock() if now is None else now
        try:
            result = prune_run_dirs(
                self.store,
                self.config.state_dir,
                older_than_s=days * DAY_S,
                now=now,
                actor="daemon",
            )
        except Exception:
            logger.warning("daemon.gc: sweep failed", exc_info=True)
            return
        if not result.pruned and not result.failed:
            logger.debug("daemon.gc: nothing to prune (retention %sd)", days)
            return
        text = (
            f"daemon.gc: pruned {len(result.pruned)} run dir(s) older than {days:g}d, "
            f"freed {format_bytes(result.bytes_freed)}"
        )
        if result.failed:
            text += f"; {len(result.failed)} could not be removed ({', '.join(result.failed)})"
        self._notify(text)

    # -- discovery ---------------------------------------------------------------------

    # Poll backoff doubles per consecutive failure, from one poll interval up
    # to this ceiling; a source that is down for an hour is polled every 30
    # minutes, not every tick.
    SOURCE_BACKOFF_MAX_S = 1800.0

    def _discover(self, now: float) -> int:
        new = 0
        for source in self.sources:
            if now < self._source_next_poll.get(source.name, 0.0):
                continue
            try:
                found = source.poll()
            except Exception:
                failures = self._source_failures.get(source.name, 0) + 1
                self._source_failures[source.name] = failures
                delay = min(
                    self.config.daemon.poll_interval_s * 2**failures, self.SOURCE_BACKOFF_MAX_S
                )
                self._source_next_poll[source.name] = now + delay
                logger.warning(
                    "source %s poll failed (%d in a row); next poll in %.0fs",
                    source.name,
                    failures,
                    delay,
                    exc_info=True,
                )
                continue
            if self._source_failures.pop(source.name, 0):
                self._source_next_poll.pop(source.name, None)
                self._notify(f"source {source.name} polling recovered")
            for item in found:
                if self.dstore.upsert_new(item, now):
                    new += 1
                    self._notify(f"queued {item.item_id}: {item.title}")
        return new

    def _source_for(self, item: WorkItem) -> WorkSource | None:
        return next((s for s in self.sources if s.name == item.source), None)

    # -- dispatch ----------------------------------------------------------------------

    def _dispatch(
        self, item: WorkItem, source: WorkSource, *, resume_run_id: str | None
    ) -> TickOutcome:
        """Run one item (fresh, or resuming its interrupted run) and settle it."""
        now = self.clock()
        run_id = resume_run_id or new_run_id()
        if resume_run_id is None:
            self.dstore.mark_running(item.item_id, run_id, now)
            item = self.dstore.get(item.item_id) or item
            source.report_started(item, run_id)
        else:
            self.dstore.mark_resuming(item.item_id, run_id, now)
            item = self.dstore.get(item.item_id) or item
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
        cancel_sent = False
        while thread.is_alive():
            thread.join(timeout=1.0)
            # `sbxloop daemon abandon|requeue` from another process can only
            # touch the row; honor it by cancelling the run.
            if (
                not cancel_sent
                and thread.is_alive()
                and self._operator_override(item.item_id, run_id) is not None
            ):
                engine.request_cancel()
                cancel_sent = True
        with self._current_lock:
            self._current = None

        error = result_box.get("error")
        # An item-level operator decision (abandon/requeue, possibly from
        # another process) outranks a pending `!sbx cancel`: the row already
        # says what the item's fate is.
        override = self._operator_override(item.item_id, run_id)
        if override is not None:
            return self._settle_override(item, source, run_id, override, result_box.get("result"))
        cancel = self._take_cancel(run_id)
        if (
            cancel is not None
            and isinstance(error, RunCancelledError)
            and self._run_is_resumable(run_id)
        ):
            # The human's cancel took effect (engine raised its cancellation
            # error at a boundary, persisted run left mid-flight). Checked
            # before the shutdown branch: a cancel during quiesce must not be
            # resumed by recovery. Gated on the exception type, not just the
            # persisted state: an infra error re-raised while the run is still
            # resumable looks identical in the store, and a run that finished
            # or genuinely failed after the request settles normally — the
            # cancel simply came too late.
            return self._settle_cancelled(item, source, run_id, cancel)
        if self._stop.is_set() and "result" not in result_box and self._run_is_resumable(run_id):
            # Interrupted by shutdown at a phase boundary: the persisted run
            # is still resumable, so leave the item running for recovery.
            # A run that actually FAILED after stop was requested has a
            # terminal persisted state and settles like any failure below —
            # shutdown must not mask genuine errors as "interrupted".
            self.dstore.finish_ledger(run_id, "interrupted", self.clock())
            return "interrupted"
        if error is not None and not isinstance(error, SbxloopError | StateError):
            logger.error("run %s crashed", run_id, exc_info=error)
        return self._settle(item, source, run_id, result_box.get("result"), error)

    def _settle_override(
        self,
        item: WorkItem,
        source: WorkSource,
        run_id: str,
        fresh: WorkItem,
        result: RunResult | None,
    ) -> TickOutcome:
        """The operator already decided this item's fate while it ran; the
        run's own outcome must not overwrite that (a cancelled run would
        otherwise take the failure path and re-queue an abandoned item).
        Operator decisions never count toward the circuit breaker."""
        now = self.clock()
        report = self._report(run_id, result)
        if fresh.state == "abandoned":
            self.dstore.finish_ledger(run_id, "abandoned", now)
            self._deliver_report(fresh)
            self._frontend_finished(item, report)
            return "abandoned"
        self.dstore.finish_ledger(run_id, "requeued", now)
        self._frontend_finished(item, report)
        self._notify(f"{item.item_id} requeued by operator; run {run_id} ended {report.state}")
        return "requeued"

    def _run_is_resumable(self, run_id: str) -> bool:
        """Whether the run was left mid-flight (interrupted) rather than
        finished. Note RESUMABLE_RUN_STATES includes 'failed' (a failed run
        may be resumed by an operator); an *interruption* is specifically a
        non-terminal state."""
        try:
            return self.store.get_run(run_id).state not in TERMINAL_RUN_STATES
        except SbxloopError:
            # No persisted run yet (died before create_run): nothing to
            # resume, but nothing failed either — recovery re-queues it.
            return True

    def _settle(
        self,
        item: WorkItem,
        source: WorkSource,
        run_id: str,
        result: RunResult | None,
        error: BaseException | None,
    ) -> TickOutcome:
        now = self.clock()
        report = self._report(run_id, result)
        if result is not None and report.succeeded:
            self._collect_backlog(run_id, source)
            self.dstore.mark_done(item.item_id, now)
            self.dstore.finish_ledger(run_id, "done", now)
            self._set_breaker(None, 0)
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
        self._set_breaker(self._breaker_opened_at, self._consecutive_failures + 1)
        self.dstore.finish_ledger(run_id, "failed", now)
        if attempts_left > 0:
            self.dstore.mark_failed(item.item_id, reason, now, requeue=True)
            source.report_retry(item, reason, attempts_left)
            self._notify(f"❌ {item.item_id} failed ({reason}); {attempts_left} attempt(s) left")
            outcome: TickOutcome = "retry"
        else:
            self.dstore.mark_failed(item.item_id, reason, now, requeue=False)
            source.report_abandoned(item, reason)
            self._notify(f"❌ {item.item_id} abandoned after {item.attempts} attempt(s): {reason}")
            outcome = "abandoned"
        self._frontend_finished(item, report)
        if self._consecutive_failures >= self.config.daemon.max_consecutive_failures:
            self._set_breaker(now, self._consecutive_failures)
            self._notify(
                f"🛑 circuit breaker opened after {self._consecutive_failures} consecutive "
                f"failures; pausing dispatch for {self.config.daemon.breaker_cooldown_s:.0f}s"
            )
        return outcome

    def _settle_cancelled(
        self, item: WorkItem, source: WorkSource, run_id: str, cancel: CancelRequest
    ) -> TickOutcome:
        now = self.clock()
        # The daemon's verdict is "cancelled" even though the persisted run
        # is still mid-flight (and therefore resumable) — that is the point.
        report = self._report(run_id, None)._replace(
            state="cancelled", cancelled_by=cancel.requester, requeued=cancel.retry
        )
        reason = f"cancelled by {cancel.requester}"
        self.dstore.finish_ledger(run_id, "cancelled", now)
        self.dstore.mark_cancelled(item.item_id, reason, now)
        if cancel.retry:
            # cancelled → queued is the same transition `!sbx retry` makes.
            self.dstore.retry(item.item_id, now, reason)
            # report_cancelled(requeued=True) below is the source-side report.
            self.dstore.take_pending_report(item.item_id)
            self._notify(f"⏹ {item.item_id} {reason}; re-queued to run again fresh")
        else:
            self._notify(
                f"⏹ {item.item_id} {reason} — `sbxloop resume {run_id}` continues it, "
                f"`!sbx retry {item.item_id}` reruns it fresh"
            )
        source.report_cancelled(item, report)
        self._frontend_finished(item, report)
        return "cancelled"

    def _set_breaker(self, opened_at: float | None, consecutive_failures: int) -> None:
        self._breaker_opened_at = opened_at
        self._consecutive_failures = consecutive_failures
        self.dstore.set_breaker(opened_at, consecutive_failures)

    def _breaker_open(self, now: float) -> bool:
        if self._breaker_opened_at is None:
            return False
        if now - self._breaker_opened_at >= self.config.daemon.breaker_cooldown_s:
            # Half-open: allow one item through; a success resets, a failure
            # re-opens via the counter.
            self._set_breaker(None, max(self._consecutive_failures - 1, 0))
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
                    # The per-run tracking issue is redundant when the source
                    # issue already is one (#251); the summary comment there
                    # carries the same information.
                    "report": self.config.daemon.tracking_issue,
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
        """Reconcile items left ``running`` by a previous process.

        Finished runs are settled here; an interrupted run is only *queued
        for resume* — the actual resume happens in :meth:`tick`, behind the
        breaker / daily cap / pause gate and the per-item resume budget.
        Recovery used to dispatch resumes directly, so a daemon restarting
        into a bad state (breaker open, cap spent, operator-paused) resumed
        anyway (#254)."""
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
                    f"recovery: {item.run_id} for {item.item_id} queued for resume "
                    f"(last activity {self.clock() - last:.0f}s ago)"
                    if last
                    else f"recovery: {item.run_id} for {item.item_id} queued for resume"
                )
                self.dstore.mark_resume_pending(item.item_id, now)
            else:
                self.dstore.mark_requeued_unstarted(item.item_id, now)
        self._settle_offline_overrides()

    def _settle_offline_overrides(self) -> None:
        """`sbxloop daemon abandon|requeue` while no daemon is running can
        only flip the row (field scenario of #229: the item was left
        running/pinned after a clean shutdown). The source was never told,
        the run's ledger row is still open (or ``interrupted``) and its
        microVMs still exist. Finish that work here so the abandon reaches
        the issue / inbox file exactly once, on the next daemon start. The
        source report itself is the row's ``pending_report`` debt, paid by
        :meth:`_deliver_pending_reports` right after (and by every tick —
        an abandoned-while-queued item has no run to find here)."""
        for run_id, item_id in self.dstore.unsettled_runs():
            item = self.dstore.get(item_id)
            if item is None:
                continue
            now = self.clock()
            if item.state == "abandoned" and item.run_id == run_id:
                self._close_dead_run(run_id, "abandoned", now)
                self._notify(f"recovery: {item_id} abandoned offline; run {run_id} closed")
            elif item.state == "queued" and item.run_id != run_id:
                # Requeued (unpinned) offline: the run is dead and will not be
                # resumed — close its ledger and drop its sandboxes.
                self._close_dead_run(run_id, "requeued", now)
                self._notify(f"recovery: {item_id} requeued offline; run {run_id} closed")
            # A queued item still pinned to this run is a pending resume; a
            # running one was reconciled above.
        self._deliver_pending_reports()

    def _remove_stale_run_sandboxes(self, run_id: str) -> None:
        """A dead process leaves the run's microVMs — and their secret
        registrations — behind. Both must go before resume re-provisions
        under the same names: a lingering secret cannot be replaced, so the
        agent would come up holding the proxy sentinel and the Copilot SDK
        401s (field failure rgn9ccjam)."""
        if self.sbx is None:
            return
        for role in ("agent", "github"):
            name = sandbox_name(run_id, role)
            try:
                remove_run_sandbox(self.sbx, name, role)
                self._notify(f"recovery: removed stale sandbox {name} (and its secrets)")
            except SbxError:
                # No such sandbox — the common case — but a secret may
                # still linger from a rollback race; clearing it is cheap.
                remove_run_sandbox_secrets(self.sbx, name, role)

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
