"""SQLite state store: runs, tasks, phase attempts, events.

One WAL-mode database at ``<state_dir>/state.db``. A checkpoint row is
written after every state transition, which is what makes ``resume`` safe:
a phase whose result was never committed is simply re-run from its start.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from sbxloop.engine.model import (
    TERMINAL_RUN_STATES,
    RunRecord,
    RunState,
    TaskRecord,
    TaskSpec,
)
from sbxloop.errors import StateError
from sbxloop_worker.protocol import Event, EventTypes, Usage

# Per-run counters the pipeline spends; the only columns `bump_run_counter`
# may touch, so a caller cannot increment an arbitrary column by name.
RUN_COUNTERS: frozenset[str] = frozenset({"review_rounds", "ci_rounds", "update_attempts"})

# Run states written before the pipeline existed, remapped at read time so a
# pre-1.0 state database still lists and resumes: both were "the task graph
# is being worked", which `building` now names.
_LEGACY_RUN_STATES: dict[str, RunState] = {"running": "building", "finalizing": "building"}

# Event types that live on the bus only. Streaming deltas are pure UI
# telemetry -- the full `agent.message` carries the same text, and resume
# never reads deltas -- so persisting one row per chunk is pure overhead.
EPHEMERAL_EVENT_TYPES: frozenset[str] = frozenset({EventTypes.AGENT_MESSAGE_DELTA})


def _is_ephemeral(event: Event) -> bool:
    return event.type in EPHEMERAL_EVENT_TYPES


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
    kept_reason TEXT,
    user_guidance TEXT NOT NULL DEFAULT '[]',
    reason     TEXT,
    stage      TEXT,
    pr_number  INTEGER,
    pr_url     TEXT,
    pr_node_id TEXT,
    branch     TEXT,
    head_sha   TEXT,
    review_rounds INTEGER NOT NULL DEFAULT 0,
    ci_rounds  INTEGER NOT NULL DEFAULT 0,
    update_attempts INTEGER NOT NULL DEFAULT 0,
    update_head TEXT,
    last_verdict TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    run_id     TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    order_idx  INTEGER NOT NULL,
    state      TEXT NOT NULL,
    spec_json  TEXT NOT NULL,
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
    ended_at   REAL NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    turns      INTEGER
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

# Columns added after 0.2.0; applied idempotently per table so existing
# state databases upgrade in place on open.
_MIGRATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "runs": (
        ("workspace", "ALTER TABLE runs ADD COLUMN workspace TEXT"),
        ("mounted", "ALTER TABLE runs ADD COLUMN mounted INTEGER NOT NULL DEFAULT 0"),
        ("kept_reason", "ALTER TABLE runs ADD COLUMN kept_reason TEXT"),
        ("reason", "ALTER TABLE runs ADD COLUMN reason TEXT"),
        (
            "user_guidance",
            "ALTER TABLE runs ADD COLUMN user_guidance TEXT NOT NULL DEFAULT '[]'",
        ),
        ("stage", "ALTER TABLE runs ADD COLUMN stage TEXT"),
        ("pr_number", "ALTER TABLE runs ADD COLUMN pr_number INTEGER"),
        ("pr_url", "ALTER TABLE runs ADD COLUMN pr_url TEXT"),
        ("pr_node_id", "ALTER TABLE runs ADD COLUMN pr_node_id TEXT"),
        ("branch", "ALTER TABLE runs ADD COLUMN branch TEXT"),
        ("head_sha", "ALTER TABLE runs ADD COLUMN head_sha TEXT"),
        (
            "review_rounds",
            "ALTER TABLE runs ADD COLUMN review_rounds INTEGER NOT NULL DEFAULT 0",
        ),
        ("ci_rounds", "ALTER TABLE runs ADD COLUMN ci_rounds INTEGER NOT NULL DEFAULT 0"),
        (
            "update_attempts",
            "ALTER TABLE runs ADD COLUMN update_attempts INTEGER NOT NULL DEFAULT 0",
        ),
        ("update_head", "ALTER TABLE runs ADD COLUMN update_head TEXT"),
        ("last_verdict", "ALTER TABLE runs ADD COLUMN last_verdict TEXT"),
    ),
    "phase_attempts": (
        ("input_tokens", "ALTER TABLE phase_attempts ADD COLUMN input_tokens INTEGER"),
        ("output_tokens", "ALTER TABLE phase_attempts ADD COLUMN output_tokens INTEGER"),
        (
            "cache_read_tokens",
            "ALTER TABLE phase_attempts ADD COLUMN cache_read_tokens INTEGER",
        ),
        (
            "cache_write_tokens",
            "ALTER TABLE phase_attempts ADD COLUMN cache_write_tokens INTEGER",
        ),
        ("turns", "ALTER TABLE phase_attempts ADD COLUMN turns INTEGER"),
    ),
}


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # One connection is shared by every caller (check_same_thread=False),
        # so writers serialise here rather than relying on SQLite's own
        # per-statement mutex: `append_event_if_state` holds an explicit
        # BEGIN IMMEDIATE across a check and an insert, and any other
        # thread committing mid-transaction would end it early. Reentrant
        # so a guarded method may call another.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # WAL + NORMAL: commits no longer fsync per insert, and a crash can
        # only lose the tail of the WAL (not corrupt the database).
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        for table, migrations in _MIGRATIONS.items():
            existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in migrations:
                if column not in existing:
                    self._conn.execute(ddl)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- runs --------------------------------------------------------------

    def create_run(self, run_id: str, outcome: str, config_json: str = "{}") -> RunRecord:
        with self._lock:
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
        """Move the run to ``state``. A non-terminal state is also recorded
        as the run's ``stage``; a terminal one leaves ``stage`` alone, so a
        failed or blocked run still knows where a resume should re-enter."""
        with self._lock:
            if state in TERMINAL_RUN_STATES:
                cursor = self._conn.execute(
                    "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                    (state, time.time(), run_id),
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE runs SET state = ?, stage = ?, updated_at = ? WHERE run_id = ?",
                    (state, state, time.time(), run_id),
                )
            if cursor.rowcount == 0:
                raise StateError(f"unknown run {run_id}")
            self._conn.commit()

    def set_run_reason(self, run_id: str, reason: str | None) -> None:
        """Record why a run stopped where it did (a budget, a refusal)."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET reason = ?, updated_at = ? WHERE run_id = ?",
                (reason, time.time(), run_id),
            )
            if cursor.rowcount == 0:
                raise StateError(f"unknown run {run_id}")
            self._conn.commit()

    def touch_run(self, run_id: str) -> None:
        """Refresh ``updated_at`` without changing anything else — the
        liveness signal for a run idling on a CI wait, where no job runs and
        so no event is persisted."""
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET updated_at = ? WHERE run_id = ?", (time.time(), run_id)
            )
            self._conn.commit()

    def set_run_pr(
        self,
        run_id: str,
        *,
        number: int,
        url: str,
        branch: str,
        head_sha: str | None,
        node_id: str | None = None,
    ) -> None:
        """Record the delivered pull request. Called on every delivery: the
        number/url/branch are stable across rounds, ``head_sha`` moves, and a
        re-delivery clears ``update_head`` (an update marker outliving the
        head it was requested at would park the landing stage forever)."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET pr_number = ?, pr_url = ?, branch = ?, head_sha = ?,"
                " pr_node_id = COALESCE(?, pr_node_id), update_head = NULL, updated_at = ?"
                " WHERE run_id = ?",
                (number, url, branch, head_sha, node_id, time.time(), run_id),
            )
            if cursor.rowcount == 0:
                raise StateError(f"unknown run {run_id}")
            self._conn.commit()

    def set_run_head(self, run_id: str, head_sha: str) -> None:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET head_sha = ?, updated_at = ? WHERE run_id = ?",
                (head_sha, time.time(), run_id),
            )
            if cursor.rowcount == 0:
                raise StateError(f"unknown run {run_id}")
            self._conn.commit()

    def set_run_verdict(self, run_id: str, verdict: str) -> None:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET last_verdict = ?, updated_at = ? WHERE run_id = ?",
                (verdict, time.time(), run_id),
            )
            if cursor.rowcount == 0:
                raise StateError(f"unknown run {run_id}")
            self._conn.commit()

    def set_update_head(self, run_id: str, head_sha: str | None) -> None:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET update_head = ?, updated_at = ? WHERE run_id = ?",
                (head_sha, time.time(), run_id),
            )
            if cursor.rowcount == 0:
                raise StateError(f"unknown run {run_id}")
            self._conn.commit()

    def bump_run_counter(self, run_id: str, counter: str) -> int:
        """Spend one unit of a per-run budget; returns the new count."""
        if counter not in RUN_COUNTERS:
            raise StateError(f"not a run counter: {counter!r}")
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE runs SET {counter} = {counter} + 1, updated_at = ? WHERE run_id = ?",  # nosec B608 — column name is whitelisted above
                (time.time(), run_id),
            )
            if cursor.rowcount == 0:
                raise StateError(f"unknown run {run_id}")
            self._conn.commit()
            row = self._conn.execute(
                f"SELECT {counter} AS n FROM runs WHERE run_id = ?",  # nosec B608
                (run_id,),
            ).fetchone()
            return int(row["n"])

    def set_run_workspace(self, run_id: str, workspace: Path, mounted: bool) -> None:
        with self._lock:
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
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET kept_reason = ? WHERE run_id = ?",
                (reason, run_id),
            )
            if cursor.rowcount == 0:
                raise StateError(f"unknown run {run_id}")
            self._conn.commit()

    def append_run_guidance(self, run_id: str, text: str) -> None:
        """Append one standing chat-guidance entry (a ``steer_run`` verdict)
        to the run. Persisted so a resumed run re-applies it to its prompts."""
        with self._lock:
            row = self._conn.execute(
                "SELECT user_guidance FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise StateError(f"unknown run {run_id}")
            items = json.loads(row["user_guidance"] or "[]")
            items.append(text)
            self._conn.execute(
                "UPDATE runs SET user_guidance = ?, updated_at = ? WHERE run_id = ?",
                (json.dumps(items), time.time(), run_id),
            )
            self._conn.commit()

    def get_run_guidance(self, run_id: str) -> list[str]:
        row = self._conn.execute(
            "SELECT user_guidance FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise StateError(f"unknown run {run_id}")
        items = json.loads(row["user_guidance"] or "[]")
        return [str(item) for item in items]

    @staticmethod
    def _run_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            outcome=row["outcome"],
            state=_LEGACY_RUN_STATES.get(row["state"], row["state"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            workspace=Path(row["workspace"]) if row["workspace"] else None,
            mounted=bool(row["mounted"]),
            kept_reason=row["kept_reason"],
            reason=row["reason"],
            stage=row["stage"],
            pr_number=row["pr_number"],
            pr_url=row["pr_url"],
            pr_node_id=row["pr_node_id"],
            branch=row["branch"],
            head_sha=row["head_sha"],
            review_rounds=int(row["review_rounds"] or 0),
            ci_rounds=int(row["ci_rounds"] or 0),
            update_attempts=int(row["update_attempts"] or 0),
            update_head=row["update_head"],
            last_verdict=row["last_verdict"],
        )

    def non_terminal_runs(self) -> list[RunRecord]:
        """Runs still recorded as in flight (anything not in
        ``TERMINAL_RUN_STATES``), oldest-updated first.

        Callers must exclude the run genuinely executing in-process before
        reconciling anything returned here.
        """
        rows = self._conn.execute("SELECT * FROM runs ORDER BY updated_at ASC").fetchall()
        return [self._run_record(row) for row in rows if row["state"] not in TERMINAL_RUN_STATES]

    def reconcile_run(self, run_id: str, state: RunState, reason: str) -> None:
        """Force a run to a terminal state and record why, durably.

        Only the ``runs`` row is written: events and phase_attempts are never
        touched, so historical chronology is preserved. Reconciliation
        chronology events are appended separately by callers via
        :meth:`append_event`.
        """
        if state not in TERMINAL_RUN_STATES:
            raise StateError(f"run state {state!r} is not terminal")
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET state = ?, reason = ?, updated_at = ? WHERE run_id = ?",
                (state, reason, time.time(), run_id),
            )
            if cursor.rowcount == 0:
                raise StateError(f"unknown run {run_id}")
            self._conn.commit()

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
        with self._lock:
            for index, spec in enumerate(specs):
                self._conn.execute(
                    "INSERT OR REPLACE INTO tasks (run_id, task_id, order_idx, state, spec_json)"
                    " VALUES (?, ?, ?, 'pending', ?)",
                    (run_id, spec.id, index, spec.model_dump_json()),
                )
            self._conn.commit()

    def append_task(self, run_id: str, spec: TaskSpec) -> TaskRecord:
        """Add one task after every existing one (a fix round). ``save_tasks``
        numbers from zero and would collide with the graph already saved."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(order_idx), -1) + 1 AS next FROM tasks WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            self._conn.execute(
                "INSERT INTO tasks (run_id, task_id, order_idx, state, spec_json)"
                " VALUES (?, ?, ?, 'pending', ?)",
                (run_id, spec.id, int(row["next"]), spec.model_dump_json()),
            )
            self._conn.commit()
        return TaskRecord(spec=spec)

    def update_task(self, run_id: str, task: TaskRecord) -> None:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE tasks SET state = ?, revisions = ?, replans = ?,"
                " last_feedback = ?, session_id = ? WHERE run_id = ? AND task_id = ?",
                (
                    task.state,
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
            records.append(
                TaskRecord(
                    spec=TaskSpec.model_validate_json(row["spec_json"]),
                    state=row["state"],
                    revisions=row["revisions"],
                    replans=row["replans"],
                    last_feedback=row["last_feedback"],
                    session_id=row["session_id"],
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
        usage: Usage | None = None,
        turns: int | None = None,
    ) -> None:
        u = usage or Usage()
        with self._lock:
            self._conn.execute(
                "INSERT INTO phase_attempts"
                " (run_id, task_id, phase, attempt, status, output_json, started_at, ended_at,"
                "  input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, turns)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    task_id,
                    phase,
                    attempt,
                    status,
                    output_json,
                    started_at,
                    time.time(),
                    u.input_tokens,
                    u.output_tokens,
                    u.cache_read_tokens,
                    u.cache_write_tokens,
                    turns,
                ),
            )
            self._conn.commit()

    def latest_phase_output(self, run_id: str, task_id: str, phase: str) -> str | None:
        """output_json of the task's most recent attempt of ``phase``."""
        row = self._conn.execute(
            "SELECT output_json FROM phase_attempts"
            " WHERE run_id = ? AND task_id = ? AND phase = ?"
            " ORDER BY id DESC LIMIT 1",
            (run_id, task_id, phase),
        ).fetchone()
        return row["output_json"] if row is not None else None

    def latest_phase_attempt(self, run_id: str, task_id: str, phase: str) -> sqlite3.Row | None:
        """The task's most recent attempt row of ``phase`` (attempt, status,
        output_json...), or None if it never ran."""
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM phase_attempts"
            " WHERE run_id = ? AND task_id = ? AND phase = ?"
            " ORDER BY id DESC LIMIT 1",
            (run_id, task_id, phase),
        ).fetchone()
        return row

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
        if _is_ephemeral(event):
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (run_id, ts, type, job_id, data_json) VALUES (?, ?, ?, ?, ?)",
                (event.run_id, event.ts, event.type, event.job_id, json.dumps(event.data)),
            )
            self._conn.commit()

    def append_event_if_state(self, event: Event, states: frozenset[str] | set[str]) -> bool:
        """Append ``event`` only if the run is currently in one of ``states``,
        checking and inserting under one write lock.

        This is the gc claim: a sweep in another process must not take a
        directory that a resume has just moved back into flight, and the
        resume must not slip in between the sweep's check and its marker.
        ``BEGIN IMMEDIATE`` holds the database write lock across both, so
        the state check and the marker are one atomic step against every
        other writer on the same state DB. Returns whether it was appended.
        """
        if _is_ephemeral(event):
            return False
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT state FROM runs WHERE run_id = ?", (event.run_id,)
                ).fetchone()
                if row is None or row["state"] not in states:
                    self._conn.rollback()
                    return False
                self._conn.execute(
                    "INSERT INTO events (run_id, ts, type, job_id, data_json)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (event.run_id, event.ts, event.type, event.job_id, json.dumps(event.data)),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            return True

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
