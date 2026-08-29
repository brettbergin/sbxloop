"""DaemonStore: work items, the run ledger, requesters, Discord threads.

Lives in the same ``state.db`` as the engine's :class:`StateStore` (WAL
mode allows the second connection) so ``sbxloop status``/``logs`` and the
daemon's own bookkeeping share one file, but it owns its own connection and
tables — the engine store stays untouched.

Item ids are normalised rather than migrated. A row written before the
typed-id migration carries a legacy ``gh:1234`` key; every lookup here goes
through :func:`_id_match`, which matches both the typed and the legacy
spelling of the same id, and every row loaded is normalised to the typed
form by :class:`~sbxloop.daemon.model.WorkItem`. So a store written by the
old daemon keeps resolving under either form, and rows written from now on
are typed — no ALTER, no rewrite pass, no window where an in-flight item
becomes unreachable.

Schema version 2 (the 1.0 pipeline). The pre-1.0 daemon kept five more
tables for the lanes that filed their own work — backlog fingerprints,
post-mortems, review charters, per-item PR acceptance state, audit
schedules — none of which exist any more: the PR's fate is now the engine
run's own (``runs.pr_number`` …). A ``state.db`` from that era is not
migrated; :meth:`DaemonStore.archive_legacy` moves it aside and the daemon
starts clean. That archive takes the engine's run history with it, which is
the deliberate "fresh state" of the cutover.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from sbxloop.daemon.model import ItemState, PendingReport, WorkItem
from sbxloop.errors import DaemonError
from sbxloop.ghids import GH_PREFIX, normalize_item_id, try_parse_gh_id
from sbxloop.log import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = "2"
LEGACY_SUFFIX = ".pre-1.0"

_WORK_ITEMS_BODY = """(
    item_id     TEXT PRIMARY KEY,
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
    requested_by TEXT,
    -- The owner/name the item came from, '' for rows written before
    -- multi-repo support (they belong to the sole configured repository).
    -- Identity is (source_key, repo): issue #4 in two repositories is two
    -- work items, which a bare UNIQUE(source_key) would have collided.
    repo        TEXT NOT NULL DEFAULT '',
    UNIQUE(source_key, repo)
)"""

_WORK_ITEMS_COLUMNS = (
    "item_id, source_key, title, body, url, state, attempts, claimed, run_id, "
    "last_error, created_at, updated_at, pending_report, requested_by"
)

_REQUESTERS_BODY = """(
    source_key   TEXT NOT NULL,
    repo         TEXT NOT NULL DEFAULT '',
    requester_id TEXT NOT NULL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (source_key, repo)
)"""

_REQUESTERS_COLUMNS = "source_key, requester_id, created_at"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS daemon_work_items {_WORK_ITEMS_BODY};
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

-- Who asked to be pinged when a run finishes. The bridge's watch registry
-- is in-memory, so without this a daemon restart silently drops every
-- pending watch — and runs last minutes to hours, exactly the window in
-- which a restart happens.
CREATE TABLE IF NOT EXISTS daemon_run_watches (
    run_id     TEXT NOT NULL,
    watcher_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(run_id, watcher_id)
);
CREATE INDEX IF NOT EXISTS idx_daemon_run_watches_run ON daemon_run_watches(run_id);

-- Who asked for an issue through the concierge, keyed by the issue number
-- it filed, so the work item that discovery later builds from that issue
-- carries the requester — recorded here rather than in the public issue
-- body, which would expose a Discord user id.
CREATE TABLE IF NOT EXISTS daemon_requesters {_REQUESTERS_BODY};

CREATE TABLE IF NOT EXISTS daemon_discord_threads (
    run_id      TEXT PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    thread_id   INTEGER NOT NULL,
    headline_id INTEGER,
    status_id   INTEGER
);

-- run_for_thread() runs per inbound Discord message in a non-control
-- channel; without this it scans a row per run the daemon has ever done.
CREATE INDEX IF NOT EXISTS idx_discord_threads_thread
    ON daemon_discord_threads(thread_id);
"""

