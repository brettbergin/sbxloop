"""The engine's state store: runs, tasks, phase attempts, events.

One WAL-mode database at ``<home>/state/state.db``. A checkpoint row is
written after every state transition, which is what makes ``resume`` safe:
a phase whose result was never committed is simply re-run from its start.

The statements are SQLAlchemy now (#539), but the concurrency contract is
the one the hand-written sqlite3 version had, because the daemon, the CLI
and the operator console all write this file at once:

* **One connection, one lock.** ``sbxloop.db.open_engine`` gives a single
  connection shared by every thread, and ``self._lock`` serialises callers
  onto it. Reentrant, because a guarded method may call another.
* **Compare-and-set, never read-then-write.** Every "change a known run"
  method is one UPDATE with the identity in its WHERE clause, and raises on
  a rowcount of zero. Two processes racing lose the CAS; they do not both
  win a read and then overwrite each other.
* **The gc claim holds the write lock across a check and an insert.** See
  :meth:`append_event_if_state`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple, cast

from sqlalchemy import Result, and_, case, func, insert, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sbxloop.db import begin_immediate, ensure_schema, open_engine
from sbxloop.db.engine_models import EventRow, PhaseAttempt, Reconciliation, Run, Task
from sbxloop.engine.model import (
    TERMINAL_RUN_STATES,
    Published,
    RunKind,
    RunRecord,
    RunState,
    TaskOutput,
    TaskRecord,
    TaskSpec,
)
from sbxloop.errors import StateError
from sbxloop_worker.protocol import Event, EventTypes, Usage

# Per-run counters the pipeline spends; the only columns `bump_run_counter`
# may touch, so a caller cannot increment an arbitrary column by name.
RUN_COUNTERS: frozenset[str] = frozenset({"review_rounds", "ci_rounds", "update_attempts"})

# What a `reconciliations` row is about. The table carries six concerns and
# told them apart by the sign of `round`; `kind` says which out loud. The
# sentinel rounds are still written beside it — the release before this one
# reads them, and a failed deploy restarts that release against the same
# database.
RECONCILE_REVIEW = "review"
RECONCILE_HUMAN = "human"
RECONCILE_ADVISORY = "advisory"
RECONCILE_BOT = "bot"
RECONCILE_CONFIRM = "confirm"
RECONCILE_NOTED = "noted"


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


class PostedRecord(NamedTuple):
    """One review finding as it was posted on the PR, read back from the store.

    ``round`` is the review round that posted it (the phase attempt number).
    ``comment_id``/``thread_id`` are ``None`` for a finding that landed
    in the review body instead of its own thread — see ``body_only``.
    """

    round: int
    anchor: str
    comment_id: int | None = None
    thread_id: str | None = None
    review_id: int | None = None

    @property
    def body_only(self) -> bool:
        return self.comment_id is None


class PhaseAttemptRecord(NamedTuple):
    """One attempt at one phase, as callers read it back.

    A record rather than a model instance or a driver row: these outlive the
    session that read them — the engine folds them after the fact, the
    console renders them in another process — and a detached instance would
    be a trap where a frozen tuple is not.

    ``task_id`` is None for a run-level phase; the usage fields are None for
    attempts recorded before the store counted tokens.
    """

    id: int
    run_id: str
    task_id: str | None
    phase: str
    attempt: int
    status: str
    output_json: str | None
    started_at: float
    ended_at: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    turns: int | None = None

    @property
    def seconds(self) -> float:
        """How long the attempt ran."""
        return self.ended_at - self.started_at


class RunWindowRecord(NamedTuple):
    """One run in an analytics window, with its phase totals folded in.

    ``active`` is the time the run's attempts were actually running, which
    is not ``updated_at - created_at``: a run parked on a merge gate has
    elapsed time it did not spend working.
    """

    run_id: str
    kind: str
    state: str
    reason: str | None
    created_at: float
    updated_at: float
    review_rounds: int
    ci_rounds: int
    turns: int
    tokens: int
    cache: int
    active: float


class PhaseWindowRecord(NamedTuple):
    """One phase in an analytics window, by the attempts that started in it.

    ``retries`` counts attempts past the first — where the loop fights
    itself. ``cache`` is kept apart from ``tokens`` because the ratio
    between them is a per-phase fact: a phase that re-sends a large fixed
    context reads far more than it writes.
    """

    phase: str
    attempts: int
    seconds: float
    turns: int
    tokens: int
    cache: int
    retries: int


class TaskTotalsRecord(NamedTuple):
    """What the tasks of a window's runs cost in rework."""

    tasks: int
    revisions: int
    replans: int
    suspect: int


