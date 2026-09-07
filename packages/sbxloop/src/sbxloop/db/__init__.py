"""The data layer: one SQLite file, SQLAlchemy models, Alembic migrations.

``<home>/state/state.db`` holds every table sbxloop persists — the engine's
five, the daemon's fourteen, and the campaign store's two.
:mod:`sbxloop.db.session` builds the connections,
:mod:`sbxloop.db.schema` applies the migrations, and
:mod:`sbxloop.db.base` carries the metadata the models hang off.
"""

from __future__ import annotations

from sbxloop.db.base import Base
from sbxloop.db.schema import current_revision, ensure_schema, head_revision
from sbxloop.db.session import BUSY_TIMEOUT_MS, begin_immediate, open_engine, readonly_uri

__all__ = [
    "BUSY_TIMEOUT_MS",
    "Base",
    "begin_immediate",
    "current_revision",
    "ensure_schema",
    "head_revision",
    "open_engine",
    "readonly_uri",
]
