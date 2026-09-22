"""Concurrent dispatch: ``[daemon] max_concurrent_runs`` above one.

With room for more than one run, a tick launches work and returns while it
executes; the next tick reaps what finished and settles it on the loop's
own thread. Controls address a run by its id, and two code runs never
share a repository. The single-run default is covered, unchanged, by
``test_daemon_loop.py``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sbxloop.config import Config
from sbxloop.daemon.controls.principal import Principal
from sbxloop.daemon.controls.results import ControlError
from sbxloop.daemon.controls.steering import SteeringStore
from sbxloop.daemon.loop import RunHandle
from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.engine.model import RunResult
from sbxloop.errors import RunCancelledError
from sbxloop.events import EventBus
from tests.unit.test_daemon_loop import Harness, gh_item

WAIT_S = 10.0


def _config(tmp_path: Path, limit: int = 2, **daemon: Any) -> Config:
    return Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "github": {"repos": [{"repo": "o/a"}, {"repo": "o/b"}]},
            "daemon": {"max_concurrent_runs": limit, "max_runs_per_day": 100, **daemon},
        }
    )


def _harness(tmp_path: Path, **daemon: Any) -> Harness:
    h = Harness(tmp_path, _config(tmp_path, **daemon))
    # No first-use clone of the fake repositories from the network.
    h.loop._ensure_workspace = lambda repo: None  # type: ignore[method-assign]
    return h


def _item(repo: str, number: str, **fields: Any) -> WorkItem:
    return gh_item(number, item_id=f"gh:{repo}:issue:{number}", repo=repo, **fields)


class Gate:
    """A runner whose runs start, report in, and wait to be released (or
    cancelled, ending the way a cancelled engine does)."""

    def __init__(self, h: Harness) -> None:
        self.h = h
        self.lock = threading.Lock()
        self.started: dict[str, threading.Event] = {}
        self.release: dict[str, threading.Event] = {}
        self.by_item: dict[str, str] = {}
        self.closed = False

    def _events(self, item_id: str) -> tuple[threading.Event, threading.Event]:
        with self.lock:
            started = self.started.setdefault(item_id, threading.Event())
            release = self.release.setdefault(item_id, threading.Event())
            if self.closed:
                release.set()
            return started, release

    def runner(
        self, item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
    ) -> RunResult:
        h = self.h
        h.runs.append((run_id, resume))
        h.store.create_run(run_id, "x", kind=item.kind)
        h.store.set_run_state(run_id, "building")
        started, release = self._events(item.item_id)
        with self.lock:
            self.by_item[item.item_id] = run_id
        engine = next(r for r in h.loop.runs if r.run_id == run_id).engine
        started.set()
        while True:
            released = release.wait(0.02)
            # A cancel wins over a release that came after it.
            if engine._cancel_event.is_set():
                raise RunCancelledError(f"run {run_id} interrupted")
            if released:
                break
        h.store.set_run_state(run_id, "merged")
        return RunResult(run_id=run_id, state="merged")

    def wait_started(self, item_id: str) -> str:
        started, _ = self._events(item_id)
        assert started.wait(WAIT_S), f"{item_id} never started"
        return self.by_item[item_id]

    def finish(self, item_id: str) -> None:
        _, release = self._events(item_id)
        release.set()

    def finish_all(self) -> None:
        """Release every run, including one whose runner has not reported in
        yet: a tick returns once a run's thread is started, so a test can get
        here first, and a run left unreleased never ends."""
        with self.lock:
            self.closed = True
            releases = list(self.release.values())
        for release in releases:
            release.set()


def _join(h: Harness, run_id: str) -> None:
    """Wait for one run's engine thread to end (not for it to be settled)."""
    handle = next(r for r in h.loop.runs if r.run_id == run_id)
    assert handle.thread is not None
    handle.thread.join(WAIT_S)
    assert not handle.thread.is_alive()


def _tick(h: Harness) -> Any:
    """A tick that must return while runs are still executing."""
    box: list[Any] = []
    t = threading.Thread(target=lambda: box.append(h.loop.tick()))
    t.start()
    t.join(WAIT_S)
    assert not t.is_alive(), "tick blocked on a run in flight"
    return box[0]


