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
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from sbxloop.daemon.store import DaemonStore, apply_daemon_schema
from sbxloop.db import Base, current_revision, ensure_schema, head_revision, open_engine
from sbxloop.db.schema import _config
from sbxloop.db.session import BUSY_TIMEOUT_MS
from sbxloop.engine.store import StateStore, apply_engine_schema
from sbxloop_worker.protocol import Event
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


class TestAnInstallationUpgradesInPlace:
    """The acceptance test for #539: a deployed database keeps working.

    Everything above checks one shape or one revision. This checks the thing
    an operator actually does — take the database a released version wrote,
    with rows in it, install the new one, and carry on — and that the file
    the new code leaves behind is still one the *previous* version can read,
    which is what a failed deploy's rollback depends on.
    """

    @staticmethod
    def _released_database(path: Path) -> None:
        """A database as the last release left it: pre-ORM schema, real rows."""
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            apply_engine_schema(conn)
            apply_daemon_schema(conn)
            conn.execute(
                "INSERT INTO runs (run_id, outcome, state, config_json, created_at,"
                " updated_at, kind, branch, pr_number) VALUES"
                " ('r_old', 'ship it', 'merged', '{}', 100.0, 200.0, 'code', 'sbx/x', 4)"
            )
            conn.execute(
                "INSERT INTO tasks (run_id, task_id, order_idx, state, spec_json)"
                " VALUES ('r_old', 't1', 0, 'done', '{\"id\": \"t1\", \"title\": \"T1\"}')"
            )
            conn.execute(
                "INSERT INTO phase_attempts (run_id, task_id, phase, attempt, status,"
                " output_json, started_at, ended_at, turns) VALUES"
                " ('r_old', 't1', 'build', 1, 'ok', '{}', 100.0, 150.0, 3)"
            )
            conn.execute(
                "INSERT INTO events (run_id, ts, type, data_json)"
                " VALUES ('r_old', 120.0, 'run.started', '{}')"
            )
            conn.execute(
                "INSERT INTO reconciliations (run_id, round, anchor, status, resolved, ts)"
                " VALUES ('r_old', -2, 'lint', 'spent', 0, 130.0)"
            )
            conn.execute(
                "INSERT INTO daemon_work_items (item_id, source_key, title, state, repo,"
                " created_at, updated_at, run_id) VALUES"
                " ('gh:issue:4', '4', 'Fix the thing', 'done', 'acme/alpha', 100.0, 200.0,"
                " 'r_old')"
            )
            conn.execute(
                "INSERT INTO daemon_runs (run_id, item_id, started_at, finished_at, result)"
                " VALUES ('r_old', 'gh:issue:4', 100.0, 200.0, 'done')"
            )
            conn.commit()
        finally:
            conn.close()

    def test_it_opens_migrates_and_reads_every_row_back(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        self._released_database(path)

        store, daemon = StateStore(path), DaemonStore(path)
        try:
            run = store.get_run("r_old")
            assert (run.state, run.kind, run.branch, run.pr_number) == (
                "merged",
                "code",
                "sbx/x",
                4,
            )
            assert [t.spec.id for t in store.get_tasks("r_old")] == ["t1"]
            assert [a.phase for a in store.phase_attempts("r_old")] == ["build"]
            assert [e.type for _, e in store.events("r_old")] == ["run.started"]
            # Read through the accessor whose kind revision 0002 backfilled.
            assert store.advisory_rounds("r_old") == frozenset({"lint"})

            item = daemon.get("gh:issue:4")
            assert item is not None and item.state == "done" and item.repo == "acme/alpha"
            assert daemon.runs_for_item("gh:issue:4") == ["r_old"]
        finally:
            store.close()
            daemon.close()

    def test_it_keeps_writing_after_the_upgrade(self, tmp_path: Path) -> None:
        """Not just readable — the upgraded database is still the live one."""
        path = tmp_path / "state.db"
        self._released_database(path)
        store = StateStore(path)
        try:
            store.create_run("r_new", "another")
            store.set_run_state("r_new", "building")
            store.append_event(
                Event(ts=300.0, run_id="r_new", job_id=None, type="run.started", data={})
            )
            assert {r.run_id for r in store.list_runs()} == {"r_old", "r_new"}
        finally:
            store.close()

    def test_the_previous_version_can_still_read_what_the_new_one_wrote(
        self, tmp_path: Path
    ) -> None:
        """The rollback contract, checked rather than asserted.

        A failed deploy reinstalls the previous version and restarts it
        against this database; it does not restore the snapshot it took. So
        every column that version reads has to still be there, and mean what
        it did. `apply_engine_schema`/`apply_daemon_schema` are that
        version's migrator — running them over a migrated database must be
        a no-op, and its own queries must still answer.
        """
        path = tmp_path / "state.db"
        self._released_database(path)
        StateStore(path).close()
        DaemonStore(path).close()

        conn = sqlite3.connect(path)
        try:
            before = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            ).fetchall()
            # The old migrator, re-run as a rolled-back release would.
            apply_engine_schema(conn)
            apply_daemon_schema(conn)
            after = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            ).fetchall()
            assert after == before, "the previous version's migrator changed the schema"

            # And the columns it reads still answer, sentinel `round`
            # included — that is what tells it an advisory round was spent.
            conn.row_factory = sqlite3.Row
            run = conn.execute("SELECT * FROM runs WHERE run_id = 'r_old'").fetchone()
            assert run["state"] == "merged" and run["pr_number"] == 4
            spent = conn.execute(
                "SELECT anchor FROM reconciliations WHERE run_id = 'r_old' AND round = -2"
            ).fetchall()
            assert [r["anchor"] for r in spent] == ["lint"]
        finally:
            conn.close()


