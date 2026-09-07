"""Alembic environment for the state database.

sbxloop never runs ``alembic`` from a shell: the daemon migrates itself when
it opens the database, so the only entry point is
:func:`sbxloop.db.schema.ensure_schema`, which passes the live connection in
through ``config.attributes``. Offline mode exists only so
``alembic revision --autogenerate`` works for a developer adding a revision.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

from sbxloop.db.base import Base

target_metadata = Base.metadata


def run_migrations_online(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=False,
    )
    with context.begin_transaction():
        context.run_migrations()


connectable = context.config.attributes.get("connection")
if connectable is None:  # pragma: no cover - developer tooling only
    context.configure(url="sqlite://", target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online(connectable)
