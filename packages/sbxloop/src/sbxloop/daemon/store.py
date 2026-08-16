"""DaemonStore: work items, the run ledger, backlog dedup, Discord threads.

Lives in the same ``state.db`` as the engine's :class:`StateStore` (WAL
mode allows the second connection) so ``sbxloop status``/``logs`` and the
daemon's own bookkeeping share one file, but it owns its own connection and
tables — the engine store stays untouched. Same schema discipline:
``CREATE TABLE IF NOT EXISTS`` on every open, additive-only changes.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from sbxloop.daemon.model import ItemState, WorkItem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daemon_work_items (
    item_id     TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    source_key  TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    claimed     INTEGER NOT NULL DEFAULT 0,
    run_id      TEXT,
    last_error  TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    pending_report TEXT,
    kind        TEXT NOT NULL DEFAULT 'patch',
    UNIQUE(source, source_key)
);
CREATE INDEX IF NOT EXISTS idx_daemon_items_state ON daemon_work_items(state, created_at);

CREATE TABLE IF NOT EXISTS daemon_runs (
    run_id      TEXT PRIMARY KEY,
    item_id     TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    result      TEXT
);
CREATE INDEX IF NOT EXISTS idx_daemon_runs_started ON daemon_runs(started_at);

CREATE TABLE IF NOT EXISTS daemon_run_resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    resumed_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_daemon_resumes_at ON daemon_run_resumes(resumed_at);
CREATE INDEX IF NOT EXISTS idx_daemon_resumes_item ON daemon_run_resumes(item_id);

CREATE TABLE IF NOT EXISTS daemon_state (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

CREATE TABLE IF NOT EXISTS daemon_backlog_filed (
    fingerprint TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    filed_as    TEXT NOT NULL,
    filed_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS daemon_discord_threads (
    run_id      TEXT PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    thread_id   INTEGER NOT NULL,
    headline_id INTEGER,
    status_id   INTEGER
);
"""

# Columns added after a table first shipped; applied idempotently at open
# (same pattern as engine/store.py's runs migrations).
_MIGRATIONS = (
    (
        "daemon_discord_threads",
        "status_id",
        "ALTER TABLE daemon_discord_threads ADD COLUMN status_id INTEGER",
    ),
    # #229: an operator decision (abandon / retry) the source has not heard yet.
    (
        "daemon_work_items",
        "pending_report",
        "ALTER TABLE daemon_work_items ADD COLUMN pending_report TEXT",
    ),
    # Discovery lane: audit items investigate and file issues, patch items
    # deliver PRs. Rows from before the column are patches.
    (
        "daemon_work_items",
        "kind",
        "ALTER TABLE daemon_work_items ADD COLUMN kind TEXT NOT NULL DEFAULT 'patch'",
    ),
)


class DiscordThread(NamedTuple):
    """Where a run lives on Discord (persisted so a restart re-attaches)."""

    channel_id: int
    thread_id: int
    headline_id: int | None
    status_id: int | None


