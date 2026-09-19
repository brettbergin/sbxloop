"""A work item queued from outside the tick wakes the daemon at once, and
what is already queued is dispatched before the forge is polled.

Field (db, 2026-09-19): a chat ask sat 17-137s between the concierge's
`start_workload` and `run.dispatch`, because nothing woke the loop from
its `poll_interval_s` wait and the tick polled the forge (four cold
worker boots, ~11s) before it looked at the queue.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from sbxloop.config import Config
from sbxloop.daemon.controls import intake
from sbxloop.daemon.model import WorkItem
from sbxloop.engine.model import RunResult
from sbxloop.events import EventBus
from tests.unit.test_daemon_loop import Harness

WAIT_S = 10.0


def _chat_harness(tmp_path: Path, **daemon: Any) -> Harness:
    cfg = Config.model_validate({"home": str(tmp_path / "state"), "daemon": daemon})
    h = Harness(tmp_path, cfg)
    h.source.name = "chat"
    return h


def _ask(item_id: str = "chat:9001", by: str | None = None) -> WorkItem:
    return WorkItem(
        item_id=item_id, source_key=item_id[5:], title="ask", kind="workload", requested_by=by
    )


class TestWake:
    def test_an_item_queued_while_the_loop_waits_is_dispatched_at_once(
        self, tmp_path: Path
    ) -> None:
        """poll_interval_s is 60 here; the ask must start well inside it."""
        h = _chat_harness(tmp_path, poll_interval_s=60.0)
        h.outcomes = ["completed"]
        started = threading.Event()
        inner = h.loop._runner

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            started.set()
            return inner(item, cfg, run_id, bus, resume)

        h.loop._runner = runner
        thread = threading.Thread(target=h.loop.run_forever, daemon=True)
        thread.start()
        try:
            # The first tick finds nothing and settles into its wait.
            time.sleep(1.0)
            assert not started.is_set()
            intake.upsert(h.loop, _ask(), by="tester")
            assert started.wait(WAIT_S), "the queued ask waited for the poll interval"
        finally:
            h.loop.request_stop()
            thread.join(WAIT_S)
        assert not thread.is_alive()
        item = h.dstore.get("chat:9001")
        assert item is not None and item.state == "done"

    def test_a_stop_still_ends_the_wait(self, tmp_path: Path) -> None:
        h = _chat_harness(tmp_path, poll_interval_s=60.0)
        thread = threading.Thread(target=h.loop.run_forever, daemon=True)
        thread.start()
        time.sleep(0.5)
        h.loop.request_stop()
        thread.join(WAIT_S)
        assert not thread.is_alive()


class TestQueueBeforePoll:
    def test_what_is_already_queued_runs_before_the_forge_is_polled(self, tmp_path: Path) -> None:
        h = _chat_harness(tmp_path)
        h.outcomes = ["completed"]
        order: list[str] = []
        inner = h.loop._runner

        def poll() -> list[WorkItem]:
            order.append("poll")
            return []

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            order.append(f"run:{item.item_id}")
            return inner(item, cfg, run_id, bus, resume)

        h.source.poll = poll  # type: ignore[method-assign]
        h.loop._runner = runner
        h.dstore.upsert_new(_ask(), h.clock())
        result = h.loop.tick()
        assert result.launched == ("chat:9001",)
        assert order[0] == "run:chat:9001"

    def test_newly_discovered_work_still_runs_in_the_same_tick(self, tmp_path: Path) -> None:
        """Polling after the queue must not cost discovered work a tick.
        (Two askers: one person's second ask waits behind their first.)"""
        h = _chat_harness(tmp_path, max_concurrent_runs=2)
        h.outcomes = ["completed", "completed"]
        # Runs hold until the tick has returned, so their writes never race
        # the tick's own (the shape test_daemon_concurrent_dispatch uses).
        release = threading.Event()
        inner = h.loop._runner

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            release.wait(WAIT_S)
            return inner(item, cfg, run_id, bus, resume)

        h.loop._runner = runner
        h.dstore.upsert_new(_ask("chat:9001", by="ana"), h.clock())
        h.source.items = [_ask("chat:9002", by="bo")]
        result = h.loop.tick()
        release.set()
        assert result.launched == ("chat:9001", "chat:9002")
        h.loop.drain()
        for item_id in ("chat:9001", "chat:9002"):
            item = h.dstore.get(item_id)
            assert item is not None and item.state == "done"