class TestTheBaselineIsFrozen:
    """Revision 0001 is a released artefact, not a live schema definition.

    Its body is the pre-ORM migrators, and every deployed database is
    already stamped at it, so Alembic will never run it against one again.
    A column added to it after it shipped therefore reaches a fresh install
    and nothing else — which is exactly how `tasks.verify_reauthors` came to
    be missing in the field while being mapped on the ORM model, and how the
    daemon came to crash-loop on `no such column` (#864, fixed by 0004).

    These two tests close that hole from both ends: the baseline may not
    move, and head must arrive at the models.
    """

    FIXTURE = Path(__file__).parent.parent / "fakes" / "baseline_0001.sql"

    @staticmethod
    def _objects(conn: sqlite3.Connection) -> dict[str, str]:
        """Every schema object, normalised the way the fixture is written.

        `sqlite_sequence` is SQLite's own bookkeeping for AUTOINCREMENT and
        `alembic_version` is the stamp itself; neither is ours to freeze.
        """
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
            " AND name NOT IN ('alembic_version', 'sqlite_sequence')"
        ).fetchall()
        return {name: " ".join(sql.split()) for name, sql in rows}

    @classmethod
    def _frozen(cls) -> dict[str, str]:
        """The fixture, read back as the same mapping `_objects` returns.

        Each object is written as a `-- <name>` line and then its statement,
        so the file stays readable as SQL and diffs one object at a time.
        """
        objects: dict[str, str] = {}
        name: str | None = None
        for line in cls.FIXTURE.read_text().splitlines():
            # A name line is a comment holding exactly one token; the
            # file's prose header is comments too, and is multi-word.
            if line.startswith("-- ") and len(line[3:].split()) == 1:
                name = line[3:].strip()
            elif line.strip() and not line.startswith("--"):
                assert name is not None, f"statement with no name: {line}"
                objects[name] = line.rstrip(";")
                name = None
        return objects

    def test_the_migrators_still_produce_the_shape_0001_shipped(self, tmp_path: Path) -> None:
        """If this fails, your schema change belongs in a new revision.

        Do not regenerate the fixture to make it pass: the fixture is what
        every installation in the field was built by, and moving it only
        hides the drift from the one path that matters.
        """
        path = tmp_path / "state.db"
        _via_stores(path)
        conn = sqlite3.connect(path)
        try:
            assert self._objects(conn) == self._frozen()
        finally:
            conn.close()

    def test_a_database_stamped_at_the_baseline_reaches_the_models(self, tmp_path: Path) -> None:
        """The production upgrade path, which no other test here walks.

        A deployed installation is *already* stamped at 0001, so it is built
        by the migrators and then stamped **without running them again** —
        which is the state a release leaves behind, and the state in which
        an edit to 0001 is silently inert. Whatever the ORM maps has to be
        there at the end of the upgrade, so every column added since 0001
        needs a revision of its own to carry it.

        The shape it starts from is the one
        `test_the_migrators_still_produce_the_shape_0001_shipped` pins to
        `baseline_0001.sql`, so this walks the released baseline even though
        it calls today's migrators to lay it down.
        """
        path = tmp_path / "state.db"
        _via_stores(path)

        engine = open_engine(path)
        try:
            with engine.connect() as conn_:
                command.stamp(_config(conn_), "0001")
            assert current_revision(engine) == "0001"
            ensure_schema(engine)
            assert current_revision(engine) == head_revision()
            with engine.connect() as conn_:
                drift = compare_metadata(MigrationContext.configure(conn_), Base.metadata)
            assert drift == [], f"head does not match the models: {drift}"
        finally:
            engine.dispose()

    def test_a_hand_patched_database_still_upgrades(self, tmp_path: Path) -> None:
        """Recovering by hand must not cost the installation its next upgrade.

        The only way to get a crash-looping daemon back up before 0004
        existed was to run the shipped `ALTER` against the file directly, so
        that is the shape some databases are in. 0004 has to absorb it:
        SQLite raises on a duplicate column rather than ignoring it.
        """
        path = tmp_path / "state.db"
        _via_stores(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN verify_reauthors INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        finally:
            conn.close()

        engine = open_engine(path)
        try:
            with engine.connect() as conn_:
                command.stamp(_config(conn_), "0001")
            ensure_schema(engine)
            assert current_revision(engine) == head_revision()
            with engine.connect() as conn_:
                assert compare_metadata(MigrationContext.configure(conn_), Base.metadata) == []
        finally:
            engine.dispose()


def _from_metadata(path: Path) -> None:
    """Build the file from ``Base.metadata`` alone, the way ``create_all``
    does for anyone who reaches for the models instead of the revisions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = open_engine(path)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


class TestTheModelsDescribeTheDeployedSchema:
    """``base.py`` says ``Base.metadata`` is the single source of truth for
    the schema. It has to actually be one: ``alembic revision --autogenerate``
    compares against it, and anything that builds a database from it —
    ``create_all`` in a test, a future shortcut for a fresh file — gets
    whatever it says.

    So this compares a database built from the metadata against one built by
    running every revision, which is what is on disk. They agreed only after
    the models were corrected; see the commit that added this class."""

    def test_the_two_paths_produce_the_same_objects(self, tmp_path: Path) -> None:
        """By name, not by DDL text. ``_ddl`` compares statements verbatim,
        which is the right test for two hand-written paths but not for this
        pair: revision 0001 carries hand-written SQL and ``create_all``
        generates its own, so the text differs by whitespace while the schema
        does not. What the objects actually are is asserted below."""
        built, replayed = tmp_path / "built" / "state.db", tmp_path / "replayed" / "state.db"
        _via_alembic(built)
        _from_metadata(replayed)
        assert set(_ddl(built)) == set(_ddl(replayed))

    def test_the_two_paths_agree_on_column_order_and_defaults(self, tmp_path: Path) -> None:
        """``_ddl`` compares statements, which a reordered ``ALTER TABLE``
        column or a differently quoted default would slip past on a table
        Alembic rebuilt. Ask SQLite what it actually stored."""
        built, replayed = tmp_path / "built" / "state.db", tmp_path / "replayed" / "state.db"
        _via_alembic(built)
        _from_metadata(replayed)

        def columns(path: Path) -> dict[str, list[tuple[object, ...]]]:
            conn = sqlite3.connect(path)
            try:
                names = [
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
                    )
                ]
                return {
                    table: [tuple(row[1:]) for row in conn.execute(f"PRAGMA table_info({table})")]
                    for table in sorted(names)
                }
            finally:
                conn.close()

        assert columns(built) == columns(replayed)

    def test_a_migrated_database_matches_the_metadata(self, tmp_path: Path) -> None:
        """Alembic's own comparison, which is what autogenerate would use."""
        path = tmp_path / "state.db"
        _via_alembic(path)
        engine = open_engine(path)
        try:
            assert current_revision(engine) == head_revision()
            with engine.connect() as conn:
                assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
        finally:
            engine.dispose()

    def test_a_legacy_database_upgrades_to_match_the_metadata_too(self, tmp_path: Path) -> None:
        """A file written by a released version has tables but no
        ``alembic_version``. It upgrades in place, and has to land on the
        same shape the models describe."""
        path = tmp_path / "state.db"
        _via_stores(path)
        engine = open_engine(path)
        try:
            ensure_schema(engine)
            assert current_revision(engine) == head_revision()
            with engine.connect() as conn:
                assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
        finally:
            engine.dispose()

    def test_a_fresh_database_keeps_working_through_the_stores(self, tmp_path: Path) -> None:
        """A schema that only compares equal is not the claim being made."""
        path = tmp_path / "state.db"
        store = StateStore(path)
        try:
            store.create_run("r1", "an outcome")
            store.append_event(Event.now("run.start", "r1", outcome="an outcome"))
            assert [row.run_id for row in store.list_runs()] == ["r1"]
        finally:
            store.close()
        daemon = DaemonStore(path)
        try:
            assert daemon.queued() == []
            assert daemon.get("gh:issue:1") is None
        finally:
            daemon.close()
