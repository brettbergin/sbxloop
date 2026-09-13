"""Registered API clients and their tokens.

``api_clients`` holds a client's name, the verifier of its secret and the
capabilities it was granted; ``api_refresh_tokens`` the refresh tokens by
digest, in families so a token presented twice revokes its whole line;
``api_token_revocations`` the access-token ids revoked before expiry.
Three new tables, nothing altered. Re-runnable on a rewound stamp.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table("api_clients"):
        op.create_table(
            "api_clients",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("secret_hash", sa.Text(), nullable=False),
            sa.Column(
                "capabilities_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")
            ),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("created_by", sa.Text(), nullable=True),
            sa.Column("revoked_at", sa.REAL(), nullable=True),
            sa.Column("last_used_at", sa.REAL(), nullable=True),
        )
    if not _has_table("api_refresh_tokens"):
        op.create_table(
            "api_refresh_tokens",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("client_id", sa.Text(), nullable=False),
            sa.Column("family_id", sa.Text(), nullable=False),
            sa.Column("token_hash", sa.Text(), nullable=False),
            sa.Column("issued_at", sa.REAL(), nullable=False),
            sa.Column("expires_at", sa.REAL(), nullable=False),
            sa.Column("used_at", sa.REAL(), nullable=True),
            sa.Column("replaced_by", sa.Text(), nullable=True),
            sa.Column("revoked_at", sa.REAL(), nullable=True),
        )
        op.create_index(
            "idx_api_refresh_tokens_client", "api_refresh_tokens", ["client_id", "family_id"]
        )
        op.create_index("idx_api_refresh_tokens_hash", "api_refresh_tokens", ["token_hash"])
    if not _has_table("api_token_revocations"):
        op.create_table(
            "api_token_revocations",
            sa.Column("jti", sa.Text(), primary_key=True),
            sa.Column("client_id", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.REAL(), nullable=False),
            sa.Column("revoked_at", sa.REAL(), nullable=False),
        )
        op.create_index(
            "idx_api_token_revocations_expires", "api_token_revocations", ["expires_at"]
        )


def downgrade() -> None:
    op.drop_index("idx_api_token_revocations_expires", table_name="api_token_revocations")
    op.drop_table("api_token_revocations")
    op.drop_index("idx_api_refresh_tokens_hash", table_name="api_refresh_tokens")
    op.drop_index("idx_api_refresh_tokens_client", table_name="api_refresh_tokens")
    op.drop_table("api_refresh_tokens")
    op.drop_table("api_clients")
