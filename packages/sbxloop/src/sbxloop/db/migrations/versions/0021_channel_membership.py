"""Channel members, agent participants, and who wrote each message and turn.

Every step is guarded by the inspector, and every backfill touches only rows
it has not filled yet, so running the revision twice changes nothing.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_CHANNELS = "collaboration_channels"
_MEMBERS = "collaboration_channel_members"
_PARTICIPANTS = "collaboration_channel_participants"
_MESSAGES = "collaboration_messages"
_TURNS = "collaboration_turns"


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _add(table: str, *columns: sa.Column) -> None:  # type: ignore[type-arg]
    existing = _columns(table)
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    _add(
        _CHANNELS,
        sa.Column(
            "visibility",
            sa.Text(),
            sa.CheckConstraint(
                "visibility IN ('private', 'workspace')",
                name=op.f("ck_collaboration_channels_visibility"),
            ),
            nullable=False,
            server_default=sa.text("'private'"),
        ),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("silenced_until", sa.REAL(), nullable=True),
        sa.Column("settings_json", sa.Text(), nullable=True),
    )
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if _MEMBERS not in tables:
        op.create_table(
            _MEMBERS,
            sa.Column("channel_id", sa.Text(), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("role", sa.Text(), nullable=False, server_default=sa.text("'member'")),
            sa.Column("added_by", sa.Text(), nullable=True),
            sa.Column("joined_at", sa.REAL(), nullable=False),
            sa.Column(
                "last_read_sequence", sa.Integer(), nullable=False, server_default=sa.text("0")
            ),
            sa.PrimaryKeyConstraint("channel_id", "user_id"),
            sa.CheckConstraint(
                "role IN ('owner', 'member')", name=op.f("ck_collaboration_channel_members_role")
            ),
        )
        op.create_index("idx_collaboration_channel_members_user", _MEMBERS, ["user_id"])
    if _PARTICIPANTS not in tables:
        op.create_table(
            _PARTICIPANTS,
            sa.Column("channel_id", sa.Text(), nullable=False),
            sa.Column("agent_slug", sa.Text(), nullable=False),
            sa.Column("mode", sa.Text(), nullable=False, server_default=sa.text("'mention'")),
            sa.Column("added_by_kind", sa.Text(), nullable=True),
            sa.Column("added_by_id", sa.Text(), nullable=True),
            sa.Column("muted_until", sa.REAL(), nullable=True),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.PrimaryKeyConstraint("channel_id", "agent_slug"),
            sa.CheckConstraint(
                "mode IN ('mention', 'ambient')",
                name=op.f("ck_collaboration_channel_participants_mode"),
            ),
        )
    _add(
        _MESSAGES,
        sa.Column("author_kind", sa.Text(), nullable=True),
        sa.Column("author_id", sa.Text(), nullable=True),
    )
    _add(
        _TURNS,
        sa.Column("author_kind", sa.Text(), nullable=True),
        sa.Column("author_id", sa.Text(), nullable=True),
        sa.Column("trigger", sa.Text(), nullable=False, server_default=sa.text("'human'")),
        sa.Column("parent_turn_id", sa.Text(), nullable=True),
        sa.Column("source_message_id", sa.Text(), nullable=True),
        sa.Column("chain_depth", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    # Before this revision a channel had exactly one person: its user.
    op.execute(
        f"UPDATE {_CHANNELS} SET created_by = user_id WHERE created_by IS NULL"  # nosec B608
    )
    op.execute(
        f"INSERT INTO {_MEMBERS} (channel_id, user_id, role, added_by, joined_at,"  # nosec B608
        " last_read_sequence)"
        f" SELECT c.id, c.user_id, 'owner', NULL, c.created_at, 0 FROM {_CHANNELS} c"
        f" WHERE NOT EXISTS (SELECT 1 FROM {_MEMBERS} m WHERE m.channel_id = c.id)"
    )
    # The person wrote the user messages; Angie spoke for every assistant
    # message that names no agent; error and stop notices are the system's.
    op.execute(
        f"UPDATE {_MESSAGES} SET"  # nosec B608
        " author_kind = CASE"
        "  WHEN role = 'user' THEN 'human'"
        "  WHEN kind IN ('turn_error', 'turn_cancelled') THEN 'system'"
        "  ELSE 'agent' END,"
        " author_id = CASE"
        f"  WHEN role = 'user' THEN (SELECT c.user_id FROM {_CHANNELS} c"
        f"   WHERE c.id = {_MESSAGES}.channel_id)"
        "  WHEN kind IN ('turn_error', 'turn_cancelled') THEN NULL"
        "  ELSE COALESCE(agent_slug, 'concierge') END"
        " WHERE author_kind IS NULL"
    )
    op.execute(
        f"UPDATE {_TURNS} SET author_kind = 'human',"  # nosec B608
        f" author_id = (SELECT c.user_id FROM {_CHANNELS} c WHERE c.id = {_TURNS}.channel_id)"
        " WHERE author_kind IS NULL"
    )


def downgrade() -> None:
    for column in ("chain_depth", "source_message_id", "parent_turn_id", "trigger"):
        op.drop_column(_TURNS, column)
    op.drop_column(_TURNS, "author_id")
    op.drop_column(_TURNS, "author_kind")
    op.drop_column(_MESSAGES, "author_id")
    op.drop_column(_MESSAGES, "author_kind")
    op.drop_table(_PARTICIPANTS)
    op.drop_index("idx_collaboration_channel_members_user", table_name=_MEMBERS)
    op.drop_table(_MEMBERS)
    for column in ("settings_json", "silenced_until", "created_by", "visibility"):
        op.drop_column(_CHANNELS, column)
