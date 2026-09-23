"""Link a work item to the chat message that asked for it, by an index.

Adds ``daemon_work_items.message_id`` with an index on it, and fills it for
the chat asks already on record: a chat item's key is the asking message's
id, optionally with a suffix after a colon, so the message is the part
before the first colon.

The projection that delivers managed work to its conversation used to find
that message by comparing it against a prefix of ``source_key``. SQLite
cannot index a function on a column, so the join was a pass over the
messages table for every work item, on every poll of the channel's
messages page.

Additive: one nullable column nothing older reads, and an index. Re-runnable:
each step checks what is already there, and the backfill only fills empty
rows.

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

_TABLE = "daemon_work_items"
_COLUMN = "message_id"
_INDEX = "idx_daemon_items_message"
#: Run kinds whose source key is the asking message's id; mirrors
#: ``sbxloop.ghids.CHAT_SOURCE_KINDS``, spelled out here because a
#: migration must keep meaning what it meant when it was written.
_CHAT_KINDS = "('workload', 'tool')"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _COLUMN not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))
    if _INDEX not in {i["name"] for i in inspector.get_indexes(_TABLE)}:
        op.create_index(_INDEX, _TABLE, [_COLUMN])
    # Only a chat ask names a message. Work keyed by an issue, a schedule
    # or an inbox file is linked to its conversation by the channel it was
    # admitted for, and its key must not be read as a message id.
    op.execute(
        f"UPDATE {_TABLE} SET {_COLUMN} = CASE"  # nosec B608 - literals above
        " WHEN instr(source_key, ':') > 0"
        " THEN substr(source_key, 1, instr(source_key, ':') - 1)"
        " ELSE source_key END"
        f" WHERE {_COLUMN} IS NULL AND source_key != ''"
        " AND item_id LIKE 'chat:%'"
        f" AND run_kind IN {_CHAT_KINDS}"
    )
    # A key that is nothing but a colon leaves an empty string behind.
    op.execute(f"UPDATE {_TABLE} SET {_COLUMN} = NULL WHERE {_COLUMN} = ''")  # nosec B608


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, _COLUMN)
