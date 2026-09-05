"""Every bridge at once: the daemon's frontend when it runs the operator
console's local bridge beside the configured Discord or Slack one.

``DaemonLoop.frontend`` is one slot and the concierge takes one ``on_watch``
and one ``thread_link``; this is the object that fills them and hands each
call to every bridge, so a bridge that raises never starves another and the
loop still never blocks on chat.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sbxloop.config import Config
from sbxloop.daemon.chat import DRAIN_WAIT_S, ChatBridge, build_bridge
from sbxloop.daemon.concierge import Concierge
from sbxloop.daemon.model import DaemonNotice, RunReport, WorkItem
from sbxloop.daemon.store import ChatThread, DaemonStore, MergeGate
from sbxloop.engine.engine import LoopEngine
from sbxloop.errors import DaemonError
from sbxloop.events import EventBus
from sbxloop.log import get_logger

log = get_logger(__name__)


class FanoutFrontend:
    """The :class:`~sbxloop.daemon.loop.Frontend` over several bridges."""

    def __init__(self, bridges: Sequence[ChatBridge]) -> None:
        self.bridges: tuple[ChatBridge, ...] = tuple(bridges)

    def bridge_for(self, backend: str) -> ChatBridge | None:
        return next((b for b in self.bridges if b.backend == backend), None)

    @property
    def primary(self) -> ChatBridge | None:
        """The external bridge when there is one, else the local one."""
        external = next((b for b in self.bridges if b.backend != "local"), None)
        return external or self.bridge_for("local")

    # -- lifecycle ---------------------------------------------------------------

    def start(self, *, connect_wait_s: float = 15.0) -> None:
        """Start every bridge in order; a failure closes the ones already
        started and re-raises naming the bridge, so the daemon exits the way
        it did with one bridge (a missing token is the operator's mistake)."""
        started: list[ChatBridge] = []
        for bridge in self.bridges:
            try:
                bridge.start(connect_wait_s=connect_wait_s)
            except Exception as exc:
                log.error("chat.bridge_failed", backend=bridge.backend, error=str(exc))
                for done in started:
                    try:
                        done.close(drain_wait_s=0.0)
                    except Exception:
                        log.debug("frontend.close_failed", backend=done.backend, exc_info=True)
                if isinstance(exc, DaemonError):
                    raise
                raise DaemonError(f"{bridge.backend} bridge failed to start: {exc}") from exc
            started.append(bridge)

    def close(self, *, drain_wait_s: float = DRAIN_WAIT_S) -> None:
        for bridge in self.bridges:
            try:
                bridge.close(drain_wait_s=drain_wait_s)
            except Exception:
                log.warning("frontend.close_failed", backend=bridge.backend, exc_info=True)

    def set_concierge(self, concierge: Concierge | None) -> None:
        for bridge in self.bridges:
            bridge.concierge = concierge

    # -- Frontend protocol -------------------------------------------------------

    def _each(self, call: str, *args: Any) -> None:
        for bridge in self.bridges:
            try:
                getattr(bridge, call)(*args)
            except Exception:
                log.warning(
                    "frontend.fanout_failed", backend=bridge.backend, call=call, exc_info=True
                )

    def daemon_notice(self, notice: DaemonNotice) -> None:
        self._each("daemon_notice", notice)

    def run_started(self, item: WorkItem, run_id: str, engine: LoopEngine, bus: EventBus) -> None:
        self._each("run_started", item, run_id, engine, bus)

    def run_finished(self, item: WorkItem, report: RunReport) -> None:
        self._each("run_finished", item, report)

    def merge_gate_opened(self, item: WorkItem, run_id: str, gate: MergeGate) -> None:
        self._each("merge_gate_opened", item, run_id, gate)

    def merge_gate_resolved(
        self,
        item: WorkItem,
        run_id: str,
        gate: MergeGate,
        outcome: str,
        by: str | None,
        detail: str | None = None,
    ) -> None:
        self._each("merge_gate_resolved", item, run_id, gate, outcome, by, detail)

    # -- what the concierge takes --------------------------------------------------

    def on_watch(self, run_id: str, requester: str) -> str | None:
        """Ask every bridge; None (registered) as soon as one bridge knew
        the requester, else the first bridge's note."""
        first_note: str | None = None
        for bridge in self.bridges:
            try:
                note = bridge.on_watch(run_id, requester)
            except Exception:
                log.warning("frontend.watch_failed", backend=bridge.backend, exc_info=True)
                continue
            if note is None:
                return None
            first_note = first_note or note
        return first_note

    def thread_link(self, thread: ChatThread) -> str:
        bridge = self.bridge_for(thread.backend) or self.primary
        return thread.thread_id if bridge is None else bridge.thread_link(thread)


def build_frontend(config: Config, dstore: DaemonStore, *, loop_ref: Any = None) -> FanoutFrontend:
    """The daemon's frontend: always the operator console's local bridge —
    first, since it is ready at once and must not wait behind a gateway
    connect — and the ``[chat] backend`` bridge when one is configured."""
    from sbxloop.daemon.local import LocalBridge

    bridges: list[ChatBridge] = [LocalBridge(config, dstore, loop_ref=loop_ref)]
    external = build_bridge(config, dstore, loop_ref=loop_ref)
    if external is not None:
        bridges.append(external)
    return FanoutFrontend(bridges)


__all__ = ["FanoutFrontend", "build_frontend"]
