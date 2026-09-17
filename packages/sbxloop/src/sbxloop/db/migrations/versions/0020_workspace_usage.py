"""Record what runs and chat turns spent against the workspace budget pool.

A new table only: an older release never reads it, so a rollback keeps
opening the database.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("workspace_usage"):
        op.create_table(
            "workspace_usage",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ts", sa.REAL(), nullable=False),
            sa.Column("source", sa.Text(), nullable=False),
            sa.Column("ref_id", sa.Text(), nullable=False),
            sa.Column("agent_slug", sa.Text(), nullable=True),
            sa.Column("channel_id", sa.Text(), nullable=True),
            sa.Column("input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("output_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column(
                "cache_read_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column(
                "cache_write_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sqlite_autoincrement=True,
        )
    indexes = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes("workspace_usage")}
    if "idx_workspace_usage_ts" not in indexes:
        op.create_index("idx_workspace_usage_ts", "workspace_usage", ["ts"])


def downgrade() -> None:
    op.drop_index("idx_workspace_usage_ts", table_name="workspace_usage")
    op.drop_table("workspace_usage")
