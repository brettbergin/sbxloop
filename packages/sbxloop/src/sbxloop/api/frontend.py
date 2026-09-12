"""The daemon's lifecycle, written into the public chronology.

An observer on the daemon's fan-out frontend: every notice, run start and
finish, and gate transition becomes a public event in the same store the
operations record lives in, and the engine's own bus wakes the projector
so a run's chronology follows within a poll. Nothing here blocks the
loop: a write is one insert, and a bus subscriber only sets an event.
"""

from __future__ import annotations

from typing import Any

from sbxloop.api.chronology import DAEMON_ACTOR, Chronology
from sbxloop.api.projector import Projector
from sbxloop.api.stream import StreamHub
from sbxloop.daemon.model import DaemonNotice, RunReport, WorkItem
from sbxloop.daemon.store import MergeGate
from sbxloop.events import EventBus
from sbxloop.log import get_logger

log = get_logger(__name__)


class ApiFrontend:
    backend = "api"

    def __init__(
        self,
        chronology: Chronology,
        hub: StreamHub,
        projector: Projector | None,
        *,
        clock: Any,
    ) -> None:
        self.chronology = chronology
        self.hub = hub
        self.projector = projector
        self.clock = clock

    def _record(self, type_: str, **fields: Any) -> None:
        try:
            self.chronology.record(type_, self.clock(), actor=DAEMON_ACTOR, **fields)
        except Exception:
            log.warning("api.frontend_write_failed", type=type_, exc_info=True)
            return
        self.hub.notify()

    # -- Frontend protocol ---------------------------------------------------------

    def daemon_notice(self, notice: DaemonNotice) -> None:
        self._record(
            "daemon.notice",
            run_id=notice.run_id,
            item_id=notice.item_id,
            data={
                "kind": notice.kind,
                "level": notice.level,
                "text": notice.text,
                "url": notice.url,
            },
        )

    def run_started(self, item: WorkItem, run_id: str, engine: Any, bus: EventBus) -> None:
        self._record(
            "run.started",
            run_id=run_id,
            item_id=item.item_id,
            data={
                "kind": item.kind,
                "title": item.title,
                "profile": item.profile,
                "recipe": item.recipe,
                "attempt": item.attempts,
            },
        )
        if self.projector is not None:
            wake = self.projector.wake
            bus.subscribe(lambda event: wake())

    def run_finished(self, item: WorkItem, report: RunReport) -> None:
        self._record(
            "run.finished",
            run_id=report.run_id,
            item_id=item.item_id,
            data={
                "kind": report.kind,
                "state": report.state,
                "reason": report.reason,
                "summary": report.task_summary,
                "pr_number": report.pr[0] if report.pr else None,
                "pr_url": report.pr[1] if report.pr else None,
                "branch": report.branch,
                "rounds": report.rounds,
                "cancelled_by": report.cancelled_by,
                "requeued": report.requeued,
            },
        )
        if self.projector is not None:
            self.projector.wake()

    def merge_gate_opened(self, item: WorkItem, run_id: str, gate: MergeGate) -> None:
        self._record(
            "gate.opened",
            run_id=run_id,
            item_id=item.item_id,
            data={
                "kind": gate.kind,
                "state": gate.state,
                "pr_number": gate.pr_number,
                "pr_url": gate.pr_url,
                "revision": gate.revision,
            },
        )

    def merge_gate_resolved(
        self,
        item: WorkItem,
        run_id: str,
        gate: MergeGate,
        outcome: str,
        by: str | None,
        detail: str | None = None,
    ) -> None:
        self._record(
            "gate.resolved",
            run_id=run_id,
            item_id=item.item_id,
            data={"kind": gate.kind, "outcome": outcome, "by": by, "detail": detail},
        )
