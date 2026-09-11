"""Engines and transactions for the one state database.

Every connection this project makes to ``<home>/state/state.db`` is built
here, and the shape it is built in is not incidental — it reproduces, on
purpose, what the hand-written ``sqlite3`` stores did before #539:

* **One connection per store.** ``StaticPool`` plus a ``creator`` that passes
  ``check_same_thread=False`` gives exactly one connection, shared by every
  thread, for the life of the store. The daemon holds two such engines (its
  own and the engine's) against the same file; the operator console holds a
  read-only pair in a second process, and the concierge a private read-only
  one of its own. WAL is what lets all of them coexist, so a pool that opened
  connections on demand would change the concurrency story, not just the
  plumbing.
* **The lock stays in the store.** These engines are not thread-safe on their
  own; each store keeps the ``threading.RLock`` that serialises its callers.
  Nothing here replaces it.
* **``isolation_level=None``** turns off pysqlite's implicit ``BEGIN`` so this
  module decides where transactions start. Ordinary ones open with a plain
  ``BEGIN`` (a deferred, read-friendly lock); :func:`begin_immediate` opens
  with ``BEGIN IMMEDIATE`` for the check-then-write paths that must hold the
  write lock across both halves. Making every transaction immediate would
  take a write lock to answer a read, which is why it is opt-in.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.pool import StaticPool

# Wait rather than fail when another connection holds the write lock: the
# daemon, the console and any CLI command share this file.
BUSY_TIMEOUT_MS = 5000

# Execution option naming the BEGIN statement a transaction opens with.
BEGIN_OPTION = "sbxloop_begin"


def readonly_uri(path: Path) -> str:
    """A ``mode=ro`` URI for ``path``, percent-encoded by ``as_uri``."""
    return f"{path.resolve().as_uri()}?mode=ro"


def open_engine(path: Path, *, readonly: bool = False, owns_schema: bool = True) -> Engine:
    """Open the state database at ``path``.

    ``readonly`` opens through a read-only URI and sets no pragma but the busy
    timeout — the console's handle, which must never write and never migrate.
    A read-write open creates the parent directory, puts the file in WAL mode
    and leaves durability at ``synchronous=NORMAL``: commits stop fsyncing one
    by one, and a crash can lose the tail of the WAL but never corrupt the
    database.

    ``owns_schema=False`` is a read-write open that sets neither of those.
    Changing the journal mode takes a brief exclusive lock, and the operator
    console writes to a file a live daemon already put in WAL — asking again
    would be contending for a lock to assert something already true. The
    daemon sets them; a second process joins.

    Applying no schema is deliberate. Call :func:`sbxloop.db.ensure_schema`
    for that, so opening a database and migrating it stay separable.
    """
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)

    def _connect() -> sqlite3.Connection:
        if readonly:
            return sqlite3.connect(
                readonly_uri(path), uri=True, check_same_thread=False, isolation_level=None
            )
        return sqlite3.connect(path, check_same_thread=False, isolation_level=None)

    engine = create_engine(
        "sqlite://",
        creator=_connect,
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: sqlite3.Connection, _record: object) -> None:
        # The pysqlite dialect sets its own isolation_level on checkout, so
        # asking the creator for autocommit is not enough — it has to be
        # taken back here, after the dialect has had its say. Without this
        # the driver opens a transaction of its own and the explicit BEGIN
        # below fails as a nested one.
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        try:
            cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            if not readonly and owns_schema:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()

    @event.listens_for(engine, "begin")
    def _begin(conn: Connection) -> None:
        # pysqlite emits nothing here now that it no longer manages
        # transactions, so the statement is ours to choose: deferred by
        # default, immediate for the callers that asked for the write lock
        # up front (see `begin_immediate`).
        conn.exec_driver_sql(conn.get_execution_options().get(BEGIN_OPTION, "BEGIN"))

    return engine


@contextmanager
def begin_immediate(engine: Engine) -> Iterator[Connection]:
    """Run a block inside ``BEGIN IMMEDIATE``.

    Takes SQLite's write lock before the first statement, so a check and the
    write that depends on it are one atomic step against every other writer —
    the process holding this cannot be overtaken between the two. This is what
    makes a claim a claim; ``BEGIN`` alone would let two processes both read
    "unclaimed" and both write.
    """
    conn = engine.connect().execution_options(**{BEGIN_OPTION: "BEGIN IMMEDIATE"})
    try:
        with conn.begin():
            yield conn
    finally:
        conn.close()
