"""The models must describe the database that is actually on disk.

``Base.metadata`` becomes the source of truth for revisions from 0002 on, so
a model that disagrees with the deployed schema would write migrations
against a database nobody has. These tests compare the two by introspection:
columns, types, nullability, defaults, primary keys and indexes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, inspect

import sbxloop.db.engine_models  # noqa: F401  - registers the models on Base
from sbxloop.daemon.store import DaemonStore
from sbxloop.db import Base, open_engine
from sbxloop.engine.store import StateStore

ENGINE_TABLES = ("runs", "tasks", "phase_attempts", "reconciliations", "events")


@pytest.fixture
def live(tmp_path: Path) -> Engine:
    """A database opened the way a deployed installation opens it."""
    path = tmp_path / "live" / "state.db"
    path.parent.mkdir(parents=True)
    StateStore(path).close()
    DaemonStore(path).close()
    return open_engine(path)


@pytest.fixture
def modelled(tmp_path: Path) -> Engine:
    """A database created from ``Base.metadata`` alone."""
    path = tmp_path / "modelled" / "state.db"
    engine = open_engine(path)
    Base.metadata.create_all(engine)
    return engine


def _columns(engine: Engine, table: str) -> dict[str, dict[str, Any]]:
    return {
        col["name"]: {
            "type": str(col["type"]).upper(),
            "nullable": col["nullable"],
            "default": _norm_default(col["default"]),
        }
        for col in inspect(engine).get_columns(table)
    }


def _norm_default(default: object) -> str | None:
    """SQLite echoes a default back as it was written; compare the value."""
    if default is None:
        return None
    return str(default).strip().strip("'").strip('"')


@pytest.mark.parametrize("table", ENGINE_TABLES)
class TestModelsMatchTheDeployedSchema:
    def test_the_columns_agree(self, live: Engine, modelled: Engine, table: str) -> None:
        assert _columns(modelled, table) == _columns(live, table)

    def test_the_primary_key_agrees(self, live: Engine, modelled: Engine, table: str) -> None:
        assert (
            inspect(modelled).get_pk_constraint(table)["constrained_columns"]
            == inspect(live).get_pk_constraint(table)["constrained_columns"]
        )

    def test_the_indexes_agree(self, live: Engine, modelled: Engine, table: str) -> None:
        def named(engine: Engine) -> dict[str, list[str]]:
            return {
                ix["name"]: list(ix["column_names"])
                for ix in inspect(engine).get_indexes(table)
                if ix["name"]
            }

        assert named(modelled) == named(live)


class TestAutogenerateSeesNoDrift:
    """The check that will actually be run when a revision is written.

    Column-by-column comparison is the readable version; this is the one that
    binds. If Alembic can see a difference between the models and a deployed
    database, then `alembic revision --autogenerate` would write a migration
    to "fix" a schema that is already correct — which, on a table SQLite can
    only alter by rebuilding, is how a migration eats a database.
    """

    def test_the_models_match_a_database_opened_the_old_way(self, live: Engine) -> None:
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        modelled = set(Base.metadata.tables)
        with live.connect() as conn:
            ctx = MigrationContext.configure(
                conn,
                opts={"include_name": lambda name, type_, _p: type_ != "table" or name in modelled},
            )
            diff = compare_metadata(ctx, Base.metadata)
        assert diff == []


class TestTheModelledSetIsComplete:
    def test_every_engine_table_has_a_model(self) -> None:
        assert set(ENGINE_TABLES) <= set(Base.metadata.tables)

    def test_no_model_invents_a_table_the_database_lacks(self, live: Engine) -> None:
        """Anything modelled must exist on a database opened the old way."""
        on_disk = set(inspect(live).get_table_names())
        assert set(Base.metadata.tables) <= on_disk
