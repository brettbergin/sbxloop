"""Persist admission sequence and editable queue order.

Revision ID: 0005
Revises: 0004

The pre-upgrade FIFO is created_at then rowid. Pinned resumes remain a
dispatch rule, independent of the numbers assigned to every existing row.
Nullable columns let a rolled-back daemon keep writing the same database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("daemon_work_items", sa.Column("enqueue_seq", sa.Integer(), nullable=True))
    op.add_column("daemon_work_items", sa.Column("queue_order", sa.Integer(), nullable=True))
    bind = op.get_bind()
    rows = bind.exec_driver_sql(
        "SELECT item_id FROM daemon_work_items ORDER BY created_at ASC, rowid ASC"
    ).all()
    for sequence, (item_id,) in enumerate(rows, 1):
        bind.exec_driver_sql(
            "UPDATE daemon_work_items SET enqueue_seq = ?, queue_order = ? WHERE item_id = ?",
            (sequence, sequence, item_id),
        )
    bind.exec_driver_sql(
        "INSERT INTO daemon_state (key, value) VALUES ('queue_enqueue_seq', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(len(rows)),),
    )
    op.create_index(
        "idx_daemon_items_enqueue_seq", "daemon_work_items", ["enqueue_seq"], unique=True
    )


def downgrade() -> None:
    op.drop_index("idx_daemon_items_enqueue_seq", table_name="daemon_work_items")
    op.drop_column("daemon_work_items", "queue_order")
    op.drop_column("daemon_work_items", "enqueue_seq")
