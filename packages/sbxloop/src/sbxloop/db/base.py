"""The declarative base every model in the one state database hangs off.

``Base.metadata`` is the single source of truth for the schema from Alembic
revision 0002 onward. Revision 0001 predates it on purpose: see
:mod:`sbxloop.db.schema`.

The naming convention matters more than it looks. SQLite names an unnamed
constraint itself, and the names it picks differ between a table created by
``CREATE TABLE`` and one rebuilt by Alembic's batch mode — so a migration
that has to drop a constraint on a deployed database can only find it if the
name was ours to begin with.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
