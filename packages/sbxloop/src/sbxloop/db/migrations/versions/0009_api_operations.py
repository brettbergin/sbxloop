"""Durable operations and the public event outbox.

Every operator command becomes one ``api_operations`` row — accepted,
claimed by a daemon generation, finished with a result or an error — and
its transitions land in ``api_events``, the one cursor space a remote
client replays. Two new tables, nothing altered: a release before this one
keeps opening the file.

Re-runnable on purpose: a database whose stamp was rewound (a rollback's
reinstall, a test) meets tables it already has, and skips them.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("api_operations") and _has_table("api_events"):
        return
    op.create_table(
        "api_operations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("effect", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("actor_json", sa.Text(), nullable=False),
        sa.Column("idempotency_scope", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column("expected_revision", sa.Integer(), nullable=True),
        sa.Column("request_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("accepted_at", sa.REAL(), nullable=False),
        sa.Column("expires_at", sa.REAL(), nullable=True),
        sa.Column("claimed_at", sa.REAL(), nullable=True),
        sa.Column("finished_at", sa.REAL(), nullable=True),
        sa.Column("claimed_generation", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.UniqueConstraint("idempotency_scope", "idempotency_key"),
    )
    op.create_index("idx_api_operations_state", "api_operations", ["state", "accepted_at"])
    op.create_index(
        "idx_api_operations_target",
        "api_operations",
        ["target_kind", "target_key", "accepted_at"],
    )
    op.create_index("idx_api_operations_accepted", "api_operations", ["accepted_at"])
    op.create_table(
        "api_events",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=True, nullable=True),
        sa.Column("recorded_at", sa.REAL(), nullable=False),
        sa.Column("occurred_at", sa.REAL(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("item_id", sa.Text(), nullable=True),
        sa.Column("operation_id", sa.Text(), nullable=True),
        sa.Column("actor_json", sa.Text(), nullable=True),
        sa.Column("source_seq", sa.Integer(), nullable=True),
        sa.Column("data_json", sa.Text(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sqlite_autoincrement=True,
    )
    op.create_index("idx_api_events_run", "api_events", ["run_id", "seq"])
    op.create_index("idx_api_events_recorded", "api_events", ["recorded_at"])


def downgrade() -> None:
    op.drop_index("idx_api_events_recorded", table_name="api_events")
    op.drop_index("idx_api_events_run", table_name="api_events")
    op.drop_table("api_events")
    op.drop_index("idx_api_operations_accepted", table_name="api_operations")
    op.drop_index("idx_api_operations_target", table_name="api_operations")
    op.drop_index("idx_api_operations_state", table_name="api_operations")
    op.drop_table("api_operations")
