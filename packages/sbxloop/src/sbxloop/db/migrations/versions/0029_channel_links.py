"""Bridge surfaces linked to channels, external identities, message origin.

Every step is guarded by the inspector and adds only what is missing, so
running the revision twice changes nothing.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

_LINKS = "collaboration_channel_links"
_IDENTITIES = "collaboration_external_identities"
_MESSAGES = "collaboration_messages"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if _LINKS not in tables:
        op.create_table(
            _LINKS,
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("channel_id", sa.Text(), nullable=False),
            sa.Column("backend", sa.Text(), nullable=False),
            sa.Column("surface_id", sa.Text(), nullable=False),
            sa.Column("thread_id", sa.Text(), nullable=True),
            sa.Column("allow_guests", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_by", sa.Text(), nullable=True),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("active", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.PrimaryKeyConstraint("id"),
            # Unnamed, like every other UNIQUE written inline in a CREATE
            # TABLE here, so autogenerate keeps seeing no drift. SQLite also
            # treats NULLs as distinct in it, so the store refuses a second
            # link to a surface itself; this is the backstop.
            sa.UniqueConstraint("backend", "surface_id", "thread_id"),
        )
        op.create_index("idx_collaboration_channel_links_channel", _LINKS, ["channel_id"])
    if _IDENTITIES not in tables:
        op.create_table(
            _IDENTITIES,
            sa.Column("backend", sa.Text(), nullable=False),
            sa.Column("external_user_id", sa.Text(), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("display_name", sa.Text(), nullable=True),
            sa.Column("verified_at", sa.REAL(), nullable=False),
            sa.PrimaryKeyConstraint("backend", "external_user_id"),
        )
        op.create_index("idx_collaboration_external_identities_user", _IDENTITIES, ["user_id"])
    present = {c["name"] for c in inspector.get_columns(_MESSAGES)}
    if "origin_json" not in present:
        op.add_column(_MESSAGES, sa.Column("origin_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column(_MESSAGES, "origin_json")
    op.drop_index("idx_collaboration_external_identities_user", table_name=_IDENTITIES)
    op.drop_table(_IDENTITIES)
    op.drop_index("idx_collaboration_channel_links_channel", table_name=_LINKS)
    op.drop_table(_LINKS)
