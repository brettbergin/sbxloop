"""From the public chronology to a ping on a person's device.

**Live only.** The dispatcher keeps its cursor in memory and starts it at
the chronology's head (:meth:`PushDispatcher.prime`): an event recorded
before it started — history, anything a restart would replay, whatever
happened while the daemon was down — is never pushed. A ping is only worth
sending while it is news; a durable cursor would turn every restart into a
burst of stale alerts on a lock screen. An event older than
:data:`STALE_S` when it is read is skipped for the same reason, and one
read while push is switched off only moves the cursor.

For each event the rules (:mod:`sbxloop.api.push.rules`) name who it is
news for; each of that person's devices whose preferences let it through
gets a job. The notification's text is stored once per person (the device
fetches it by ref) and the relay is handed only references. Jobs are sent
off every request path, on the dispatcher's own thread, with exponential
backoff: a 502 the relay does not call final, a 429 (after at least its
``Retry-After``) and a transport error are retried up to ``[push]
max_attempts``; a 410 forgets the device; any 400 drops the push (an
``invalid_handle`` also un-enrolls the device, so its next registration
enrolls it again). Nothing logged names a token or a handle — a device is
logged by id.
"""

from __future__ import annotations

import heapq
import itertools
import json
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from sbxloop.api.push.relay import RelayClient
from sbxloop.api.push.rules import TYPES, Event, Notice, NoticeRules, allowed
from sbxloop.api.push.store import DeviceStore, DeviceTarget
from sbxloop.config import Config
from sbxloop.daemon.store import DaemonStore
from sbxloop.db.api_models import ApiEventRow
from sbxloop.log import get_logger

log = get_logger(__name__)

#: How often the thread looks at the chronology when nothing wakes it.
POLL_S = 1.0
#: Events read per pass.
BATCH = 500
#: An event older than this when read is no longer news.
STALE_S = 900.0
#: How often stored notifications are pruned.
PRUNE_EVERY_S = 3600.0
#: How long one person is spared a second ping for the same gate.
DEDUPE_S = 3600.0
DEDUPE_LIMIT = 4096


@dataclass(order=True)
class _Job:
    due: float
    order: int
    device_id: str = field(compare=False)
    kind: str = field(compare=False)
    payload: dict[str, str] = field(compare=False)
    attempts: int = field(default=0, compare=False)


