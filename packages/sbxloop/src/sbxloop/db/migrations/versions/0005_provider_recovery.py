"""Persist provider cooldowns and interrupted agent jobs.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_holds",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("failure_json", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("next_at", sa.REAL(), nullable=True),
        sa.Column("active", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.REAL(), nullable=False),
    )
    op.create_table(
        "provider_jobs",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("job_key", sa.Text(), primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("pending", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("provider_jobs")
    op.drop_table("provider_holds")
