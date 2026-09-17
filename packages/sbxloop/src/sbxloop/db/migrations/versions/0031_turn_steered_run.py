"""Record the run a turn steered instead of answering.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collaboration_turns")}
    if "steered_run_id" not in columns:
        op.add_column("collaboration_turns", sa.Column("steered_run_id", sa.Text()))


def downgrade() -> None:
    op.drop_column("collaboration_turns", "steered_run_id")
