"""A recipe item is a `tool` run.

Recipe items were first queued as `workload` runs — a fixed task graph
handed to the operator persona. They are now `tool` runs, with no agent in
them; a queued or restartable item persisted under the earlier kind would
otherwise fail to load.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE daemon_work_items SET run_kind = 'tool' "
            "WHERE recipe IS NOT NULL AND run_kind = 'workload'"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE daemon_work_items SET run_kind = 'workload' "
            "WHERE recipe IS NOT NULL AND run_kind = 'tool'"
        )
    )
