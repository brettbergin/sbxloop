"""SQLite state store: runs, tasks, phase attempts, events.

One WAL-mode database at ``<state_dir>/state.db``. A checkpoint row is
written after every state transition, which is what makes ``resume`` safe:
a phase whose result was never committed is simply re-run from its start.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from sbxloop.engine.model import (
    PlanModel,
    RunRecord,
    RunState,
    TaskRecord,
    TaskSpec,
    TaskState,
)
from sbxloop.errors import StateError
from sbxloop_worker.protocol import Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    outcome    TEXT NOT NULL,
    state      TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    workspace  TEXT,
    mounted    INTEGER NOT NULL DEFAULT 0,
    kept_reason TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    run_id     TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    order_idx  INTEGER NOT NULL,
    state      TEXT NOT NULL,
    spec_json  TEXT NOT NULL,
    plan_json  TEXT,
    revisions  INTEGER NOT NULL DEFAULT 0,
    replans    INTEGER NOT NULL DEFAULT 0,
    last_feedback TEXT NOT NULL DEFAULT '',
    session_id TEXT,
    PRIMARY KEY (run_id, task_id)
);
CREATE TABLE IF NOT EXISTS phase_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    task_id    TEXT,
    phase      TEXT NOT NULL,
    attempt    INTEGER NOT NULL,
    status     TEXT NOT NULL,
    output_json TEXT,
    started_at REAL NOT NULL,
    ended_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL,
    ts        REAL NOT NULL,
    type      TEXT NOT NULL,
    job_id    TEXT,
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events (run_id, seq);
"""

