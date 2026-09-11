"""Count the verify commands a task has re-authored.

Revision ID: 0004
Revises: 0003

``tasks.verify_reauthors`` shipped with the re-authoring budget (#864), but
it was added to revision 0001's migrator rather than to a revision of its
own. 0001 had already shipped, so every deployed database was stamped at it
and Alembic never ran that code again: the column reached a fresh install
and nothing else. The daemon crash-looped on the first ``select(Task)``
after the upgrade — ``no such column: tasks.verify_reauthors`` — and took
the run it was serving down with it.

This is that column, as a revision, where it should have been.

The add is guarded because the column is already present in the field on
any database hand-patched with the shipped ``ALTER`` to get a daemon back
up. Adding it twice is an error SQLite raises rather than absorbs, and a
recovered installation must not fail its next upgrade for having been
recovered.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_TABLE = "tasks"
_COLUMN = "verify_reauthors"


def _has_column(bind: sa.Connection) -> bool:
    return _COLUMN in {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({_TABLE})")}


def upgrade() -> None:
    if _has_column(op.get_bind()):
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    if not _has_column(op.get_bind()):
        return
    op.drop_column(_TABLE, _COLUMN)
