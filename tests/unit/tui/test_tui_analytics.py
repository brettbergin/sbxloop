"""The Overview's fold: a window of runs turned into the few numbers that
answer "is this performing well". Three things it must get right, each of
which was a wrong answer first: active is not elapsed, turns are the cost,
and a cancelled run is not a failure."""

from __future__ import annotations

from pathlib import Path

from sbxloop.engine.store import StateStore
from sbxloop.tui.analytics import Cache, Lane, compute, fold
from sbxloop_worker.protocol import Usage

NOW = 1_800_000_000.0
DAY = 86400.0


def seed(db: Path) -> StateStore:
    """A week with both run kinds, a parked run, a cancelled one and two
    failures that share a cause."""
    store = StateStore(db)
    plan = [
        # run_id,    kind,       state,       started days ago, active, elapsed, turns
        ("r_big", "code", "merged", 6.0, 4000.0, 40000.0, 478),
        ("r_ok", "code", "merged", 5.0, 1200.0, 1500.0, 90),
        ("r_park", "code", "merged", 4.0, 600.0, 50000.0, 60),
        ("r_fail1", "code", "failed", 3.0, 300.0, 400.0, 20),
        ("r_fail2", "code", "failed", 2.0, 200.0, 300.0, 15),
        ("r_cancel", "code", "cancelled", 2.0, 100.0, 150.0, 5),
        ("r_wl", "workload", "completed", 1.0, 300.0, 400.0, 31),
    ]
    for run_id, kind, state, days, active, elapsed, turns in plan:
        created = NOW - days * DAY
        store.create_run(run_id, f"outcome for {run_id}", kind=kind)
        store._conn.execute(
            "UPDATE runs SET state=?, created_at=?, updated_at=?, reason=? WHERE run_id=?",
            (
                state,
                created,
                created + elapsed,
                "github op raw.api failed: 502" if state == "failed" else None,
                run_id,
            ),
        )
        store.record_phase(
            run_id,
            "build",
            task_id="t1",
            attempt=1,
            status="ok",
            output_json="{}",
            started_at=created,
            turns=turns,
            usage=Usage(input_tokens=turns * 1000, output_tokens=0, cache_read_tokens=turns * 5000),
        )
        store._conn.execute(
            "UPDATE phase_attempts SET ended_at=? WHERE run_id=?", (created + active, run_id)
        )
    store._conn.commit()
    return store


def test_active_is_not_elapsed_and_parked_is_named(tmp_path: Path) -> None:
    """The distinction the whole screen turns on: a run parked on a human
    has elapsed time it never spent working."""
    store = seed(tmp_path / "s.db")
    try:
        a = compute(store, now=NOW, window_s=7 * DAY)
    finally:
        store.close()
    code = a.lane("code")
    assert code.runs == 6
    assert code.active == 6400.0, "the sum of the phase attempts' own clocks"
    assert code.elapsed == 92350.0
    assert code.parked == code.elapsed - code.active
    assert round(code.parked_share, 2) == 0.93, "most of elapsed was waiting on a human"
    # r_park did 10 minutes of work and waited most of a day.
    parked = {r.run_id: r for r in a.longest_parked}
    assert parked["r_park"].active == 600.0
    assert parked["r_park"].parked == 49400.0
    assert a.longest_parked[0].run_id == "r_park", "ranked by wait, not by work"


def test_a_cancelled_run_is_a_decision_not_a_failure(tmp_path: Path) -> None:
    store = seed(tmp_path / "s.db")
    try:
        a = compute(store, now=NOW, window_s=7 * DAY)
    finally:
        store.close()
    code = a.lane("code")
    assert (code.landed, code.failed, code.cancelled) == (3, 2, 1)
    assert code.judged == 5, "the cancelled run is out of the denominator"
    assert code.ok_rate == 3 / 5
    assert Lane("code").ok_rate is None, "no judged runs is not 0%"


def test_kinds_stay_apart(tmp_path: Path) -> None:
    """Code and workload runs differ by an order of magnitude; blending
    them would describe neither."""
    store = seed(tmp_path / "s.db")
    try:
        a = compute(store, now=NOW, window_s=7 * DAY)
    finally:
        store.close()
    assert set(a.lanes) == {"code", "workload"}
    assert a.lane("workload").runs == 1 and a.lane("workload").turns == 31
    assert a.lane("code").turns == 668
    assert a.total.runs == 7 and a.total.turns == 699, "the headline still totals"
    assert a.lane("nothing-like-this").runs == 0, "an absent kind is an empty lane"


def test_turns_lead_and_the_costliest_run_is_findable(tmp_path: Path) -> None:
    store = seed(tmp_path / "s.db")
    try:
        a = compute(store, now=NOW, window_s=7 * DAY)
    finally:
        store.close()
    assert a.costliest[0].run_id == "r_big"
    assert a.costliest[0].turns == 478
    code = a.lane("code")
    assert round(code.turns_per_run, 1) == round(668 / 6, 1)
    assert code.tokens_per_turn == 1000.0, "the fixed context each turn re-sends"


def test_failures_are_grouped_by_cause(tmp_path: Path) -> None:
    """Two failures with the same head reason are one row, so a common
    cause is visible instead of two one-offs."""
    store = seed(tmp_path / "s.db")
    try:
        a = compute(store, now=NOW, window_s=7 * DAY)
    finally:
        store.close()
    assert a.failures == (("github op raw.api failed", 2),)


def test_the_window_buckets_and_excludes_what_is_outside_it(tmp_path: Path) -> None:
    store = seed(tmp_path / "s.db")
    try:
        week = compute(store, now=NOW, window_s=7 * DAY, buckets=7)
        day = compute(store, now=NOW, window_s=1 * DAY, buckets=4)
    finally:
        store.close()
    assert sum(week.daily["runs"]) == 7
    assert len(week.daily["runs"]) == 7
    assert sum(day.daily["runs"]) == day.total.runs == 1, "only the workload run is inside a day"
    assert not day.empty and compute_empty()


def compute_empty() -> bool:
    return fold([], [], since=0.0, until=1.0).empty


def test_phases_rank_by_time(tmp_path: Path) -> None:
    store = seed(tmp_path / "s.db")
    try:
        a = compute(store, now=NOW, window_s=7 * DAY)
    finally:
        store.close()
    assert [p.phase for p in a.phases] == ["build"]
    assert a.phases[0].attempts == 7
    assert a.active_seconds == sum(p.seconds for p in a.phases)


def test_the_cache_serves_one_window_per_ttl() -> None:
    cache = Cache(ttl_s=10.0)
    assert cache.stale(100.0) and cache.get() is None
    value = fold([], [], since=0.0, until=1.0)
    cache.put(value, 100.0)
    assert not cache.stale(105.0) and cache.get() is value
    assert cache.stale(110.0), "past the ttl it is recomputed"
    cache.clear()
    assert cache.stale(105.0)
