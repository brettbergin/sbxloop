"""What a run has already said in a channel.

Adds ``channel_run_posts``: one row per post a run made, keyed by the
dedupe key the run named it with, so a replayed or resumed run posts the
same moment once. Adds ``collaboration_messages.post_kind``: what an
``agent_update`` message is (plan, progress, review, delivery, reply or
notice), null for every message written before run posts existed.

Additive: one new table and one nullable column nothing older reads.
Re-runnable: each step checks what is already there.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_POSTS = "channel_run_posts"
_MESSAGES = "collaboration_messages"
_INDEX = "idx_channel_run_posts_run"


def upgrade() -> None:
    if _POSTS not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            _POSTS,
            sa.Column("dedupe_key", sa.Text(), primary_key=True),
            sa.Column("run_id", sa.Text(), nullable=False),
            sa.Column("message_id", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("posted_at", sa.REAL(), nullable=False),
        )
    if _INDEX not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(_POSTS)}:
        op.create_index(_INDEX, _POSTS, ["run_id", "posted_at"])
    if "post_kind" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_MESSAGES)}:
        op.add_column(_MESSAGES, sa.Column("post_kind", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column(_MESSAGES, "post_kind")
    op.drop_index(_INDEX, table_name=_POSTS)
    op.drop_table(_POSTS)
