"""Task scheduling: dependency gating, skips, and concurrent lanes.

The task loop was strictly serial even where the DAG said two tasks were
independent, and a run's wall clock is essentially its turn count times the
per-turn latency (measured: 272 turns, ~10s each, 55 minutes). ``lanes`` is
how an operator buys that back.

These drive :meth:`LoopEngine._schedule_tasks` directly with ``_run_task``
stubbed. The end-to-end harness in ``test_engine.py`` scripts agent replies
in a fixed order, which cannot express "two lanes consumed these in either
order" — so ordering and overlap are pinned here instead, deterministically.
"""

from __future__ import annotations

import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.engine.engine import LoopEngine
from sbxloop.engine.model import TaskRecord, TaskSpec
from sbxloop.engine.store import StateStore
from sbxloop.events import EventBus
from sbxloop.sbx.cli import SbxCLI

# Long enough that a scheduler which refuses to overlap fails the test rather
# than hanging it; short enough that the failure is quick.
OVERLAP_TIMEOUT_S = 10.0


def spec(task_id: str, deps: list[str] | None = None) -> TaskSpec:
    return TaskSpec(id=task_id, title=f"Task {task_id}", depends_on=deps or [])


class Scheduler:
    """A LoopEngine whose ``_run_task`` is replaced by a recorder."""

    def __init__(self, tmp_path: Path, specs: list[TaskSpec], lanes: int) -> None:
        config = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "budgets": {"max_parallel_tasks": lanes},
            }
        )
        self.engine = LoopEngine(
            config,
            store=StateStore(tmp_path / "state" / "state.db"),
            bus=EventBus(),
            # Never invoked: every path that would shell out is stubbed.
            sbx=SbxCLI(binary=str(tmp_path / "no-such-sbx")),
            install_workers=False,
        )
        self.run_id = "rsched0001"
        self.engine.store.create_run(self.run_id, "ship it")
        self.engine.store.save_tasks(self.run_id, specs)
        self.started: list[str] = []
        self.ended: list[str] = []
        self._lock = threading.Lock()

    def run(self, body: Any) -> tuple[set[str], set[str]]:
        def stub(_run_id: str, _phases: Any, task: TaskRecord, *_rest: Any) -> None:
            with self._lock:
                self.started.append(task.spec.id)
            try:
                body(task)
            finally:
                with self._lock:
                    self.ended.append(task.spec.id)

        self.engine._run_task = stub  # type: ignore[method-assign]
        tasks = self.engine.store.get_tasks(self.run_id)
        return self.engine._schedule_tasks(self.run_id, None, tasks, 0.0, None, None)  # type: ignore[arg-type]


def done(task: TaskRecord) -> None:
    task.state = "done"


class TestSerialLane:
    def test_one_lane_keeps_dependency_order_and_never_overlaps(self, tmp_path: Path) -> None:
        """The default must be byte-for-byte the serial loop it replaced: at
        one lane the task list is already in dependency order, so the first
        non-terminal task always has its dependencies behind it."""
        sched = Scheduler(tmp_path, [spec("t1"), spec("t2"), spec("t3")], lanes=1)
        live: list[str] = []

        def body(task: TaskRecord) -> None:
            live.append(task.spec.id)
            assert live == [task.spec.id], "a second lane ran under max_parallel_tasks=1"
            live.remove(task.spec.id)
            done(task)

        failed, skipped = sched.run(body)
        assert sched.started == ["t1", "t2", "t3"]
        assert (failed, skipped) == (set(), set())