TERMINAL_ITEM_STATES: frozenset[str] = frozenset({"done", "failed", "blocked", "cancelled"})


class DiscordThread(NamedTuple):
    """Where a run lives on Discord (persisted so a restart re-attaches)."""

    channel_id: int
    thread_id: int
    headline_id: int | None
    status_id: int | None


def _id_variants(item_id: str) -> tuple[str, str]:
    """Both spellings of a GitHub item id (typed and legacy bare).

    Non-GitHub ids (``inbox:x.md``) come back duplicated, so callers can
    always bind exactly two parameters.
    """
    parsed = try_parse_gh_id(item_id)
    if parsed is None:
        return (item_id, item_id)
    if parsed.kind == "issue":
        return (parsed.item_id, f"{GH_PREFIX}{parsed.number}")
    return (parsed.item_id, parsed.item_id)


def _id_match(item_id: str) -> tuple[str, tuple[str, str]]:
    """``("item_id IN (?, ?)", params)`` for a lookup that accepts either
    spelling of the id."""
    return ("item_id IN (?, ?)", _id_variants(item_id))


def _row_to_item(row: sqlite3.Row) -> WorkItem:
    return WorkItem(
        item_id=row["item_id"],
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
        pending_report=row["pending_report"],
        requested_by=row["requested_by"],
        repo=row["repo"] or None,
    )


def _loggable(fields: dict[str, object]) -> dict[str, object]:
    """Column assignments as log fields: ``state`` first, long text clipped."""
    out: dict[str, object] = {}
    for key, value in fields.items():
        if key == "state":
            continue  # already logged from the re-read row
        out[key] = value[:120] if isinstance(value, str) else value
    return out


def _repo_from_url(url: object) -> str | None:
    """``owner/name`` out of a GitHub issue URL, or None if it is not one."""
    if not isinstance(url, str) or not url:
        return None
    match = re.search(r"github\.com/([^/\s]+)/([^/\s]+)/(?:issues|pull)/\d+", url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}  # nosec B608


