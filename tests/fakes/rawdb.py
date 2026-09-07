"""Raw statements against a store's database, for tests that need them.

A handful of tests have to write something no store method will produce: a
run backdated past a retention window, a state spelling the current code no
longer emits, a config blanked to look like a row from before it was saved.
They used to reach for ``store._conn``. Since #539 there is no such
attribute — the store holds a SQLAlchemy engine — so the escape hatch lives
here, named, rather than being spelled out inline at twenty call sites.

Reach for this only when the thing being set up genuinely has no API. If a
store method would do it, use the store method: a test that bypasses the API
to arrange state is a test that stops noticing when the API breaks.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from sqlalchemy import Engine


class HasEngine(Protocol):
    _engine: Engine


def exec_raw(store: HasEngine, sql: str, params: Sequence[Any] = ()) -> None:
    """Run one statement against ``store``'s database and commit it.

    Positional parameters, bound the way the driver binds them — these are
    raw statements against the physical schema, not queries the ORM builds.
    """
    with store._engine.begin() as conn:
        conn.exec_driver_sql(sql, tuple(params))


def query_raw(store: HasEngine, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    """Read raw rows out of a store's database.

    For the checks that are *about* the physical schema — a PRAGMA, a
    sqlite_master listing — which no store method exposes and none should.
    """
    with store._engine.connect() as conn:
        return list(conn.exec_driver_sql(sql, tuple(params)))


def backdate(store: HasEngine, run_id: str, updated_at: float) -> None:
    """Move a run's ``updated_at`` into the past.

    Retention, orphan detection and the gc sweep all key off how long a run
    has sat still, and no store method moves that backwards — ``touch_run``
    only ever moves it forward.
    """
    exec_raw(store, "UPDATE runs SET updated_at = ? WHERE run_id = ?", (updated_at, run_id))
