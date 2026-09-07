"""The Alembic baseline must reproduce, exactly, what the stores open today.

The migration to SQLAlchemy (#539) is only safe if the schema does not move
under a deployed database. These tests are the proof: every frozen shape in
``tests/fakes/legacy_db`` is opened both ways and the resulting DDL compared
statement for statement.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sbxloop.daemon.store import apply_daemon_schema
from sbxloop.db import current_revision, ensure_schema, head_revision, open_engine
from sbxloop.db.session import BUSY_TIMEOUT_MS
from sbxloop.engine.store import StateStore, apply_engine_schema
from tests.fakes import legacy_db


def _ddl(path: Path) -> dict[str, str]:
    """Every object in the database, keyed by name, normalised for whitespace.

    ``alembic_version`` is excluded: it is the one table the old path never
    creates, and its presence is the point of the change rather than a drift.
    """
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
    finally:
        conn.close()
    return {name: " ".join(sql.split()) for name, sql in rows if name not in {"alembic_version"}}


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir(parents=True, exist_ok=True)
    new.mkdir(parents=True, exist_ok=True)
    return old, new


def _via_stores(path: Path) -> None:
    """Bring the file up the way a released version did, with no Alembic.

    The stores themselves migrate through Alembic now, so opening one would
    compare the new path against itself. These are the functions a previous
    release ran on open, called against a bare connection exactly as it
    called them — which is what a database in the field was built by.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        apply_engine_schema(conn)
        apply_daemon_schema(conn)
    finally:
        conn.close()


def _via_alembic(path: Path) -> None:
    """Open the file the way the code does now."""
    engine = open_engine(path)
    try:
        ensure_schema(engine)
    finally:
        engine.dispose()


class TestBaselineMatchesTheStores:
    def test_a_fresh_database_gets_the_same_schema_either_way(self, tmp_path: Path) -> None:
        old, new = tmp_path / "old" / "state.db", tmp_path / "new" / "state.db"
        _via_stores(old)
        _via_alembic(new)
        assert _ddl(new) == _ddl(old)

    @pytest.mark.parametrize("shape", sorted(legacy_db.DAEMON_SHAPES))
    def test_every_frozen_daemon_shape_upgrades_the_same_way(
        self, tmp_path: Path, shape: str
    ) -> None:
        old_dir, new_dir = _dirs(tmp_path)
        old = legacy_db.daemon_db(old_dir, shape)
        new = legacy_db.daemon_db(new_dir, shape)
        _via_stores(old)
        _via_alembic(new)
        assert _ddl(new) == _ddl(old)

    @pytest.mark.parametrize("shape", sorted(legacy_db.ENGINE_SHAPES))
    def test_every_frozen_engine_shape_upgrades_the_same_way(
        self, tmp_path: Path, shape: str
    ) -> None:
        old_dir, new_dir = _dirs(tmp_path)
        old = legacy_db.engine_db(old_dir, shape)
        new = legacy_db.engine_db(new_dir, shape)
        _via_stores(old)
        _via_alembic(new)
        assert _ddl(new) == _ddl(old)

    def test_a_store_still_opens_a_database_alembic_migrated(self, tmp_path: Path) -> None:
        """The two paths are interchangeable in both directions."""
        path = tmp_path / "state.db"
        _via_alembic(path)
        store = StateStore(path)
        try:
            store.create_run("r1", "an outcome")
            assert store.get_run("r1").state == "created"
        finally:
            store.close()

    def test_alembic_stamps_a_database_a_release_created(self, tmp_path: Path) -> None:
        """An installation upgraded in place is stamped, not rebuilt."""
        path = tmp_path / "state.db"
        _via_stores(path)
        before = _ddl(path)
        engine = open_engine(path)
        try:
            assert current_revision(engine) is None
            ensure_schema(engine)
            assert current_revision(engine) == head_revision()
        finally:
            engine.dispose()
        assert _ddl(path) == before


class TestPragmas:
    """The durability and concurrency settings the stores relied on."""

    def test_a_read_write_engine_keeps_wal_normal_and_the_busy_timeout(
        self, tmp_path: Path
    ) -> None:
        engine = open_engine(tmp_path / "state.db")
        try:
            with engine.connect() as conn:
                assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
                assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1
                assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == BUSY_TIMEOUT_MS
        finally:
            engine.dispose()

    def test_a_readonly_engine_refuses_a_write(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        _via_alembic(path)
        engine = open_engine(path, readonly=True)
        try:
            with engine.connect() as conn:
                assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == BUSY_TIMEOUT_MS
                with pytest.raises(Exception, match="readonly"):
                    conn.exec_driver_sql("CREATE TABLE nope (x)")
        finally:
            engine.dispose()
