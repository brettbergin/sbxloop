"""Persist reactions on collaboration messages.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collaboration_messages")}
    if "reactions_json" not in columns:
        op.add_column(
            "collaboration_messages",
            sa.Column("reactions_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        )


def downgrade() -> None:
    op.drop_column("collaboration_messages", "reactions_json")
