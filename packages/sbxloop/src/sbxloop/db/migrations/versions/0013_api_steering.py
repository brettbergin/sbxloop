"""Steering instructions submitted through the remote API.

``api_steering`` is one row per instruction: who submitted it, the run and
run revision it was meant for, the text and what it cites, and what became
of it — handed to the run (``delivered``, with the engine's message id),
answered (``handled``, with the agent's reply and the action it took),
refused (``failed``), or never heard (``undelivered``: the run ended
first). One new table, nothing altered. Re-runnable on a rewound stamp.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("api_steering"):
        return
    op.create_table(
        "api_steering",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("principal_json", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_refs_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("expected_revision", sa.Integer(), nullable=True),
        sa.Column("submitted_at", sa.REAL(), nullable=False),
        sa.Column("deadline_at", sa.REAL(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("delivered_at", sa.REAL(), nullable=True),
        sa.Column("handled_at", sa.REAL(), nullable=True),
        sa.Column("reply", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("operation_id", sa.Text(), nullable=True),
    )
    op.create_index("idx_api_steering_run", "api_steering", ["run_id", "submitted_at"])
    op.create_index("idx_api_steering_message", "api_steering", ["message_id"])


def downgrade() -> None:
    op.drop_index("idx_api_steering_message", table_name="api_steering")
    op.drop_index("idx_api_steering_run", table_name="api_steering")
    op.drop_table("api_steering")
