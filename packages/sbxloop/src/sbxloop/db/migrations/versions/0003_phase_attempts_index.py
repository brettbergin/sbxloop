"""Index the lookup `phase_attempts` is read by.

Revision ID: 0003
Revises: 0002

Every read of this table but one filters on ``run_id`` and orders by ``id``
descending — "the latest attempt of this phase for this task". There was no
index, so each of those was a scan of every attempt the installation had
ever recorded, and the table only grows.

That cost is paid on the hot path: the engine asks for the latest attempt
of a phase at each stage, the run detail screen folds every attempt of a
run, and `posted_findings` walks them all to rebuild thread identity on a
resume. Purely additive — an index changes no row.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `run_id` first because every query has it; `task_id` and `phase` next
    # because the latest-attempt lookup gives all three; `id` last so the
    # ORDER BY reads off the index instead of sorting.
    op.create_index(
        "idx_phase_attempts_lookup",
        "phase_attempts",
        ["run_id", "task_id", "phase", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_phase_attempts_lookup", table_name="phase_attempts")
