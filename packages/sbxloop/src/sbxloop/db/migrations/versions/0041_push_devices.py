"""The device registry and rendered notifications behind push.

``api_push_devices`` is one row per mobile device a person registered: the
push token only as a digest and its last characters, the environment it
was issued for, the relay's opaque handle and the person's notification
preferences. ``api_push_notifications`` is what each push was about, kept
here so the device fetches the text instead of the relay ever carrying it.
Two new tables, nothing altered. Re-runnable on a rewound stamp.

Revision ID: 0041
Revises: 0040
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "api_push_devices" not in tables:
        op.create_table(
            "api_push_devices",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("platform", sa.Text(), nullable=False),
            sa.Column("token_digest", sa.Text(), nullable=False),
            sa.Column("token_suffix", sa.Text(), nullable=False),
            sa.Column("env", sa.Text(), nullable=False),
            sa.Column("server_ref", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=True),
            sa.Column("prefs_json", sa.Text(), nullable=False),
            sa.Column("handle", sa.Text(), nullable=False),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("updated_at", sa.REAL(), nullable=False),
            sa.Column("last_push_at", sa.REAL(), nullable=True),
            sa.UniqueConstraint("user_id", "token_digest"),
        )
        op.create_index("idx_api_push_devices_user", "api_push_devices", ["user_id", "created_at"])
    if "api_push_notifications" not in tables:
        op.create_table(
            "api_push_notifications",
            sa.Column("ref", sa.Text(), primary_key=True),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("channel_id", sa.Text(), nullable=True),
            sa.Column("turn_id", sa.Text(), nullable=True),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("event_seq", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.REAL(), nullable=False),
        )
        op.create_index(
            "idx_api_push_notifications_user",
            "api_push_notifications",
            ["user_id", "created_at"],
        )
        op.create_index(
            "idx_api_push_notifications_created", "api_push_notifications", ["created_at"]
        )


def downgrade() -> None:
    op.drop_index("idx_api_push_notifications_created", table_name="api_push_notifications")
    op.drop_index("idx_api_push_notifications_user", table_name="api_push_notifications")
    op.drop_table("api_push_notifications")
    op.drop_index("idx_api_push_devices_user", table_name="api_push_devices")
    op.drop_table("api_push_devices")
