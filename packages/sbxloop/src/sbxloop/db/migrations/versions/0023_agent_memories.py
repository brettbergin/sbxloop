"""Each agent's long-term memory.

A new table only: an older release never reads it, so rolling back leaves
it in place harmlessly.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("agent_memories"):
        op.create_table(
            "agent_memories",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("agent_slug", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("source_channel_id", sa.Text(), nullable=True),
            sa.Column("source_run_id", sa.Text(), nullable=True),
            sa.Column("source_message_id", sa.Text(), nullable=True),
            sa.Column("author", sa.Text(), nullable=False),
            sa.Column("pinned", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.REAL(), nullable=True),
            sa.Column("updated_at", sa.REAL(), nullable=True),
            sa.Column("last_used_at", sa.REAL(), nullable=True),
            sa.Column("deleted_at", sa.REAL(), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("agent_memories")}
    if "idx_agent_memories_agent" not in indexes:
        op.create_index(
            "idx_agent_memories_agent",
            "agent_memories",
            ["agent_slug", "deleted_at", "updated_at"],
        )


def downgrade() -> None:
    op.drop_index("idx_agent_memories_agent", table_name="agent_memories")
    op.drop_table("agent_memories")
