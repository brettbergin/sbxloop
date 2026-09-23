"""Give the per-person events recorded before revision 0026 their audience.

Revision 0026 added ``api_events.audience_user_id`` but filled only
``channel_id`` for the events already recorded, so a person's own team,
preference, workflow and profile events from an older release carried no
audience and reached every workspace member. This fills it for those event
types: from the ``user_id`` their data names, or from the owner of the team,
preference or workflow their data names. An event whose person can no
longer be told (its team, preference or workflow is gone, or its data is
malformed) gets the empty audience: no person's id is empty, so it reaches
no workspace member, and a plain API client still sees it as before.

``collaboration.user.created`` is left alone: the sign-in path records it
for everyone, so the type is not about one person only.

Data only, and re-runnable: only rows without an audience are filled, and a
table the lookup needs that is missing leaves its rows to the empty audience.

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

_EVENTS = "api_events"
#: The audience of a per-person event whose person cannot be told.
_NOBODY = ""
#: Event types whose data names their person as ``user_id``.
_BY_USER = (
    "collaboration.user.updated",
    "collaboration.preferences.reset",
    "collaboration.channel.read",
)
#: Event types whose data names a row owned by their person: the owning
#: table and the data key that holds the row's id.
_BY_OWNER = (
    ("collaboration_teams", "team_id", ("created", "updated", "deleted"), "team"),
    ("collaboration_preferences", "preference_id", ("created", "updated", "deleted"), "preference"),
    ("collaboration_workflows", "workflow_id", ("created", "updated", "deleted"), "workflow"),
)


def _text_at(key: str) -> str:
    # CASE, not AND: SQLite guarantees the CASE order, so a malformed row is
    # never handed to json_type (which would fail the whole statement).
    data = f"{_EVENTS}.data_json"
    return (
        f"CASE WHEN json_valid({data}) THEN"
        f" CASE WHEN json_type({data}, '$.{key}') = 'text'"
        f" THEN json_extract({data}, '$.{key}') END END"
    )


def _types(names: tuple[str, ...]) -> str:
    return ", ".join(f"'{name}'" for name in names)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_EVENTS):
        return
    if "audience_user_id" not in {c["name"] for c in inspector.get_columns(_EVENTS)}:
        return
    op.execute(
        f"UPDATE {_EVENTS} SET audience_user_id = COALESCE({_text_at('user_id')}, '{_NOBODY}')"  # nosec B608
        f" WHERE audience_user_id IS NULL AND type IN ({_types(_BY_USER)})"
    )
    for table, key, verbs, noun in _BY_OWNER:
        types = _types(tuple(f"collaboration.{noun}.{verb}" for verb in verbs))
        owner = (
            # Module constants only; no value from the database or a caller.
            f"(SELECT o.user_id FROM {table} o WHERE o.id = {_text_at(key)})"  # nosec B608
            if inspector.has_table(table)
            else "NULL"
        )
        op.execute(
            f"UPDATE {_EVENTS} SET audience_user_id = COALESCE({owner}, '{_NOBODY}')"  # nosec B608
            f" WHERE audience_user_id IS NULL AND type IN ({types})"
        )


def downgrade() -> None:
    # Data only: an older release reads the column it already had, and which
    # rows this revision filled is not recorded.
    pass