def _row_to_item(row: sqlite3.Row) -> WorkItem:
    return WorkItem(
        item_id=row["item_id"],
        source=row["source"],
        source_key=row["source_key"],
        kind=row["kind"] or "patch",
        title=row["title"],
        body=row["body"],
        url=row["url"],
        state=row["state"],
        attempts=row["attempts"],
        claimed=bool(row["claimed"]),
        run_id=row["run_id"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        pending_report=row["pending_report"],
    )


class DaemonStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # The engine commits per event on the same file while a run is in
        # flight; wait for it instead of failing on SQLITE_BUSY.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        for table, column, ddl in _MIGRATIONS:
            existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self._conn.execute(ddl)
        self._conn.commit()
        # One connection shared by the daemon, bridge and CLI threads:
        # sqlite3 rejects concurrent use of a connection from two threads
        # ("bad parameter or other API misuse"), so EVERY statement — reads
        # included — runs under this lock.
        self._lock = threading.RLock()

    def close(self) -> None:
        self._conn.close()

    # -- work items ----------------------------------------------------------

    def upsert_new(self, item: WorkItem, now: float) -> bool:
        """Record a discovered item; True if it was not already known.

        A finished (done/abandoned) row with the same source key is
        superseded when the content changed — an operator re-dropping an
        edited inbox file, or re-triggering an issue, is a new work item.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT item_id, state, title, body FROM daemon_work_items "
                "WHERE source = ? AND source_key = ?",
                (item.source, item.source_key),
            ).fetchone()
            if row is not None:
                terminal = row["state"] in ("done", "abandoned", "cancelled")
                changed = (row["title"], row["body"]) != (item.title, item.body)
                if not (terminal and changed):
                    return False
                self._conn.execute(
                    "DELETE FROM daemon_work_items WHERE item_id = ?", (row["item_id"],)
                )
            self._conn.execute(
                "INSERT INTO daemon_work_items (item_id, source, source_key, kind, title, body, "
                "url, state, attempts, claimed, run_id, last_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, 0, NULL, NULL, ?, ?)",
                (
                    item.item_id,
                    item.source,
                    item.source_key,
                    item.kind,
                    item.title,
                    item.body,
                    item.url,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return True

    def get(self, item_id: str) -> WorkItem | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM daemon_work_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            return _row_to_item(row) if row else None

    def next_queued(self, now: float, backoff_s: float) -> WorkItem | None:
        """Oldest queued item whose retry backoff (attempts * backoff) has
        elapsed since its last update. Ties on ``created_at`` (a batch
        upserted with one ``now``) break on insertion order (rowid), so
        dispatch is genuinely FIFO.

        A queued item that still carries a ``run_id`` is an interrupted run
        awaiting resume (see :meth:`mark_resume_pending`): it was in flight
        when the previous process died, so it goes first and skips the
        retry backoff — that backoff spaces out *failed* attempts, and an
        interruption is not a failure."""
        with self._lock:
            for row in self._conn.execute(
                "SELECT * FROM daemon_work_items WHERE state = 'queued' "
                "ORDER BY (run_id IS NULL) ASC, created_at ASC, rowid ASC"
            ):
                item = _row_to_item(row)
                if (
                    item.run_id is not None
                    or item.attempts == 0
                    or now - item.updated_at >= item.attempts * backoff_s
                ):
                    return item
            return None

    def queued(self) -> list[WorkItem]:
        with self._lock:
            return [
                _row_to_item(row)
                for row in self._conn.execute(
                    "SELECT * FROM daemon_work_items WHERE state = 'queued' "
                    "ORDER BY created_at ASC, rowid ASC"
                )
            ]

    def running_items(self) -> list[WorkItem]:
        with self._lock:
            return [
                _row_to_item(row)
                for row in self._conn.execute(
                    "SELECT * FROM daemon_work_items WHERE state = 'running'"
                )
            ]

    def items(self, states: Sequence[ItemState] | None = None) -> list[WorkItem]:
        """Every known item (optionally filtered by state), oldest first —
        the operator's view for ``sbxloop daemon items`` / ``!sbx items``."""
        with self._lock:
            if states:
                marks = ", ".join("?" for _ in states)
                rows = self._conn.execute(
                    f"SELECT * FROM daemon_work_items WHERE state IN ({marks}) "  # nosec B608
                    "ORDER BY created_at ASC, rowid ASC",
                    tuple(states),
                )
            else:
                rows = self._conn.execute(
                    "SELECT * FROM daemon_work_items ORDER BY created_at ASC, rowid ASC"
                )
            return [_row_to_item(row) for row in rows]

    # -- operator controls (#229) ------------------------------------------------

    def abandon(self, item_id: str, reason: str, now: float) -> WorkItem:
        """Operator abandon: queued/running → abandoned. ``run_id`` is kept
        so the ledger and ``sbxloop logs`` still tie the item to the run
        that made the operator give up on it; the loop treats
        "abandoned while pinned to my run" as its cue to cancel that run.
        The source is owed a report (``pending_report``): whoever delivers
        it — the loop's settle path, recovery, or the tick sweep after a
        row-only CLI abandon — clears the debt."""
        return self._transition(
            item_id,
            now,
            ("queued", "running", "failed"),
            lambda item: f"{item_id} is already {item.state}",
            state="abandoned",
            last_error=reason[:2000],
            pending_report="abandoned",
        )

    def retry(self, item_id: str, now: float, reason: str | None = None) -> WorkItem:
        """Operator retry: an abandoned, cancelled or backoff-waiting item
        goes back to the queue as if freshly discovered. The attempt budget
        and retry backoff exist to bound *autonomous* churn, so a human
        decision starts both over — attempts reset, eligible on the next
        tick (the daily run cap still applies) — and the run is unpinned so
        the next dispatch plans from scratch instead of resuming the plan
        that failed (#228: recovery would have resumed a doomed plan; #254).
        The source-side claim is kept: re-claiming would fail (the inbox
        file / trigger label moved when it was first claimed). ``reason``
        (e.g. "re-queued by X") is kept as ``last_error`` for the audit
        trail in ``items``; without one the old error is cleared. The source
        is owed a ``requeued`` report (GitHub: re-claim, drop the failed
        label; inbox: move the file back out of failed/)."""
        return self._transition(
            item_id,
            now,
            ("abandoned", "cancelled", "queued"),
            lambda item: (
                f"{item_id} is {item.state}; only abandoned, cancelled or queued items can be "
                "retried" + (" (abandon it first)" if item.state == "running" else "")
            ),
            state="queued",
            attempts=0,
            run_id=None,
            last_error=reason[:2000] if reason else None,
            pending_report="requeued",
        )

    def requeue(self, item_id: str, now: float) -> WorkItem:
        """Operator requeue: a running (or queued) item loses its pinned run
        and goes back to the queue with its attempt count intact — the
        next dispatch starts a fresh run rather than resuming. The source
        hears nothing: the item is still claimed and still work."""
        return self._transition(
            item_id,
            now,
            ("running", "queued"),
            lambda item: (
                f"{item_id} is {item.state}; only running or queued items can be requeued"
                + (" (use retry)" if item.state in ("abandoned", "cancelled") else "")
            ),
            state="queued",
            run_id=None,
        )

    def _transition(
        self,
        item_id: str,
        now: float,
        allowed: tuple[str, ...],
        refuse: Callable[[WorkItem], str],
        **fields: object,
    ) -> WorkItem:
        """One conditional statement (``WHERE state IN (...)``) instead of a
        read-then-write: the CLI runs in another process, so a daemon that
        settles the item as done between the two must not have its verdict
        overwritten by a command issued on stale state. No row updated →
        re-read and refuse with the state that is actually there."""
        assignments = ", ".join(f"{name} = ?" for name in fields)
        marks = ", ".join("?" for _ in allowed)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE daemon_work_items SET {assignments}, updated_at = ? "  # nosec B608
                f"WHERE item_id = ? AND state IN ({marks})",
                (*fields.values(), now, item_id, *allowed),
            )
            self._conn.commit()
            if cursor.rowcount == 1:
                return self._require(item_id)
            raise ValueError(refuse(self._require(item_id)))

    def pending_reports(self) -> list[WorkItem]:
        """Items whose operator decision (abandon / retry) the source has
        not been told about yet, oldest decision first."""
        with self._lock:
            return [
                _row_to_item(row)
                for row in self._conn.execute(
                    "SELECT * FROM daemon_work_items WHERE pending_report IS NOT NULL "
                    "ORDER BY updated_at ASC, rowid ASC"
                )
            ]

    def take_pending_report(self, item_id: str) -> bool:
        """Claim the item's owed report for delivery; False if there was
        none (or another thread already took it). Not ``_update``: this is
        not an item change and must not move ``updated_at``, the
        retry-backoff clock."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE daemon_work_items SET pending_report = NULL "
                "WHERE item_id = ? AND pending_report IS NOT NULL",
                (item_id,),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def _require(self, item_id: str) -> WorkItem:
        item = self.get(item_id)
        if item is None:
            raise KeyError(f"unknown work item {item_id!r}")
        return item

    def mark_claimed(self, item_id: str, now: float) -> None:
        self._update(item_id, now, claimed=1)

    def mark_running(self, item_id: str, run_id: str, now: float) -> None:
        """Move to running, count the attempt, and open the ledger row —
        all before the engine starts, so a crash still leaves the item→run
        link for recovery."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE daemon_work_items SET state = 'running', attempts = attempts + 1, "
                "run_id = ?, updated_at = ? WHERE item_id = ?",
                (run_id, now, item_id),
            )
            if cursor.rowcount != 1:
                # An unknown item must not leave an orphan ledger row that the
                # daily cap would count.
                self._conn.rollback()
                raise KeyError(f"unknown work item {item_id!r}")
            self._conn.execute(
                "INSERT OR REPLACE INTO daemon_runs (run_id, item_id, started_at) VALUES (?, ?, ?)",
                (run_id, item_id, now),
            )
            self._conn.commit()

    def mark_resume_pending(self, item_id: str, now: float) -> None:
        """Recovery found the item's run interrupted mid-flight: back to
        the queue with the run pinned, so the next tick resumes it through
        the same breaker/cap/pause gate a fresh dispatch faces (#254) —
        recovery itself never starts engines."""
        self._update(item_id, now, state="queued")

    def mark_resuming(self, item_id: str, run_id: str, now: float) -> None:
        """Resume the pinned run: back to running (the attempt count is
        unchanged — a resume is the same attempt) and record the resume in
        its own ledger so the daily cap and the per-item resume budget see
        it. ``daemon_runs`` is keyed by run id, so a second segment of the
        same run cannot be a second row there."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE daemon_work_items SET state = 'running', updated_at = ? "
                "WHERE item_id = ? AND run_id = ?",
                (now, item_id, run_id),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                raise KeyError(f"work item {item_id!r} does not carry run {run_id!r}")
            self._conn.execute(
                "INSERT INTO daemon_run_resumes (run_id, item_id, resumed_at) VALUES (?, ?, ?)",
                (run_id, item_id, now),
            )
            self._conn.commit()

    def mark_done(self, item_id: str, now: float) -> None:
        self._update(item_id, now, state="done", last_error=None)

    def mark_failed(self, item_id: str, error: str, now: float, *, requeue: bool) -> None:
        # A requeued item must not keep its run pinned: queued + run_id
        # means "resume this run", and a failed run is dispatched fresh. An
        # abandoned item keeps it for forensics.
        fields: dict[str, object] = {
            "state": "queued" if requeue else "abandoned",
            "last_error": error[:2000],
        }
        if requeue:
            fields["run_id"] = None
        self._update(item_id, now, **fields)

    def mark_cancelled(self, item_id: str, reason: str, now: float) -> None:
        """Operator cancel: terminal for the daemon, unlike ``mark_failed``
        which either re-queues (with backoff) or abandons."""
        self._update(item_id, now, state="cancelled", last_error=reason[:2000])

    def mark_requeued_unstarted(self, item_id: str, now: float) -> None:
        """Crash between claim and start: back to the queue, claim kept."""
        self._update(item_id, now, state="queued", run_id=None)

    def _update(self, item_id: str, now: float, **fields: object) -> None:
        # Column names come from this module's own keyword calls, never from
        # input; every value is a bound parameter.
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE daemon_work_items SET {assignments}, updated_at = ? "  # nosec B608
                "WHERE item_id = ?",
                (*fields.values(), now, item_id),
            )
            self._conn.commit()

    def set_state(self, item_id: str, state: ItemState, now: float) -> None:
        self._update(item_id, now, state=state)

    # -- run ledger ------------------------------------------------------------

    def runs_started_since(self, ts: float) -> int:
        """Fresh starts plus resumes in the window: each resume spends a
        full engine wall clock, so the daily cap counts it (#254/#234)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT (SELECT COUNT(*) FROM daemon_runs WHERE started_at >= ?) + "
                "(SELECT COUNT(*) FROM daemon_run_resumes WHERE resumed_at >= ?) AS n",
                (ts, ts),
            ).fetchone()
            return int(row["n"])

    def resumes_since(self, ts: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM daemon_run_resumes WHERE resumed_at >= ?", (ts,)
            ).fetchone()
            return int(row["n"])

    def resumes_for_item(self, item_id: str) -> int:
        """Resumes across ALL of the item's runs: the budget bounds total
        effort per item, not per plan."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM daemon_run_resumes WHERE item_id = ?", (item_id,)
            ).fetchone()
            return int(row["n"])

    def unsettled_runs(self) -> list[tuple[str, str]]:
        """``(run_id, item_id)`` for every ledger row the loop never closed
        with a verdict: still open (the process died mid-run) or closed as
        ``interrupted`` (a clean shutdown expecting to resume). Recovery
        uses it to notice an item that an operator abandoned/requeued
        *offline* — the row-only CLI cannot report to the source or clean
        up the dead run's sandboxes, and the item is no longer ``running``
        so the ordinary reconciliation never sees it (#229)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, item_id FROM daemon_runs "
                "WHERE finished_at IS NULL OR result = 'interrupted' ORDER BY started_at ASC"
            )
            return [(str(row["run_id"]), str(row["item_id"])) for row in rows]

    def finish_ledger(self, run_id: str, result: str, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE daemon_runs SET finished_at = ?, result = ? WHERE run_id = ?",
                (now, result, run_id),
            )
            self._conn.commit()

    # -- circuit breaker ---------------------------------------------------------

    def breaker(self) -> tuple[float | None, int]:
        """(opened_at, consecutive_failures) as last persisted. Kept in the
        db rather than on the loop object so a crash-restart cycle cannot
        reset the breaker (#254)."""
        with self._lock:
            rows = {
                row["key"]: row["value"]
                for row in self._conn.execute(
                    "SELECT key, value FROM daemon_state WHERE key IN "
                    "('breaker_opened_at', 'consecutive_failures')"
                )
            }
        opened = rows.get("breaker_opened_at")
        failures = rows.get("consecutive_failures")
        return (
            float(opened) if opened not in (None, "") else None,
            int(failures) if failures not in (None, "") else 0,
        )

    def set_breaker(self, opened_at: float | None, consecutive_failures: int) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO daemon_state (key, value) VALUES (?, ?)",
                [
                    ("breaker_opened_at", "" if opened_at is None else repr(float(opened_at))),
                    ("consecutive_failures", str(int(consecutive_failures))),
                ],
            )
            self._conn.commit()

    # -- backlog dedup ---------------------------------------------------------

    def backlog_seen(self, fingerprint: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM daemon_backlog_filed WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            return row is not None

    def backlog_record(self, fingerprint: str, run_id: str, filed_as: str, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO daemon_backlog_filed "
                "(fingerprint, run_id, filed_as, filed_at) VALUES (?, ?, ?, ?)",
                (fingerprint, run_id, filed_as, now),
            )
            self._conn.commit()

    # -- discord threads -------------------------------------------------------

    def discord_thread(self, run_id: str) -> DiscordThread | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT channel_id, thread_id, headline_id, status_id FROM daemon_discord_threads "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            return DiscordThread(
                int(row["channel_id"]), int(row["thread_id"]), row["headline_id"], row["status_id"]
            )

    def set_discord_status_id(self, run_id: str, status_id: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE daemon_discord_threads SET status_id = ? WHERE run_id = ?",
                (status_id, run_id),
            )
            self._conn.commit()

    def run_for_thread(self, thread_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id FROM daemon_discord_threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            return str(row["run_id"]) if row else None

    def record_discord_thread(
        self, run_id: str, channel_id: int, thread_id: int, headline_id: int | None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO daemon_discord_threads "
                "(run_id, channel_id, thread_id, headline_id) VALUES (?, ?, ?, ?)",
                (run_id, channel_id, thread_id, headline_id),
            )
            self._conn.commit()