# FROZEN. Everything below is the body of Alembic revision 0001, and 0001
# has shipped: every deployed database is already stamped at it, so Alembic
# will never run this code against one again. A column added here now
# reaches a fresh install and nothing else, and the field crashes on the
# first query that selects it.
#
# To change the schema, add a revision under db/migrations/versions instead.
# `tests/unit/test_db_schema.py` freezes the shape this produces and will
# fail if it moves.
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
    last_verdict TEXT,
    exhausted  TEXT,
    granted_rounds INTEGER NOT NULL DEFAULT 0,
    pr_title   TEXT,
    credentials TEXT NOT NULL DEFAULT '[]',
    kind       TEXT NOT NULL DEFAULT 'code',
    published  TEXT NOT NULL DEFAULT '[]'
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
    verify_fingerprints TEXT NOT NULL DEFAULT '[]',
    verify_suspect INTEGER NOT NULL DEFAULT 0,
    output_json TEXT,
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
CREATE TABLE IF NOT EXISTS reconciliations (
    run_id     TEXT NOT NULL,
    round      INTEGER NOT NULL,
    anchor     TEXT NOT NULL,
    status     TEXT NOT NULL,
    resolved   INTEGER NOT NULL DEFAULT 0,
    ts         REAL NOT NULL,
    PRIMARY KEY (run_id, round, anchor)
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

# Columns added after 0.2.0 and before revision 0001; applied idempotently
# per table so a pre-0001 state database upgrades in place on open. FROZEN
# for the same reason as _SCHEMA above — a new column goes in a revision.
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
        ("exhausted", "ALTER TABLE runs ADD COLUMN exhausted TEXT"),
        ("pr_title", "ALTER TABLE runs ADD COLUMN pr_title TEXT"),
        (
            "granted_rounds",
            "ALTER TABLE runs ADD COLUMN granted_rounds INTEGER NOT NULL DEFAULT 0",
        ),
        ("credentials", "ALTER TABLE runs ADD COLUMN credentials TEXT NOT NULL DEFAULT '[]'"),
        # Every run before #755 was a developer run: the default IS the
        # migration, so a legacy row reads back as `code` untouched.
        ("kind", "ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'code'"),
        # Where a workload's result went (#759); nothing published before.
        ("published", "ALTER TABLE runs ADD COLUMN published TEXT NOT NULL DEFAULT '[]'"),
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
    "tasks": (
        (
            "verify_fingerprints",
            "ALTER TABLE tasks ADD COLUMN verify_fingerprints TEXT NOT NULL DEFAULT '[]'",
        ),
        (
            "verify_suspect",
            "ALTER TABLE tasks ADD COLUMN verify_suspect INTEGER NOT NULL DEFAULT 0",
        ),
        # A workload task's TaskOutput (#757); NULL for every code task.
        ("output_json", "ALTER TABLE tasks ADD COLUMN output_json TEXT"),
    ),
}


def apply_engine_schema(conn: sqlite3.Connection) -> None:
    """Bring any engine database this project ever wrote to the current shape.

    Creates the five tables from nothing on a fresh file, and adds every
    column released after 0.2.0 that the file is missing — idempotent, so an
    already-current database is untouched. Nothing is backfilled: each added
    column's DDL default is the migration, and the two run states written
    before the pipeline existed are remapped on read, never rewritten.

    This is the whole of the engine side of Alembic revision 0001, which is
    why it takes a bare connection rather than a store: it has to run against
    a database no store could open yet.
    """
    conn.executescript(_SCHEMA)
    for table, migrations in _MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # nosec B608
        for column, ddl in migrations:
            if column not in existing:
                conn.execute(ddl)
    conn.commit()


def _attempt_record(row: PhaseAttempt) -> PhaseAttemptRecord:
    return PhaseAttemptRecord(
        id=row.id,
        run_id=row.run_id,
        task_id=row.task_id,
        phase=row.phase,
        attempt=row.attempt,
        status=row.status,
        output_json=row.output_json,
        started_at=row.started_at,
        ended_at=row.ended_at,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cache_read_tokens=row.cache_read_tokens,
        cache_write_tokens=row.cache_write_tokens,
        turns=row.turns,
    )


def _rowcount(result: Result[Any]) -> int:
    """How many rows a DML statement touched.

    Every compare-and-set in this store turns on it: zero means the WHERE
    clause matched nothing, which is how a caller learns it lost a race
    rather than silently writing nothing.
    """
    return cast("CursorResult[Any]", result).rowcount


def _event_values(event: Event) -> dict[str, object]:
    return {
        "run_id": event.run_id,
        "ts": event.ts,
        "type": event.type,
        "job_id": event.job_id,
        "data_json": json.dumps(event.data),
    }


