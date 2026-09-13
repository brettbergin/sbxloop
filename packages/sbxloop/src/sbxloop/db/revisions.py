"""The revision counter a remote command's precondition is checked against.

``runs``, ``daemon_work_items``, ``daemon_merge_gates`` and
``daemon_review_holds`` carry a ``revision`` column that an ``AFTER
UPDATE`` trigger bumps on every write — whichever code wrote the row, this
release's or a rolled-back one's — so a command carrying
``expected_revision`` is refused when the person acted on a superseded
state. The trigger is defined once here: Alembic revision 0010 creates it
on a deployed database, and the ``after_create`` listeners below create it
on a database built from the metadata, so the two paths stay one schema.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import DDL, event

#: The tables a revision counter lands on, with their primary key column.
REVISIONED: tuple[tuple[str, str], ...] = (
    ("runs", "run_id"),
    ("daemon_work_items", "item_id"),
    ("daemon_merge_gates", "run_id"),
    ("daemon_review_holds", "run_id"),
)


def trigger_name(table: str) -> str:
    return f"trg_{table}_revision"


def trigger_ddl(table: str, key: str) -> str:
    """``WHEN NEW.revision = OLD.revision``: a write that set the counter
    itself (the trigger's own UPDATE, or a deliberate reset) is not bumped
    again, so the trigger never recurses."""
    # Not a query over input: `table` and `key` come from REVISIONED, a
    # tuple written here, never from a caller.
    return (
        f"CREATE TRIGGER IF NOT EXISTS {trigger_name(table)} "  # nosec B608
        f"AFTER UPDATE ON {table} "
        f"WHEN NEW.revision = OLD.revision "
        f"BEGIN UPDATE {table} SET revision = OLD.revision + 1 "
        f"WHERE {key} = NEW.{key}; END"
    )


def attach(table: Any, key: str) -> None:
    """Create the table's trigger whenever the metadata creates the table
    (``table`` is a model's ``__table__``)."""
    event.listen(
        table,
        "after_create",
        DDL(trigger_ddl(str(table.name), key)),  # type: ignore[no-untyped-call]
    )
