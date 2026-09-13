"""Persisted pause holds, and a revision counter on the rows a remote
command may act on.

``daemon_holds`` keeps the named pause holds across restarts. ``revision``
on ``runs``, ``daemon_work_items``, ``daemon_merge_gates`` and
``daemon_review_holds`` is bumped by an ``AFTER UPDATE`` trigger on every
write, whichever release wrote it — a rolled-back release's writes bump it
too — so a command carrying ``expected_revision`` can be refused when the
person acted on a superseded state. Additive: a new table, defaulted
columns, triggers that touch nothing an older release reads.

Re-runnable on purpose: a database whose stamp was rewound meets what it
already has and skips it.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from sbxloop.db.revisions import REVISIONED, trigger_ddl, trigger_name

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    return any(col["name"] == column for col in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("daemon_holds"):
        op.create_table(
            "daemon_holds",
            sa.Column("name", sa.Text(), primary_key=True, nullable=True),
            sa.Column("owner_id", sa.Text(), nullable=True),
            sa.Column("owner_display", sa.Text(), nullable=True),
            sa.Column("via", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("reason", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("operation_id", sa.Text(), nullable=True),
        )
    for table, key in REVISIONED:
        if not _has_column(table, "revision"):
            op.add_column(
                table,
                sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
            )
        op.execute(sa.text(trigger_ddl(table, key)))


def downgrade() -> None:
    for table, _ in REVISIONED:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name(table)}"))
        with op.batch_alter_table(table) as batch:
            batch.drop_column("revision")
    op.drop_table("daemon_holds")
