"""A week with enough runs in it for a distribution to have a shape."""

from __future__ import annotations

import random
import time

from sbxloop.engine.store import StateStore
from sbxloop.paths import SbxloopHome
from sbxloop_worker.protocol import Usage

DAY = 86400.0


def seed_many(home: SbxloopHome, *, count: int = 40, seed: int = 7) -> None:
    """`count` runs spread over the window, costs drawn from a spread wide
    enough that a histogram has something to show."""
    rng = random.Random(seed)
    now = time.time()
    store = StateStore(home.state_db)
    for index in range(count):
        run_id = f"r_many{index:02d}"
        turns = int(abs(rng.gauss(60, 30))) + 5
        active = turns * rng.uniform(8, 25)
        elapsed = active * rng.uniform(1.1, 6.0)
        created = now - rng.uniform(0.2, 6.5) * DAY
        state = "merged" if index % 5 else "failed"
        store.create_run(run_id, f"outcome {run_id}", kind="code")
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
            usage=Usage(input_tokens=turns * 1000, output_tokens=0, cache_read_tokens=0),
        )
        store._conn.execute(
            "UPDATE phase_attempts SET ended_at=? WHERE run_id=?", (created + active, run_id)
        )
    store._conn.commit()
    store.close()


__all__ = ["seed_many"]
