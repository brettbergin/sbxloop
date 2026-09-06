"""A workload held at publishing (`[[workloads]] publish = "hold"`, #760):
park, release, drop.

The daemon-side half: a run that ends ``held`` parks its item the way
the merge gate does — a durable gate row of kind ``publish``, the source
told once how to release, nothing dispatching the parked item — and one
release re-queues the item with its run pinned so the next tick resumes
it at the publishing stage. The engine-side half (the hold itself, and a
resume that publishes once) is pinned in test_engine_workload.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop.daemon.control import dispatch
from sbxloop.daemon.model import WorkItem
from tests.unit.test_daemon_loop import FakeSource, Harness, gh_item
from tests.unit.test_daemon_merge_gate import GateFrontend

ITEM = "gh:issue:1"


class HeldSource(FakeSource):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.held_ok = True

    def report_held(self, item: WorkItem) -> bool:
        self.calls.append(("held", item.item_id))
        return self.held_ok


def held_harness(tmp_path: Path) -> Harness:
    h = Harness(tmp_path)
    h.source = HeldSource()
    h.loop.source = h.source
    h.loop.frontend = GateFrontend()
    return h


def park(h: Harness, **item_overrides: Any) -> str:
    """Dispatch one workload that ends held; returns its run id."""
    h.source.items = [gh_item("1", kind="workload", **item_overrides)]
    h.outcomes = ["held"]
    result = h.loop.tick()
    assert result.outcome == "held", result
    item = h.dstore.get(ITEM)
    assert item is not None and item.run_id is not None
    return item.run_id


def ledger_result(h: Harness, run_id: str) -> str | None:
    row = h.dstore._conn.execute(
        "SELECT result FROM daemon_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return None if row is None else row[0]


def frontend(h: Harness) -> GateFrontend:
    front = h.loop.frontend
    assert isinstance(front, GateFrontend)
    return front


class TestParkOnHeld:
    def test_the_item_parks_with_a_publish_gate_and_a_reset_breaker(self, tmp_path: Path) -> None:
        h = held_harness(tmp_path)
        run_id = park(h)
        item = h.dstore.get(ITEM)
        assert item is not None and item.state == "gated"
        assert item.run_id == run_id, "the run stays pinned to the item"
        gate = h.dstore.merge_gate_for(ITEM)
        assert gate is not None and (gate.kind, gate.state) == ("publish", "open")
        assert (gate.pr_number, gate.pr_url, gate.branch) == (0, "", None)
        assert h.loop.status()["breaker_open"] is False
        assert ledger_result(h, run_id) == "held"

    def test_the_source_hears_the_park_once_and_the_humans_are_asked(self, tmp_path: Path) -> None:
        h = held_harness(tmp_path)
        run_id = park(h, requested_by="U123")
        assert [c[0] for c in h.source.calls] == ["claim", "started", "held"]
        item = h.dstore.get(ITEM)
        assert item is not None and item.pending_report is None
        front = frontend(h)
        ((opened_run, gate),) = front.gates_opened
        assert opened_run == run_id and gate.kind == "publish"
        assert gate.notify_ids == ("U123",)
        (notice,) = [n for n in front.notices if n.kind == "run.held"]
        assert "release" in notice.text and ITEM in notice.text
        ((_, report),) = front.finished
        assert report.state == "held" and not report.published

    def test_a_failed_park_report_stays_owed(self, tmp_path: Path) -> None:
        h = held_harness(tmp_path)
        source = h.source
        assert isinstance(source, HeldSource)
        source.held_ok = False
        park(h)
        item = h.dstore.get(ITEM)
        assert item is not None and item.pending_report == "held"
        source.held_ok = True
        h.loop.tick()
        item = h.dstore.get(ITEM)
        assert item is not None and item.pending_report is None

    def test_recovery_settles_a_run_that_ended_held_as_a_hold(self, tmp_path: Path) -> None:
        """The daemon died between the engine ending ``held`` and the
        settle: recovery parks the item, it does not retry the run."""
        h = held_harness(tmp_path)
        h.source.items = [gh_item("1", kind="workload")]
        h.outcomes = ["completed"]
        h.loop.tick()
        item = h.dstore.get(ITEM)
        assert item is not None and item.run_id is not None
        run_id = item.run_id
        # Rewind the store to the moment before the settle: the run ended
        # held and the item still says running.
        h.store.set_run_state(run_id, "held")
        h.dstore._conn.execute(
            "UPDATE daemon_work_items SET state = 'running', pending_report = NULL "
            "WHERE item_id = ?",
            (ITEM,),
        )
        h.dstore._conn.commit()
        h.loop.recover()
        item = h.dstore.get(ITEM)
        assert item is not None and (item.state, item.run_id) == ("gated", run_id)
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and (gate.kind, gate.state) == ("publish", "open")

    def test_a_held_item_is_never_dispatched(self, tmp_path: Path) -> None:
        h = held_harness(tmp_path)
        park(h)
        runs = len(h.runs)
        for _ in range(3):
            h.clock.t += 600
            assert h.loop.tick().dispatched is None
        assert len(h.runs) == runs


class TestRelease:
    def test_release_requeues_the_item_pinned_and_the_next_tick_publishes(
        self, tmp_path: Path
    ) -> None:
        h = held_harness(tmp_path)
        run_id = park(h, requested_by="U123")
        reply = dispatch(h.loop, f"release {ITEM}", by="brett", via="test")
        assert reply.ok and "released by brett" in reply.text
        item = h.dstore.get(ITEM)
        assert item is not None and (item.state, item.run_id) == ("queued", run_id)
        assert item.not_before is None and item.last_error == "released by brett"
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and (gate.state, gate.resolved_by) == ("approving", "brett")
        assert any(n.kind == "run.released" for n in frontend(h).notices)

        h.outcomes = ["completed"]
        result = h.loop.tick()
        assert result.outcome == "done", result
        assert h.runs[-1] == (run_id, True), "the same run, resumed"
        item = h.dstore.get(ITEM)
        assert item is not None and item.state == "done"
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and (gate.state, gate.resolved_by) == ("released", "brett")
        assert frontend(h).gates_resolved == [(run_id, "released", "brett")]
        assert [c[0] for c in h.source.calls][-1] == "completed"
        resuming = [n for n in frontend(h).notices if n.kind == "run.resuming"]
        assert resuming and "released by brett" in resuming[-1].text

    def test_merge_and_approve_release_a_publish_gate_too(self, tmp_path: Path) -> None:
        h = held_harness(tmp_path)
        park(h)
        reply = dispatch(h.loop, f"merge {ITEM}", via="test")
        assert reply.ok and "released" in reply.text

    def test_a_second_release_loses_the_cas(self, tmp_path: Path) -> None:
        h = held_harness(tmp_path)
        park(h)
        assert dispatch(h.loop, f"release {ITEM}", via="test").ok
        again = dispatch(h.loop, f"release {ITEM}", via="test")
        assert again.ok and "already being released" in again.text

    def test_a_released_result_cannot_be_released_again(self, tmp_path: Path) -> None:
        h = held_harness(tmp_path)
        park(h)
        dispatch(h.loop, f"release {ITEM}", via="test")
        h.outcomes = ["completed"]
        h.loop.tick()
        reply = dispatch(h.loop, f"release {ITEM}", via="test")
        assert not reply.ok and "already released" in reply.text

    def test_a_release_the_daemon_cannot_resume_does_not_count(self, tmp_path: Path) -> None:
        """The release is not charged to the crash-resume budget: releasing
        with a budget of zero still resumes the run."""
        from sbxloop.config import Config

        config = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": {"max_resumes_per_item": 0},
            }
        )
        h = Harness(tmp_path, config)
        h.source = HeldSource()
        h.loop.source = h.source
        h.loop.frontend = GateFrontend()
        run_id = park(h)
        dispatch(h.loop, f"release {ITEM}", via="test")
        h.outcomes = ["completed"]
        assert h.loop.tick().outcome == "done"
        assert h.runs[-1] == (run_id, True)

    def test_a_released_run_that_fails_dismisses_the_gate_and_retries_fresh(
        self, tmp_path: Path
    ) -> None:
        h = held_harness(tmp_path)
        run_id = park(h)
        dispatch(h.loop, f"release {ITEM}", by="brett", via="test")
        h.outcomes = ["failed"]
        result = h.loop.tick()
        assert result.outcome == "retry", result
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and gate.state == "dismissed"
        assert gate.detail == "run ended failed"
        assert frontend(h).gates_resolved == [(run_id, "dismissed", "brett")]
        item = h.dstore.get(ITEM)
        assert item is not None and item.state == "queued" and item.run_id is None

    def test_the_boot_reconcile_leaves_a_released_hold_alone(self, tmp_path: Path) -> None:
        """An `approving` publish gate is a queued item with its run pinned,
        not an interrupted approval: reopening it would double-release."""
        h = held_harness(tmp_path)
        run_id = park(h)
        dispatch(h.loop, f"release {ITEM}", via="test")
        h.loop._reconcile_gates()
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and gate.state == "approving"
        assert not [n for n in frontend(h).notices if n.kind == "gate.merge_failed"]
        h.outcomes = ["completed"]
        assert h.loop.tick().outcome == "done"


class TestDrop:
    def test_abandon_drops_the_held_result(self, tmp_path: Path) -> None:
        h = held_harness(tmp_path)
        run_id = park(h)
        h.loop.abandon_item(ITEM, "not wanted after all")
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and gate.state == "dismissed"
        assert frontend(h).gates_resolved == [(run_id, "dismissed", None)]
        (notice,) = [n for n in frontend(h).notices if n.kind == "gate.dismissed"]
        assert "dropped unpublished" in notice.text
        item = h.dstore.get(ITEM)
        assert item is not None and item.state == "failed"
        with pytest.raises(ValueError, match="dropped"):
            h.loop.approve_merge(ITEM)


class TestStore:
    def test_a_pre_kind_gate_table_upgrades_in_place(self, tmp_path: Path) -> None:
        import sqlite3

        from sbxloop.daemon.store import DaemonStore

        db = tmp_path / "state.db"
        store = DaemonStore(db)
        store.create_merge_gate(
            "r1", "gh:issue:7", "o/r", 9, "https://x/pull/9", None, [], "t", 1.0
        )
        store.close()
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE daemon_merge_gates DROP COLUMN kind")
        conn.commit()
        conn.close()
        store = DaemonStore(db)
        try:
            gate = store.merge_gate_for("r1")
            assert gate is not None and gate.kind == "merge"
            store.create_merge_gate(
                "r2", "gh:issue:8", "", 0, "", None, ["U1"], "t2", 2.0, kind="publish"
            )
            held = store.merge_gate_for("gh:issue:8")
            assert held is not None and held.kind == "publish"
            assert [g.kind for g in store.open_merge_gates()] == ["merge", "publish"]
        finally:
            store.close()

    def test_resume_for_release_only_moves_a_gated_item(self, tmp_path: Path) -> None:
        from sbxloop.daemon.store import DaemonStore

        store = DaemonStore(tmp_path / "state.db")
        try:
            store.upsert_new(gh_item("1", kind="workload"), 1.0)
            with pytest.raises(ValueError, match="not pinned"):
                store.resume_for_release(ITEM, "r1", 2.0, "brett")
            store.mark_claiming(ITEM, "tok", 2.0)
            store.mark_claimed(ITEM, 2.0)
            store.mark_running(ITEM, "r1", 3.0)
            with pytest.raises(ValueError, match="only a held item"):
                store.resume_for_release(ITEM, "r1", 4.0, "brett")
        finally:
            store.close()