class StateStore:
    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        """Open the store. ``readonly`` opens the file through a read-only
        URI and runs no schema statement (the operator console's handle; a
        write then fails in SQLite); the file must already exist."""
        self.path = path
        self.readonly = readonly
        # One connection is shared by every caller, so writers serialise
        # here rather than relying on SQLite's own per-statement mutex:
        # `append_event_if_state` holds an explicit BEGIN IMMEDIATE across a
        # check and an insert, and any other thread committing mid-
        # transaction would end it early. Reentrant so a guarded method may
        # call another.
        self._lock = threading.RLock()
        if readonly and not path.exists():
            raise StateError(f"{path} does not exist")
        self._engine = open_engine(path, readonly=readonly)
        if not readonly:
            ensure_schema(self._engine)

    @contextmanager
    def _write(self) -> Iterator[Session]:
        """A session that commits on the way out, under the store's lock."""
        with self._lock, Session(self._engine) as session:
            yield session
            session.commit()

    @contextmanager
    def _read(self) -> Iterator[Session]:
        """A session for a query. Held under the lock too: one connection
        cannot serve two threads at once, reads included."""
        with self._lock, Session(self._engine) as session:
            yield session

    def _set_run(self, session: Session, run_id: str, **values: Any) -> None:
        """One conditional UPDATE against a known run, raising if it is not.

        The identity is in the WHERE clause rather than checked first, so a
        run settled by another process loses this write instead of being
        overwritten by it.
        """
        result = session.execute(update(Run).where(Run.run_id == run_id).values(**values))
        if _rowcount(result) == 0:
            raise StateError(f"unknown run {run_id}")

    def close(self) -> None:
        self._engine.dispose()

    # -- runs --------------------------------------------------------------

    def create_run(
        self,
        run_id: str,
        outcome: str,
        config_json: str = "{}",
        *,
        credentials: Sequence[str] = (),
        kind: RunKind = "code",
    ) -> RunRecord:
        names = list(dict.fromkeys(credentials))
        now = time.time()
        try:
            with self._write() as session:
                session.execute(
                    insert(Run).values(
                        run_id=run_id,
                        outcome=outcome,
                        state="created",
                        config_json=config_json,
                        created_at=now,
                        updated_at=now,
                        credentials=json.dumps(names),
                        kind=kind,
                    )
                )
        except IntegrityError as exc:
            raise StateError(f"run {run_id} already exists") from exc
        return RunRecord(
            run_id=run_id,
            outcome=outcome,
            state="created",
            kind=kind,
            created_at=now,
            updated_at=now,
            credentials=names,
        )

    def add_run_published(self, run_id: str, entry: Published) -> None:
        """Record one more place the run's result went (#759), as it lands
        — so a resume at publishing skips it and the record says where the
        result is."""
        with self._write() as session:
            current = session.scalar(select(Run.published).where(Run.run_id == run_id))
            if current is None:
                raise StateError(f"unknown run {run_id}")
            published = [*json.loads(current or "[]"), entry.model_dump(mode="json")]
            self._set_run(session, run_id, published=json.dumps(published), updated_at=time.time())

    def set_run_credentials(self, run_id: str, credentials: Sequence[str]) -> None:
        """Record the ``[[credentials]]`` this run is granted (#765) — the
        whole grant, replacing what was there — so a resume re-provisions
        the same service sandbox."""
        names = list(dict.fromkeys(credentials))
        with self._write() as session:
            self._set_run(session, run_id, credentials=json.dumps(names), updated_at=time.time())

    def set_run_state(self, run_id: str, state: RunState) -> None:
        """Move the run to ``state``. A non-terminal state is also recorded
        as the run's ``stage``; a terminal one leaves ``stage`` alone, so a
        failed or blocked run still knows where a resume should re-enter."""
        values: dict[str, Any] = {"state": state, "updated_at": time.time()}
        if state not in TERMINAL_RUN_STATES and state != "provider_held":
            values["stage"] = state
        with self._write() as session:
            self._set_run(session, run_id, **values)

    def set_run_reason(self, run_id: str, reason: str | None) -> None:
        """Record why a run stopped where it did (a budget, a refusal)."""
        with self._write() as session:
            self._set_run(session, run_id, reason=reason, updated_at=time.time())

    def set_run_exhausted(self, run_id: str, kind: str | None) -> None:
        """Record which fix-round budget the run ran out of (``None`` clears
        it). Set by the engine when a round exhaustion ends the run; read by
        the daemon to tell "stopped one round short" from "broke" (#523)."""
        with self._write() as session:
            self._set_run(session, run_id, exhausted=kind, updated_at=time.time())

    def grant_rounds(self, run_id: str, rounds: int) -> int:
        """Extend this run's fix-round budgets by ``rounds`` and clear its
        exhaustion mark, so a resume gets real further rounds instead of
        re-exhausting at the first request for changes. Returns the total
        granted so far."""
        if rounds < 1:
            raise StateError(f"rounds to grant must be positive, not {rounds}")
        with self._write() as session:
            self._set_run(
                session,
                run_id,
                granted_rounds=Run.granted_rounds + rounds,
                exhausted=None,
                updated_at=time.time(),
            )
            session.flush()
            total = session.scalar(select(Run.granted_rounds).where(Run.run_id == run_id))
            return int(total or 0)

    def touch_run(self, run_id: str) -> None:
        """Refresh ``updated_at`` without changing anything else — the
        liveness signal for a run idling on a CI wait, where no job runs and
        so no event is persisted."""
        with self._write() as session:
            # No rowcount check: a run that has gone is not an error to a
            # liveness ping, and never was.
            session.execute(update(Run).where(Run.run_id == run_id).values(updated_at=time.time()))

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
        with self._write() as session:
            self._set_run(
                session,
                run_id,
                pr_number=number,
                pr_url=url,
                branch=branch,
                head_sha=head_sha,
                # Keep the node id a previous delivery recorded when this
                # one did not carry it.
                pr_node_id=func.coalesce(node_id, Run.pr_node_id),
                update_head=None,
                updated_at=time.time(),
            )

    def set_run_title(self, run_id: str, title: str | None) -> None:
        """The plan's own PR title (#621); None clears it."""
        with self._write() as session:
            self._set_run(session, run_id, pr_title=title, updated_at=time.time())

    def set_run_head(self, run_id: str, head_sha: str) -> None:
        with self._write() as session:
            self._set_run(session, run_id, head_sha=head_sha, updated_at=time.time())

    def set_run_verdict(self, run_id: str, verdict: str) -> None:
        with self._write() as session:
            self._set_run(session, run_id, last_verdict=verdict, updated_at=time.time())

    def set_update_head(self, run_id: str, head_sha: str | None) -> None:
        with self._write() as session:
            self._set_run(session, run_id, update_head=head_sha, updated_at=time.time())

    def bump_run_counter(self, run_id: str, counter: str) -> int:
        """Spend one unit of a per-run budget; returns the new count."""
        if counter not in RUN_COUNTERS:
            raise StateError(f"not a run counter: {counter!r}")
        # Whitelisted above, then resolved to a mapped column rather than
        # interpolated into SQL — the name can no longer reach the statement
        # as text at all.
        column = getattr(Run, counter)
        with self._write() as session:
            self._set_run(session, run_id, **{counter: column + 1}, updated_at=time.time())
            session.flush()
            total = session.scalar(select(column).where(Run.run_id == run_id))
            return int(total or 0)

    def set_run_workspace(self, run_id: str, workspace: Path, mounted: bool) -> None:
        with self._write() as session:
            self._set_run(
                session,
                run_id,
                workspace=str(workspace),
                mounted=int(mounted),
                updated_at=time.time(),
            )

    def set_run_kept(self, run_id: str, reason: str | None) -> None:
        """Mark a run's sandboxes as deliberately kept (``"debug"``,
        ``"manual"``), or clear the marker with None. ``sandbox prune``
        excludes kept runs unless explicitly told otherwise."""
        with self._write() as session:
            # `updated_at` is left alone on purpose: keeping a sandbox is a
            # note about a run, not activity on it.
            self._set_run(session, run_id, kept_reason=reason)

    def append_run_guidance(self, run_id: str, text: str) -> None:
        """Append one standing chat-guidance entry (a ``steer_run`` verdict)
        to the run. Persisted so a resumed run re-applies it to its prompts."""
        with self._write() as session:
            current = session.scalar(select(Run.user_guidance).where(Run.run_id == run_id))
            if current is None:
                raise StateError(f"unknown run {run_id}")
            items = [*json.loads(current or "[]"), text]
            self._set_run(session, run_id, user_guidance=json.dumps(items), updated_at=time.time())

    def get_run_guidance(self, run_id: str) -> list[str]:
        with self._read() as session:
            current = session.scalar(select(Run.user_guidance).where(Run.run_id == run_id))
        if current is None:
            raise StateError(f"unknown run {run_id}")
        return [str(item) for item in json.loads(current or "[]")]

    @staticmethod
    def _run_record(row: Run) -> RunRecord:
        """A frozen record, detached from the session that read it.

        Records rather than model objects cross this boundary on purpose:
        the daemon hands rows between threads and the concierge reads on a
        connection of its own, so a live instance would be an expired or
        detached one by the time it was used.
        """
        return RunRecord(
            run_id=row.run_id,
            outcome=row.outcome,
            state=_LEGACY_RUN_STATES.get(row.state, row.state),  # type: ignore[arg-type]
            kind=row.kind or "code",  # type: ignore[arg-type]
            created_at=row.created_at,
            updated_at=row.updated_at,
            workspace=Path(row.workspace) if row.workspace else None,
            mounted=bool(row.mounted),
            kept_reason=row.kept_reason,
            reason=row.reason,
            stage=row.stage,
            pr_number=row.pr_number,
            pr_url=row.pr_url,
            pr_node_id=row.pr_node_id,
            branch=row.branch,
            head_sha=row.head_sha,
            pr_title=row.pr_title,
            review_rounds=int(row.review_rounds or 0),
            ci_rounds=int(row.ci_rounds or 0),
            update_attempts=int(row.update_attempts or 0),
            update_head=row.update_head,
            last_verdict=row.last_verdict,
            exhausted=row.exhausted,
            granted_rounds=int(row.granted_rounds or 0),
            credentials=[str(name) for name in json.loads(row.credentials or "[]")],
            published=[
                Published.model_validate(entry) for entry in json.loads(row.published or "[]")
            ],
            revision=int(row.revision or 0),
        )

    def non_terminal_runs(self) -> list[RunRecord]:
        """Runs still recorded as in flight (anything not in
        ``TERMINAL_RUN_STATES``), oldest-updated first.

        Callers must exclude the run genuinely executing in-process before
        reconciling anything returned here.
        """
        with self._read() as session:
            rows = session.scalars(select(Run).order_by(Run.updated_at.asc())).all()
            # Filtered in Python, not SQL: the two legacy state spellings
            # are remapped by `_run_record`, so "non-terminal" is a fact
            # about the record, not about the column.
            return [self._run_record(row) for row in rows if row.state not in TERMINAL_RUN_STATES]

    def reconcile_run(self, run_id: str, state: RunState, reason: str) -> None:
        """Force a run to a terminal state and record why, durably.

        Only the ``runs`` row is written: events and phase_attempts are never
        touched, so historical chronology is preserved. Reconciliation
        chronology events are appended separately by callers via
        :meth:`append_event`.
        """
        if state not in TERMINAL_RUN_STATES:
            raise StateError(f"run state {state!r} is not terminal")
        with self._write() as session:
            self._set_run(session, run_id, state=state, reason=reason, updated_at=time.time())

    def get_run(self, run_id: str) -> RunRecord:
        with self._read() as session:
            row = session.get(Run, run_id)
            if row is None:
                raise StateError(f"unknown run {run_id}")
            return self._run_record(row)

    def get_run_config(self, run_id: str) -> str:
        """The config JSON persisted at run creation. ``'{}'`` means nothing
        was persisted (rows from versions that predate config storage)."""
        with self._read() as session:
            config = session.scalar(select(Run.config_json).where(Run.run_id == run_id))
        if config is None:
            raise StateError(f"unknown run {run_id}")
        return str(config)

    def list_runs(self) -> list[RunRecord]:
        with self._read() as session:
            rows = session.scalars(select(Run).order_by(Run.created_at.desc())).all()
            return [self._run_record(row) for row in rows]

    def recent_runs(self, limit: int = 200) -> list[RunRecord]:
        """The runs touched most recently, newest first.

        Not the ones *started* most recently: a run merged this morning
        that began on Tuesday is the interesting one, and ordering by
        ``created_at`` buries it under runs that have not moved since. The
        limit is applied to this order too, so a long-running run cannot
        fall off the end of the list while it is still being worked on."""
        with self._read() as session:
            rows = session.scalars(select(Run).order_by(Run.updated_at.desc()).limit(limit)).all()
            return [self._run_record(row) for row in rows]

    def run_costs(self, run_ids: Sequence[str]) -> dict[str, tuple[int, float]]:
        """Turns and working seconds for each of ``run_ids`` — one grouped
        query, not one per run, because this is on the console's poll."""
        ids = list(run_ids)
        if not ids:
            return {}
        # The ids go in as one bound JSON array rather than as a generated
        # list of placeholders: the statement is static, and a long list
        # cannot run into SQLite's variable limit.
        stmt = (
            select(
                PhaseAttempt.run_id,
                func.coalesce(func.sum(PhaseAttempt.turns), 0).label("turns"),
                func.coalesce(func.sum(PhaseAttempt.ended_at - PhaseAttempt.started_at), 0.0).label(
                    "active"
                ),
            )
            .where(
                PhaseAttempt.run_id.in_(
                    select(text("value")).select_from(func.json_each(json.dumps(ids)))
                )
            )
            .group_by(PhaseAttempt.run_id)
        )
        with self._read() as session:
            rows = session.execute(stmt).all()
        return {str(r.run_id): (int(r.turns), float(r.active)) for r in rows}

    # -- windows, for the console's analytics ------------------------------
    #
    # Both fold in SQL rather than in Python. The console recomputes these
    # every few seconds against a store that only grows, and a query per
    # run would be a scan per run.

    def runs_between(self, since: float, until: float) -> list[RunWindowRecord]:
        """Every run *created* in the window, with its phase totals folded
        in: turns, tokens, cache reads and ``active`` (the time its phase
        attempts were actually running, which is not the same as
        ``updated_at - created_at`` — a run parked on a merge gate has
        elapsed time it did not spend working).

        A run is attributed whole to the window it started in, attempts
        included, so a total here is "what the runs that began in this
        window cost", not "work done during this window"."""
        stmt = (
            select(
                Run.run_id,
                Run.kind,
                Run.state,
                Run.reason,
                Run.created_at,
                Run.updated_at,
                Run.review_rounds,
                Run.ci_rounds,
                func.coalesce(func.sum(PhaseAttempt.turns), 0).label("turns"),
                func.coalesce(
                    func.sum(
                        func.coalesce(PhaseAttempt.input_tokens, 0)
                        + func.coalesce(PhaseAttempt.output_tokens, 0)
                    ),
                    0,
                ).label("tokens"),
                func.coalesce(func.sum(PhaseAttempt.cache_read_tokens), 0).label("cache"),
                func.coalesce(func.sum(PhaseAttempt.ended_at - PhaseAttempt.started_at), 0.0).label(
                    "active"
                ),
            )
            .select_from(Run)
            .outerjoin(PhaseAttempt, PhaseAttempt.run_id == Run.run_id)
            .where(Run.created_at >= since, Run.created_at < until)
            .group_by(Run.run_id)
            .order_by(Run.created_at)
        )
        with self._read() as session:
            return [RunWindowRecord(*row) for row in session.execute(stmt)]

    def phases_between(self, since: float, until: float) -> list[PhaseWindowRecord]:
        """Each phase in the window, by the attempts that *started* in it:
        how long it ran, what it cost, and how often it had to go round
        again.

        ``retries`` counts attempts past the first — where the loop fights
        itself. ``cache`` is separate from ``tokens`` because the ratio
        between them is a per-phase fact, not a global one: a phase that
        re-sends a large fixed context reads far more than it writes.
        Longest first."""
        seconds = func.coalesce(
            func.sum(PhaseAttempt.ended_at - PhaseAttempt.started_at), 0.0
        ).label("seconds")
        stmt = (
            select(
                PhaseAttempt.phase,
                func.count().label("attempts"),
                seconds,
                func.coalesce(func.sum(PhaseAttempt.turns), 0).label("turns"),
                func.coalesce(
                    func.sum(
                        func.coalesce(PhaseAttempt.input_tokens, 0)
                        + func.coalesce(PhaseAttempt.output_tokens, 0)
                    ),
                    0,
                ).label("tokens"),
                func.coalesce(func.sum(PhaseAttempt.cache_read_tokens), 0).label("cache"),
                func.coalesce(func.sum(case((PhaseAttempt.attempt > 1, 1), else_=0)), 0).label(
                    "retries"
                ),
            )
            .where(PhaseAttempt.started_at >= since, PhaseAttempt.started_at < until)
            .group_by(PhaseAttempt.phase)
            .order_by(seconds.desc())
        )
        with self._read() as session:
            return [PhaseWindowRecord(*row) for row in session.execute(stmt)]

    def task_totals_between(self, since: float, until: float) -> TaskTotalsRecord:
        """What the tasks of the window's runs cost in rework: revisions a
        task needed, replans it forced, and how many were flagged as having
        a suspect verify."""
        stmt = (
            select(
                func.count().label("tasks"),
                func.coalesce(func.sum(Task.revisions), 0).label("revisions"),
                func.coalesce(func.sum(Task.replans), 0).label("replans"),
                func.coalesce(func.sum(Task.verify_suspect), 0).label("suspect"),
            )
            .select_from(Task)
            .join(Run, Run.run_id == Task.run_id)
            .where(Run.created_at >= since, Run.created_at < until)
        )
        with self._read() as session:
            return TaskTotalsRecord(*session.execute(stmt).one())

    # -- tasks -------------------------------------------------------------

    def save_tasks(self, run_id: str, specs: list[TaskSpec]) -> None:
        if not specs:
            return
        with self._write() as session:
            # OR REPLACE: saving the graph again is how a replan rewrites it,
            # and the task ids are stable across the two.
            session.execute(
                insert(Task).prefix_with("OR REPLACE"),
                [
                    {
                        "run_id": run_id,
                        "task_id": spec.id,
                        "order_idx": index,
                        "state": "pending",
                        "spec_json": spec.model_dump_json(),
                    }
                    for index, spec in enumerate(specs)
                ],
            )

    def append_task(self, run_id: str, spec: TaskSpec) -> TaskRecord:
        """Add one task after every existing one (a fix round). ``save_tasks``
        numbers from zero and would collide with the graph already saved."""
        with self._write() as session:
            next_idx = session.scalar(
                select(func.coalesce(func.max(Task.order_idx), -1) + 1).where(Task.run_id == run_id)
            )
            session.execute(
                insert(Task).values(
                    run_id=run_id,
                    task_id=spec.id,
                    order_idx=int(next_idx or 0),
                    state="pending",
                    spec_json=spec.model_dump_json(),
                )
            )
        return TaskRecord(spec=spec)

    def update_task(self, run_id: str, task: TaskRecord) -> None:
        with self._write() as session:
            result = session.execute(
                update(Task)
                .where(Task.run_id == run_id, Task.task_id == task.spec.id)
                .values(
                    state=task.state,
                    # The spec is rewritten here, not only at graph time: a
                    # re-authored verify command changes it mid-task, and a
                    # resumed run must read back the exam it actually ran.
                    spec_json=task.spec.model_dump_json(),
                    revisions=task.revisions,
                    replans=task.replans,
                    last_feedback=task.last_feedback,
                    session_id=task.session_id,
                    verify_fingerprints=json.dumps(task.verify_fingerprints),
                    verify_suspect=int(task.verify_suspect),
                    verify_reauthors=task.verify_reauthors,
                    output_json=(
                        task.output.model_dump_json() if task.output is not None else None
                    ),
                )
            )
            if _rowcount(result) == 0:
                raise StateError(f"unknown task {task.spec.id} in run {run_id}")

    def page_runs(
        self,
        *,
        states: Sequence[str] | None = None,
        kinds: Sequence[str] | None = None,
        after: tuple[float, str] | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        """A page of runs in :meth:`recent_runs` order (touched most
        recently first), keyed on ``(updated_at, run_id)`` so a reader
        paging while runs move sees no gap and no repeat (#1036).
        ``states`` are matched on the record, after the legacy spellings
        are remapped, so the filter is applied in Python on a bounded
        over-read rather than trusted to the column."""
        stmt = select(Run).order_by(Run.updated_at.desc(), Run.run_id.desc())
        if kinds:
            stmt = stmt.where(Run.kind.in_(list(kinds)))
        if after is not None:
            updated_at, run_id = after
            stmt = stmt.where(
                or_(
                    Run.updated_at < updated_at,
                    and_(Run.updated_at == updated_at, Run.run_id < run_id),
                )
            )
        wanted = set(states or ())
        out: list[RunRecord] = []
        with self._read() as session:
            for row in session.scalars(stmt):
                record = self._run_record(row)
                if wanted and record.state not in wanted:
                    continue
                out.append(record)
                if len(out) >= limit:
                    break
        return out

    def get_tasks(self, run_id: str) -> list[TaskRecord]:
        with self._read() as session:
            rows = session.scalars(
                select(Task).where(Task.run_id == run_id).order_by(Task.order_idx)
            ).all()
            return [self._task_record(row) for row in rows]

    @staticmethod
    def _task_record(row: Task) -> TaskRecord:
        return TaskRecord(
            spec=TaskSpec.model_validate_json(row.spec_json),
            state=row.state,  # type: ignore[arg-type]
            revisions=row.revisions,
            replans=row.replans,
            last_feedback=row.last_feedback,
            session_id=row.session_id,
            verify_fingerprints=json.loads(row.verify_fingerprints or "[]"),
            verify_suspect=bool(row.verify_suspect),
            verify_reauthors=row.verify_reauthors,
            output=(TaskOutput.model_validate_json(row.output_json) if row.output_json else None),
        )

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
        with self._write() as session:
            session.execute(
                insert(PhaseAttempt).values(
                    run_id=run_id,
                    task_id=task_id,
                    phase=phase,
                    attempt=attempt,
                    status=status,
                    output_json=output_json,
                    started_at=started_at,
                    ended_at=time.time(),
                    input_tokens=u.input_tokens,
                    output_tokens=u.output_tokens,
                    cache_read_tokens=u.cache_read_tokens,
                    cache_write_tokens=u.cache_write_tokens,
                    turns=turns,
                )
            )

    def latest_phase_output(self, run_id: str, task_id: str, phase: str) -> str | None:
        """output_json of the task's most recent attempt of ``phase``."""
        with self._read() as session:
            return session.scalar(
                select(PhaseAttempt.output_json)
                .where(
                    PhaseAttempt.run_id == run_id,
                    PhaseAttempt.task_id == task_id,
                    PhaseAttempt.phase == phase,
                )
                .order_by(PhaseAttempt.id.desc())
                .limit(1)
            )

    def latest_phase_attempt(
        self, run_id: str, task_id: str, phase: str
    ) -> PhaseAttemptRecord | None:
        """The task's most recent attempt row of ``phase`` (attempt, status,
        output_json...), or None if it never ran."""
        with self._read() as session:
            row = session.execute(
                select(PhaseAttempt)
                .where(
                    PhaseAttempt.run_id == run_id,
                    PhaseAttempt.task_id == task_id,
                    PhaseAttempt.phase == phase,
                )
                .order_by(PhaseAttempt.id.desc())
                .limit(1)
            ).first()
            return None if row is None else _attempt_record(row[0])

    def phase_attempts(self, run_id: str, task_id: str | None = None) -> list[PhaseAttemptRecord]:
        stmt = select(PhaseAttempt).where(PhaseAttempt.run_id == run_id)
        if task_id is not None:
            stmt = stmt.where(PhaseAttempt.task_id == task_id)
        with self._read() as session:
            rows = session.scalars(stmt.order_by(PhaseAttempt.id)).all()
            return [_attempt_record(row) for row in rows]

    def session_models(self, run_id: str) -> dict[str, str]:
        """Requested identities of completed builder/operator sessions.

        Legacy rows have no identity: their workspace/report still survives,
        but an SDK session must not silently resume under a different model.
        """
        models: dict[str, str] = {}
        for row in self.phase_attempts(run_id):
            if row.phase not in {"build", "execute"} or not row.output_json:
                continue
            try:
                data = json.loads(row.output_json)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            session_id, model = data.get("session_id"), data.get("requested_model")
            if isinstance(session_id, str) and isinstance(model, str):
                models[session_id] = model
        return models

    def posted_findings(self, run_id: str) -> list[PostedRecord]:
        """Every review finding this run posted on its PR, oldest round first.

        Read back from the ``review`` phase rows' ``output_json['posted']``,
        so a resumed run reconstructs prior-round thread identity from the
        store alone — no GitHub call. Body-only findings (no inline comment)
        are included, flagged ``body_only``.
        """
        records: list[PostedRecord] = []
        for row in self.phase_attempts(run_id):
            if row.phase != "review":
                continue
            try:
                data = json.loads(row.output_json or "{}")
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            review = data.get("review")
            review_id = review.get("id") if isinstance(review, dict) else None
            posted = data.get("posted")
            if not isinstance(posted, list):
                continue
            for item in posted:
                if not isinstance(item, dict) or not item.get("anchor"):
                    continue
                comment_id = item.get("comment_id")
                # Rows written before the id was neutral spell it as the
                # GraphQL ``thread_node_id``; both forms read the same.
                thread_id = item.get("thread_id")
                if thread_id is None:
                    thread_id = item.get("thread_node_id")
                records.append(
                    PostedRecord(
                        round=int(row.attempt),
                        anchor=str(item["anchor"]),
                        comment_id=int(comment_id) if comment_id is not None else None,
                        thread_id=str(thread_id) if thread_id is not None else None,
                        review_id=int(review_id) if review_id is not None else None,
                    )
                )
        return records

    # -- reconciliation ----------------------------------------------------

    def record_reconciliation(
        self,
        run_id: str,
        round: int,
        anchor: str,
        status: str,
        *,
        resolved: bool = False,
        kind: str = RECONCILE_REVIEW,
    ) -> None:
        """Note that this run/round has spoken to ``anchor`` on the PR.

        Written as each reply lands, so a resume between posting a reply and
        recording it is the only window the (second, live-thread) idempotency
        check has to cover. Idempotent by primary key.
        """
        stmt = sqlite_insert(Reconciliation).values(
            run_id=run_id,
            round=round,
            anchor=anchor,
            status=status,
            resolved=1 if resolved else 0,
            ts=time.time(),
            kind=kind,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["run_id", "round", "anchor"],
            set_={
                "status": stmt.excluded.status,
                "resolved": stmt.excluded.resolved,
                "ts": stmt.excluded.ts,
                "kind": stmt.excluded.kind,
            },
        )
        with self._write() as session:
            session.execute(stmt)

    def reconciliations(
        self, run_id: str, round: int | None = None, *, kind: str | None = None
    ) -> dict[str, str]:
        """``{anchor: status}`` already reconciled for this run (and round).

        ``kind`` narrows to one of the six concerns this table carries. The
        sentinel ``round`` alone would answer the same question — it still
        does, and is still written — but naming the kind means a caller that
        wants human replies says so, and a row whose band was misread shows
        up as a mismatch rather than as a plausible answer.
        """
        stmt = select(Reconciliation.anchor, Reconciliation.status).where(
            Reconciliation.run_id == run_id
        )
        if round is not None:
            stmt = stmt.where(Reconciliation.round == round)
        if kind is not None:
            stmt = stmt.where(Reconciliation.kind == kind)
        with self._read() as session:
            return {str(anchor): str(status) for anchor, status in session.execute(stmt)}

    # Human objections are recorded in the same table under a sentinel round:
    # they belong to the run, not to a review round of ours, and the key is
    # the objection's GitHub identity rather than a `path:line` anchor.
    HUMAN_ROUND = -1

    def record_human_reply(self, run_id: str, key: str, status: str) -> None:
        """Note that this run has replied to the human objection ``key``.

        What stops a standing ``CHANGES_REQUESTED`` — which only its author
        can dismiss — from buying another full fix pass on every landing
        attempt (#520).
        """
        self.record_reconciliation(run_id, self.HUMAN_ROUND, key, status, kind=RECONCILE_HUMAN)

    def answered_objections(self, run_id: str) -> dict[str, str]:
        """``{key: status}`` of the human objections this run has answered."""
        return self.reconciliations(run_id, self.HUMAN_ROUND, kind=RECONCILE_HUMAN)

    # Advisory-check fix rounds (#611) live under their own sentinel round:
    # a regression on a check the base does not require gets one round,
    # and this is how the next landing pass knows it was spent.
    ADVISORY_ROUND = -2

    def record_advisory_round(self, run_id: str, check: str) -> None:
        """Note that this run spent its one fix round on advisory ``check``."""
        self.record_reconciliation(
            run_id, self.ADVISORY_ROUND, check, "spent", kind=RECONCILE_ADVISORY
        )

    def advisory_rounds(self, run_id: str) -> frozenset[str]:
        """The advisory checks this run has already spent a round on."""
        return frozenset(self.reconciliations(run_id, self.ADVISORY_ROUND, kind=RECONCILE_ADVISORY))

    # An automated reviewer's changes-requested review buys one fix round
    # per run (#613); the same sentinel pattern says whether it was spent.
    BOT_ROUND = -3

    def record_bot_round(self, run_id: str) -> None:
        self.record_reconciliation(run_id, self.BOT_ROUND, "bot", "spent", kind=RECONCILE_BOT)

    def bot_round_spent(self, run_id: str) -> bool:
        return "bot" in self.reconciliations(run_id, self.BOT_ROUND, kind=RECONCILE_BOT)

    # Round n+1's confirmations of carried-over findings share the table too,
    # under their own negative rounds: they are keyed by the *same* anchors a
    # reconciliation pass uses, so mixing them would make one look like the
    # other's idempotency record.
    CONFIRM_ROUND_BASE = -100

    def record_confirmation(
        self, run_id: str, round: int, anchor: str, status: str, *, resolved: bool = False
    ) -> None:
        """Note that review round ``round`` has confirmed ``anchor`` in its thread."""
        self.record_reconciliation(
            run_id,
            self.CONFIRM_ROUND_BASE - round,
            anchor,
            status,
            resolved=resolved,
            kind=RECONCILE_CONFIRM,
        )

    def confirmations(self, run_id: str, round: int) -> dict[str, str]:
        """``{anchor: status}`` this review round has already confirmed."""
        return self.reconciliations(run_id, self.CONFIRM_ROUND_BASE - round, kind=RECONCILE_CONFIRM)

    # "noted, not blocking" replies live in their own negative band too, so
    # an approving round's note on an anchor never shadows a reconciliation
    # or a confirmation of the same anchor.
    NOTED_ROUND_BASE = -1000

    def record_noted(
        self, run_id: str, round: int, anchor: str, status: str, *, resolved: bool = False
    ) -> None:
        """Note that review round ``round`` has answered a non-blocking finding."""
        self.record_reconciliation(
            run_id,
            self.NOTED_ROUND_BASE - round,
            anchor,
            status,
            resolved=resolved,
            kind=RECONCILE_NOTED,
        )

    def noted(self, run_id: str, round: int) -> dict[str, str]:
        """``{anchor: status}`` this review round has already noted."""
        return self.reconciliations(run_id, self.NOTED_ROUND_BASE - round, kind=RECONCILE_NOTED)

    # -- events ------------------------------------------------------------

    def last_event_ts(self, run_id: str) -> float | None:
        """Timestamp of the run's most recent persisted event, if any.

        Every bus event except streaming deltas (heartbeats included) is
        persisted, so this is the best liveness signal available for a run
        whose recorded state says non-terminal but whose process may be
        long dead.
        """
        with self._read() as session:
            return session.scalar(select(func.max(EventRow.ts)).where(EventRow.run_id == run_id))

    def append_event(self, event: Event) -> None:
        if _is_ephemeral(event):
            return
        with self._write() as session:
            session.execute(insert(EventRow).values(**_event_values(event)))

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
        with self._lock, begin_immediate(self._engine) as conn:
            state = conn.execute(
                select(Run.state).where(Run.run_id == event.run_id)
            ).scalar_one_or_none()
            if state is None or state not in states:
                return False
            conn.execute(insert(EventRow).values(**_event_values(event)))
            return True

    def events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        type_prefix: str | None = None,
    ) -> Iterator[tuple[int, Event]]:
        stmt = select(EventRow).where(EventRow.run_id == run_id, EventRow.seq > after_seq)
        if type_prefix:
            stmt = stmt.where(EventRow.type.like(type_prefix + "%"))
        with self._read() as session:
            rows = session.scalars(stmt.order_by(EventRow.seq)).all()
            records = [(row.seq, self._event_record(row)) for row in rows]
        yield from records

    def last_event(self, run_id: str, type_prefix: str) -> Event | None:
        """The newest event of a type (prefix) on the run, one indexed read."""
        with self._read() as session:
            row = session.scalars(
                select(EventRow)
                .where(EventRow.run_id == run_id, EventRow.type.like(type_prefix + "%"))
                .order_by(EventRow.seq.desc())
                .limit(1)
            ).first()
            return None if row is None else self._event_record(row)

    def last_event_ts_many(self, run_ids: Sequence[str]) -> dict[str, float]:
        """``run_id -> newest event ts`` for the given runs, in one query."""
        if not run_ids:
            return {}
        stmt = (
            select(EventRow.run_id, func.max(EventRow.ts).label("ts"))
            .where(EventRow.run_id.in_(list(run_ids)))
            .group_by(EventRow.run_id)
        )
        with self._read() as session:
            rows = session.execute(stmt).all()
        return {str(r.run_id): float(r.ts) for r in rows if r.ts is not None}

    @staticmethod
    def _event_record(row: EventRow) -> Event:
        return Event(
            ts=row.ts,
            run_id=row.run_id,
            job_id=row.job_id,
            type=row.type,
            data=json.loads(row.data_json),
        )
