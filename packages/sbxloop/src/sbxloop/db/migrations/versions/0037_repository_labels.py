"""What a registered repository's sbxloop labels looked like when last read.

Adds three columns to ``daemon_repositories``: when the daemon last read
the repository's labels, which label names it read for, and which of them
the repository did not carry. The console's repositories page answers
"is this repository set up for sbxloop" from these without a forge call
of its own, and a label sync records its result here.

Additive: three nullable columns an older daemon does not read.
Re-runnable: each column is added only when it is absent.

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_TABLE = "daemon_repositories"
_COLUMNS: tuple[tuple[str, Any], ...] = (
    ("labels_checked_at", sa.REAL()),
    ("labels_expected", sa.Text()),
    ("labels_missing", sa.Text()),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return
    present = {column["name"] for column in inspector.get_columns(_TABLE)}
    for name, kind in _COLUMNS:
        if name not in present:
            op.add_column(_TABLE, sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    for name, _kind in _COLUMNS:
        op.drop_column(_TABLE, name)
