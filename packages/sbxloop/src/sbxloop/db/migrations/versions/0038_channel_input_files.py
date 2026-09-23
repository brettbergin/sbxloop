"""Persist channel input originals separately from run-generated artifacts.

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "collaboration_input_files"
    if not sa.inspect(op.get_bind()).has_table(table):
        op.create_table(
            table,
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("channel_id", sa.Text(), nullable=False),
            sa.Column("uploader_id", sa.Text(), nullable=False),
            sa.Column("client_upload_id", sa.Text(), nullable=False),
            sa.Column("display_name", sa.Text(), nullable=False),
            sa.Column("declared_size", sa.Integer()),
            sa.Column("size", sa.Integer()),
            sa.Column("sha256", sa.Text()),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("message_id", sa.Text()),
            sa.Column("position", sa.Integer()),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("uploaded_at", sa.REAL()),
            sa.Column("deleted_at", sa.REAL()),
            sa.UniqueConstraint("channel_id", "uploader_id", "client_upload_id"),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}
    if "idx_collaboration_input_files_channel" not in indexes:
        op.create_index(
            "idx_collaboration_input_files_channel", table, ["channel_id", "created_at"]
        )
    if "idx_collaboration_input_files_workspace" not in indexes:
        op.create_index(
            "idx_collaboration_input_files_workspace", table, ["workspace_id", "status"]
        )


def downgrade() -> None:
    # A previous binary may ignore input files, but rollback must not delete
    # user originals or the database references required to restore them.
    pass
