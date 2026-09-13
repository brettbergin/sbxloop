"""Opaque public identifiers for source-derived resources.

``api_public_ids`` maps the id a remote client sees (``itm_…``, ``repo_…``)
to the resource behind it: a kind, the workspace, and the internal key —
for a work item the repository *and* the item id together, so two
repositories' issue numbers never alias one public id. Assigned lazily
the first time a resource is read through the API and stable from then
on. One new table, nothing altered. Re-runnable on a rewound stamp.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("api_public_ids"):
        return
    op.create_table(
        "api_public_ids",
        sa.Column("public_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("internal_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.REAL(), nullable=False),
        sa.UniqueConstraint("kind", "workspace_id", "internal_key"),
    )


def downgrade() -> None:
    op.drop_table("api_public_ids")
