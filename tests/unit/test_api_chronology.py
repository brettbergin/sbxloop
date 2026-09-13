"""The public chronology: engine events projected exactly once with the
watermark in the same transaction, daemon events recorded, history pruned
with the cursor below it refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.api.chronology import (
    PRUNED_KEY,
    WATERMARK_KEY,
    Chronology,
    event_id,
    parse_event_id,
)
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop_worker.protocol import Event


@pytest.fixture
def stores(tmp_path: Path) -> tuple[StateStore, DaemonStore]:
    path = tmp_path / "state.db"
    return StateStore(path), DaemonStore(path)


def _engine_events(store: StateStore, run_id: str, n: int) -> None:
    store.create_run(run_id, "outcome")
    for i in range(n):
        store.append_event(Event(ts=100.0 + i, run_id=run_id, type="worker.stdout", data={"i": i}))


class TestProjection:
    def test_engine_events_are_projected_once_in_order(
        self, stores: tuple[StateStore, DaemonStore]
    ) -> None:
        store, dstore = stores
        chron = Chronology(dstore)
        assert chron.watermark() is None and chron.lag() == 0
        _engine_events(store, "r1", 3)
        assert chron.lag() == 3
        assert chron.project(now=500.0) == 3
        assert chron.lag() == 0 and chron.project(now=501.0) == 0
        rows = chron.read()
        assert [r.type for r in rows] == ["worker.stdout"] * 3
        assert [r.source_seq for r in rows] == [1, 2, 3]
        assert [r.data["i"] for r in rows] == [0, 1, 2]
        assert rows[0].occurred_at == 100.0 and rows[0].recorded_at == 500.0
        assert rows[0].run_id == "r1" and rows[0].actor is None
        assert dstore.get_value(WATERMARK_KEY) == "3"

    def test_batches_and_a_crash_between_copy_and_watermark(
        self, stores: tuple[StateStore, DaemonStore]
    ) -> None:
        store, dstore = stores
        chron = Chronology(dstore)
        chron.BATCH = 2
        _engine_events(store, "r1", 5)

        def explode(seq: int) -> None:
            raise RuntimeError("crash after copy")

        chron.after_copy = explode
        with pytest.raises(RuntimeError):
            chron.project(now=1.0)
        # Nothing half-projected: the rows and the watermark roll back together.
        assert chron.read() == [] and dstore.get_value(WATERMARK_KEY) is None
        chron.after_copy = lambda seq: None
        assert chron.project(now=2.0) == 5
        assert [r.source_seq for r in chron.read()] == [1, 2, 3, 4, 5]

    def test_daemon_events_interleave_by_sequence(
        self, stores: tuple[StateStore, DaemonStore]
    ) -> None:
        store, dstore = stores
        chron = Chronology(dstore)
        first = chron.record("daemon.notice", 10.0, item_id="gh:issue:1", data={"text": "hi"})
        _engine_events(store, "r1", 1)
        chron.project(now=11.0)
        last = chron.record("run.finished", 12.0, run_id="r1", actor={"kind": "system"})
        rows = chron.read()
        assert [r.seq for r in rows] == [first, first + 1, last]
        assert rows[0].item_id == "gh:issue:1" and rows[0].data == {"text": "hi"}
        assert rows[2].actor == {"kind": "system"} and rows[2].source_seq is None
        assert chron.read(after=first) == rows[1:]
        assert [r.type for r in chron.read(type_prefix="run.")] == ["run.finished"]
        assert [r.seq for r in chron.read(run_id="r1")] == [first + 1, last]
        assert chron.read(limit=1) == rows[:1]


class TestRetention:
    def test_prune_forgets_old_rows_and_expires_cursors_below(
        self, stores: tuple[StateStore, DaemonStore]
    ) -> None:
        _store, dstore = stores
        chron = Chronology(dstore)
        a = chron.record("daemon.notice", 10.0)
        b = chron.record("daemon.notice", 20.0)
        c = chron.record("daemon.notice", 30.0)
        assert chron.prune(before=5.0) == 0 and not chron.expired(0)
        assert chron.prune(before=25.0) == 2
        assert dstore.get_value(PRUNED_KEY) == str(b)
        assert [r.seq for r in chron.read()] == [c]
        # A cursor at the last pruned row is fine: the next event is held.
        assert not chron.expired(b) and not chron.expired(c)
        # One below it would skip `b`; none at all would skip `a` and `b`.
        assert chron.expired(a) and chron.expired(0)
        assert chron.bounds() == (c, b)


def test_event_ids_round_trip() -> None:
    assert event_id(7) == "evt_7" and parse_event_id("evt_7") == 7
    assert parse_event_id("evt_") is None and parse_event_id("op_7") is None
    assert parse_event_id("evt_x1") is None
