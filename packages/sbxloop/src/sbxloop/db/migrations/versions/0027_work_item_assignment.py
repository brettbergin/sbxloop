"""Record the channel, lead and agent assignment a work item was admitted with.

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_TABLE = "daemon_work_items"
_TEXT_COLUMNS = ("channel_id", "lead_agent", "assignment_json", "origin_agent", "parent_item_id")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    present = {c["name"] for c in inspector.get_columns(_TABLE)}
    for column in _TEXT_COLUMNS:
        if column not in present:
            op.add_column(_TABLE, sa.Column(column, sa.Text(), nullable=True))
    if "chain_depth" not in present:
        op.add_column(
            _TABLE,
            sa.Column("chain_depth", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    op.drop_column(_TABLE, "chain_depth")
    for column in reversed(_TEXT_COLUMNS):
        op.drop_column(_TABLE, column)
