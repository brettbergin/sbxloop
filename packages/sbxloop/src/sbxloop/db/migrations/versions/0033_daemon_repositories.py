"""Where a repository is registered: the daemon's database.

Adds ``daemon_repositories``: one row per registered repository — its
name, forge, whether it is enabled, its delivery base, where the
registration came from (the file's ``[[vcs.repos]]`` import, or the API)
and who made it. A removed registration keeps its row with ``removed_at``
set, so the file's copy is not imported again at the next start.

Additive: one new table nothing older reads. Re-runnable: the create
checks what is already there.

Revision ID: 0033
Revises: 0032
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

_TABLE = "daemon_repositories"


def upgrade() -> None:
    if _TABLE not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            _TABLE,
            sa.Column("repo", sa.Text(), primary_key=True),
            sa.Column("kind", sa.Text(), nullable=True),
            sa.Column("enabled", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("deliver_base", sa.Text(), nullable=True),
            sa.Column("source", sa.Text(), nullable=False),
            sa.Column("created_by", sa.Text(), nullable=True),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("updated_at", sa.REAL(), nullable=False),
            sa.Column("removed_at", sa.REAL(), nullable=True),
        )


def downgrade() -> None:
    op.drop_table(_TABLE)
