"""Persist collaboration intent so queued turns can survive daemon restart.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("collaboration_turns")
    }
    if "intent" not in columns:
        op.add_column(
            "collaboration_turns",
            sa.Column(
                "intent", sa.Text(), nullable=False, server_default=sa.text("'conversation'")
            ),
        )


def downgrade() -> None:
    op.drop_column("collaboration_turns", "intent")