def _release_all(h: Harness, gate: Gate) -> None:
    gate.finish_all()
    for handle in h.loop.runs:
        if handle.thread is not None:
            handle.thread.join(WAIT_S)
    # drain() waits for as long as any run is alive, which is what a daemon
    # wants and what hangs a test: a run the gate never released would spin
    # it forever. Fail here, naming the run, instead.
    stuck = [r.run_id for r in h.loop.runs if r.thread is not None and r.thread.is_alive()]
    assert not stuck, f"runs still executing after release: {stuck}"
    h.loop.drain()


class TestGate:
    def test_releasing_everything_ends_a_run_that_had_not_reported_in(self, tmp_path: Path) -> None:
        # A tick returns once a run's thread is started, not once its runner
        # has reported in; a release that lands in between must still end it.
        h = _harness(tmp_path)
        gate = Gate(h)
        entered = threading.Event()
        proceed = threading.Event()

        def late_runner(*args: Any) -> RunResult:
            entered.set()
            assert proceed.wait(WAIT_S)
            return gate.runner(*args)

        h.loop._runner = late_runner
        h.source.items = [_item("o/a", "1")]
        assert _tick(h).launched == ("gh:o/a:issue:1",)
        assert entered.wait(WAIT_S)
        (handle,) = h.loop.runs
        assert handle.thread is not None
        try:
            gate.finish_all()
            proceed.set()
            handle.thread.join(WAIT_S)
            assert not handle.thread.is_alive(), "a released run kept executing"
        finally:
            handle.engine.request_cancel()
            handle.thread.join(WAIT_S)
        h.loop.drain()
        assert h.dstore.get("gh:o/a:issue:1").state == "done"  # type: ignore[union-attr]


