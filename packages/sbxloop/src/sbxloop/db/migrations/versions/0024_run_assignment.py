"""Record which named agents a run, its tasks and its phase attempts went to.

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("runs", "assignment_json"),
    ("tasks", "assignee"),
    ("phase_attempts", "agent_slug"),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table, column in _COLUMNS:
        if column not in {c["name"] for c in inspector.get_columns(table)}:
            op.add_column(table, sa.Column(column, sa.Text(), nullable=True))


def downgrade() -> None:
    for table, column in reversed(_COLUMNS):
        op.drop_column(table, column)