class TestConcurrentLanes:
    def test_independent_tasks_overlap(self, tmp_path: Path) -> None:
        """The point of the whole exercise: two tasks the DAG says are
        independent must be in flight at the same time."""
        sched = Scheduler(tmp_path, [spec("t1"), spec("t2")], lanes=2)
        barrier = threading.Barrier(2)

        def body(task: TaskRecord) -> None:
            # Neither task can pass this without the other arriving.
            barrier.wait(timeout=OVERLAP_TIMEOUT_S)
            done(task)

        failed, skipped = sched.run(body)
        assert sorted(sched.started) == ["t1", "t2"]
        assert (failed, skipped) == (set(), set())

    def test_a_dependent_waits_for_its_dependency_to_finish(self, tmp_path: Path) -> None:
        """Readiness is "every dependency has finished", not "is earlier in
        the list" — position stops implying anything once lanes overlap."""
        sched = Scheduler(tmp_path, [spec("t1"), spec("t2", deps=["t1"])], lanes=2)

        def body(task: TaskRecord) -> None:
            if task.spec.id == "t1":
                # If t2 were launched on position alone it would start here.
                threading.Event().wait(0.05)
                assert "t2" not in sched.started
            done(task)

        sched.run(body)
        assert sched.started == ["t1", "t2"]
        assert sched.ended == ["t1", "t2"]

    def test_a_wide_graph_fills_every_lane_and_no_more(self, tmp_path: Path) -> None:
        """Five ready tasks, three lanes: the first three meet at the gate,
        which pins the peak at exactly the configured width. The remaining
        two never make a full group, so their wait breaks rather than
        hanging the test."""
        sched = Scheduler(tmp_path, [spec(f"t{i}") for i in range(1, 6)], lanes=3)
        gate = threading.Barrier(3)
        lock = threading.Lock()
        peak = live = 0

        def body(task: TaskRecord) -> None:
            nonlocal peak, live
            with lock:
                live += 1
                peak = max(peak, live)
            with suppress(threading.BrokenBarrierError):
                gate.wait(timeout=0.5)
            with lock:
                live -= 1
            done(task)

        sched.run(body)
        assert len(sched.started) == 5
        assert peak == 3, f"expected exactly 3 lanes in flight, saw {peak}"


class TestFailurePropagation:
    def test_a_failed_dependency_skips_its_dependents(self, tmp_path: Path) -> None:
        sched = Scheduler(
            tmp_path,
            [spec("t1"), spec("t2", deps=["t1"]), spec("t3", deps=["t2"]), spec("t4")],
            lanes=2,
        )

        def body(task: TaskRecord) -> None:
            task.state = "failed" if task.spec.id == "t1" else "done"

        failed, skipped = sched.run(body)
        assert failed == {"t1"}
        assert skipped == {"t2", "t3"}, "a skip must cascade to its own dependents"
        # The skipped tasks are never driven, and the independent one still is.
        assert sorted(sched.started) == ["t1", "t4"]

    def test_an_infrastructure_error_lets_running_lanes_finish_then_raises(
        self, tmp_path: Path
    ) -> None:
        """A lane dying must not abandon its siblings mid-phase: their state
        is what makes the run resumable, so they are allowed to checkpoint
        before the error leaves the scheduler."""
        sched = Scheduler(tmp_path, [spec("t1"), spec("t2")], lanes=2)
        entered = threading.Barrier(2)

        def body(task: TaskRecord) -> None:
            entered.wait(timeout=OVERLAP_TIMEOUT_S)
            if task.spec.id == "t1":
                raise RuntimeError("worker died")
            threading.Event().wait(0.05)
            done(task)

        with pytest.raises(RuntimeError, match="worker died"):
            sched.run(body)
        assert sorted(sched.ended) == ["t1", "t2"], "the surviving lane was cut short"

    def test_terminal_tasks_are_left_alone_on_resume(self, tmp_path: Path) -> None:
        """A resumed run re-enters with tasks already finished; they must be
        counted, not re-driven."""
        sched = Scheduler(tmp_path, [spec("t1"), spec("t2")], lanes=2)
        record = sched.engine.store.get_tasks(sched.run_id)[0]
        record.state = "failed"
        sched.engine.store.update_task(sched.run_id, record)

        failed, _skipped = sched.run(done)
        assert failed == {"t1"}
        assert sched.started == ["t2"]
