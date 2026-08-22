"""DaemonLoop: discover → claim → run → report, forever.

One item at a time, one fresh :class:`LoopEngine` per item (engines are
single-use: their cancel flag never clears), one shared daemon-owned
:class:`StateStore`, a fresh :class:`EventBus` per run (each engine adds
permanent subscribers to its bus). Spend guardrails — a calendar-day run
cap that counts runs started since 00:00 in ``daemon.run_cap_timezone``
(default ``UTC``) and resets at the next midnight there; a per-item
attempt cap; a
consecutive-failure circuit breaker — are
the daemon's only defense against a mislabeled issue in a fully autonomous
setup, so they are enforced in the tick, not left to configuration hope.

Shutdown is cooperative: a signal sets the stop flag, asks the in-flight
engine to cancel (honored at its next task boundary), and joins it briefly.
Interrupted runs are resumable by design, so the item stays ``running``;
:meth:`recover` re-queues it with the run pinned on the next start and the
tick resumes it through the same guardrails as any dispatch.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, NamedTuple, Protocol
from zoneinfo import ZoneInfo

from sbxloop import hostgit
from sbxloop.config import Config, GithubConfig, SandboxConfig
from sbxloop.daemon.audits import audit_marker, due_charters, issue_body, load_charters
from sbxloop.daemon.backlog import (
    AUDIT_INSTRUCTIONS,
    BACKLOG_INSTRUCTIONS,
    ToolFindings,
    collect_backlog,
    collect_tool_findings,
)
from sbxloop.daemon.discord_format import (
    charter_skipped_notice,
    code,
    filed_notice,
    findings_summary,
    link,
    refs_text,
)
from sbxloop.daemon.logsink import event_log_subscriber
from sbxloop.daemon.model import RunReport, TickOutcome, TickResult, WorkItem
from sbxloop.daemon.postmortem import build_dossier
from sbxloop.daemon.sources import WorkSource
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.model import (
    RESUMABLE_RUN_STATES,
    TERMINAL_RUN_STATES,
    RunRecord,
    RunResult,
    RunState,
)
from sbxloop.engine.store import StateStore
from sbxloop.errors import (
    ProvisionError,
    RunCancelledError,
    SbxError,
    SbxloopError,
    StateError,
)
from sbxloop.events import Event, EventBus, HostEventTypes
from sbxloop.gc import DAY_S, format_bytes, prune_run_dirs
from sbxloop.gh.ops import SubmittedReview
from sbxloop.ids import new_run_id
from sbxloop.log import bind_run, clear_run, get_logger
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import sandbox_name
from sbxloop.sbx.prune import remove_run_sandbox, remove_run_sandbox_secrets

log = get_logger(__name__)

# How often the audit scheduler fast-forwards the checkout to see new charters.
AUDIT_REFRESH_S = 600.0


def day_window(now: float, tz: str) -> tuple[float, float]:
    """The calendar day containing epoch ``now`` in IANA zone ``tz``, as
    ``(start_epoch, next_start_epoch)``.

    Every instant in the same local calendar date maps to the same
    ``start_epoch``, and the count only resets when local midnight passes. ``next_start_epoch`` is
    the next local midnight (which is not always 86400s later — DST days
    are 23 or 25 hours long)."""
    zone = ZoneInfo(tz)
    local = datetime.fromtimestamp(now, tz=zone)
    start = datetime.combine(local.date(), dtime(0, 0), tzinfo=zone)
    next_start = datetime.combine(local.date() + timedelta(days=1), dtime(0, 0), tzinfo=zone)
    return start.timestamp(), next_start.timestamp()


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
        self._audit_problems_seen: set[str] = set()
        self._last_audit_refresh = float("-inf")
        self._current: RunHandle | None = None
        self._current_lock = threading.Lock()
        self._cancel_request: CancelRequest | None = None
        # Breaker state lives in the store: a crash-restart loop must not
        # reset it (#254). These attributes are the write-through cache.
        self._breaker_opened_at, self._consecutive_failures = self.dstore.breaker()
        self._last_cap_log = 0.0
        self._last_idle_kind: str | None = None
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
            self._notify(
                f"abandoning {item_id}: cancelling its run {fresh.run_id}",
                "item.abandon_cancelling",
                item=item_id,
                run=fresh.run_id,
                reason=why,
            )
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
        log.info("item.retry", item=item_id, by=who)
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
            self._notify(
                f"requeue: {item_id} — cancelling its run {pinned}",
                "item.requeue_cancelling",
                item=item_id,
                run=pinned,
            )
        elif pinned is not None:
            self._close_dead_run(pinned, "requeued", now)
            self._notify(
                f"requeue: {item_id} unpinned from {pinned}",
                "item.requeue_unpinned",
                item=item_id,
                run=pinned,
            )
        else:
            self._notify(f"requeue: {item_id} re-queued", "item.requeued", item=item_id)
        return fresh

    def _close_dead_run(self, run_id: str, result: str, now: float) -> None:
        """A pinned run that will never be resumed: drop its sandboxes and
        secrets first (so an interruption here leaves the ledger open for
        recovery to finish the job), then close its ledger row."""
        self._remove_stale_run_sandboxes(run_id)
        self._end_run_cancelled(run_id, f"cancelled: run {result} (never resumed)", source=result)
        self.dstore.finish_ledger(run_id, result, now)

    def _end_run_cancelled(
        self,
        run_id: str,
        reason: str,
        *,
        requester: str | None = None,
        retry: bool = False,
        source: str,
    ) -> None:
        """Transition the *engine* run record to the terminal state
        ``cancelled`` and append (never mutate) a chronology event saying so.

        Without this the run row is only ever written by the in-process run
        loop, so a cancelled item left a phantom ``running`` run behind
        (#374). A run already in a terminal state, or with no record at all
        (died before ``create_run``), is a no-op.
        """
        try:
            record = self.store.get_run(run_id)
        except (SbxloopError, StateError):
            log.debug("run.cancel_no_record", run=run_id, hint="died before create_run")
            return
        if record.state in TERMINAL_RUN_STATES:
            return
        try:
            self.store.reconcile_run(run_id, "cancelled", reason)
            self.store.append_event(
                Event.now(
                    "run.cancelled",
                    run_id,
                    reason=reason,
                    by=requester,
                    requeued=retry,
                    via=source,
                )
            )
        except (SbxloopError, StateError) as exc:
            log.warning("run.cancel_write_failed", run=run_id, error=str(exc))

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
            self._notify(
                f"❌ {item.item_id} abandoned by operator: {why}",
                "item.abandoned_by_operator",
                item=item.item_id,
                source=source.name,
                reason=why,
            )
        else:
            # A row-only retry records who asked as its last_error.
            who = by or (item.last_error or "").removeprefix("re-queued by ") or "operator"
            source.report_requeued(item, who)
            self._notify(
                f"↻ {item.item_id} re-queued by {who} (attempts reset)",
                "item.requeued_by_operator",
                item=item.item_id,
                source=source.name,
                by=who,
            )

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
            log.info(
                "daemon.quiesce",
                item=handle.item.item_id,
                run=handle.run_id,
                grace_s=self.config.daemon.shutdown_grace_s,
            )
            handle.engine.request_cancel()
        else:
            log.info("daemon.quiesce", run=None)
        thread = getattr(self, "_engine_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.config.daemon.shutdown_grace_s)
            if thread.is_alive():
                log.warning(
                    "daemon.quiesce_timeout",
                    run=handle.run_id if handle is not None else None,
                    grace_s=self.config.daemon.shutdown_grace_s,
                    hint="engine still running past shutdown grace; the run stays resumable",
                )

    def status(self) -> dict[str, Any]:
        now = self.clock()
        day_start, day_end = day_window(now, self.config.daemon.run_cap_timezone)
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
            "runs_today": self.dstore.runs_started_since(day_start),
            "runs_today_resets_at": day_end,
            "run_cap_timezone": self.config.daemon.run_cap_timezone,
            "resumes_today": self.dstore.resumes_since(day_start),
            "max_runs_per_day": self.config.daemon.max_runs_per_day,
            "breaker_open": self._breaker_open(now),
            "consecutive_failures": self._consecutive_failures,
            "paused": self._paused,
            "stopping": self._stop.is_set(),
        }

    # -- main loop --------------------------------------------------------------------

    def run_forever(self) -> None:
        self._notify(
            "daemon started",
            "daemon.started",
            poll_interval_s=self.config.daemon.poll_interval_s,
            sources=[s.name for s in self.sources],
        )
        ticks = 0
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                result = self.tick()
                ticks += 1
                self._log_tick(result, time.monotonic() - started)
                if result.dispatched is None:
                    self._stop.wait(self.config.daemon.poll_interval_s)
        finally:
            self._notify("daemon stopped", "daemon.stopped", ticks=ticks)

    def _log_tick(self, result: TickResult, duration_s: float) -> None:
        """Every tick at DEBUG; a *change* of why the daemon is idle at INFO,
        so a journal at the default level says once that the daemon is
        paused / breaker-open / backing off / capped — and once when work
        resumes — instead of nothing at all (or the same line each poll)."""
        log.debug(
            "daemon.tick",
            discovered=result.discovered,
            dispatched=result.dispatched,
            outcome=result.outcome,
            idle=result.idle_kind,
            idle_detail=result.idle_detail,
            duration_s=round(duration_s, 3),
        )
        kind = result.idle_kind if result.dispatched is None else None
        if kind != self._last_idle_kind:
            if kind is None:
                log.info("daemon.active", item=result.dispatched, outcome=result.outcome)
            else:
                log.info(
                    "daemon.idle",
                    idle=kind,
                    idle_detail=result.idle_detail,
                    queued=len(self.dstore.queued()),
                )
            self._last_idle_kind = kind

    def tick(self) -> TickResult:
        now = self.clock()
        # Before the gates: an operator decision made from another process
        # reaches its source even while paused or with the breaker open.
        self._deliver_pending_reports()
        self._maybe_gc(now)
        # Liveness safety net for phantom active runs (#374); sweeps even while
        # paused, the very state the field report was filed from.
        self._reconcile_stale_runs(now)
        if self._paused:
            return TickResult(idle_kind="paused")
        if self._breaker_open(now):
            return TickResult(idle_kind="breaker")
        day_start, day_end = day_window(now, self.config.daemon.run_cap_timezone)
        started_today = self.dstore.runs_started_since(day_start)
        if started_today >= self.config.daemon.max_runs_per_day:
            if now - self._last_cap_log > 3600:
                self._last_cap_log = now
                cap = self.config.daemon.max_runs_per_day
                tz = self.config.daemon.run_cap_timezone
                self._notify(
                    f"run cap reached for today ({tz}): {started_today}/{cap}; "
                    f"resets at 00:00 {tz}",
                    "daemon.daily_cap",
                    started_today=started_today,
                    cap=cap,
                    timezone=tz,
                    resets_at=day_end,
                )
            return TickResult(idle_kind="daily_cap")
        self._schedule_audits(now)
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
            log.warning(
                "item.abandoned",
                item=item.item_id,
                source=item.source,
                reason="no such source is active this start",
            )
            return TickResult(discovered=discovered, idle_kind="no_work")
        if not item.claimed:
            if not source.claim(item):
                self.dstore.mark_failed(item.item_id, "claim failed", now, requeue=False)
                self._notify(
                    f"could not claim {item.item_id} ({item.title}); dropped",
                    "item.claim_failed",
                    item=item.item_id,
                    source=source.name,
                    title=item.title,
                )
                return TickResult(
                    discovered=discovered, dispatched=item.item_id, outcome="abandoned"
                )
            self.dstore.mark_claimed(item.item_id, now)
            log.info("item.claimed", item=item.item_id, source=source.name, title=item.title)
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
                f"(budget {budget}); settling as a failed attempt",
                "run.resume_budget_exhausted",
                item=item.item_id,
                run=run_id,
                resumes=resumes,
                budget=budget,
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
        self._notify(
            f"resuming {run_id} for {item.item_id} (resume {resumes + 1}/{budget})",
            "run.resuming",
            item=item.item_id,
            run=run_id,
            resume=resumes + 1,
            budget=budget,
        )
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
            log.warning(
                "daemon.gc_failed",
                state_dir=str(self.config.state_dir),
                retention_days=days,
                exc_info=True,
            )
            return
        if not result.pruned and not result.failed:
            log.debug("daemon.gc_nothing_to_prune", retention_days=days)
            return
        text = (
            f"daemon.gc: pruned {len(result.pruned)} run dir(s) older than {days:g}d, "
            f"freed {format_bytes(result.bytes_freed)}"
        )
        if result.failed:
            text += f"; {len(result.failed)} could not be removed ({', '.join(result.failed)})"
        self._notify(
            text,
            "daemon.gc",
            pruned=len(result.pruned),
            failed=len(result.failed),
            bytes_freed=result.bytes_freed,
            retention_days=days,
        )

    # -- discovery ---------------------------------------------------------------------

    # Poll backoff doubles per consecutive failure, from one poll interval up
    # to this ceiling; a source that is down for an hour is polled every 30
    # minutes, not every tick.
    SOURCE_BACKOFF_MAX_S = 1800.0

    def _discover(self, now: float) -> int:
        new = 0
        for source in self.sources:
            next_poll = self._source_next_poll.get(source.name, 0.0)
            if now < next_poll:
                log.debug("source.poll_skipped", source=source.name, backoff_left_s=next_poll - now)
                continue
            started = time.monotonic()
            try:
                found = source.poll()
            except Exception:
                failures = self._source_failures.get(source.name, 0) + 1
                self._source_failures[source.name] = failures
                delay = min(
                    self.config.daemon.poll_interval_s * 2**failures, self.SOURCE_BACKOFF_MAX_S
                )
                self._source_next_poll[source.name] = now + delay
                log.warning(
                    "source.poll_failed",
                    source=source.name,
                    failures=failures,
                    next_poll_in_s=round(delay),
                    duration_s=round(time.monotonic() - started, 2),
                    exc_info=True,
                )
                continue
            if failures_cleared := self._source_failures.pop(source.name, 0):
                self._source_next_poll.pop(source.name, None)
                self._notify(
                    f"source {source.name} polling recovered",
                    "source.poll_recovered",
                    source=source.name,
                    after_failures=failures_cleared,
                )
            fresh = 0
            for item in found:
                if self.dstore.upsert_new(item, now):
                    fresh += 1
                    self._notify(
                        f"queued {item.item_id}: {item.title}",
                        "item.queued",
                        item=item.item_id,
                        source=source.name,
                        kind=item.kind,
                        title=item.title,
                    )
            new += fresh
            log.debug(
                "source.polled",
                source=source.name,
                found=len(found),
                new=fresh,
                duration_s=round(time.monotonic() - started, 2),
            )
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
        started = time.monotonic()
        if resume_run_id is None:
            self.dstore.mark_running(item.item_id, run_id, now)
            item = self.dstore.get(item.item_id) or item
            source.report_started(item, run_id)
            # Fresh runs only: a resumed run is pinned to the clone it
            # already has, so moving the source would change nothing.
            self._refresh_workspace()
        else:
            self.dstore.mark_resuming(item.item_id, run_id, now)
            item = self.dstore.get(item.item_id) or item
        log.info(
            "run.dispatch",
            item=item.item_id,
            run=run_id,
            source=source.name,
            kind=item.kind,
            attempt=item.attempts,
            max_attempts=self.config.daemon.max_attempts_per_item,
            resume=resume_run_id is not None,
            title=item.title,
        )
        item_config = self._item_config(item)
        bus = EventBus()
        bus.subscribe(event_log_subscriber)
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
                log.warning(
                    "frontend.run_started_failed", item=item.item_id, run=run_id, exc_info=True
                )

        result_box: dict[str, Any] = {}

        def target() -> None:
            # Context vars are per-thread: stamp run/item on everything the
            # engine logs from here (provisioning, worker client, phases).
            bind_run(run_id, item.item_id, source=item.source)
            try:
                result_box["result"] = self._runner(
                    item, item_config, run_id, bus, resume_run_id is not None
                )
            except BaseException as exc:
                result_box["error"] = exc
            finally:
                clear_run()

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
                and (override := self._operator_override(item.item_id, run_id)) is not None
            ):
                log.info(
                    "run.cancel_requested",
                    item=item.item_id,
                    run=run_id,
                    reason=f"operator override: item now {override.state}",
                )
                engine.request_cancel()
                cancel_sent = True
        with self._current_lock:
            self._current = None

        error = result_box.get("error")
        result = result_box.get("result")
        log.info(
            "run.finished",
            item=item.item_id,
            run=run_id,
            outcome=(
                result.state
                if result is not None
                else "interrupted"
                if self._stop.is_set()
                else type(error).__name__
                if error is not None
                else "unknown"
            ),
            duration_s=round(time.monotonic() - started, 1),
            attempt=item.attempts,
        )
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
            log.warning(
                "run.interrupted",
                item=item.item_id,
                run=run_id,
                resumable=True,
                hint="shutdown at a phase boundary; recovery queues it for resume",
            )
            return "interrupted"
        if error is not None and not isinstance(error, SbxloopError | StateError):
            log.error(
                "run.crashed",
                item=item.item_id,
                run=run_id,
                attempt=item.attempts,
                duration_s=round(time.monotonic() - started, 1),
                exc_info=error,
            )
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
            self._end_run_cancelled(
                run_id, f"cancelled: {item.item_id} abandoned by operator", source="abandon"
            )
            self.dstore.finish_ledger(run_id, "abandoned", now)
            self._deliver_report(fresh)
            self._frontend_finished(item, report)
            return "abandoned"
        self._end_run_cancelled(
            run_id, f"cancelled: {item.item_id} requeued by operator", source="requeue"
        )
        self.dstore.finish_ledger(run_id, "requeued", now)
        self._frontend_finished(item, report)
        self._notify(
            f"{item.item_id} requeued by operator; run {run_id} ended {report.state}",
            "run.requeued_by_operator",
            item=item.item_id,
            run=run_id,
            state=report.state,
        )
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
            log.debug("run.no_record", run=run_id, hint="died before create_run")
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
            posted = self._post_review(item, run_id)
            # A review's findings go on the pull request, never into the
            # tracker: filing them as issues is the behaviour being replaced,
            # and a reviewer with an issue-shaped outlet will use it.
            filed = [] if posted is not None else self._collect_backlog(run_id, source)
            tool = self._collect_tool_findings(run_id, source)
            report = report._replace(
                filed=tuple(filed), tool_filed=tuple(tool.filed), tool_noted=tuple(tool.unfiled)
            )
            self.dstore.mark_done(item.item_id, now)
            self.dstore.finish_ledger(run_id, "done", now)
            if self._consecutive_failures:
                log.info("breaker.reset", after_failures=self._consecutive_failures)
            self._set_breaker(None, 0)
            source.report_success(item, report)
            self._frontend_finished(item, report)
            findings = findings_summary(report, repo=self._repo, kind=item.kind)
            self._notify(
                f"✅ {item.item_id} done ({report.task_summary})"
                + (f" · PR {report.delivery[1]}" if report.delivery else "")
                + (f" · {findings}" if findings else ""),
                "run.done",
                item=item.item_id,
                run=run_id,
                tasks=report.task_summary,
                pr=report.delivery[1] if report.delivery else None,
                filed=len(report.filed),
                attempt=item.attempts,
            )
            self._file_review(item, run_id, report)
            return "done"
        if result is not None and result.state == "completed" and report.delivery_error:
            # Work done, PR failed: a human must look; retrying would redo the work.
            filed = self._collect_backlog(run_id, source)
            self.dstore.mark_failed(
                item.item_id, f"delivery failed: {report.delivery_error}", now, requeue=False
            )
            self.dstore.finish_ledger(run_id, "delivery_failed", now)
            source.report_delivery_failed(item, report)
            self._frontend_finished(item, report)
            findings = findings_summary(report._replace(filed=tuple(filed)), repo=self._repo)
            self._notify(
                f"⚠ {item.item_id} completed but delivery failed: {report.delivery_error}"
                + (f" · {findings}" if findings else ""),
                "run.delivery_failed",
                level="error",
                item=item.item_id,
                run=run_id,
                error=report.delivery_error,
                hint="work is done but no PR; a human must look — retrying would redo the work",
            )
            self._file_postmortem(item, run_id, f"delivery failed: {report.delivery_error}")
            return "delivery_failed"
        reason = str(error) if error is not None else f"run ended {report.state}"
        if item.kind == "audit" and result is not None:
            # An audit that failed on the harness (a verify command it never
            # needed) still wrote its findings; they are evidence, not code,
            # so file them rather than lose them (field: rakvqn6fr).
            filed = self._collect_backlog(run_id, source)
            tool = self._collect_tool_findings(run_id, source)
            if filed or tool.filed:
                self._notify(
                    f"🔎 {item.item_id} failed but its findings were filed · "
                    f"{refs_text([*filed, *tool.filed], self._repo)}",
                    "run.audit_findings_filed",
                    item=item.item_id,
                    run=run_id,
                    filed=len(filed) + len(tool.filed),
                )
        attempts_left = self.config.daemon.max_attempts_per_item - item.attempts
        self._set_breaker(self._breaker_opened_at, self._consecutive_failures + 1)
        self.dstore.finish_ledger(run_id, "failed", now)
        if attempts_left > 0:
            self.dstore.mark_failed(item.item_id, reason, now, requeue=True)
            source.report_retry(item, reason, attempts_left)
            self._notify(
                f"❌ {item.item_id} failed ({reason}); {attempts_left} attempt(s) left",
                "run.failed",
                level="warning",
                item=item.item_id,
                run=run_id,
                reason=reason,
                attempt=item.attempts,
                attempts_left=attempts_left,
                retry_backoff_s=self.config.daemon.retry_backoff_s,
                consecutive_failures=self._consecutive_failures,
            )
            outcome: TickOutcome = "retry"
        else:
            self.dstore.mark_failed(item.item_id, reason, now, requeue=False)
            source.report_abandoned(item, reason)
            self._notify(
                f"❌ {item.item_id} abandoned after {item.attempts} attempt(s): {reason}",
                "run.abandoned",
                level="error",
                item=item.item_id,
                run=run_id,
                reason=reason,
                attempts=item.attempts,
                consecutive_failures=self._consecutive_failures,
            )
            self._file_postmortem(item, run_id, f"abandoned: {reason}")
            outcome = "abandoned"
        self._frontend_finished(item, report)
        if self._consecutive_failures >= self.config.daemon.max_consecutive_failures:
            self._set_breaker(now, self._consecutive_failures)
            self._notify(
                f"🛑 circuit breaker opened after {self._consecutive_failures} consecutive "
                f"failures; pausing dispatch for {self.config.daemon.breaker_cooldown_s:.0f}s",
                "breaker.opened",
                level="error",
                consecutive_failures=self._consecutive_failures,
                cooldown_s=self.config.daemon.breaker_cooldown_s,
            )
        return outcome

    def _settle_cancelled(
        self, item: WorkItem, source: WorkSource, run_id: str, cancel: CancelRequest
    ) -> TickOutcome:
        now = self.clock()
        report = self._report(run_id, None)._replace(
            state="cancelled", cancelled_by=cancel.requester, requeued=cancel.retry
        )
        reason = f"cancelled by {cancel.requester}" + (" (retry)" if cancel.retry else "")
        # The engine state store (state.db) and the daemon store are separate
        # databases, so the run row and the item row cannot share one
        # transaction. The run record is written FIRST and the item writes
        # follow adjacently: an interruption between them leaves a terminal
        # run and a still-running item (which recovery re-queues) rather than
        # the phantom `running` run of #374.
        self._end_run_cancelled(
            run_id,
            reason,
            requester=cancel.requester,
            retry=cancel.retry,
            source="operator_cancel",
        )
        self.dstore.finish_ledger(run_id, "cancelled", now)
        self.dstore.mark_cancelled(item.item_id, reason, now)
        if cancel.retry:
            # cancelled → queued is the same transition `!sbx retry` makes.
            self.dstore.retry(item.item_id, now, reason)
            # report_cancelled(requeued=True) below is the source-side report.
            self.dstore.take_pending_report(item.item_id)
            self._notify(
                f"⏹ {item.item_id} {reason}; re-queued to run again fresh",
                "run.cancelled",
                item=item.item_id,
                run=run_id,
                by=cancel.requester,
                requeued=True,
            )
        else:
            self._notify(
                f"⏹ {item.item_id} {reason} — `sbxloop resume {run_id}` continues it, "
                f"`!sbx retry {item.item_id}` reruns it fresh",
                "run.cancelled",
                item=item.item_id,
                run=run_id,
                by=cancel.requester,
                requeued=False,
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
            self._notify(
                "circuit breaker half-open; allowing one item",
                "breaker.half_open",
                consecutive_failures=self._consecutive_failures,
            )
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
                    # An audit's output is the issues it files; delivering
                    # its (deliberately unchanged) tree would only raise
                    # "nothing to deliver" and mis-settle it as failed.
                    "deliver": item.kind != "audit",
                    "deliver_draft": self.config.daemon.deliver_draft,
                    "create_repo": False,
                }
            )
        update: dict[str, Any] = {"github": gh, "keep_on_failure": False}
        sandbox = self.config.sandbox
        if (
            self._workspace_checkout() is not None
            and sandbox.workspace_isolation != self.config.daemon.workspace_isolation
        ):
            # Unattended runs answer the dirty-tree question by config
            # (#255): `auto`'s refusal has no human to act on it and would
            # fail every issue while someone has uncommitted work in the
            # checkout. Only a git checkout gets the override — for a plain
            # directory `auto` already means in-place, and forcing `clone`
            # there would turn every run into a provisioning error.
            update["sandbox"] = SandboxConfig.model_validate(
                {
                    **sandbox.model_dump(),
                    "workspace_isolation": self.config.daemon.workspace_isolation,
                }
            )
        return self.config.model_copy(update=update)

    def _workspace_checkout(self) -> Path | None:
        """The configured workspace when it is the root of a git checkout
        (the only case isolation, and the fetch refresh, apply to)."""
        source = self.config.sandbox.workspace
        if source is None or hostgit.find_git() is None:
            return None
        source = source.resolve()
        return source if hostgit.repo_toplevel(source) == source else None

    def _refresh_workspace(self) -> None:
        """Fetch + fast-forward the source checkout so the run's clone starts
        from current ``origin/<branch>`` (#255). Never fatal: a stale HEAD
        is still a run, a failed fetch (network blip, remote gone) is a
        warning in the chronology, not a failed issue."""
        if not self.config.daemon.refresh_workspace:
            return
        if self.config.daemon.workspace_isolation == "in-place":
            # In-place runs mutate the checkout directly; fast-forwarding
            # under a tree the previous run edited is not ours to do.
            return
        source = self._workspace_checkout()
        if source is None:
            return
        log.debug("workspace.refresh_start", path=str(source))
        started = time.monotonic()
        try:
            result = hostgit.refresh_from_origin(source)
        except ProvisionError as exc:
            self._notify(
                f"⚠ workspace refresh failed; running from local HEAD: {exc}",
                "workspace.refresh_failed",
                level="warning",
                path=str(source),
                error=str(exc),
                duration_s=round(time.monotonic() - started, 1),
            )
            return
        if result.advanced:
            self._notify(
                f"refreshed workspace: {result.message}",
                "workspace.refreshed",
                path=str(source),
                detail=result.message,
                duration_s=round(time.monotonic() - started, 1),
            )
        else:
            log.info(
                "workspace.refresh_unchanged",
                path=str(source),
                detail=result.message,
                duration_s=round(time.monotonic() - started, 1),
            )

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
        if item.kind == "audit":
            parts.append(AUDIT_INSTRUCTIONS)
        elif self.config.daemon.backlog != "off":
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

    def report_for(self, run_id: str) -> RunReport:
        """The report card of any run this daemon's store knows — the same
        mining the settle path does, for the concierge and other readers."""
        return self._report(run_id, None)

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
            # The report then claims no delivery — say so, or the settle
            # path's "delivery failed" verdict has no explanation.
            log.warning(
                "run.report_events_unreadable",
                run=run_id,
                hint="tracking issue / PR / delivery error unknown for this report",
                exc_info=True,
            )
        workspace = str(result.workspace) if result is not None and result.workspace else None
        return RunReport(run_id, state, summary, tracking, delivery, delivery_error, workspace)

    def _schedule_audits(self, now: float) -> None:
        """Open due charters from the checkout's ``audit_dir`` as audit issues.

        Runs after the pause/breaker/cap gates (a stressed daemon does not
        add work for itself) and before discovery, so a freshly filed audit
        is picked up in the same tick. GitHub is the schedule's source of
        truth (a still-open audit is never re-filed; one created within the
        interval counts as filed) with the store as a cache; a GitHub
        hiccup skips this tick, never raises."""
        daemon = self.config.daemon
        if not daemon.audits:
            return
        github: Any = next((s for s in self.sources if s.name == "github"), None)
        if github is None or not hasattr(github, "file_audit"):
            return
        checkout = self._workspace_checkout()
        if checkout is None:
            return
        # The checkout is normally refreshed only when a run starts, so
        # charters merged after the last run would never be seen (field:
        # the first deploy read a clone that predated its own charters).
        # Refresh here too, throttled — a fetch a minute is not the point.
        if now - self._last_audit_refresh >= AUDIT_REFRESH_S:
            self._last_audit_refresh = now
            self._refresh_workspace()
        charters, problems = load_charters(checkout, daemon.audit_dir)
        for problem in problems:
            if problem not in self._audit_problems_seen:
                self._audit_problems_seen.add(problem)
                self._notify(
                    charter_skipped_notice(problem, daemon.audit_dir),
                    "audit.charter_skipped",
                    level="warning",
                    audit_dir=str(daemon.audit_dir),
                    problem=problem,
                )
        for charter in due_charters(charters, self.dstore.audit_last_filed(), now):
            try:
                since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - charter.every_s))
                is_open, filed_recently = github.audit_issue_state(charter.title, since)
                if is_open or filed_recently:
                    # Someone (or a previous process) already filed it: sync
                    # the cache so we stop asking GitHub every tick.
                    log.info(
                        "audit.already_filed",
                        charter=charter.name,
                        is_open=is_open,
                        filed_recently=filed_recently,
                    )
                    self.dstore.record_audit(charter.name, "gh:existing", now)
                    continue
                ref = github.file_audit(
                    charter.title, issue_body(charter, audit_marker(charter.name))
                )
                self.dstore.record_audit(charter.name, ref, now)
                self._notify(
                    filed_notice(
                        "audit",
                        ref,
                        repo=self._repo,
                        target=f"charter {code(charter.name)}",
                        detail=charter.title,
                    ),
                    "audit.filed",
                    charter=charter.name,
                    ref=ref,
                    every_s=charter.every_s,
                )
            except Exception:
                log.warning("audit.file_failed", charter=charter.name, exc_info=True)

    def _collect_tool_findings(self, run_id: str, source: WorkSource) -> ToolFindings:
        """Findings addressed to the tool: filed upstream when tool_repo is
        set, otherwise noted for the closing comment. Never the project's."""
        if self.config.daemon.backlog == "off":
            return ToolFindings([], [])
        target = next((s for s in self.sources if s.name == self.config.daemon.backlog), None)
        if target is None:
            return ToolFindings([], [])
        try:
            return collect_tool_findings(
                self.store.get_run(run_id),
                dstore=self.dstore,
                source=target,
                max_items=self.config.daemon.backlog_max_per_run,
                now=self.clock(),
            )
        except SbxloopError:
            log.warning("backlog.tool_findings_failed", run=run_id, exc_info=True)
            return ToolFindings([], [])

    def _file_review(self, item: WorkItem, run_id: str, report: RunReport) -> None:
        """The loop evaluating the code it just wrote: after a patch item
        delivers a PR, open a review audit of that PR. Once per run, patch
        items only (an audit has no PR), capped per calendar day, best-effort."""
        daemon = self.config.daemon
        if not daemon.review_deliveries or item.kind != "patch" or report.delivery is None:
            return
        github: Any = next((s for s in self.sources if s.name == "github"), None)
        if github is None or not hasattr(github, "file_review"):
            return
        now = self.clock()
        try:
            if self.dstore.review_filed(run_id):
                return
            day_start, _ = day_window(now, daemon.run_cap_timezone)
            if self.dstore.reviews_since(day_start) >= daemon.reviews_per_day:
                log.info(
                    "review.skipped",
                    item=item.item_id,
                    run=run_id,
                    reason="calendar-day cap reached",
                    cap=daemon.reviews_per_day,
                )
                return
            number, url = report.delivery
            ref = github.file_review(item, number, url, run_id)
            self.dstore.record_review(run_id, number, ref, now)
            self._notify(
                filed_notice(
                    "review",
                    ref,
                    repo=self._repo,
                    target=f"PR {link(f'#{number}', url)} · {item.item_id}",
                ),
                "review.filed",
                item=item.item_id,
                run=run_id,
                pr=number,
                ref=ref,
            )
        except Exception:
            log.warning("review.file_failed", item=item.item_id, run=run_id, exc_info=True)

    def _file_postmortem(self, item: WorkItem, run_id: str, reason: str) -> None:
        """Turn the daemon's own failure into a discovery-lane charter.

        Only for patch items — a failed audit filing a post-mortem that is
        itself an audit would recurse — only once per run, and capped per
        calendar day so a bad night cannot flood the tracker. Best-effort:
        the item is already settled; this must never change that."""
        daemon = self.config.daemon
        if not daemon.postmortems or item.kind != "patch":
            return
        github: Any = next((s for s in self.sources if s.name == "github"), None)
        if github is None or not hasattr(github, "file_postmortem"):
            return
        now = self.clock()
        try:
            if self.dstore.postmortem_filed(run_id):
                return
            day_start, _ = day_window(now, daemon.run_cap_timezone)
            if self.dstore.postmortems_since(day_start) >= daemon.postmortems_per_day:
                log.info(
                    "postmortem.skipped",
                    item=item.item_id,
                    run=run_id,
                    reason="calendar-day cap reached",
                    cap=daemon.postmortems_per_day,
                )
                return
            run_ids = self.dstore.runs_for_item(item.item_id) or [run_id]
            dossier = build_dossier(
                self.store, item, run_ids, reason, state_dir=str(self.config.state_dir)
            )
            ref = github.file_postmortem(item, dossier, run_id)
            self.dstore.record_postmortem(run_id, item.item_id, ref, now)
            self._notify(
                filed_notice(
                    "post-mortem", ref, repo=self._repo, target=item.item_id, detail=reason
                ),
                "postmortem.filed",
                item=item.item_id,
                run=run_id,
                ref=ref,
                reason=reason,
                runs=len(run_ids),
            )
        except Exception:
            log.warning("postmortem.file_failed", item=item.item_id, run=run_id, exc_info=True)

    def _post_review(self, item: WorkItem, run_id: str) -> SubmittedReview | None:
        """Post this run's review to the PR it reviewed, when it is one.

        Returns None for every item that is not a review, which is what
        keeps the ordinary backlog lane untouched. Best-effort: the run has
        already succeeded, and a GitHub hiccup here must not turn that into
        a failure — the review is visibly absent on the PR either way, which
        is the honest signal.
        """
        target = self.dstore.review_target(item.item_id)
        if target is None:
            return None
        pr_number, origin_run_id = target
        github: Any = next((s for s in self.sources if s.name == "github"), None)
        if github is None or not hasattr(github, "post_review"):
            log.warning("review.no_github_source", item=item.item_id, pr=pr_number)
            return None
        try:
            posted = github.post_review(self.store.get_run(run_id), pr_number, origin_run_id)
        except Exception:
            log.warning(
                "review.post_failed", item=item.item_id, run=run_id, pr=pr_number, exc_info=True
            )
            return None
        if posted is None:
            return None
        verdict = "approved" if posted.event == "APPROVE" else "requested changes"
        gate = "" if posted.gates_merge else " (as a comment — it does not gate the merge)"
        self._notify(
            f"🔎 review {verdict} on PR #{pr_number}{gate}: {posted.url}",
            "review.posted",
            item=item.item_id,
            run=run_id,
            pr=pr_number,
            verdict=posted.event,
            gates_merge=posted.gates_merge,
        )
        assert isinstance(posted, SubmittedReview)
        return posted

    def _collect_backlog(self, run_id: str, source: WorkSource) -> list[str]:
        """File the run's backlog items; returns their refs (``gh:<n>``)."""
        mode = self.config.daemon.backlog
        if mode == "off":
            return []
        target = next((s for s in self.sources if s.name == mode), None)
        if target is None:
            # Startup already warned once (daemon.backlog_needs_github /
            # source list); per run this is only worth a debug trace.
            log.debug("backlog.no_target_source", run=run_id, backlog=mode)
            return []
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
            log.warning("backlog.collect_failed", run=run_id, backlog=mode, exc_info=True)
            return []
        if filed:
            log.info("backlog.filed", run=run_id, backlog=mode, count=len(filed), refs=list(filed))
        return list(filed)

    # -- recovery ------------------------------------------------------------------------

    def recover(self) -> None:
        """Reconcile items left ``running`` by a previous process.

        Finished runs are settled here; an interrupted run is only *queued
        for resume* — the actual resume happens in :meth:`tick`, behind the
        breaker / daily cap / pause gate and the per-item resume budget.
        Recovery used to dispatch resumes directly, so a daemon restarting
        into a bad state (breaker open, cap spent, operator-paused) resumed
        anyway (#254).

        Finishes with :meth:`_reconcile_orphan_runs`, which closes any run
        row a dead process left non-terminal (#374)."""
        for item in self.dstore.running_items():
            source = self._source_for(item)
            now = self.clock()
            if source is None:
                self.dstore.mark_failed(item.item_id, "no source on recovery", now, requeue=False)
                log.warning(
                    "recovery.item_abandoned",
                    item=item.item_id,
                    run=item.run_id,
                    source=item.source,
                    reason="no such source is active this start",
                )
                continue
            if item.run_id is None:
                self.dstore.mark_requeued_unstarted(item.item_id, now)
                self._notify(
                    f"recovery: {item.item_id} re-queued (claimed, never started)",
                    "recovery.requeued",
                    item=item.item_id,
                    reason="claimed, never started",
                )
                continue
            try:
                record = self.store.get_run(item.run_id)
            except SbxloopError:
                self.dstore.mark_requeued_unstarted(item.item_id, now)
                log.warning(
                    "recovery.requeued",
                    item=item.item_id,
                    run=item.run_id,
                    reason="run record missing; starting over",
                )
                continue
            if record.state == "completed":
                self._notify(
                    f"recovery: {item.run_id} had completed; settling {item.item_id}",
                    "recovery.settling",
                    item=item.item_id,
                    run=item.run_id,
                    state=record.state,
                )
                self._settle(item, source, item.run_id, self._result_from_record(item.run_id), None)
            elif record.state in TERMINAL_RUN_STATES:
                self._notify(
                    f"recovery: {item.run_id} ended {record.state}; applying failure path",
                    "recovery.settling",
                    item=item.item_id,
                    run=item.run_id,
                    state=record.state,
                )
                self._settle(
                    item, source, item.run_id, None, StateError(f"run ended {record.state}")
                )
            elif record.state in RESUMABLE_RUN_STATES:
                last = self.store.last_event_ts(item.run_id)
                self._notify(
                    f"recovery: {item.run_id} for {item.item_id} queued for resume "
                    f"(last activity {self.clock() - last:.0f}s ago)"
                    if last
                    else f"recovery: {item.run_id} for {item.item_id} queued for resume",
                    "recovery.resume_pending",
                    item=item.item_id,
                    run=item.run_id,
                    state=record.state,
                    idle_s=round(self.clock() - last) if last else None,
                )
                self.dstore.mark_resume_pending(item.item_id, now)
            else:
                self.dstore.mark_requeued_unstarted(item.item_id, now)
                log.warning(
                    "recovery.requeued",
                    item=item.item_id,
                    run=item.run_id,
                    state=record.state,
                    reason="run state neither terminal nor resumable; starting over",
                )
        self._settle_offline_overrides()
        self._reconcile_orphan_runs()

    def _reconcile_orphan_runs(self) -> None:
        """Force every *orphaned* non-terminal run to a terminal state (#374).

        The run row is only ever written by the in-process run loop, so a
        cancelled item or a dead process left phantom ``running`` /
        ``decomposing`` runs behind: ``list_runs`` disagreed with
        ``!sbx status`` and anything counting active runs was misled.

        Two kinds of run are deliberately left alone: the run genuinely
        executing in this process, and one queued for resume (item
        ``queued`` with the run still pinned — the ``mark_resume_pending``
        path above), which tick will pick up. Everything else is closed as
        ``cancelled`` (its item was cancelled) or ``failed`` (orphaned).
        Chronology is only ever appended to.
        """
        with self._current_lock:
            handle = self._current
        live_run_id = handle.run_id if handle is not None else None
        for record in self.store.non_terminal_runs():
            if record.run_id == live_run_id:
                continue
            item_id = self.dstore.item_for_run(record.run_id)
            item = self.dstore.get(item_id) if item_id else None
            if item is not None and item.state == "queued" and item.run_id == record.run_id:
                continue  # pending resume: tick owns it
            self._reconcile_run_record(record)

    def _reconcile_run_record(
        self, record: RunRecord, *, reason_override: str | None = None
    ) -> bool:
        """Close one non-terminal run, appending (never mutating) chronology.

        Shared by startup reconciliation and the tick-time staleness sweep.
        A cancelled work item wins over ``reason_override`` so operator
        attribution is preserved in the run's reason.
        """
        item_id = self.dstore.item_for_run(record.run_id)
        item = self.dstore.get(item_id) if item_id else None
        if item is not None and item.state == "cancelled":
            state: RunState = "cancelled"
            reason = "work item cancelled"
            if item.last_error:
                reason = f"work item cancelled: {item.last_error}"
        else:
            state = "failed"
            reason = reason_override or "orphaned: daemon restarted while run was in flight"
        try:
            self.store.reconcile_run(record.run_id, state, reason)
            self.store.append_event(
                Event.now(
                    "run.reconciled",
                    record.run_id,
                    state=state,
                    reason=reason,
                    previous_state=record.state,
                    item=item_id,
                )
            )
        except (SbxloopError, StateError) as exc:
            log.warning("recovery.run_reconcile_failed", run=record.run_id, error=str(exc))
            return False
        self._notify(
            f"recovery: orphaned run {record.run_id} {record.state} -> {state} ({reason})",
            "recovery.run_reconciled",
            run=record.run_id,
            item=item_id,
            state=state,
            previous=record.state,
            level="warning",
        )
        return True

    def _reconcile_stale_runs(self, now: float) -> None:
        """Liveness safety net (#374): with no run executing in this process,
        close non-terminal runs that have shown no activity for
        ``[daemon] run_stale_after_s``. Disabled when the threshold is 0."""
        threshold = self.config.daemon.run_stale_after_s
        if threshold <= 0:
            return
        with self._current_lock:
            if self._current is not None:
                return  # a run is genuinely in flight; nothing here is stale
        for record in self.store.non_terminal_runs():
            last_activity = max(record.updated_at, self.store.last_event_ts(record.run_id) or 0.0)
            idle = now - last_activity
            if idle <= threshold:
                continue
            item_id = self.dstore.item_for_run(record.run_id)
            item = self.dstore.get(item_id) if item_id else None
            if item is not None and item.state == "queued" and item.run_id == record.run_id:
                continue  # pending resume: tick owns it
            self._reconcile_run_record(
                record, reason_override=f"orphaned: stale, no activity for {int(idle)}s"
            )

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
                self._notify(
                    f"recovery: {item_id} abandoned offline; run {run_id} closed",
                    "recovery.offline_abandon",
                    item=item_id,
                    run=run_id,
                )
            elif item.state == "queued" and item.run_id != run_id:
                # Requeued (unpinned) offline: the run is dead and will not be
                # resumed — close its ledger and drop its sandboxes.
                self._close_dead_run(run_id, "requeued", now)
                self._notify(
                    f"recovery: {item_id} requeued offline; run {run_id} closed",
                    "recovery.offline_requeue",
                    item=item_id,
                    run=run_id,
                )
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
                self._notify(
                    f"recovery: removed stale sandbox {name} (and its secrets)",
                    "recovery.stale_sandbox_removed",
                    run=run_id,
                    sandbox=name,
                    role=role,
                )
            except SbxError:
                # No such sandbox — the common case — but a secret may
                # still linger from a rollback race; clearing it is cheap.
                log.debug("recovery.no_stale_sandbox", run=run_id, sandbox=name, role=role)
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

    @property
    def _repo(self) -> str | None:
        """The GitHub repo filed refs point into (``gh:12`` → a link)."""
        return self.config.github.repo

    def _notify(
        self, text: str, event: str = "daemon.notice", *, level: str = "info", **fields: Any
    ) -> None:
        """Narrate to the humans (Discord) *and* the journal: ``text`` is the
        prose the frontend shows; ``event`` and ``fields`` are the structured
        record the log keeps (``level`` picks its severity)."""
        getattr(log, level)(event, text=text, **fields)
        if self.frontend is not None:
            try:
                self.frontend.daemon_event(text)
            except Exception:
                log.warning("frontend.daemon_event_failed", notice=event, exc_info=True)

    def _frontend_finished(self, item: WorkItem, report: RunReport) -> None:
        if self.frontend is not None:
            try:
                self.frontend.run_finished(item, report)
            except Exception:
                log.warning(
                    "frontend.run_finished_failed",
                    item=item.item_id,
                    run=report.run_id,
                    exc_info=True,
                )
