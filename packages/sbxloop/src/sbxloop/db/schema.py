"""Applying the schema: Alembic, driven from the store's own open.

The daemon deploys unattended and upgrades in place — nobody runs a command
between ``pip install`` and the restart — so migrating has to happen when a
store opens the database, not when an operator remembers to ask. That is what
:func:`ensure_schema` does.

One chain covers all twenty-one tables. All stores live in one file, so one
``alembic_version`` row describes it; a store that opened only "its own"
tables would still be looking at a file the other one migrates.

The contract every revision here is held to (#539): **additive only**. A
failed deploy rolls back by reinstalling the previous version and restarting
it against the database this code already migrated — the snapshot it took is
not restored — so the previous version has to keep reading the file
correctly. New tables and new nullable-or-defaulted columns satisfy that. A
rename, a drop or a retype does not, and belongs in the release *after* the
one that stopped depending on the old shape.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine

from sbxloop.db import campaign_models  # noqa: F401 - register the campaign tables in Base.metadata

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

#: Alembic narrates every step at INFO. That is right for a developer
#: running it by hand and wrong here: the daemon migrates on open, and its
#: log ring buffer is what an operator reads over chat — three lines of
#: "Context impl SQLiteImpl" per start would push out the run they were
#: looking for. A migration that actually fails still raises.
_ALEMBIC_LOGGERS = ("alembic", "alembic.runtime.migration", "alembic.autogenerate")


@contextmanager
def _quiet_alembic() -> Iterator[None]:
    """Hold Alembic's own loggers at WARNING for the duration of a call."""
    saved = [(logging.getLogger(name), logging.getLogger(name).level) for name in _ALEMBIC_LOGGERS]
    for logger, _ in saved:
        logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for logger, level in saved:
            logger.setLevel(level)


def _config(connection: Connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("version_locations", str(MIGRATIONS_DIR / "versions"))
    # Without this Alembic splits `version_locations` on spaces and commas,
    # which a home directory containing either would break.
    cfg.set_main_option("path_separator", "os")
    cfg.attributes["connection"] = connection
    return cfg


def head_revision() -> str:
    """The revision a freshly migrated database should be stamped at."""
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    head = script.get_current_head()
    if head is None:  # pragma: no cover - the versions directory is packaged
        raise RuntimeError(f"no Alembic revisions found in {MIGRATIONS_DIR}")
    return head


def current_revision(engine: Engine) -> str | None:
    """The revision ``engine``'s database is stamped at, or None if unstamped."""
    # `MigrationContext.configure` narrates too, and this runs on every open.
    with _quiet_alembic(), engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def ensure_schema(engine: Engine) -> None:
    """Bring the database behind ``engine`` up to the current schema.

    Safe to call on a fresh file, on a database written by any released
    version, and on one already at head — the last costs one read of
    ``alembic_version`` and nothing more, which is why it can sit in a store's
    constructor.
    """
    with _quiet_alembic():
        if current_revision(engine) == head_revision():
            return
        with engine.connect() as conn:
            command.upgrade(_config(conn), "head")
