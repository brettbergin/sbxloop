"""Scope public events to a channel and to a person.

Adds ``api_events.channel_id`` (the channel an event belongs to) and
``api_events.audience_user_id`` (the one person it is for), with an index on
``(channel_id, seq)``, and fills ``channel_id`` for the events already
recorded: collaboration events from the ``channel_id`` their data carries, and
agent memory events from their ``source_channel_id``.

Additive: two nullable columns nothing older reads. Re-runnable: each step
checks what is already there, and the backfill only fills empty rows.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_EVENTS = "api_events"
_INDEX = "idx_api_events_channel"
# Event type prefix, and where its data names the channel it belongs to.
_BACKFILL = (
    ("collaboration.", "$.channel_id"),
    ("agent.memory.", "$.source_channel_id"),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspector.get_columns(_EVENTS)}
    for name in ("channel_id", "audience_user_id"):
        if name not in existing:
            op.add_column(_EVENTS, sa.Column(name, sa.Text(), nullable=True))
    if _INDEX not in {i["name"] for i in inspector.get_indexes(_EVENTS)}:
        op.create_index(_INDEX, _EVENTS, ["channel_id", "seq"])
    for prefix, path in _BACKFILL:
        # CASE, not AND: SQLite guarantees the CASE order, so a malformed row
        # is never handed to json_type (which would fail the whole statement).
        op.execute(
            f"UPDATE {_EVENTS} SET channel_id = CASE WHEN json_valid(data_json) THEN"  # nosec B608
            f" CASE WHEN json_type(data_json, '{path}') = 'text'"
            f" THEN json_extract(data_json, '{path}') END END"
            " WHERE channel_id IS NULL AND data_json IS NOT NULL"
            f" AND type LIKE '{prefix}%'"
        )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_EVENTS)
    op.drop_column(_EVENTS, "audience_user_id")
    op.drop_column(_EVENTS, "channel_id")
