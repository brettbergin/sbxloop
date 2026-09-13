"""The artifact catalog behind the remote API.

``api_artifacts`` is one row per file a finished run left behind: its
identity, digest, size and media type, the run and task it came from, and
whether the bytes are still on the host — a pruned run keeps its rows with
``available`` off, so history says what was delivered even after the
payload is gone. One new table, nothing altered. Re-runnable on a rewound
stamp.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("api_artifacts"):
        return
    op.create_table(
        "api_artifacts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("relpath", sa.Text(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.REAL(), nullable=False),
        sa.Column("available", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("tombstoned_at", sa.REAL(), nullable=True),
        sa.UniqueConstraint("run_id", "relpath"),
    )
    op.create_index("idx_api_artifacts_run", "api_artifacts", ["run_id", "relpath"])


def downgrade() -> None:
    op.drop_index("idx_api_artifacts_run", table_name="api_artifacts")
    op.drop_table("api_artifacts")
