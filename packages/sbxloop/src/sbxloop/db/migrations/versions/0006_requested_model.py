"""Keep interrupted calls on their requested model during live refresh.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("provider_jobs", sa.Column("requested_model", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("provider_jobs", "requested_model")
