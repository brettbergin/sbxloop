"""Persist the seeded workload recipe and the target it was queued for.

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
    op.add_column("daemon_work_items", sa.Column("recipe", sa.Text(), nullable=True))
    op.add_column("daemon_work_items", sa.Column("recipe_target", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("daemon_work_items", "recipe_target")
    op.drop_column("daemon_work_items", "recipe")