class DaemonStore:
    @classmethod
    def archive_legacy(cls, path: Path, *, clock: Callable[[], float] = time.time) -> Path | None:
        """Move a pre-1.0 ``state.db`` (and its ``-wal``/``-shm``) aside.

        Returns the archive path, or None when there was nothing to do: no
        file, a database the daemon never touched (an engine-only store from
        ``sbxloop run`` on a CLI host is left alone), or one already at the
        current schema. Called before either store opens the file — the
        daemon host is deployed unattended, so the cutover has to be
        something the daemon does for itself on its first start.
        """
        if not path.is_file():
            return None
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = _tables(conn)
            if "daemon_work_items" not in tables:
                return None
            if "daemon_state" in tables:
                row = conn.execute(
                    "SELECT value FROM daemon_state WHERE key = 'schema_version'"
                ).fetchone()
                if row is not None and str(row[0]) == SCHEMA_VERSION:
                    return None
        finally:
            conn.close()
        target = path.with_name(f"{path.name}{LEGACY_SUFFIX}")
        if target.exists():
            target = path.with_name(f"{path.name}{LEGACY_SUFFIX}.{int(clock())}")
        for suffix in ("", "-wal", "-shm"):
            src = path.with_name(path.name + suffix)
            if src.exists():
                src.rename(target.with_name(target.name + suffix))
        log.warning(
            "store.archived_legacy",
            db=str(path),
            archived_to=str(target),
            hint="pre-1.0 daemon state is not migrated; the daemon starts with a fresh store",
        )
        return target

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # The engine commits per event on the same file while a run is in
        # flight; wait for it instead of failing on SQLITE_BUSY.
        self._conn.execute("PRAGMA busy_timeout=5000")
        if "daemon_work_items" in _tables(self._conn) and "kind" in _columns(
            self._conn, "daemon_work_items"
        ):
            # Opened by a CLI command before the daemon's first start did
            # the archive; say so rather than fail on a missing column.
            self._conn.close()
            raise DaemonError(
                f"{path} is a pre-1.0 daemon state database; start `sbxloop daemon` once "
                f"to archive it (to {path.name}{LEGACY_SUFFIX}) and begin a fresh store"
            )
        self._conn.executescript(_SCHEMA)
        self._migrate_repo_columns()
        self._conn.execute(
            "INSERT OR REPLACE INTO daemon_state (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        self._conn.commit()
        log.debug("store.opened", db=str(path), schema=SCHEMA_VERSION)
        # One connection shared by the daemon, bridge and CLI threads:
        # sqlite3 rejects concurrent use of a connection from two threads
        # ("bad parameter or other API misuse"), so EVERY statement — reads
        # included — runs under this lock.
        self._lock = threading.RLock()

    def close(self) -> None:
        self._conn.close()

    def _migrate_repo_columns(self) -> None:
        """Bring a store created before multi-repo to the current shape.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table alone, so a
        daemon upgraded in place still has the single-repo shape — and its
        key is the one that must change: ``UNIQUE(source_key)`` on
        ``daemon_work_items`` (``PRIMARY KEY(source_key)`` on
        ``daemon_requesters``) collides the moment two configured
        repositories each have an issue with the same number, which is
        exactly what an upgraded store is about to see. SQLite cannot drop a
        constraint with ALTER, so the tables are rebuilt: new shape, rows
        copied with ``repo = ''``, drop, rename, indexes recreated — all in
        one transaction. Copied rows are then settled at daemon startup —
        backfilled with the sole configured repository
        (:meth:`backfill_repo`) or, when several are configured, named from
        their issue URL (:meth:`attribute_repoless`), with whatever is left
        over dropped if discovery can re-create it (:meth:`drop_repoless`)
        and otherwise failed with an operator notice
        (:meth:`strand_repoless`) — so they are never claimed by whichever
        repo happens to be polled first.
        """
        rebuilds = (
            ("daemon_work_items", _WORK_ITEMS_BODY, _WORK_ITEMS_COLUMNS),
            ("daemon_requesters", _REQUESTERS_BODY, _REQUESTERS_COLUMNS),
        )
        todo = [r for r in rebuilds if "repo" not in _columns(self._conn, r[0])]
        if not todo:
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for table, body, columns in todo:
                tmp = f"{table}__new"
                self._conn.execute(f"CREATE TABLE {tmp} {body}")  # nosec B608 - literals above
                self._conn.execute(
                    f"INSERT INTO {tmp} ({columns}, repo) "  # nosec B608 - literals above
                    f"SELECT {columns}, '' FROM {table}"
                )
                self._conn.execute(f"DROP TABLE {table}")  # nosec B608 - literal above
                self._conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")  # nosec B608
                log.info("store.migrated", table=table, rebuilt_for="repo")
            # Dropping the old table took its indexes with it.
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def backfill_repo(self, repo: str | None) -> int:
        """Give repo-less rows the daemon's sole configured repository.

        Rows written before multi-repo (or by a single-repo daemon) carry
        ``repo = ''``. They belong to whatever repository *that* daemon
        polled, which only the caller knows — so the daemon names it at
        startup rather than letting the first repository discovered claim
        them. ``UPDATE OR IGNORE``: a row that would collide with an already
        qualified row for the same issue is left as it is. Returns the number
        of rows updated.
        """
        if not repo:
            return 0
        updated = 0
        with self._lock:
            for table in ("daemon_work_items", "daemon_requesters"):
                cur = self._conn.execute(
                    f"UPDATE OR IGNORE {table} SET repo = ? WHERE repo = ''",  # nosec B608
                    (repo,),
                )
                updated += cur.rowcount or 0
            self._conn.commit()
        if updated:
            log.info("store.repo_backfilled", repo=repo, rows=updated)
        return updated

    def attribute_repoless(self, repos: Sequence[str]) -> int:
        """Give repo-less rows the repository named by their issue URL.

        A row written before multi-repo carries ``repo = ''``, but its
        ``url`` (``https://github.com/<owner>/<name>/issues/<n>``) still
        names the repository it came from. When that repository is one of
        the configured ones the row can be attributed exactly, with no
        guessing — which matters most for rows discovery cannot re-create
        (see :meth:`drop_repoless`). ``UPDATE OR IGNORE``: a row colliding
        with an already qualified row for the same issue is left alone.
        Returns the number of rows updated.
        """
        known = {r.casefold(): r for r in repos if r}
        if not known:
            return 0
        updated = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, url FROM daemon_work_items WHERE repo = ''"
            ).fetchall()
            for row in rows:
                repo = known.get((_repo_from_url(row["url"]) or "").casefold())
                if repo is None:
                    continue
                cur = self._conn.execute(
                    "UPDATE OR IGNORE daemon_work_items SET repo = ? "
                    "WHERE item_id = ? AND repo = ''",
                    (repo, row["item_id"]),
                )
                updated += cur.rowcount or 0
            self._conn.commit()
        if updated:
            log.info("store.repo_attributed_from_url", rows=updated)
        return updated

    def drop_repoless(self) -> int:
        """Discard the repo-less rows discovery can genuinely re-create.

        Called at startup when no single configured repository can own them
        (several repos configured, or the legacy ``[github] repo`` is gone)
        and their issue URL named no configured repository either
        (:meth:`attribute_repoless`). Leaving such a row would be worse than
        dropping it: a ``repo = ''`` row no longer dedups against the
        repo-qualified item discovery builds for the same issue, so the
        issue would be queued twice and the second copy would fail to claim
        its trigger label; and running the legacy row itself would route the
        clone/branch/PR to whichever repository happens to be first in the
        config.

        Only *untouched queue* rows are deleted: ``state = 'queued'`` with
        ``claimed = 0`` and no pinned run. Those are the only ones the next
        poll re-creates, because claiming swaps the trigger label for the
        in-progress one — once an item is claimed its issue no longer
        matches the discovery search and can never be rediscovered. Claimed,
        running and resume-pending repo-less rows are therefore left for
        :meth:`strand_repoless` to settle visibly instead of vanishing.
        Terminal rows are kept as history — they are never dispatched and
        never dedup against a live item (a changed terminal row is
        superseded). Requester notes are kept too: they are read with a
        repo-less fallback only for unqualified items, and are harmless
        otherwise. Returns the number of rows deleted.
        """
        where = "WHERE repo = '' AND state = 'queued' AND claimed = 0 AND run_id IS NULL"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT item_id FROM daemon_work_items {where}"  # nosec B608 - literal above
            ).fetchall()
            if not rows:
                return 0
            self._conn.execute(
                f"DELETE FROM daemon_work_items {where}"  # nosec B608 - literal above
            )
            self._conn.commit()
        log.info(
            "store.repoless_items_dropped",
            rows=len(rows),
            items=[str(r["item_id"]) for r in rows],
            reason="no configured repository to attribute them to; they were never "
            "claimed, so discovery will re-create them repo-qualified",
        )
        return len(rows)

    def strand_repoless(self, reason: str, now: float) -> list[WorkItem]:
        """Settle the repo-less rows that must not simply vanish.

        Whatever :meth:`attribute_repoless` could not name and
        :meth:`drop_repoless` must not delete — claimed, running or
        resume-pending rows — is failed here with an explicit ``reason``, so
        the row stays visible in ``items`` and any pinned run reconciles
        against a real item rather than being force-failed as an orphan.

        No ``pending_report`` debt is left: these are exactly the rows whose
        repository could *not* be named, so a report would be posted against
        whichever repository the source happens to try first — a comment on
        an unrelated issue with the same number. The caller gets the items
        back instead and must raise an operator notice naming each id and
        issue URL, because their issue is left carrying
        ``sbxloop:in-progress`` for a human to clear.
        """
        marks = ", ".join("?" * len(TERMINAL_ITEM_STATES))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM daemon_work_items "  # nosec B608 - literals above
                f"WHERE repo = '' AND state NOT IN ({marks})",
                tuple(sorted(TERMINAL_ITEM_STATES)),
            ).fetchall()
            stranded = [_row_to_item(row) for row in rows]
            for item in stranded:
                self._conn.execute(
                    "UPDATE daemon_work_items SET state = 'failed', last_error = ?, "
                    "pending_report = NULL, updated_at = ? WHERE item_id = ?",
                    (reason[:2000], now, item.item_id),
                )
            self._conn.commit()
        if stranded:
            log.warning(
                "store.repoless_items_stranded",
                rows=len(stranded),
                items=[i.item_id for i in stranded],
                urls=[i.url for i in stranded],
                reason=reason,
            )
        return stranded

    # -- work items ----------------------------------------------------------

    def upsert_new(self, item: WorkItem, now: float) -> bool:
        """Record a discovered item; True if it was not already known.

        A finished row with the same issue is superseded when the content
        changed — re-triggering an edited issue is a new work item. The
        requester the concierge recorded for the issue, if any, is copied
        onto the item.
        """
        with self._lock:
            repo = item.repo or ""
            # Identity is (issue, repository), matched exactly — with one
            # concession: an item whose id is *unqualified* can only come
            # from a single-repo daemon (discovery qualifies ids as soon as
            # a second repository is configured), so it may adopt a row
            # written before items carried a repo at all. A qualified item
            # never does: letting it claim a repo-less row would hand rows
            # from the old sole repository to whichever repository happens
            # to be polled first. Those rows are given their repository once,
            # by name, in :meth:`backfill_repo` at daemon startup — or, when
            # no single repository can own them, dropped there
            # (:meth:`drop_repoless`) so discovery re-creates them qualified,
            # once every row that could be attributed has been.
            parsed = try_parse_gh_id(item.item_id)
            adopt_legacy = bool(repo) and (parsed is None or parsed.repo is None)
            if adopt_legacy:
                row = self._conn.execute(
                    "SELECT item_id, state, title, body, repo FROM daemon_work_items "
                    "WHERE source_key = ? AND repo IN (?, '') ORDER BY repo = ? DESC LIMIT 1",
                    (item.source_key, repo, repo),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT item_id, state, title, body, repo FROM daemon_work_items "
                    "WHERE source_key = ? AND repo = ? LIMIT 1",
                    (item.source_key, repo),
                ).fetchone()
            if row is not None and row["repo"] == "" and repo:
                # Backfill: the item now knows its repository.
                self._conn.execute(
                    "UPDATE daemon_work_items SET repo = ? WHERE item_id = ?",
                    (repo, row["item_id"]),
                )
                self._conn.commit()
            if row is not None:
                terminal = row["state"] in TERMINAL_ITEM_STATES
                changed = (row["title"], row["body"]) != (item.title, item.body)
                if not (terminal and changed):
                    return False
                log.debug(
                    "store.item_superseded",
                    item=row["item_id"],
                    previous_state=row["state"],
                    reason="terminal row, content changed",
                )
                self._conn.execute(
                    "DELETE FROM daemon_work_items WHERE item_id = ?", (row["item_id"],)
                )
            # Exact repository, plus — for the same reason as above — a note
            # written before notes carried one, but only for an unqualified
            # item (a single-repo daemon, where there is only one repository
            # the note can mean).
            where, params = (
                ("repo IN (?, '')", (item.source_key, repo))
                if repo and adopt_legacy
                else ("repo = ?", (item.source_key, repo))
                if repo
                else ("1 = 1", (item.source_key,))
            )
            requester = self._conn.execute(
                "SELECT requester_id FROM daemon_requesters "  # nosec B608 - literal above
                f"WHERE source_key = ? AND {where} ORDER BY repo != '' DESC LIMIT 1",
                params,
            ).fetchone()
            requested_by = (
                item.requested_by
                if item.requested_by
                else (str(requester["requester_id"]) if requester is not None else None)
            )
            self._conn.execute(
                "INSERT INTO daemon_work_items (item_id, source_key, title, body, url, state, "
                "attempts, claimed, run_id, last_error, created_at, updated_at, requested_by, "
                "repo) "
                "VALUES (?, ?, ?, ?, ?, 'queued', 0, 0, NULL, NULL, ?, ?, ?, ?)",
                (
                    normalize_item_id(item.item_id),
                    item.source_key,
                    item.title,
                    item.body,
                    item.url,
                    now,
                    now,
                    requested_by,
                    repo,
                ),
            )
            self._conn.commit()
            return True

    def note_requester(
        self, source_key: str, requester_id: str, now: float, repo: str | None = None
    ) -> None:
        """Remember who asked for the issue ``source_key`` (the concierge's
        write path), so the work item discovery builds from it carries the
        requester and the run's finish can ping them. ``repo`` scopes the
        note: the same issue number in two repositories is two requests."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO daemon_requesters "
                "(source_key, repo, requester_id, created_at) VALUES (?, ?, ?, ?)",
                (source_key, repo or "", requester_id, now),
            )
            self._conn.commit()

    def get(self, item_id: str) -> WorkItem | None:
        """Look the item up under either spelling of its id: a row stored
        with a legacy ``gh:1234`` key resolves for ``gh:issue:1234`` too."""
        where, params = _id_match(item_id)
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM daemon_work_items WHERE {where}",  # nosec B608
                params,
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
        """Operator abandon: queued/running/blocked → failed. ``run_id`` is
        kept so the ledger and ``sbxloop logs`` still tie the item to the run
        that made the operator give up on it; the loop treats "abandoned
        while pinned to my run" as its cue to cancel that run. The source is
        owed a report (``pending_report``): whoever delivers it — the loop's
        settle path, recovery, or the tick sweep after a row-only CLI
        abandon — clears the debt."""
        return self._transition(
            item_id,
            now,
            ("queued", "running", "blocked"),
            lambda item: f"{item_id} is already {item.state}",
            state="failed",
            last_error=reason[:2000],
            pending_report="abandoned",
        )

    def retry(self, item_id: str, now: float, reason: str | None = None) -> WorkItem:
        """Operator retry: a failed, blocked, cancelled or backoff-waiting
        item goes back to the queue as if freshly discovered. The attempt
        budget and retry backoff exist to bound *autonomous* churn, so a
        human decision starts both over — attempts reset, eligible on the
        next tick (the daily run cap still applies) — and the run is
        unpinned so the next dispatch plans from scratch instead of
        resuming the plan that failed (#228, #254). The source-side claim is
        kept: re-claiming would fail (the trigger label moved when it was
        first claimed). ``reason`` (e.g. "re-queued by X") is kept as
        ``last_error`` for the audit trail in ``items``; without one the old
        error is cleared. The source is owed a ``requeued`` report
        (re-claim, drop the failed/blocked label)."""
        return self._transition(
            item_id,
            now,
            ("failed", "blocked", "cancelled", "queued"),
            lambda item: (
                f"{item_id} is {item.state}; only failed, blocked, cancelled or queued items "
                "can be retried" + (" (abandon it first)" if item.state == "running" else "")
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
                + (" (use retry)" if item.state in ("failed", "blocked", "cancelled") else "")
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
        where, ids = _id_match(item_id)
        item_id = normalize_item_id(item_id)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE daemon_work_items SET {assignments}, updated_at = ? "  # nosec B608
                f"WHERE {where} AND state IN ({marks})",
                (*fields.values(), now, *ids, *allowed),
            )
            self._conn.commit()
            if cursor.rowcount == 1:
                fresh = self._require(item_id)
                log.debug("store.transition", item=item_id, state=fresh.state, **_loggable(fields))
                return fresh
            current = self._require(item_id)
            log.debug(
                "store.transition_refused",
                item=item_id,
                state=current.state,
                allowed=list(allowed),
                wanted=_loggable(fields),
            )
            raise ValueError(refuse(current))

    def pending_reports(self) -> list[WorkItem]:
        """Items whose decision the source has not been told about yet
        (an operator's abandon / retry, or a run's merged / blocked outcome
        whose report did not land), oldest decision first."""
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
        where, ids = _id_match(item_id)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE daemon_work_items SET pending_report = NULL "  # nosec B608
                f"WHERE {where} AND pending_report IS NOT NULL",
                ids,
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
        where, ids = _id_match(item_id)
        item_id = normalize_item_id(item_id)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE daemon_work_items SET state = 'running', attempts = attempts + 1, "
                f"run_id = ?, updated_at = ? WHERE {where}",  # nosec B608
                (run_id, now, *ids),
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
        where, ids = _id_match(item_id)
        item_id = normalize_item_id(item_id)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE daemon_work_items SET state = 'running', updated_at = ? "  # nosec B608
                f"WHERE {where} AND run_id = ?",
                (now, *ids, run_id),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                raise KeyError(f"work item {item_id!r} does not carry run {run_id!r}")
            self._conn.execute(
                "INSERT INTO daemon_run_resumes (run_id, item_id, resumed_at) VALUES (?, ?, ?)",
                (run_id, item_id, now),
            )
            self._conn.commit()

    def mark_done(
        self, item_id: str, now: float, *, pending_report: PendingReport | None = None
    ) -> None:
        """The run merged its PR. ``pending_report="merged"`` records the
        issue close as a debt the tick pays (and retries) — a GitHub hiccup
        on the close must not leave the issue open with no one to close it."""
        self._update(item_id, now, state="done", last_error=None, pending_report=pending_report)

    def mark_blocked(self, item_id: str, reason: str, now: float) -> None:
        """The run cleared its own bar but GitHub would not finish the PR;
        a human has to look. Terminal for the daemon, ``retry``-able by
        hand; the source is owed the blocked report."""
        self._update(
            item_id, now, state="blocked", last_error=reason[:2000], pending_report="blocked"
        )

    def mark_failed(self, item_id: str, error: str, now: float, *, requeue: bool) -> None:
        # A requeued item must not keep its run pinned: queued + run_id
        # means "resume this run", and a failed run is dispatched fresh. A
        # failed item keeps it for forensics.
        fields: dict[str, object] = {
            "state": "queued" if requeue else "failed",
            "last_error": error[:2000],
        }
        if requeue:
            fields["run_id"] = None
        self._update(item_id, now, **fields)

    def mark_cancelled(self, item_id: str, reason: str, now: float) -> None:
        """Operator cancel: terminal for the daemon, unlike ``mark_failed``
        which either re-queues (with backoff) or fails."""
        self._update(item_id, now, state="cancelled", last_error=reason[:2000])

    def mark_requeued_unstarted(self, item_id: str, now: float) -> None:
        """Crash between claim and start: back to the queue, claim kept."""
        self._update(item_id, now, state="queued", run_id=None)

    def _update(self, item_id: str, now: float, **fields: object) -> None:
        # Column names come from this module's own keyword calls, never from
        # input; every value is a bound parameter.
        assignments = ", ".join(f"{name} = ?" for name in fields)
        where, ids = _id_match(item_id)
        item_id = normalize_item_id(item_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE daemon_work_items SET {assignments}, updated_at = ? "  # nosec B608
                f"WHERE {where}",
                (*fields.values(), now, *ids),
            )
            self._conn.commit()
        log.debug("store.update", item=item_id, **_loggable(fields))

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
        where, ids = _id_match(item_id)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM daemon_run_resumes WHERE {where}",  # nosec B608
                ids,
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
            return [(str(row["run_id"]), normalize_item_id(str(row["item_id"]))) for row in rows]

    def finished_run_ids(self, run_ids: Sequence[str]) -> set[str]:
        """Which of these runs already have a ledger `finished_at`. Used at
        Discord watch reload to drop watches for runs that completed while
        the daemon was down: the `run_finished` event that would have
        pinged them has already fired, so reviving the entry would just
        leave it waiting for an event that will never come again."""
        if not run_ids:
            return set()
        with self._lock:
            placeholders = ", ".join("?" for _ in run_ids)
            rows = self._conn.execute(
                f"SELECT run_id FROM daemon_runs WHERE run_id IN ({placeholders}) "  # nosec B608
                "AND finished_at IS NOT NULL",
                tuple(run_ids),
            )
            return {str(r["run_id"]) for r in rows}

    def finish_ledger(self, run_id: str, result: str, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE daemon_runs SET finished_at = ?, result = ? WHERE run_id = ?",
                (now, result, run_id),
            )
            self._conn.commit()
        log.debug("store.ledger_closed", run=run_id, result=result)

    def runs_for_item(self, item_id: str) -> list[str]:
        """Run ids the item has been dispatched under, oldest first."""
        where, ids = _id_match(item_id)
        with self._lock:
            return [
                str(r["run_id"])
                for r in self._conn.execute(
                    f"SELECT run_id FROM daemon_runs WHERE {where} "  # nosec B608
                    "ORDER BY started_at",
                    ids,
                )
            ]

    # -- generic daemon state ---------------------------------------------------

    def get_value(self, key: str) -> str | None:
        """One ``daemon_state`` value (small process-level facts such as the
        concierge's SDK session id)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM daemon_state WHERE key = ?", (key,)
            ).fetchone()
            return None if row is None or row["value"] is None else str(row["value"])

    def set_value(self, key: str, value: str | None) -> None:
        """Set (or, with ``None``, delete) one ``daemon_state`` value."""
        with self._lock:
            if value is None:
                self._conn.execute("DELETE FROM daemon_state WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT OR REPLACE INTO daemon_state (key, value) VALUES (?, ?)",
                    (key, value),
                )
            self._conn.commit()

    def item_for_run(self, run_id: str) -> str | None:
        """The work item a run was dispatched for (ledger lookup)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT item_id FROM daemon_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return None if row is None else normalize_item_id(str(row["item_id"]))

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
        log.debug(
            "store.breaker",
            open=opened_at is not None,
            consecutive_failures=consecutive_failures,
        )
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO daemon_state (key, value) VALUES (?, ?)",
                [
                    ("breaker_opened_at", "" if opened_at is None else repr(float(opened_at))),
                    ("consecutive_failures", str(int(consecutive_failures))),
                ],
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

    # -- run watches -----------------------------------------------------------

    def add_run_watch(self, run_id: str, watcher_id: str, now: float) -> None:
        """Register interest in a run's completion; idempotent per watcher."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO daemon_run_watches (run_id, watcher_id, created_at) "
                "VALUES (?, ?, ?)",
                (run_id, watcher_id, now),
            )
            self._conn.commit()

    def run_watchers(self, run_id: str) -> list[str]:
        with self._lock:
            return [
                str(r["watcher_id"])
                for r in self._conn.execute(
                    "SELECT watcher_id FROM daemon_run_watches WHERE run_id = ? ORDER BY rowid",
                    (run_id,),
                )
            ]

    def take_run_watchers(self, run_id: str) -> list[str]:
        """Return the run's watchers and clear them in one transaction."""
        with self._lock:
            watchers = [
                str(r["watcher_id"])
                for r in self._conn.execute(
                    "SELECT watcher_id FROM daemon_run_watches WHERE run_id = ? ORDER BY rowid",
                    (run_id,),
                )
            ]
            self._conn.execute("DELETE FROM daemon_run_watches WHERE run_id = ?", (run_id,))
            self._conn.commit()
            return watchers

    def all_run_watches(self) -> dict[str, list[str]]:
        """Every pending watch, for reloading the bridge registry at startup."""
        with self._lock:
            watches: dict[str, list[str]] = {}
            for r in self._conn.execute(
                "SELECT run_id, watcher_id FROM daemon_run_watches ORDER BY rowid"
            ):
                watches.setdefault(str(r["run_id"]), []).append(str(r["watcher_id"]))
            return watches

    def clear_run_watch(self, run_id: str) -> None:
        """Drop a run's watch row without returning it — used by the bridge's
        `_evict_watch` when an entry is dropped for a reason other than a
        normal finish (a `WATCHERS_CAP` trim, or reconciling a reload
        against a run that already finished while the daemon was down),
        where `take_run_watchers`'s return value would just be discarded."""
        with self._lock:
            self._conn.execute("DELETE FROM daemon_run_watches WHERE run_id = ?", (run_id,))
            self._conn.commit()
