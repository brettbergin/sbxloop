"""Persist the repository target for entrygraph workloads.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("daemon_work_items", sa.Column("entrygraph_target", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("daemon_work_items", "entrygraph_target")
