"""Whether a local account's email came from a path its holder could not steer.

Adds ``collaboration_users.email_verified``: set when the address was taken
at the installation's first registration, from an invite addressed to it,
or from a provider claim marked verified; cleared when the person changes
it through the API. An OIDC first sign-in links to a local account only
when both sides vouch for the address, so an address a member typed in
cannot capture a colleague's first sign-in.

Every account that exists before the revision reads back unverified: the
database cannot tell whether its address was ever changed, so none of them
is linkable, and a first sign-in with its address creates a second account
to merge instead.

Additive: one column with a default nothing older reads. Re-runnable: the
add checks what is already there.

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

_TABLE = "collaboration_users"
_COLUMN = "email_verified"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _COLUMN not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
