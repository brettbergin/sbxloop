"""Local users, channels, messages, turns, teams, preferences and workflows.

The collaboration layer is additive: existing remote API and daemon tables
are untouched, so an older sbxloop release can still open a database that
has been upgraded by this revision.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("collaboration_users"):
        op.create_table(
            "collaboration_users",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("client_id", sa.Text(), nullable=False, unique=True),
            sa.Column("username", sa.Text(), nullable=False, unique=True),
            sa.Column("email", sa.Text(), nullable=False, unique=True),
            sa.Column("full_name", sa.Text(), nullable=True),
            sa.Column("timezone", sa.Text(), nullable=False, server_default=sa.text("'UTC'")),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("updated_at", sa.REAL(), nullable=False),
            sa.Column("active", sa.Integer(), nullable=False, server_default=sa.text("1")),
        )
    if not inspector.has_table("collaboration_channels"):
        op.create_table(
            "collaboration_channels",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False, server_default=sa.text("'local'")),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'active'")),
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("updated_at", sa.REAL(), nullable=False),
            sa.Column("deleted_at", sa.REAL(), nullable=True),
        )
        op.create_index(
            "idx_collaboration_channels_user_updated",
            "collaboration_channels",
            ["user_id", "updated_at"],
        )
    if not inspector.has_table("collaboration_messages"):
        op.create_table(
            "collaboration_messages",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("channel_id", sa.Text(), nullable=False),
            sa.Column("turn_id", sa.Text(), nullable=True),
            sa.Column("client_message_id", sa.Text(), nullable=True),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("role", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'message'")),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("agent_slug", sa.Text(), nullable=True),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.UniqueConstraint("channel_id", "sequence"),
            sa.UniqueConstraint("channel_id", "client_message_id"),
        )
        op.create_index(
            "idx_collaboration_messages_channel",
            "collaboration_messages",
            ["channel_id", "sequence"],
        )
        op.create_index(
            "idx_collaboration_messages_turn",
            "collaboration_messages",
            ["turn_id", "sequence"],
        )
    if not inspector.has_table("collaboration_turns"):
        op.create_table(
            "collaboration_turns",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("channel_id", sa.Text(), nullable=False),
            sa.Column("client_turn_id", sa.Text(), nullable=True),
            sa.Column("input_message_id", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("targets_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("started_at", sa.REAL(), nullable=True),
            sa.Column("completed_at", sa.REAL(), nullable=True),
            sa.UniqueConstraint("channel_id", "client_turn_id"),
        )
        op.create_index(
            "idx_collaboration_turns_channel",
            "collaboration_turns",
            ["channel_id", "created_at"],
        )
        op.create_index(
            "idx_collaboration_turns_status",
            "collaboration_turns",
            ["status", "created_at"],
        )
    if not inspector.has_table("collaboration_teams"):
        op.create_table(
            "collaboration_teams",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("slug", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("goal", sa.Text(), nullable=True),
            sa.Column(
                "agent_slugs_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")
            ),
            sa.Column("enabled", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("updated_at", sa.REAL(), nullable=False),
            sa.UniqueConstraint("user_id", "slug"),
        )
        op.create_index(
            "idx_collaboration_teams_user",
            "collaboration_teams",
            ["user_id", "created_at"],
        )
    if not inspector.has_table("collaboration_preferences"):
        op.create_table(
            "collaboration_preferences",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("updated_at", sa.REAL(), nullable=False),
            sa.UniqueConstraint("user_id", "name"),
        )
        op.create_index(
            "idx_collaboration_preferences_user",
            "collaboration_preferences",
            ["user_id", "name"],
        )
    if not inspector.has_table("collaboration_workflows"):
        op.create_table(
            "collaboration_workflows",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("slug", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("trigger_event", sa.Text(), nullable=True),
            sa.Column("enabled", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("updated_at", sa.REAL(), nullable=False),
            sa.UniqueConstraint("user_id", "slug"),
        )
        op.create_index(
            "idx_collaboration_workflows_user",
            "collaboration_workflows",
            ["user_id", "name"],
        )


def downgrade() -> None:
    op.drop_index("idx_collaboration_workflows_user", table_name="collaboration_workflows")
    op.drop_table("collaboration_workflows")
    op.drop_index("idx_collaboration_preferences_user", table_name="collaboration_preferences")
    op.drop_table("collaboration_preferences")
    op.drop_index("idx_collaboration_teams_user", table_name="collaboration_teams")
    op.drop_table("collaboration_teams")
    op.drop_index("idx_collaboration_turns_status", table_name="collaboration_turns")
    op.drop_index("idx_collaboration_turns_channel", table_name="collaboration_turns")
    op.drop_table("collaboration_turns")
    op.drop_index("idx_collaboration_messages_turn", table_name="collaboration_messages")
    op.drop_index("idx_collaboration_messages_channel", table_name="collaboration_messages")
    op.drop_table("collaboration_messages")
    op.drop_index("idx_collaboration_channels_user_updated", table_name="collaboration_channels")
    op.drop_table("collaboration_channels")
    op.drop_table("collaboration_users")
