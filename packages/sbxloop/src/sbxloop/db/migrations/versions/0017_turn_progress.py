"""Persist per-participant progress and stop state for collaborative turns.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collaboration_turns")}
    if "participants_json" not in columns:
        op.add_column(
            "collaboration_turns",
            sa.Column(
                "participants_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")
            ),
        )


def downgrade() -> None:
    op.drop_column("collaboration_turns", "participants_json")