class TestConcurrentDispatch:
    def test_two_items_are_in_flight_at_once_and_the_tick_returns(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        gate = Gate(h)
        h.loop._runner = gate.runner
        h.source.items = [_item("o/a", "1"), _item("o/b", "2")]
        try:
            result = _tick(h)
            assert result.launched == ("gh:o/a:issue:1", "gh:o/b:issue:2")
            assert result.dispatched == "gh:o/a:issue:1" and result.outcome == "started"
            # Both runners are executing at the same time.
            first = gate.wait_started("gh:o/a:issue:1")
            second = gate.wait_started("gh:o/b:issue:2")
            assert {r.run_id for r in h.loop.runs} == {first, second}
            for item_id in ("gh:o/a:issue:1", "gh:o/b:issue:2"):
                assert h.dstore.get(item_id).state == "running"  # type: ignore[union-attr]
            # Full: the next tick neither blocks nor launches.
            busy = _tick(h)
            assert busy.dispatched is None and busy.idle_kind == "busy"
        finally:
            _release_all(h, gate)

    def test_a_third_item_waits_for_a_free_slot(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        gate = Gate(h)
        h.loop._runner = gate.runner
        h.source.items = [_item("o/a", "1"), _item("o/b", "2"), _item("o/b", "3", kind="workload")]
        try:
            assert _tick(h).launched == ("gh:o/a:issue:1", "gh:o/b:issue:2")
            first = gate.wait_started("gh:o/a:issue:1")
            assert h.dstore.get("gh:o/b:issue:3").state == "queued"  # type: ignore[union-attr]
            gate.finish("gh:o/a:issue:1")
            _join(h, first)
            result = _tick(h)
            assert result.settled == (("gh:o/a:issue:1", "done"),)
            assert result.launched == ("gh:o/b:issue:3",)
            gate.wait_started("gh:o/b:issue:3")
        finally:
            _release_all(h, gate)

    def test_reaping_settles_finished_runs_on_the_loop_thread(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        gate = Gate(h)
        h.loop._runner = gate.runner
        settled_on: list[str] = []

        class Frontend:
            def daemon_notice(self, notice: Any) -> None: ...
            def run_started(self, *a: Any) -> None: ...
            def run_finished(self, item: WorkItem, report: RunReport) -> None:
                settled_on.append(threading.current_thread().name)

        h.loop.frontend = Frontend()  # type: ignore[assignment]
        h.source.items = [_item("o/a", "1"), _item("o/b", "2")]
        try:
            h.loop.tick()
            first = gate.wait_started("gh:o/a:issue:1")
            second = gate.wait_started("gh:o/b:issue:2")
            gate.finish("gh:o/a:issue:1")
            gate.finish("gh:o/b:issue:2")
            _join(h, first)
            _join(h, second)
            # Finished, but nothing settles until the loop reaps.
            assert h.dstore.get("gh:o/a:issue:1").state == "running"  # type: ignore[union-attr]
            result = h.loop.tick()
            assert sorted(result.settled) == [
                ("gh:o/a:issue:1", "done"),
                ("gh:o/b:issue:2", "done"),
            ]
            assert h.loop.runs == []
            for item_id in ("gh:o/a:issue:1", "gh:o/b:issue:2"):
                assert h.dstore.get(item_id).state == "done"  # type: ignore[union-attr]
            assert settled_on == [threading.current_thread().name] * 2
        finally:
            _release_all(h, gate)

    def test_cancel_and_steer_reach_the_named_run_only(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        gate = Gate(h)
        h.loop._runner = gate.runner
        h.source.items = [_item("o/a", "1"), _item("o/b", "2")]
        try:
            h.loop.tick()
            first = gate.wait_started("gh:o/a:issue:1")
            second = gate.wait_started("gh:o/b:issue:2")
            engines = {r.run_id: r.engine for r in h.loop.runs}
            h.loop.steer_run(second, "use the other helper", by="ops")
            assert engines[second]._chat_queue.get_nowait().text == "use the other helper"
            assert engines[first]._chat_queue.empty()
            outcome = h.loop.cancel_run(second, by="ops")
            assert outcome.mode == "current" and outcome.target == second
            assert engines[second]._cancel_event.is_set()
            assert not engines[first]._cancel_event.is_set()
            _join(h, second)
            result = h.loop.tick()
            assert result.settled == (("gh:o/b:issue:2", "cancelled"),)
            assert h.dstore.get("gh:o/b:issue:2").state == "cancelled"  # type: ignore[union-attr]
            # The other run is untouched and still in flight.
            assert [r.run_id for r in h.loop.runs] == [first]
            assert h.dstore.get("gh:o/a:issue:1").state == "running"  # type: ignore[union-attr]
            with pytest.raises(ControlError) as refused:
                h.loop.steer_run(second, "too late")
            assert refused.value.code == "not_eligible"
        finally:
            _release_all(h, gate)
        assert h.dstore.get("gh:o/a:issue:1").state == "done"  # type: ignore[union-attr]

    def test_bare_cancel_means_the_oldest_run(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        gate = Gate(h)
        h.loop._runner = gate.runner
        h.source.items = [_item("o/a", "1"), _item("o/b", "2")]
        try:
            h.loop.tick()
            first = gate.wait_started("gh:o/a:issue:1")
            second = gate.wait_started("gh:o/b:issue:2")
            assert h.loop.current is not None and h.loop.current.run_id == first
            assert h.loop.cancel_current("ops") is True
            engines = {r.run_id: r.engine for r in h.loop.runs}
            assert engines[first]._cancel_event.is_set()
            assert not engines[second]._cancel_event.is_set()
        finally:
            _release_all(h, gate)
        assert h.dstore.get("gh:o/a:issue:1").state == "cancelled"  # type: ignore[union-attr]
        assert h.dstore.get("gh:o/b:issue:2").state == "done"  # type: ignore[union-attr]

    def test_status_lists_every_run_and_keeps_the_oldest_as_current(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        gate = Gate(h)
        h.loop._runner = gate.runner
        h.source.items = [_item("o/a", "1"), _item("o/b", "2")]
        try:
            h.loop.tick()
            first = gate.wait_started("gh:o/a:issue:1")
            second = gate.wait_started("gh:o/b:issue:2")
            status = h.loop.status()
            assert [r["run_id"] for r in status["runs"]] == [first, second]
            assert status["current"]["run_id"] == first
            assert status["max_concurrent_runs"] == 2
        finally:
            _release_all(h, gate)
        assert h.loop.status()["runs"] == []


class TestRepositoryExclusivity:
    def test_two_code_runs_for_one_repository_never_overlap(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        gate = Gate(h)
        h.loop._runner = gate.runner
        h.source.items = [_item("o/a", "1"), _item("o/a", "2"), _item("o/b", "3")]
        try:
            result = _tick(h)
            # The second o/a item is passed over; o/b takes the free slot.
            assert result.launched == ("gh:o/a:issue:1", "gh:o/b:issue:3")
            first = gate.wait_started("gh:o/a:issue:1")
            gate.wait_started("gh:o/b:issue:3")
            assert h.dstore.get("gh:o/a:issue:2").state == "queued"  # type: ignore[union-attr]
            gate.finish("gh:o/b:issue:3")
            for handle in h.loop.runs:
                if handle.item.item_id == "gh:o/b:issue:3":
                    assert handle.thread is not None
                    handle.thread.join(WAIT_S)
            # A slot is free, but the only queued item shares o/a with a live run.
            waiting = _tick(h)
            assert waiting.settled == (("gh:o/b:issue:3", "done"),)
            assert waiting.launched == () and waiting.idle_kind == "busy"
            assert "o/a" in (waiting.idle_detail or "")
            gate.finish("gh:o/a:issue:1")
            _join(h, first)
            after = _tick(h)
            assert after.launched == ("gh:o/a:issue:2",)
            gate.wait_started("gh:o/a:issue:2")
        finally:
            _release_all(h, gate)
        assert h.dstore.get("gh:o/a:issue:2").state == "done"  # type: ignore[union-attr]

    def test_a_workload_shares_a_repository_but_skips_its_refresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h = _harness(tmp_path)
        gate = Gate(h)
        h.loop._runner = gate.runner
        refreshed: list[str | None] = []
        monkeypatch.setattr(h.loop, "_ensure_workspace", refreshed.append)
        h.source.items = [_item("o/a", "1")]
        try:
            h.loop.tick()
            gate.wait_started("gh:o/a:issue:1")
            assert refreshed == ["o/a"]
            h.source.items = [_item("o/a", "2", kind="workload")]
            assert _tick(h).launched == ("gh:o/a:issue:2",)
            gate.wait_started("gh:o/a:issue:2")
            # The checkout under a live run's feet is left alone.
            assert refreshed == ["o/a"]
        finally:
            _release_all(h, gate)

    def test_a_code_run_refreshes_while_a_workload_shares_its_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The opposite order: a workload works from its own data
        directory, never from the checkout, so a code run admitted beside
        it still starts from current ``origin/<branch>``. Only a live code
        run keeps the checkout from moving."""
        h = _harness(tmp_path)
        gate = Gate(h)
        h.loop._runner = gate.runner
        refreshed: list[str | None] = []
        monkeypatch.setattr(h.loop, "_ensure_workspace", refreshed.append)
        h.source.items = [_item("o/a", "1", kind="workload")]
        try:
            h.loop.tick()
            gate.wait_started("gh:o/a:issue:1")
            assert refreshed == ["o/a"]
            h.source.items = [_item("o/a", "2")]
            assert _tick(h).launched == ("gh:o/a:issue:2",)
            gate.wait_started("gh:o/a:issue:2")
            # The workload is not on the checkout: the code run refreshes it.
            assert refreshed == ["o/a", "o/a"]
        finally:
            _release_all(h, gate)


class TestShutdown:
    def test_drain_settles_every_run_after_a_stop(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        gate = Gate(h)
        h.loop._runner = gate.runner
        h.source.items = [_item("o/a", "1"), _item("o/b", "2")]
        h.loop.tick()
        gate.wait_started("gh:o/a:issue:1")
        gate.wait_started("gh:o/b:issue:2")
        h.loop.request_stop()
        done = threading.Thread(target=h.loop.run_forever)
        done.start()
        gate.finish("gh:o/a:issue:1")
        gate.finish("gh:o/b:issue:2")
        done.join(WAIT_S)
        assert not done.is_alive()
        assert h.loop.runs == []
        for item_id in ("gh:o/a:issue:1", "gh:o/b:issue:2"):
            assert h.dstore.get(item_id).state == "done"  # type: ignore[union-attr]

    def test_quiesce_cancels_every_run(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, shutdown_grace_s=WAIT_S)
        gate = Gate(h)
        h.loop._runner = gate.runner
        h.source.items = [_item("o/a", "1"), _item("o/b", "2")]
        h.loop.tick()
        gate.wait_started("gh:o/a:issue:1")
        gate.wait_started("gh:o/b:issue:2")
        engines = [r.engine for r in h.loop.runs]
        h.loop.quiesce()
        assert all(e._cancel_event.is_set() for e in engines)
        assert all(r.thread is not None and not r.thread.is_alive() for r in h.loop.runs)
        h.loop.drain()
        # Interrupted by shutdown: left running for recovery to resume.
        for item_id in ("gh:o/a:issue:1", "gh:o/b:issue:2"):
            assert h.dstore.get(item_id).state == "running"  # type: ignore[union-attr]


class TestLiveRunIds:
    def test_orphan_steering_is_settled_except_for_every_live_run(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        store = SteeringStore(h.dstore)
        ids = {}
        for run_id in ("r_live1", "r_live2", "r_dead"):
            record = store.create(
                run_id=run_id,
                text="x",
                principal=Principal.trusted("ops", "ctl"),
                source_refs=[],
                expected_revision=None,
                now=1.0,
                deadline_at=None,
                operation_id=None,
            )
            store.delivered(record.id, f"m-{run_id}", 2.0)
            ids[run_id] = record.id
        assert store.settle_orphans({"r_live1", "r_live2"}, 3.0) == 1
        states = {run_id: store.get(sid).status for run_id, sid in ids.items()}  # type: ignore[union-attr]
        assert states == {
            "r_live1": "delivered",
            "r_live2": "delivered",
            "r_dead": "undelivered",
        }

    def test_recovery_leaves_every_live_run_alone(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        for run_id in ("r_live1", "r_live2", "r_orphan"):
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "building")
        for number, run_id in (("1", "r_live1"), ("2", "r_live2")):
            h.loop._register(RunHandle(_item("o/a", number), run_id, None, EventBus()))  # type: ignore[arg-type]
        h.loop._reconcile_orphan_runs()
        assert h.store.get_run("r_live1").state == "building"
        assert h.store.get_run("r_live2").state == "building"
        assert h.store.get_run("r_orphan").state == "failed"


class TestHalfOpenProbe:
    """A breaker past its cooldown, or a provider hold past its wait, lets
    one probe run through; the other slots stay empty until it settles."""

    def test_a_half_open_breaker_launches_exactly_one_probe(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, max_consecutive_failures=2, breaker_cooldown_s=100)
        gate = Gate(h)
        h.loop._runner = gate.runner
        h.loop._set_breaker(h.clock.t - 200, 2)
        h.source.items = [_item("o/a", "1"), _item("o/b", "2")]
        try:
            assert _tick(h).launched == ("gh:o/a:issue:1",)
            probe = gate.wait_started("gh:o/a:issue:1")
            # A later tick while the probe runs launches nothing.
            waiting = _tick(h)
            assert waiting.launched == () and waiting.idle_kind == "breaker"
            assert h.dstore.get("gh:o/b:issue:2").state == "queued"  # type: ignore[union-attr]
            # The probe succeeds: the breaker closes and the slot fills.
            gate.finish("gh:o/a:issue:1")
            _join(h, probe)
            after = _tick(h)
            assert after.settled == (("gh:o/a:issue:1", "done"),)
            assert after.launched == ("gh:o/b:issue:2",)
            gate.wait_started("gh:o/b:issue:2")
            assert h.loop.status()["consecutive_failures"] == 0
        finally:
            _release_all(h, gate)

    def test_an_expired_provider_hold_launches_exactly_one_probe(self, tmp_path: Path) -> None:
        from tests.unit.test_provider_recovery import job, rejected

        h = _harness(tmp_path)
        gate = Gate(h)
        h.loop._runner = gate.runner
        recovery = h.loop._provider_recovery()
        hold = recovery.record(job(run_id="r_held"), rejected("throttle"))
        assert hold.next_at is not None
        h.clock.t = hold.next_at + 1
        h.source.items = [_item("o/a", "1"), _item("o/b", "2")]
        try:
            assert _tick(h).launched == ("gh:o/a:issue:1",)
            gate.wait_started("gh:o/a:issue:1")
            waiting = _tick(h)
            assert waiting.launched == () and waiting.idle_kind == "provider_held"
            # The probe's call succeeds, which releases the hold.
            recovery.release()
            assert _tick(h).launched == ("gh:o/b:issue:2",)
            gate.wait_started("gh:o/b:issue:2")
        finally:
            _release_all(h, gate)


class TestKnob:
    def test_default_is_one_run_at_a_time(self, tmp_path: Path) -> None:
        assert Config.model_validate({"home": str(tmp_path)}).daemon.max_concurrent_runs == 1

    @pytest.mark.parametrize("value", [0, 5])
    def test_out_of_range_is_refused(self, tmp_path: Path, value: int) -> None:
        with pytest.raises(ValidationError):
            Config.model_validate({"home": str(tmp_path), "daemon": {"max_concurrent_runs": value}})