class PushDispatcher:
    def __init__(
        self,
        dstore: DaemonStore,
        devices: DeviceStore,
        rules: NoticeRules,
        *,
        config: Callable[[], Config],
        relay: Callable[[], RelayClient],
        clock: Callable[[], float],
        poll_s: float = POLL_S,
    ) -> None:
        self.dstore = dstore
        self.devices = devices
        self.rules = rules
        self.config = config
        self.relay = relay
        self.clock = clock
        self.poll_s = poll_s
        self._cursor: int | None = None
        self._jobs: list[_Job] = []
        self._order = itertools.count()
        self._lock = threading.Lock()
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_prune = 0.0

    # -- the cursor ----------------------------------------------------------------

    def prime(self) -> None:
        """Start from the chronology's head: nothing already recorded is news."""
        with self.dstore.read() as session:
            newest = session.scalar(select(func.max(ApiEventRow.seq)))
        self._cursor = int(newest or 0)

    def scan(self) -> int:
        """Turn the events recorded since the last scan into jobs; returns
        how many notifications were stored. A dispatcher never primed
        sends nothing."""
        if self._cursor is None:
            return 0
        push = self.config().push
        now = self.clock()
        stored = 0
        while True:
            events, cursor = self._read(self._cursor)
            self._cursor = cursor
            if not push.available:
                if len(events) < BATCH:
                    return stored
                continue
            notices: list[tuple[Event, Notice]] = []
            with self.dstore.read() as session:
                for event, recorded_at in events:
                    if now - recorded_at > STALE_S:
                        continue
                    try:
                        notices.extend((event, n) for n in self.rules.notices(session, event))
                    except Exception:
                        log.warning(
                            "push.rules_failed", seq=event.seq, type=event.type, exc_info=True
                        )
            for event, notice in notices:
                stored += self._dispatch(event, notice, now)
            if len(events) < BATCH:
                return stored

    def _read(self, after: int) -> tuple[list[tuple[Event, float]], int]:
        """The next batch of relevant events after ``after``, and the cursor
        that follows it."""
        with self.dstore.read() as session:
            newest = int(session.scalar(select(func.max(ApiEventRow.seq))) or 0)
            rows = session.scalars(
                select(ApiEventRow)
                .where(
                    ApiEventRow.seq > after,
                    ApiEventRow.seq <= newest,
                    ApiEventRow.type.in_(sorted(TYPES)),
                )
                .order_by(ApiEventRow.seq.asc())
                .limit(BATCH)
            ).all()
            events = [
                (
                    Event(
                        seq=int(row.seq),
                        type=str(row.type),
                        channel_id=row.channel_id,
                        run_id=row.run_id,
                        item_id=row.item_id,
                        data=json.loads(row.data_json) if row.data_json else {},
                    ),
                    float(row.recorded_at),
                )
                for row in rows
            ]
        cursor = events[-1][0].seq if len(events) == BATCH else max(newest, after)
        return events, cursor

    # -- matching ------------------------------------------------------------------

    def _dispatch(self, event: Event, notice: Notice, now: float) -> int:
        targets = [
            target
            for target in self.devices.targets([notice.user_id]).get(notice.user_id, [])
            if allowed(target.prefs, notice.kind, notice.channel_id)
        ]
        if not targets or self._duplicate(notice, now):
            return 0
        ref = self.devices.record(
            user_id=notice.user_id,
            kind=notice.kind,
            channel_id=notice.channel_id,
            turn_id=notice.turn_id,
            title=notice.title,
            body=notice.body,
            event_seq=event.seq,
            now=now,
        )
        for target in targets:
            self._enqueue(target, notice.kind, ref, notice.channel_id, now)
        log.info(
            "push.queued",
            kind=notice.kind,
            ref=ref,
            user=notice.user_id,
            devices=[target.id for target in targets],
            seq=event.seq,
        )
        return 1

    def _duplicate(self, notice: Notice, now: float) -> bool:
        if notice.dedupe is None:
            return False
        key = (notice.user_id, notice.dedupe)
        with self._lock:
            while self._seen and (
                len(self._seen) > DEDUPE_LIMIT or next(iter(self._seen.values())) < now - DEDUPE_S
            ):
                self._seen.popitem(last=False)
            if key in self._seen:
                return True
            self._seen[key] = now
        return False

    def _enqueue(
        self, target: DeviceTarget, kind: str, ref: str, channel_id: str | None, now: float
    ) -> None:
        payload = {"srv": target.server_ref, "k": kind, "ref": ref, "thread": channel_id or ""}
        with self._lock:
            heapq.heappush(
                self._jobs,
                _Job(now, next(self._order), device_id=target.id, kind=kind, payload=payload),
            )
        self._wake.set()

    def enqueue_test(self, target: DeviceTarget, ref: str) -> None:
        """Queue a test push to one device, whatever its preferences."""
        self._enqueue(target, "test", ref, None, self.clock())

    def pending(self) -> int:
        with self._lock:
            return len(self._jobs)

    # -- sending -------------------------------------------------------------------

    def deliver_due(self) -> int:
        """Send every job that is due now; returns how many were tried."""
        now = self.clock()
        tried = 0
        while True:
            with self._lock:
                if not self._jobs or self._jobs[0].due > now:
                    return tried
                job = heapq.heappop(self._jobs)
            tried += 1
            self._send(job, now)

    def _send(self, job: _Job, now: float) -> None:
        target = self.devices.target(job.device_id)
        if target is None:
            log.info(
                "push.skipped", device=job.device_id, ref=job.payload["ref"], reason="no_device"
            )
            return
        push = self.config().push
        result = self.relay().push(target.handle, job.payload)
        job.attempts += 1
        fields: dict[str, Any] = {
            "device": job.device_id,
            "ref": job.payload["ref"],
            "kind": job.kind,
            "attempt": job.attempts,
            "status": result.status,
            "error": result.error,
        }
        if result.outcome == "sent":
            self.devices.pushed(job.device_id, now)
            log.debug("push.sent", **fields)
            return
        if result.outcome == "unregistered":
            self.devices.forget(job.device_id)
            log.info("push.device_pruned", **fields)
            return
        if result.outcome == "invalid_handle":
            self.devices.unenroll(job.device_id, now)
            log.warning("push.handle_rejected", **fields)
            return
        if result.outcome == "refused":
            log.warning("push.dropped", **fields)
            return
        if job.attempts >= push.max_attempts:
            log.error("push.gave_up", **fields)
            return
        delay = min(push.backoff_s * 2 ** (job.attempts - 1), push.backoff_max_s)
        if result.retry_after is not None:
            delay = min(max(delay, result.retry_after), push.backoff_max_s)
        job.due = now + delay
        with self._lock:
            heapq.heappush(self._jobs, job)
        log.warning("push.retrying", delay_s=delay, **fields)

    # -- housekeeping --------------------------------------------------------------

    def prune(self) -> int:
        """Drop notifications past the chronology's own retention."""
        before = self.clock() - float(self.config().api.replay_retention_s)
        removed = self.devices.prune(before)
        if removed:
            log.info("push.notifications_pruned", rows=removed)
        return removed

    def step(self) -> None:
        """One pass: new events, due sends, the prune when it is time."""
        self.scan()
        self.deliver_due()
        now = self.clock()
        if now - self._last_prune >= PRUNE_EVERY_S:
            self._last_prune = now
            self.prune()

    # -- the thread ----------------------------------------------------------------

    def wake(self) -> None:
        """From any thread: there may be something to send."""
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self.prime()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sbxloop-api-push", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _next_wait(self) -> float:
        with self._lock:
            due = self._jobs[0].due if self._jobs else None
        if due is None:
            return self.poll_s
        return max(0.0, min(self.poll_s, due - self.clock()))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.warning("push.dispatch_failed", exc_info=True)
            self._wake.wait(self._next_wait())
            self._wake.clear()


__all__ = ["PushDispatcher"]
