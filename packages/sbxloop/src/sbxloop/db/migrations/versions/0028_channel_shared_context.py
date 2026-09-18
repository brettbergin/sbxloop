"""Files a channel can see, and the summary of what fell out of its history.

Two new tables only: an older release never reads them, so rolling back
leaves them in place harmlessly. Both creations are inspector-guarded, so
running the revision twice is a no-op.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

_ARTIFACTS = "collaboration_message_artifacts"
_SUMMARIES = "collaboration_channel_summaries"
_CHANNEL_INDEX = "idx_collaboration_message_artifacts_channel"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_ARTIFACTS):
        op.create_table(
            _ARTIFACTS,
            sa.Column("message_id", sa.Text(), nullable=False),
            sa.Column("artifact_id", sa.Text(), nullable=False),
            sa.Column("channel_id", sa.Text(), nullable=False),
            sa.Column("run_id", sa.Text(), nullable=True),
            sa.Column("relpath", sa.Text(), nullable=False),
            sa.Column("media_type", sa.Text(), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.PrimaryKeyConstraint("message_id", "artifact_id"),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_ARTIFACTS)}
    if _CHANNEL_INDEX not in indexes:
        op.create_index(_CHANNEL_INDEX, _ARTIFACTS, ["channel_id"])
    if not sa.inspect(op.get_bind()).has_table(_SUMMARIES):
        op.create_table(
            _SUMMARIES,
            sa.Column("channel_id", sa.Text(), nullable=False),
            sa.Column("through_sequence", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.PrimaryKeyConstraint("channel_id", "through_sequence"),
        )


def downgrade() -> None:
    op.drop_table(_SUMMARIES)
    op.drop_index(_CHANNEL_INDEX, table_name=_ARTIFACTS)
    op.drop_table(_ARTIFACTS)
