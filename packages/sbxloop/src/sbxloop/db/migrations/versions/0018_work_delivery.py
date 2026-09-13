"""Persist managed work references on conversation messages.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collaboration_messages")}
    if "work_json" not in columns:
        op.add_column("collaboration_messages", sa.Column("work_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("collaboration_messages", "work_json")
