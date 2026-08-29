"""DaemonLoop: discover → claim → run → settle, forever.

One item at a time, one fresh :class:`LoopEngine` per item (engines are
single-use: their cancel flag never clears), one shared daemon-owned
:class:`StateStore`, a fresh :class:`EventBus` per run (each engine adds
permanent subscribers to its bus). The engine carries the item all the way
— task graph, gate, pull request, its own review, fix rounds, CI, merge —
so the daemon's whole job is to hand it an issue and settle on how the run
ended: ``merged`` closes the issue, ``failed`` retries or gives up,
``blocked`` hands the PR to a human. The daemon never files work of its
own.

Spend guardrails — a calendar-day run cap that counts runs started since
00:00 in ``daemon.run_cap_timezone`` (default ``UTC``) and resets at the
next midnight there; a per-item attempt cap; a consecutive-failure circuit
breaker — are the daemon's only defense against a mislabeled issue in a
fully autonomous setup, so they are enforced in the tick, not left to
configuration hope.

Shutdown is cooperative: a signal sets the stop flag, asks the in-flight
engine to cancel (honored at its next boundary), and joins it briefly.
Interrupted runs are resumable by design, so the item stays ``running``;
:meth:`recover` re-queues it with the run pinned on the next start and the
tick resumes it through the same guardrails as any dispatch.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, NamedTuple, Protocol
from zoneinfo import ZoneInfo

from sbxloop import hostgit
from sbxloop.config import Config, GithubConfig, SandboxConfig
from sbxloop.daemon.logsink import event_log_subscriber
from sbxloop.daemon.model import (
    DaemonNotice,
    NoticeKind,
    NoticeLevel,
    RunReport,
    TickOutcome,
    TickResult,
    WorkItem,
)
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
from sbxloop.events import Event, EventBus
from sbxloop.gc import DAY_S, format_bytes, prune_run_dirs
from sbxloop.ghids import normalize_item_id, try_parse_gh_id
from sbxloop.ids import new_run_id
from sbxloop.log import bind_run, clear_run, get_logger
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import sandbox_name
from sbxloop.sbx.prune import remove_run_sandbox, remove_run_sandbox_secrets

log = get_logger(__name__)

# Hidden markers earlier sbxloop versions left in issue bodies; they are
# bookkeeping, not part of the outcome the agent should read.
_MARKER_RE = re.compile(r"<!--\s*sbxloop-\S+.*?-->", re.DOTALL)


def day_window(now: float, tz: str) -> tuple[float, float]:
    """The calendar day containing epoch ``now`` in IANA zone ``tz``, as
    ``(start_epoch, next_start_epoch)``.

    Every instant in the same local calendar date maps to the same
    ``start_epoch``, and the count only resets when local midnight passes.
    ``next_start_epoch`` is the next local midnight (which is not always
    86400s later — DST days are 23 or 25 hours long)."""
    zone = ZoneInfo(tz)
    local = datetime.fromtimestamp(now, tz=zone)
    start = datetime.combine(local.date(), dtime(0, 0), tzinfo=zone)
    next_start = datetime.combine(local.date() + timedelta(days=1), dtime(0, 0), tzinfo=zone)
    return start.timestamp(), next_start.timestamp()


class Frontend(Protocol):
    """What a human-facing channel (Discord) sees of the loop's lifecycle.
    Every call is best-effort: the loop never depends on a frontend."""

    def daemon_notice(self, notice: DaemonNotice) -> None: ...
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
        source: WorkSource,
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
        self.source = source
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
        self._last_idle_kind: str | None = None
        # Poll backoff: consecutive failures and the earliest next poll, so a
        # source that is down (GitHub outage, dead github sandbox) is not
        # hammered every tick.
        self._source_failures = 0
        self._source_next_poll = 0.0
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
        item_id = normalize_item_id(item_id)
        why = reason or "abandoned by operator"
        now = self.clock()
        fresh = self.dstore.abandon(item_id, why, now)
        if self._cancel_if_current(item_id):
            self._notice(
                "item.abandon_cancelling",
                f"abandoning {item_id}: cancelling its run {fresh.run_id}",
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
        """Put a settled (failed/blocked/cancelled) item back in the queue
        with a clean slate at a human's request — fresh plan, not a resume —
        and tell the source who did it (re-claim, drop the failed label)."""
        item_id = normalize_item_id(item_id)
        who = by or "operator"
        fresh = self.dstore.retry(item_id, self.clock(), f"re-queued by {who}")
        log.info("item.retry", item=item_id, by=who)
        self._deliver_report(fresh, by=who)
        return fresh

    def requeue_item(self, item_id: str) -> WorkItem:
        """Drop an item's pinned run so its next dispatch starts fresh
        (attempts intact). Cancels the run if it is the one in flight."""
        item_id = normalize_item_id(item_id)
        before = self.dstore.get(item_id)
        pinned = before.run_id if before is not None else None
        now = self.clock()
        fresh = self.dstore.requeue(item_id, now)
        if self._cancel_if_current(item_id):
            self._notice(
                "item.requeue_cancelling",
                f"requeue: {item_id} — cancelling its run {pinned}",
                item=item_id,
                run=pinned,
            )
        elif pinned is not None:
            self._close_dead_run(pinned, "requeued", now)
            self._notice(
                "item.requeue_unpinned",
                f"requeue: {item_id} unpinned from {pinned}",
                item=item_id,
                run=pinned,
            )
        else:
            self._notice("item.requeued", f"requeue: {item_id} re-queued", item=item_id)
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
        """Tell the source about decisions it has not heard: an operator's
        ``sbxloop daemon abandon|retry`` from another process can only flip
        the row, and a run's merged/blocked report may have failed on a
        GitHub hiccup. Runs at the top of every tick and after recovery; the
        in-flight item is not here (its settle path delivers, once the run
        is really down)."""
        for item in self.dstore.pending_reports():
            self._deliver_report(item)

    def _deliver_report(self, item: WorkItem, *, by: str | None = None) -> None:
        """Deliver ``item.pending_report`` to the source, exactly once.

        Operator decisions (abandon/requeue) take the debt atomically first,
        so a Discord command on the bridge thread and the tick sweep on the
        loop thread cannot both report. A run outcome (merged/blocked) takes
        it only once the source confirms every step landed — an issue close
        that did not happen must be retried, not recorded.
        """
        kind = item.pending_report
        if kind is None:
            return
        if kind in ("merged", "blocked"):
            if self._report_outcome(item, kind):
                self.dstore.take_pending_report(item.item_id)
            return
        if not self.dstore.take_pending_report(item.item_id):
            return
        if kind == "abandoned":
            why = item.last_error or "abandoned by operator"
            self.source.report_abandoned(item, why)
            self._notice(
                "item.abandoned",
                f"❌ {item.item_id} abandoned by operator: {why}",
                item=item.item_id,
                run=item.run_id,
                reason=why,
            )
        else:
            # A row-only retry records who asked as its last_error.
            who = by or (item.last_error or "").removeprefix("re-queued by ") or "operator"
            self.source.report_requeued(item, who)
            self._notice(
                "item.requeued",
                f"↻ {item.item_id} re-queued by {who} (attempts reset)",
                item=item.item_id,
                by=who,
            )

    def _report_outcome(self, item: WorkItem, kind: str) -> bool:
        """Pay a merged/blocked report; True when the source confirmed it."""
        pr_number: int | None = None
        pr_url = ""
        if item.run_id is not None:
            try:
                record = self.store.get_run(item.run_id)
                pr_number, pr_url = record.pr_number, record.pr_url or ""
            except (SbxloopError, StateError):
                log.debug("report.no_run_record", item=item.item_id, run=item.run_id)
        if kind == "merged":
            delivered = self.source.report_merged(item, pr_number, pr_url)
        else:
            reason = item.last_error or "GitHub would not let the loop finish the pull request"
            delivered = self.source.report_blocked(item, reason, pr_number, pr_url)
        if not delivered:
            log.warning(
                "report.deferred",
                item=item.item_id,
                kind=kind,
                hint="the source did not confirm; retried on the next tick",
            )
        return delivered

    def _cancel_if_current(self, item_id: str) -> bool:
        with self._current_lock:
            handle = self._current
        if handle is None or handle.item.item_id != normalize_item_id(item_id):
            return False
        handle.engine.request_cancel()
        return True

    def _operator_override(self, item_id: str, run_id: str) -> WorkItem | None:
        """The item row is the daemon's only channel from a CLI running in
        another process: an item that is no longer ``running`` on *this*
        run was abandoned/requeued by an operator. Returns the fresh row
        when so."""
        fresh = self.dstore.get(normalize_item_id(item_id))
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
        self._notice(
            "daemon.started",
            "daemon started",
            poll_interval_s=self.config.daemon.poll_interval_s,
            source=self.source.name,
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
            self._notice("daemon.stopped", "daemon stopped", ticks=ticks)

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
        # Before the gates: a decision made from another process — or a
        # merged/blocked report GitHub refused last time — reaches the source
        # even while paused or with the breaker open.
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
                self._notice(
                    "daemon.daily_cap",
                    f"run cap reached for today ({tz}): {started_today}/{cap}; "
                    f"resets at 00:00 {tz}",
                    started_today=started_today,
                    cap=cap,
                    timezone=tz,
                    resets_at=day_end,
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
        if not item.claimed:
            if not self.source.claim(item):
                self.dstore.mark_failed(item.item_id, "claim failed", now, requeue=False)
                self._notice(
                    "item.claim_failed",
                    f"could not claim {item.item_id} ({item.title}); dropped",
                    item=item.item_id,
                    title=item.title,
                )
                return TickResult(discovered=discovered, dispatched=item.item_id, outcome="failed")
            self.dstore.mark_claimed(item.item_id, now)
            log.info("item.claimed", item=item.item_id, title=item.title)
        if item.run_id is not None:
            outcome = self._resume(item, now)
        else:
            outcome = self._dispatch(item, resume_run_id=None)
        return TickResult(discovered=discovered, dispatched=item.item_id, outcome=outcome)

    def _resume(self, item: WorkItem, now: float) -> TickOutcome:
        """Resume the run recovery pinned on a queued item — or, past the
        per-item resume budget, settle that run as a failed attempt so a
        plan that keeps getting interrupted cannot burn engine wall clock
        forever (#234)."""
        run_id = item.run_id
        assert run_id is not None
        resumes = self.dstore.resumes_for_item(item.item_id)
        budget = self.config.daemon.max_resumes_per_item
        if resumes >= budget:
            self._notice(
                "run.resume_budget_exhausted",
                f"{item.item_id}: {run_id} interrupted again after {resumes} resume(s) "
                f"(budget {budget}); settling as a failed attempt",
                item=item.item_id,
                run=run_id,
                resumes=resumes,
                budget=budget,
            )
            return self._settle(
                item,
                run_id,
                None,
                StateError(f"run {run_id} interrupted; resume budget ({budget}) exhausted"),
            )
        # A dead process leaves its microVMs alive; resume re-provisions
        # under the same names and `sbx create` refuses a name that exists
        # (field: SIGKILL mid-run → 'sandbox already exists' on the very
        # next start).
        self._remove_stale_run_sandboxes(run_id)
        self._notice(
            "run.resuming",
            f"resuming {run_id} for {item.item_id} (resume {resumes + 1}/{budget})",
            item=item.item_id,
            run=run_id,
            resume=resumes + 1,
            budget=budget,
        )
        return self._dispatch(item, resume_run_id=run_id)

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
        self._notice(
            "daemon.gc",
            text,
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
        source = self.source
        if now < self._source_next_poll:
            log.debug(
                "source.poll_skipped",
                source=source.name,
                backoff_left_s=self._source_next_poll - now,
            )
            return 0
        started = time.monotonic()
        try:
            found = source.poll()
        except Exception:
            self._source_failures += 1
            delay = min(
                self.config.daemon.poll_interval_s * 2**self._source_failures,
                self.SOURCE_BACKOFF_MAX_S,
            )
            self._source_next_poll = now + delay
            log.warning(
                "source.poll_failed",
                source=source.name,
                failures=self._source_failures,
                next_poll_in_s=round(delay),
                duration_s=round(time.monotonic() - started, 2),
                exc_info=True,
            )
            return 0
        if self._source_failures:
            after = self._source_failures
            self._source_failures = 0
            self._source_next_poll = 0.0
            self._notice(
                "source.poll_recovered",
                f"source {source.name} polling recovered",
                after_failures=after,
            )
        fresh = 0
        for item in found:
            if self.dstore.upsert_new(item, now):
                fresh += 1
                self._notice(
                    "item.queued",
                    f"queued {item.item_id}: {item.title}",
                    item=item.item_id,
                    url=item.url or None,
                    title=item.title,
                )
        log.debug(
            "source.polled",
            source=source.name,
            found=len(found),
            new=fresh,
            duration_s=round(time.monotonic() - started, 2),
        )
        return fresh

    # -- dispatch ----------------------------------------------------------------------

    def _dispatch(self, item: WorkItem, *, resume_run_id: str | None) -> TickOutcome:
        """Run one item (fresh, or resuming its interrupted run) and settle it."""
        now = self.clock()
        run_id = resume_run_id or new_run_id()
        started = time.monotonic()
        if resume_run_id is None:
            self.dstore.mark_running(item.item_id, run_id, now)
            item = self.dstore.get(item.item_id) or item
            self.source.report_started(item, run_id)
            # Fresh runs only: a resumed run is pinned to the clone it
            # already has, so moving the source would change nothing.
            self._refresh_workspace(self._item_repo(item))
        else:
            self.dstore.mark_resuming(item.item_id, run_id, now)
            item = self.dstore.get(item.item_id) or item
        log.info(
            "run.dispatch",
            item=item.item_id,
            run=run_id,
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
            bind_run(run_id, item.item_id, source=self.source.name)
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
            return self._settle_override(item, run_id, override, result_box.get("result"))
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
            return self._settle_cancelled(item, run_id, cancel)
        if self._stop.is_set() and "result" not in result_box and self._run_is_resumable(run_id):
            # Interrupted by shutdown at a boundary: the persisted run is
            # still resumable, so leave the item running for recovery. A run
            # that actually FAILED after stop was requested has a terminal
            # persisted state and settles like any failure below — shutdown
            # must not mask genuine errors as "interrupted".
            self.dstore.finish_ledger(run_id, "interrupted", self.clock())
            log.warning(
                "run.interrupted",
                item=item.item_id,
                run=run_id,
                resumable=True,
                hint="shutdown at a boundary; recovery queues it for resume",
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
        return self._settle(item, run_id, result_box.get("result"), error)

    def _close_run_record(self, run_id: str, reason: str) -> None:
        """Terminate the *run* row for a run this settle just ended for good.

        ``finish_ledger`` closes the daemon's own ledger, but the engine's
        ``runs`` row is written only by the in-process run loop — so a run
        that died inside a phase (a decompose the verify lint rejected, say)
        left ``decomposing`` behind it. Recovery's stale sweep does close it
        eventually, after ``run_stale_after_s``: six hours in the field
        (runs rv2y1a8ke and rq826h546 of item gh:issue:478), and until then
        ``list_runs`` and everything counting active runs disagreed with
        reality — the very mismatch #374 exists to prevent, on a path its
        sweep only reaches by timeout.

        Nothing here is resumable. A requeued item drops its run pin (see
        ``DaemonStore.mark_failed``: queued + ``run_id`` means "resume this
        run"), and a failed item is terminal. Best-effort — the item is
        already settled and no bookkeeping failure may unsettle it.
        """
        try:
            record = self.store.get_run(run_id)
            if record.state in TERMINAL_RUN_STATES:
                return
            self.store.reconcile_run(run_id, "failed", reason)
            self.store.append_event(
                Event.now(
                    "run.reconciled",
                    run_id,
                    state="failed",
                    reason=reason,
                    previous_state=record.state,
                )
            )
            log.info("run.record_closed", run=run_id, previous_state=record.state, reason=reason)
        except (SbxloopError, StateError) as exc:
            log.warning("run.record_close_failed", run=run_id, error=str(exc))

    def _settle_override(
        self,
        item: WorkItem,
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
        if fresh.state == "failed":
            self._end_run_cancelled(
                run_id, f"cancelled: {item.item_id} abandoned by operator", source="abandon"
            )
            self.dstore.finish_ledger(run_id, "abandoned", now)
            self._deliver_report(fresh)
            self._frontend_finished(item, report)
            return "failed"
        self._end_run_cancelled(
            run_id, f"cancelled: {item.item_id} requeued by operator", source="requeue"
        )
        self.dstore.finish_ledger(run_id, "requeued", now)
        self._frontend_finished(item, report)
        self._notice(
            "run.requeued",
            f"{item.item_id} requeued by operator; run {run_id} ended {report.state}",
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
        run_id: str,
        result: RunResult | None,
        error: BaseException | None,
    ) -> TickOutcome:
        """Turn how the run ended into what happens to the item.

        ``merged`` is done: the issue closes. ``blocked`` is handed over: the
        run cleared its own bar and GitHub refused, which no further attempt
        would change, so it neither retries nor counts toward the breaker.
        Everything else — a failed run, an error, a run that somehow ended
        ``completed`` with a repository to deliver to — is a failed attempt:
        retried with backoff while the item has attempts left, then given up.
        """
        now = self.clock()
        report = self._report(run_id, result)
        state = result.state if result is not None else None
        # Without a repository there is nothing to land: the engine ends
        # `completed` after its gate, and that is the whole job.
        landed = state == "merged" or (state == "completed" and not self.config.github.enabled)
        if landed:
            self.dstore.finish_ledger(run_id, "done", now)
            if self._consecutive_failures:
                log.info("breaker.reset", after_failures=self._consecutive_failures)
            self._set_breaker(None, 0)
            self.dstore.mark_done(item.item_id, now, pending_report="merged")
            fresh = self.dstore.get(item.item_id) or item
            self._deliver_report(fresh)
            self._frontend_finished(item, report)
            pr_text = f" · PR {report.pr[1]}" if report.pr and report.pr[1] else ""
            self._notice(
                "run.done",
                f"🎉 {item.item_id} merged ({report.task_summary}){pr_text}",
                item=item.item_id,
                run=run_id,
                url=report.pr[1] if report.pr else None,
                tasks=report.task_summary,
                pr=report.pr[0] if report.pr else None,
                rounds=report.rounds,
                attempt=item.attempts,
            )
            return "done"
        if state == "blocked" or (state == "completed" and self.config.github.enabled):
            reason = (
                (result.reason if result is not None else None)
                or ("run ended completed without landing" if state == "completed" else None)
                or "GitHub would not let the loop finish the pull request"
            )
            self.dstore.finish_ledger(run_id, "blocked", now)
            self.dstore.mark_blocked(item.item_id, reason, now)
            fresh = self.dstore.get(item.item_id) or item
            self._deliver_report(fresh)
            self._frontend_finished(item, report._replace(reason=reason))
            pr_text = f" · PR {report.pr[1]}" if report.pr and report.pr[1] else ""
            self._notice(
                "run.blocked",
                f"🚧 {item.item_id} blocked: {reason}{pr_text} — a human needs to look",
                level="error",
                item=item.item_id,
                run=run_id,
                url=report.pr[1] if report.pr else None,
                reason=reason,
                pr=report.pr[0] if report.pr else None,
            )
            return "blocked"
        reason = str(error) if error is not None else (report.reason or f"run ended {report.state}")
        attempts_left = self.config.daemon.max_attempts_per_item - item.attempts
        self._set_breaker(self._breaker_opened_at, self._consecutive_failures + 1)
        self.dstore.finish_ledger(run_id, "failed", now)
        self._close_run_record(run_id, reason)
        if attempts_left > 0:
            self.dstore.mark_failed(item.item_id, reason, now, requeue=True)
            self.source.report_retry(item, reason, attempts_left)
            self._notice(
                "run.failed",
                f"❌ {item.item_id} failed ({reason}); {attempts_left} attempt(s) left",
                level="warning",
                item=item.item_id,
                run=run_id,
                url=report.pr[1] if report.pr else None,
                reason=reason,
                attempt=item.attempts,
                attempts_left=attempts_left,
                retry_backoff_s=self.config.daemon.retry_backoff_s,
                consecutive_failures=self._consecutive_failures,
            )
            outcome: TickOutcome = "retry"
        else:
            self.dstore.mark_failed(item.item_id, reason, now, requeue=False)
            self.source.report_abandoned(item, reason)
            self._notice(
                "run.abandoned",
                f"❌ {item.item_id} abandoned after {item.attempts} attempt(s): {reason}",
                level="error",
                item=item.item_id,
                run=run_id,
                url=report.pr[1] if report.pr else None,
                reason=reason,
                attempts=item.attempts,
                consecutive_failures=self._consecutive_failures,
            )
            outcome = "failed"
        self._frontend_finished(item, report)
        if self._consecutive_failures >= self.config.daemon.max_consecutive_failures:
            self._set_breaker(now, self._consecutive_failures)
            self._notice(
                "breaker.opened",
                f"🛑 circuit breaker opened after {self._consecutive_failures} consecutive "
                f"failures; pausing dispatch for {self.config.daemon.breaker_cooldown_s:.0f}s",
                level="error",
                consecutive_failures=self._consecutive_failures,
                cooldown_s=self.config.daemon.breaker_cooldown_s,
            )
        return outcome

    def _settle_cancelled(self, item: WorkItem, run_id: str, cancel: CancelRequest) -> TickOutcome:
        now = self.clock()
        report = self._report(run_id, None)._replace(
            state="cancelled", cancelled_by=cancel.requester, requeued=cancel.retry
        )
        reason = f"cancelled by {cancel.requester}" + (" (retry)" if cancel.retry else "")
        # The engine state store (state.db) and the daemon store are separate
        # connections, so the run row and the item row cannot share one
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
            self._notice(
                "run.cancelled",
                f"⏹ {item.item_id} {reason}; re-queued to run again fresh",
                item=item.item_id,
                run=run_id,
                by=cancel.requester,
                requeued=True,
            )
        else:
            self._notice(
                "run.cancelled",
                f"⏹ {item.item_id} {reason} — `sbxloop resume {run_id}` continues it, "
                f"`!sbx retry {item.item_id}` reruns it fresh",
                item=item.item_id,
                run=run_id,
                by=cancel.requester,
                requeued=False,
            )
        self.source.report_cancelled(item, report)
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
            self._notice(
                "breaker.half_open",
                "circuit breaker half-open; allowing one item",
                consecutive_failures=self._consecutive_failures,
            )
            return False
        return True

    # -- item -> run mapping ---------------------------------------------------------

    def _item_repo(self, item: WorkItem) -> str | None:
        """The ``owner/name`` this item's run must act on.

        The item carries it since multi-repo discovery; rows written before
        that (and legacy ``gh:<n>`` ids) carry none, and fall back to the
        configured default — the single-repo behaviour, unchanged.
        """
        repo = item.repo
        if repo is None:
            parsed = try_parse_gh_id(item.item_id)
            repo = parsed.repo if parsed is not None else None
        if repo is not None and self.config.github.find_repo(repo) is None:
            log.warning("run.unknown_item_repo", item=item.item_id, repo=repo)
            return None
        return repo

    def _item_config(self, item: WorkItem) -> Config:
        # Narrow the section to the item's repository first, so the run's
        # per-repo deliver_base / token_env win over the global defaults.
        item_repo = self._item_repo(item)
        gh = GithubConfig.model_validate(
            {
                **self.config.github.for_repo(
                    item_repo, workspace=self.config.workspace_for_repo(item_repo)
                ).model_dump(),
                "create_repo": False,
                # "Closes #N" in the PR body: GitHub links issue and PR and
                # closes the issue on merge even when the daemon is not
                # running to do it.
                "deliver_closes": int(item.source_key) if item.source_key.isdigit() else None,
            }
        )
        update: dict[str, Any] = {"github": gh, "keep_on_failure": False}
        sandbox = self.config.sandbox
        if (
            self._workspace_checkout(item_repo) is not None
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

    def _workspace_checkout(self, repo: str | None = None) -> Path | None:
        """``repo``'s configured workspace when it is the root of a git
        checkout (the only case isolation, and the fetch refresh, apply to).

        Resolved per repository (:meth:`Config.workspace_for_repo`), never
        from the daemon-wide path: with several repositories configured one
        ``[sandbox] workspace`` would otherwise stand in for every repo's
        tree (#526).
        """
        source = self.config.workspace_for_repo(repo)
        if source is None or hostgit.find_git() is None:
            return None
        source = source.resolve()
        return source if hostgit.repo_toplevel(source) == source else None

    def _refresh_workspace(self, repo: str | None = None) -> None:
        """Fetch + fast-forward *the claimed repo's* source checkout so the
        run's clone starts from current ``origin/<branch>`` (#255). Never
        fatal: a stale HEAD is still a run, a failed fetch (network blip,
        remote gone) is a warning in the chronology, not a failed issue.

        A repo with no workspace of its own is skipped with a log line, and
        a checkout whose ``origin`` names a different repository is refused
        rather than fast-forwarded (#526).
        """
        if not self.config.daemon.refresh_workspace:
            return
        if self.config.daemon.workspace_isolation == "in-place":
            # In-place runs mutate the checkout directly; fast-forwarding
            # under a tree the previous run edited is not ours to do.
            return
        source = self._workspace_checkout(repo)
        if source is None:
            log.info(
                "workspace.refresh_skipped",
                repo=repo,
                reason="no git checkout resolved for this repository",
            )
            return
        if repo is not None and hostgit.origin_matches_repo(source, repo) is False:
            actual = hostgit.normalise_repo_url(hostgit.origin_url(source))
            log.warning(
                "workspace.refresh_refused",
                repo=repo,
                path=str(source),
                origin=actual,
                reason=(
                    f"{source} is a checkout of {actual}, not {repo}; refusing to "
                    "refresh another repository's tree"
                ),
            )
            return
        log.debug("workspace.refresh_start", path=str(source))
        started = time.monotonic()
        try:
            result = hostgit.refresh_from_origin(source)
        except ProvisionError as exc:
            self._notice(
                "workspace.refresh_failed",
                f"⚠ workspace refresh failed; running from local HEAD: {exc}",
                level="warning",
                path=str(source),
                error=str(exc),
                duration_s=round(time.monotonic() - started, 1),
            )
            return
        if result.advanced:
            self._notice(
                "workspace.refreshed",
                f"refreshed workspace: {result.message}",
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
        """The issue as the decomposer reads it: title, body, provenance.
        Nothing else — the run has no lanes to be told about."""
        parts = [item.title.strip()]
        body = _MARKER_RE.sub("", item.body).strip()
        if body:
            parts.append(body)
        origin = (
            f"GitHub issue #{item.source_key} in {self._item_repo(item) or self.config.github.repo}"
        )
        if item.url:
            origin += f" ({item.url})"
        parts.append(f"---\nThis work item came from: {origin}.")
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
        return engine.start(self.outcome_text(item), run_id=run_id, repo=self._item_repo(item))

    # -- reporting -----------------------------------------------------------------------

    def report_for(self, run_id: str) -> RunReport:
        """The report card of any run this daemon's store knows — what the
        settle path reads, for the concierge and other readers."""
        return self._report(run_id, None)

    def _report(self, run_id: str, result: RunResult | None) -> RunReport:
        record: RunRecord | None
        try:
            record = self.store.get_run(run_id)
            state = record.state
            tasks = self.store.get_tasks(run_id)
        except SbxloopError:
            record = None
            state = result.state if result is not None else "failed"
            tasks = result.tasks if result is not None else []
        done = sum(1 for t in tasks if t.state == "done")
        summary = f"{done}/{len(tasks)} tasks done" if tasks else "no tasks ran"
        pr: tuple[int, str] | None = None
        branch = None
        rounds = 0
        reason = result.reason if result is not None else None
        if record is not None:
            if record.pr_number is not None:
                pr = (record.pr_number, record.pr_url or "")
            branch = record.branch
            rounds = record.review_rounds + record.ci_rounds
            reason = reason or record.reason
        elif result is not None and result.pr_number is not None:
            pr = (result.pr_number, result.pr_url or "")
        workspace = (
            str(result.workspace)
            if result is not None and result.workspace
            else (str(record.workspace) if record is not None and record.workspace else None)
        )
        return RunReport(
            run_id,
            state,
            summary,
            pr=pr,
            branch=branch,
            rounds=rounds,
            reason=reason,
            workspace=workspace,
        )

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
            now = self.clock()
            if item.run_id is None:
                self.dstore.mark_requeued_unstarted(item.item_id, now)
                self._notice(
                    "recovery.requeued",
                    f"recovery: {item.item_id} re-queued (claimed, never started)",
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
            if record.state in ("merged", "blocked", "completed"):
                self._notice(
                    "recovery.settling",
                    f"recovery: {item.run_id} had ended {record.state}; settling {item.item_id}",
                    item=item.item_id,
                    run=item.run_id,
                    state=record.state,
                )
                self._settle(item, item.run_id, self._result_from_record(item.run_id), None)
            elif record.state in TERMINAL_RUN_STATES:
                self._notice(
                    "recovery.settling",
                    f"recovery: {item.run_id} ended {record.state}; applying failure path",
                    item=item.item_id,
                    run=item.run_id,
                    state=record.state,
                )
                self._settle(item, item.run_id, None, StateError(f"run ended {record.state}"))
            elif record.state in RESUMABLE_RUN_STATES:
                last = self.store.last_event_ts(item.run_id)
                self._notice(
                    "recovery.resume_pending",
                    f"recovery: {item.run_id} for {item.item_id} queued for resume "
                    f"(last activity {self.clock() - last:.0f}s ago)"
                    if last
                    else f"recovery: {item.run_id} for {item.item_id} queued for resume",
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
        self._notice(
            "recovery.run_reconciled",
            f"recovery: orphaned run {record.run_id} {record.state} -> {state} ({reason})",
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
        the issue exactly once, on the next daemon start. The source report
        itself is the row's ``pending_report`` debt, paid by
        :meth:`_deliver_pending_reports` right after (and by every tick —
        an abandoned-while-queued item has no run to find here)."""
        for run_id, item_id in self.dstore.unsettled_runs():
            item = self.dstore.get(item_id)
            if item is None:
                continue
            now = self.clock()
            if item.state == "failed" and item.run_id == run_id:
                self._close_dead_run(run_id, "abandoned", now)
                self._notice(
                    "recovery.offline_abandon",
                    f"recovery: {item_id} abandoned offline; run {run_id} closed",
                    item=item_id,
                    run=run_id,
                )
            elif item.state == "queued" and item.run_id != run_id:
                # Requeued (unpinned) offline: the run is dead and will not be
                # resumed — close its ledger and drop its sandboxes.
                self._close_dead_run(run_id, "requeued", now)
                self._notice(
                    "recovery.offline_requeue",
                    f"recovery: {item_id} requeued offline; run {run_id} closed",
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
                self._notice(
                    "recovery.stale_sandbox_removed",
                    f"recovery: removed stale sandbox {name} (and its secrets)",
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
            pr_number=record.pr_number,
            pr_url=record.pr_url,
            reason=record.reason,
        )

    # -- helpers ------------------------------------------------------------------------

    def _notice(
        self,
        kind: NoticeKind,
        text: str,
        *,
        item: str | None = None,
        run: str | None = None,
        url: str | None = None,
        level: NoticeLevel = "info",
        **fields: Any,
    ) -> None:
        """Narrate to the humans (Discord) *and* the journal: ``text`` is the
        prose the frontend shows, routed by ``item``/``run``; ``kind`` and
        ``fields`` are the structured record the log keeps (``level`` picks
        its severity)."""
        getattr(log, level)(kind, text=text, item=item, run=run, **fields)
        if self.frontend is not None:
            try:
                self.frontend.daemon_notice(
                    DaemonNotice(kind, text, item_id=item, run_id=run, url=url, level=level)
                )
            except Exception:
                log.warning("frontend.daemon_notice_failed", notice=kind, exc_info=True)

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