# Columns added after 0.2.0; applied idempotently so existing state
# databases upgrade in place on open.
_RUNS_MIGRATIONS = (
    ("workspace", "ALTER TABLE runs ADD COLUMN workspace TEXT"),
    ("mounted", "ALTER TABLE runs ADD COLUMN mounted INTEGER NOT NULL DEFAULT 0"),
    ("kept_reason", "ALTER TABLE runs ADD COLUMN kept_reason TEXT"),
)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(runs)")}
        for column, ddl in _RUNS_MIGRATIONS:
            if column not in existing:
                self._conn.execute(ddl)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- runs --------------------------------------------------------------

    def create_run(self, run_id: str, outcome: str, config_json: str = "{}") -> RunRecord:
        now = time.time()
        try:
            self._conn.execute(
                "INSERT INTO runs (run_id, outcome, state, config_json, created_at, updated_at)"
                " VALUES (?, ?, 'created', ?, ?, ?)",
                (run_id, outcome, config_json, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise StateError(f"run {run_id} already exists") from exc
        self._conn.commit()
        return RunRecord(
            run_id=run_id, outcome=outcome, state="created", created_at=now, updated_at=now
        )

    def set_run_state(self, run_id: str, state: RunState) -> None:
        cursor = self._conn.execute(
            "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
            (state, time.time(), run_id),
        )
        if cursor.rowcount == 0:
            raise StateError(f"unknown run {run_id}")
        self._conn.commit()

    def set_run_workspace(self, run_id: str, workspace: Path, mounted: bool) -> None:
        cursor = self._conn.execute(
            "UPDATE runs SET workspace = ?, mounted = ?, updated_at = ? WHERE run_id = ?",
            (str(workspace), int(mounted), time.time(), run_id),
        )
        if cursor.rowcount == 0:
            raise StateError(f"unknown run {run_id}")
        self._conn.commit()

    def set_run_kept(self, run_id: str, reason: str | None) -> None:
        """Mark a run's sandboxes as deliberately kept (``"debug"``,
        ``"manual"``), or clear the marker with None. ``sandbox prune``
        excludes kept runs unless explicitly told otherwise."""
        cursor = self._conn.execute(
            "UPDATE runs SET kept_reason = ? WHERE run_id = ?",
            (reason, run_id),
        )
        if cursor.rowcount == 0:
            raise StateError(f"unknown run {run_id}")
        self._conn.commit()

    @staticmethod
    def _run_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            outcome=row["outcome"],
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            workspace=Path(row["workspace"]) if row["workspace"] else None,
            mounted=bool(row["mounted"]),
            kept_reason=row["kept_reason"],
        )

    def get_run(self, run_id: str) -> RunRecord:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise StateError(f"unknown run {run_id}")
        return self._run_record(row)

    def get_run_config(self, run_id: str) -> str:
        """The config JSON persisted at run creation. ``'{}'`` means nothing
        was persisted (rows from versions that predate config storage)."""
        row = self._conn.execute(
            "SELECT config_json FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"unknown run {run_id}")
        return str(row["config_json"])

    def list_runs(self) -> list[RunRecord]:
        rows = self._conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        return [self._run_record(row) for row in rows]

    # -- tasks -------------------------------------------------------------

    def save_tasks(self, run_id: str, specs: list[TaskSpec]) -> None:
        for index, spec in enumerate(specs):
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks (run_id, task_id, order_idx, state, spec_json)"
                " VALUES (?, ?, ?, 'pending', ?)",
                (run_id, spec.id, index, spec.model_dump_json()),
            )
        self._conn.commit()

    def update_task(self, run_id: str, task: TaskRecord) -> None:
        cursor = self._conn.execute(
            "UPDATE tasks SET state = ?, plan_json = ?, revisions = ?, replans = ?,"
            " last_feedback = ?, session_id = ? WHERE run_id = ? AND task_id = ?",
            (
                task.state,
                task.plan.model_dump_json() if task.plan else None,
                task.revisions,
                task.replans,
                task.last_feedback,
                task.session_id,
                run_id,
                task.spec.id,
            ),
        )
        if cursor.rowcount == 0:
            raise StateError(f"unknown task {task.spec.id} in run {run_id}")
        self._conn.commit()

    def get_tasks(self, run_id: str) -> list[TaskRecord]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY order_idx", (run_id,)
        ).fetchall()
        records: list[TaskRecord] = []
        for row in rows:
            state: TaskState = row["state"]
            records.append(
                TaskRecord(
                    spec=TaskSpec.model_validate_json(row["spec_json"]),
                    state=state,
                    revisions=row["revisions"],
                    replans=row["replans"],
                    last_feedback=row["last_feedback"],
                    session_id=row["session_id"],
                    plan=(
                        PlanModel.model_validate_json(row["plan_json"])
                        if row["plan_json"]
                        else None
                    ),
                )
            )
        return records

    # -- phase attempts ----------------------------------------------------

    def record_phase(
        self,
        run_id: str,
        phase: str,
        *,
        task_id: str | None,
        attempt: int,
        status: str,
        output_json: str | None,
        started_at: float,
    ) -> None:
        self._conn.execute(
            "INSERT INTO phase_attempts"
            " (run_id, task_id, phase, attempt, status, output_json, started_at, ended_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, task_id, phase, attempt, status, output_json, started_at, time.time()),
        )
        self._conn.commit()

    def phase_attempts(self, run_id: str, task_id: str | None = None) -> list[sqlite3.Row]:
        if task_id is None:
            return list(
                self._conn.execute(
                    "SELECT * FROM phase_attempts WHERE run_id = ? ORDER BY id", (run_id,)
                )
            )
        return list(
            self._conn.execute(
                "SELECT * FROM phase_attempts WHERE run_id = ? AND task_id = ? ORDER BY id",
                (run_id, task_id),
            )
        )

    # -- events ------------------------------------------------------------

    def last_event_ts(self, run_id: str) -> float | None:
        """Timestamp of the run's most recent persisted event, if any.

        Every bus event (heartbeats included) is persisted, so this is the
        best liveness signal available for a run whose recorded state says
        non-terminal but whose process may be long dead.
        """
        row = self._conn.execute(
            "SELECT MAX(ts) AS ts FROM events WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row["ts"] if row and row["ts"] is not None else None

    def append_event(self, event: Event) -> None:
        self._conn.execute(
            "INSERT INTO events (run_id, ts, type, job_id, data_json) VALUES (?, ?, ?, ?, ?)",
            (event.run_id, event.ts, event.type, event.job_id, json.dumps(event.data)),
        )
        self._conn.commit()

    def events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        type_prefix: str | None = None,
    ) -> Iterator[tuple[int, Event]]:
        query = "SELECT * FROM events WHERE run_id = ? AND seq > ?"
        params: list[object] = [run_id, after_seq]
        if type_prefix:
            query += " AND type LIKE ?"
            params.append(type_prefix + "%")
        query += " ORDER BY seq"
        for row in self._conn.execute(query, params):
            yield (
                row["seq"],
                Event(
                    ts=row["ts"],
                    run_id=row["run_id"],
                    job_id=row["job_id"],
                    type=row["type"],
                    data=json.loads(row["data_json"]),
                ),
            )
