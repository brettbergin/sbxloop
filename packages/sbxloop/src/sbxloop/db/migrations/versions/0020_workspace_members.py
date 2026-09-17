"""Workspace membership, invitations and provider identities on local users.

Every existing local user becomes an owner of the installation's one
workspace. Each step checks what is already there, so a database whose stamp
was rolled back by hand upgrades again without error or duplicate rows.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

#: The workspace a single installation has (``principal.WORKSPACE_ID``),
#: frozen here so the revision never changes with the code.
_WORKSPACE_ID = "local"


def _user_columns() -> tuple[sa.Column[Any], ...]:
    """Fresh column objects: a Column belongs to one table once built."""
    return (
        sa.Column("auth_source", sa.Text(), nullable=False, server_default=sa.text("'local'")),
        sa.Column("oidc_issuer", sa.Text(), nullable=True),
        sa.Column("oidc_subject", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("last_seen_at", sa.REAL(), nullable=True),
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("collaboration_users")}
    for column in _user_columns():
        if column.name not in columns:
            op.add_column("collaboration_users", column)
    indexes = {i["name"] for i in inspector.get_indexes("collaboration_users")}
    if "idx_collaboration_users_oidc" not in indexes:
        op.create_index(
            "idx_collaboration_users_oidc",
            "collaboration_users",
            ["oidc_issuer", "oidc_subject"],
            unique=True,
            sqlite_where=sa.text("oidc_issuer IS NOT NULL AND oidc_subject IS NOT NULL"),
        )
    if not inspector.has_table("workspace_members"):
        op.create_table(
            "workspace_members",
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("user_id", sa.Text(), nullable=False),
            sa.Column("role", sa.Text(), nullable=False),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("invited_by", sa.Text(), nullable=True),
            sa.CheckConstraint(
                "role IN ('owner', 'admin', 'member')", name="ck_workspace_members_role"
            ),
            sa.ForeignKeyConstraint(["user_id"], ["collaboration_users.id"]),
            sa.PrimaryKeyConstraint("workspace_id", "user_id"),
        )
    if not inspector.has_table("workspace_invites"):
        op.create_table(
            "workspace_invites",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("email", sa.Text(), nullable=True),
            sa.Column("role", sa.Text(), nullable=False),
            sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
            sa.Column("expires_at", sa.REAL(), nullable=False),
            sa.Column("accepted_at", sa.REAL(), nullable=True),
            sa.Column("created_by", sa.Text(), nullable=False),
            sa.Column("created_at", sa.REAL(), nullable=False),
        )
    # Existing users owned the installation outright; they keep that standing.
    bind.execute(
        sa.text(
            "INSERT INTO workspace_members (workspace_id, user_id, role, created_at, invited_by)"
            " SELECT :workspace, u.id, 'owner', u.created_at, NULL"
            " FROM collaboration_users AS u"
            " WHERE NOT EXISTS (SELECT 1 FROM workspace_members AS m"
            " WHERE m.workspace_id = :workspace AND m.user_id = u.id)"
        ),
        {"workspace": _WORKSPACE_ID},
    )


def downgrade() -> None:
    op.drop_table("workspace_invites")
    op.drop_table("workspace_members")
    op.drop_index("idx_collaboration_users_oidc", table_name="collaboration_users")
    for column in reversed(_user_columns()):
        op.drop_column("collaboration_users", column.name)
