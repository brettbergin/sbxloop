"""Store the agents people create beside the built-ins and ``[[agents]]``.

Additive: a new table nothing older reads. Re-runnable: a database that
already has the table is left as it is.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("agents"):
        op.create_table(
            "agents",
            sa.Column("slug", sa.Text(), primary_key=True),
            sa.Column("spec_json", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'active'")),
            sa.Column("created_by", sa.Text(), nullable=True),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("updated_at", sa.REAL(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        )


def downgrade() -> None:
    op.drop_table("agents")
