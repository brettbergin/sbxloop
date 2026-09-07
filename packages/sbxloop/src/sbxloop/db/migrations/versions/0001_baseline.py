"""Baseline: every shape this project ever wrote, brought to 401bbc3.

Revision ID: 0001
Revises:

This revision does not create a schema from a model — it *is* the
hand-written migrator both stores ran on open before #539, called here
unchanged. That is deliberate.

A deployed ``state.db`` can be any of sixteen historical shapes and carries
no ``alembic_version``. Detecting which one it is would mean rewriting the
inspect-and-ALTER logic that already exists, is already tested against
``tests/fakes/legacy_db.py``, and has already run on every installation in
the field. So this revision keeps that code as its body and takes its
contract from it: *bring any database this project ever wrote up to the
current shape, and create it from nothing if it is absent.*

A database that is already current comes out untouched — every statement in
here is guarded by ``IF NOT EXISTS`` or a column/key check — so stamping an
existing installation costs one pass of introspection and nothing else.

Revisions from 0002 on are ordinary Alembic operations against
``Base.metadata``.
"""

from __future__ import annotations

import sqlite3

from alembic import op

from sbxloop.daemon.store import apply_daemon_schema
from sbxloop.engine.store import apply_engine_schema

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The migrators below predate SQLAlchemy and speak DBAPI, which is the
    # point: they are the code that already ran on every deployed database.
    conn = op.get_bind().connection.driver_connection
    assert isinstance(conn, sqlite3.Connection)  # nosec B101 - SQLite is the only backend
    apply_engine_schema(conn)
    apply_daemon_schema(conn)


def downgrade() -> None:
    # There is nothing below the baseline: a database that predates it is a
    # database with no tables, and dropping nineteen tables to reach that is
    # not a migration, it is data loss. `sbxloop backup` is the way back.
    raise NotImplementedError("the baseline revision is not reversible")
