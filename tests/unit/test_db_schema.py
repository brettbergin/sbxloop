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
from alembic import command

from sbxloop.daemon.store import apply_daemon_schema
from sbxloop.db import current_revision, ensure_schema, open_engine
from sbxloop.db.schema import _config
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
    """Open the file the way the code does now: migrated to head."""
    engine = open_engine(path)
    try:
        ensure_schema(engine)
    finally:
        engine.dispose()


def _via_alembic_baseline(path: Path) -> None:
    """Migrate only as far as revision 0001.

    The baseline's contract is that it reproduces the pre-ORM migrator
    exactly, and that is what the comparisons below check. Later revisions
    are *meant* to change the schema — comparing head against the old path
    would only ever prove that they did.
    """
    engine = open_engine(path)
    try:
        with engine.connect() as conn:
            command.upgrade(_config(conn), "0001")
    finally:
        engine.dispose()


class TestBaselineMatchesTheStores:
    def test_a_fresh_database_gets_the_same_schema_either_way(self, tmp_path: Path) -> None:
        old, new = tmp_path / "old" / "state.db", tmp_path / "new" / "state.db"
        _via_stores(old)
        _via_alembic_baseline(new)
        assert _ddl(new) == _ddl(old)

    @pytest.mark.parametrize("shape", sorted(legacy_db.DAEMON_SHAPES))
    def test_every_frozen_daemon_shape_upgrades_the_same_way(
        self, tmp_path: Path, shape: str
    ) -> None:
        old_dir, new_dir = _dirs(tmp_path)
        old = legacy_db.daemon_db(old_dir, shape)
        new = legacy_db.daemon_db(new_dir, shape)
        _via_stores(old)
        _via_alembic_baseline(new)
        assert _ddl(new) == _ddl(old)

    @pytest.mark.parametrize("shape", sorted(legacy_db.ENGINE_SHAPES))
    def test_every_frozen_engine_shape_upgrades_the_same_way(
        self, tmp_path: Path, shape: str
    ) -> None:
        old_dir, new_dir = _dirs(tmp_path)
        old = legacy_db.engine_db(old_dir, shape)
        new = legacy_db.engine_db(new_dir, shape)
        _via_stores(old)
        _via_alembic_baseline(new)
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
        finally:
            engine.dispose()
        _via_alembic_baseline(path)
        engine = open_engine(path)
        try:
            assert current_revision(engine) == "0001"
        finally:
            engine.dispose()
        # Stamping an existing installation reads its shape; it does not
        # rebuild it. Later revisions may add to it — the baseline may not.
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


class TestReconciliationKindBackfill:
    """Revision 0002 has to name rows a released version already wrote.

    `reconciliations` told its six concerns apart by the sign of `round`.
    The backfill reads those bands, so a database full of rows written
    before the column existed has to come out classified correctly — and
    `round` has to come out untouched, because the release before this one
    still reads the bands and a failed deploy restarts it against this file.
    """

    BANDS = (
        # (round as written, the kind it should be named)
        (0, "review"),
        (3, "review"),
        (-1, "human"),
        (-2, "advisory"),
        (-3, "bot"),
        (-100, "confirm"),
        (-101, "confirm"),
        (-1000, "noted"),
        (-1001, "noted"),
    )

    def _pre_0002(self, path: Path) -> None:
        """A database at revision 0001, with a row in every band."""
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            apply_engine_schema(conn)
            apply_daemon_schema(conn)
            for index, (round_, kind) in enumerate(self.BANDS):
                # The bot band is keyed by a fixed anchor rather than a
                # location, so `bot_round_spent` can ask a yes/no question.
                anchor = "bot" if kind == "bot" else f"a{index}"
                conn.execute(
                    "INSERT INTO reconciliations (run_id, round, anchor, status, resolved, ts)"
                    " VALUES ('r1', ?, ?, 'answered', 0, 1.0)",
                    (round_, anchor),
                )
            conn.commit()
        finally:
            conn.close()

    def test_every_band_is_named_and_no_round_moves(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        self._pre_0002(path)
        _via_alembic(path)

        conn = sqlite3.connect(path)
        try:
            rows = conn.execute("SELECT round, kind FROM reconciliations ORDER BY rowid").fetchall()
        finally:
            conn.close()
        assert rows == [(round_, kind) for round_, kind in self.BANDS]

    def test_the_store_reads_each_concern_back_through_its_own_accessor(
        self, tmp_path: Path
    ) -> None:
        """The classification is not just a label — the readers use it."""
        path = tmp_path / "state.db"
        _via_alembic(path)
        store = StateStore(path)
        try:
            store.create_run("r1", "x")
            store.record_reconciliation("r1", 1, "src/a.py:1", "answered")
            store.record_human_reply("r1", "PRR_1", "answered")
            store.record_advisory_round("r1", "lint")
            store.record_bot_round("r1")
            store.record_confirmation("r1", 1, "src/b.py:2", "confirmed")
            store.record_noted("r1", 1, "src/c.py:3", "noted")

            assert store.reconciliations("r1", 1) == {"src/a.py:1": "answered"}
            assert store.answered_objections("r1") == {"PRR_1": "answered"}
            assert store.advisory_rounds("r1") == frozenset({"lint"})
            assert store.bot_round_spent("r1") is True
            assert store.confirmations("r1", 1) == {"src/b.py:2": "confirmed"}
            assert store.noted("r1", 1) == {"src/c.py:3": "noted"}
        finally:
            store.close()

    def test_a_row_written_before_the_column_still_reads_back(self, tmp_path: Path) -> None:
        """The whole point: an upgraded database answers as it always did."""
        path = tmp_path / "state.db"
        self._pre_0002(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "INSERT INTO runs (run_id, outcome, state, created_at, updated_at)"
                " VALUES ('r1', 'x', 'created', 1.0, 1.0)"
            )
            conn.commit()
        finally:
            conn.close()

        store = StateStore(path)
        try:
            assert store.answered_objections("r1") == {"a2": "answered"}
            assert store.advisory_rounds("r1") == frozenset({"a3"})
            assert store.bot_round_spent("r1") is True
            assert store.confirmations("r1", 0) == {"a5": "answered"}
            assert store.noted("r1", 0) == {"a7": "answered"}
        finally:
            store.close()
