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
from pathlib import Path

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
    headline_id INTEGER
);
"""


def _row_to_item(row: sqlite3.Row) -> WorkItem:
    return WorkItem(
        item_id=row["item_id"],
        source=row["source"],
        source_key=row["source_key"],
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
        self._conn.commit()
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
                terminal = row["state"] in ("done", "abandoned")
                changed = (row["title"], row["body"]) != (item.title, item.body)
                if not (terminal and changed):
                    return False
                self._conn.execute(
                    "DELETE FROM daemon_work_items WHERE item_id = ?", (row["item_id"],)
                )
            self._conn.execute(
                "INSERT INTO daemon_work_items (item_id, source, source_key, title, body, "
                "url, state, attempts, claimed, run_id, last_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, 0, NULL, NULL, ?, ?)",
                (
                    item.item_id,
                    item.source,
                    item.source_key,
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
        row = self._conn.execute(
            "SELECT * FROM daemon_work_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        return _row_to_item(row) if row else None

    def next_queued(self, now: float, backoff_s: float) -> WorkItem | None:
        """Oldest queued item whose retry backoff (attempts * backoff) has
        elapsed since its last update. Ties on ``created_at`` (a batch
        upserted with one ``now``) break on insertion order (rowid), so
        dispatch is genuinely FIFO."""
        for row in self._conn.execute(
            "SELECT * FROM daemon_work_items WHERE state = 'queued' "
            "ORDER BY created_at ASC, rowid ASC"
        ):
            item = _row_to_item(row)
            if item.attempts == 0 or now - item.updated_at >= item.attempts * backoff_s:
                return item
        return None

    def queued(self) -> list[WorkItem]:
        return [
            _row_to_item(row)
            for row in self._conn.execute(
                "SELECT * FROM daemon_work_items WHERE state = 'queued' "
                "ORDER BY created_at ASC, rowid ASC"
            )
        ]

    def running_items(self) -> list[WorkItem]:
        return [
            _row_to_item(row)
            for row in self._conn.execute("SELECT * FROM daemon_work_items WHERE state = 'running'")
        ]

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

    def mark_done(self, item_id: str, now: float) -> None:
        self._update(item_id, now, state="done", last_error=None)

    def mark_failed(self, item_id: str, error: str, now: float, *, requeue: bool) -> None:
        self._update(
            item_id,
            now,
            state="queued" if requeue else "abandoned",
            last_error=error[:2000],
        )

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
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM daemon_runs WHERE started_at >= ?", (ts,)
        ).fetchone()
        return int(row["n"])

    def finish_ledger(self, run_id: str, result: str, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE daemon_runs SET finished_at = ?, result = ? WHERE run_id = ?",
                (now, result, run_id),
            )
            self._conn.commit()

    # -- backlog dedup ---------------------------------------------------------

    def backlog_seen(self, fingerprint: str) -> bool:
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

    def discord_thread(self, run_id: str) -> tuple[int, int, int | None] | None:
        row = self._conn.execute(
            "SELECT channel_id, thread_id, headline_id FROM daemon_discord_threads "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return (int(row["channel_id"]), int(row["thread_id"]), row["headline_id"])

    def run_for_thread(self, thread_id: int) -> str | None:
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
