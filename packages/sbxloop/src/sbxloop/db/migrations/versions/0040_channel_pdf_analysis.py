"""Persist bounded PDF analysis beside immutable channel originals.

Revision ID: 0040
Revises: 0039
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("collaboration_input_files")
    }
    for name, kind in (
        ("analysis_status", sa.Text()),
        ("analysis_json", sa.Text()),
        ("analysis_version", sa.Integer()),
    ):
        if name not in columns:
            op.add_column("collaboration_input_files", sa.Column(name, kind))


def downgrade() -> None:
    # Derived analysis may safely be regenerated, but rollback does not discard it.
    pass
