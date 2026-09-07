"""Name what a reconciliation row is, instead of encoding it in a sign.

Revision ID: 0002
Revises: 0001

``reconciliations`` carries six different concerns, told apart by the sign
and magnitude of ``round``: at or above zero it is a real review round, and
below it are human replies (-1), advisory round spend (-2), bot round spend
(-3), round confirmations (-100 and down) and noted findings (-1000 and
down). Reading that table means knowing the bands by heart.

This adds a ``kind`` column that says so, and backfills it from the bands.

It does **not** renumber ``round``, and the new code keeps writing the
sentinels. That is the additive-only contract: a failed deploy rolls back by
restarting the previous version against this database, and that version
reads the bands. Normalising ``round`` is a later release's job, once no
deployed version depends on the old encoding.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# The bands, as `sbxloop.engine.store` defines them. Each predicate is
# exclusive rather than merely ordered: a confirm round is -100 and below
# and a noted round -1000 and below, so "confirm" has to exclude the noted
# band explicitly. Ordering alone would let the second UPDATE claim rows the
# first had already named.
_BACKFILL = (
    ("review", "round >= 0"),
    ("human", "round = -1"),
    ("advisory", "round = -2"),
    ("bot", "round = -3"),
    ("confirm", "round <= -100 AND round > -1000"),
    ("noted", "round <= -1000"),
)


def upgrade() -> None:
    op.add_column(
        "reconciliations",
        sa.Column("kind", sa.Text(), nullable=False, server_default="review"),
    )
    rows = sa.table("reconciliations", sa.column("kind", sa.Text()))
    for kind, predicate in _BACKFILL:
        # The value is bound; only the band predicate is text, and it comes
        # from the tuple above rather than from anything read at runtime.
        op.execute(rows.update().where(sa.text(predicate)).values(kind=kind))


def downgrade() -> None:
    op.drop_column("reconciliations", "kind")
