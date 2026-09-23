"""Bound OIDC login lifetimes and remember authenticated provider logout events.

Additive tables; legacy tokens keep their old shape and are refused for humans
when local authentication is disabled because their provenance is unknown.

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "api_oidc_sessions" not in tables:
        op.create_table(
            "api_oidc_sessions",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("client_id", sa.Text(), nullable=False),
            sa.Column("issuer", sa.Text(), nullable=False),
            sa.Column("subject", sa.Text(), nullable=False),
            sa.Column("provider_sid", sa.Text()),
            sa.Column("issued_at", sa.REAL(), nullable=False),
            sa.Column("expires_at", sa.REAL(), nullable=False),
            sa.Column("revoked_at", sa.REAL()),
        )
        op.create_index("idx_api_oidc_sessions_subject", "api_oidc_sessions", ["issuer", "subject"])
        op.create_index(
            "idx_api_oidc_sessions_sid", "api_oidc_sessions", ["issuer", "provider_sid"]
        )
    if "api_oidc_logouts" not in tables:
        op.create_table(
            "api_oidc_logouts",
            sa.Column("issuer", sa.Text(), primary_key=True),
            sa.Column("jti", sa.Text(), primary_key=True),
            sa.Column("subject", sa.Text()),
            sa.Column("provider_sid", sa.Text()),
            sa.Column("issued_at", sa.REAL(), nullable=False),
            sa.Column("expires_at", sa.REAL(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("api_oidc_logouts")
    op.drop_index("idx_api_oidc_sessions_sid", table_name="api_oidc_sessions")
    op.drop_index("idx_api_oidc_sessions_subject", table_name="api_oidc_sessions")
    op.drop_table("api_oidc_sessions")
