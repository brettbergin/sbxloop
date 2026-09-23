"""Durable job conversations and a transactionally maintained projection queue.

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

from typing import cast

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sbxloop.db.job_models import (
        ExternalItemRow,
        ExternalJobRow,
        ExternalPendingRow,
        ExternalRunRow,
    )

    for model in (ExternalJobRow, ExternalItemRow, ExternalRunRow, ExternalPendingRow):
        cast(sa.Table, model.__table__).create(op.get_bind(), checkfirst=True)
    if "idx_daemon_runs_item_started" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("daemon_runs")
    }:
        op.create_index(
            "idx_daemon_runs_item_started", "daemon_runs", ["item_id", "started_at", "run_id"]
        )
    for kind, table, key in (
        ("item", "daemon_work_items", "item_id"),
        ("run", "runs", "run_id"),
        ("run", "daemon_runs", "run_id"),
        ("run", "events", "run_id"),
    ):
        for action in ("INSERT", "UPDATE"):
            # Fixed identifiers only; no user values enter migration DDL.
            op.execute(
                sa.text(
                    f"CREATE TRIGGER IF NOT EXISTS external_job_{table}_{action.lower()} "
                    f"AFTER {action} ON {table} BEGIN "
                    "INSERT INTO collaboration_job_pending(kind, resource_id) "
                    f"VALUES ('{kind}', NEW.{key}) "
                    "ON CONFLICT(kind, resource_id) DO UPDATE SET "
                    "generation=generation+1, historical=0; END"  # nosec B608
                )
            )


def downgrade() -> None:
    op.drop_index("idx_daemon_runs_item_started", table_name="daemon_runs")
    for table in ("daemon_work_items", "runs", "daemon_runs", "events"):
        for action in ("insert", "update"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS external_job_{table}_{action}"))
    for table in (
        "collaboration_job_pending",
        "collaboration_job_runs",
        "collaboration_job_items",
        "collaboration_jobs",
    ):
        op.drop_table(table)
