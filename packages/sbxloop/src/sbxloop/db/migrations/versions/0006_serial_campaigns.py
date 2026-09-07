"""Pin campaign scope and each step's verified delivery evidence.

Revision ID: 0006
Revises: 0005

These tables are independent of replaceable work-item rows. Rediscovery,
claim recovery and retries can update those rows without losing a
campaign's authorization, dependencies or completed delivery checkpoints.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daemon_campaigns",
        sa.Column("campaign_id", sa.Text(), nullable=False),
        sa.Column("plan_hash", sa.Text(), nullable=False),
        sa.Column("plan_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.Column("updated_at", sa.REAL(), nullable=False),
        sa.Column("held", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prepared", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hold_reason", sa.Text(), nullable=True),
        sa.Column("hold_actor", sa.Text(), nullable=True),
        sa.Column("blocker_item_id", sa.Text(), nullable=True),
        sa.Column("blocker_reason", sa.Text(), nullable=True),
        sa.Column("order_actor", sa.Text(), nullable=True),
        sa.Column("order_changed_at", sa.REAL(), nullable=True),
        sa.PrimaryKeyConstraint("campaign_id", name=op.f("pk_daemon_campaigns")),
    )
    op.create_table(
        "daemon_campaign_steps",
        sa.Column("member_key", sa.Text(), nullable=False),
        sa.Column("campaign_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("step_json", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.REAL(), nullable=True),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["daemon_campaigns.campaign_id"],
            name=op.f("fk_daemon_campaign_steps_campaign_id_daemon_campaigns"),
        ),
        sa.PrimaryKeyConstraint("member_key", name=op.f("pk_daemon_campaign_steps")),
        sa.UniqueConstraint("campaign_id", "position"),
        sa.UniqueConstraint("source_key", "repo"),
    )


def downgrade() -> None:
    op.drop_table("daemon_campaign_steps")
    op.drop_table("daemon_campaigns")
