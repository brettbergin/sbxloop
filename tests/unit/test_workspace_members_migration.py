"""The workspace-membership revision: an installation's existing local user
becomes the workspace owner, and running the revision twice changes nothing."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command

from sbxloop.db import ensure_schema, open_engine
from sbxloop.db.schema import _config

PRE_MEMBERSHIP = "0019"


def _database_at(path: Path, revision: str) -> None:
    engine = open_engine(path)
    try:
        with engine.connect() as conn:
            command.upgrade(_config(conn), revision)
    finally:
        engine.dispose()


def _seed_local_user(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO api_clients (id, name, secret_hash, capabilities_json, created_at)"
            " VALUES ('local_abc', 'owner', 'x', '[]', 10.0)"
        )
        conn.execute(
            "INSERT INTO collaboration_users"
            " (id, client_id, username, email, created_at, updated_at)"
            " VALUES ('usr_abc', 'local_abc', 'owner', 'owner@example.test', 10.0, 11.0)"
        )
        conn.commit()
    finally:
        conn.close()


def _members(path: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT workspace_id, user_id, role, created_at, invited_by FROM workspace_members"
        ).fetchall()
    finally:
        conn.close()


def _upgrade(path: Path) -> None:
    engine = open_engine(path)
    try:
        ensure_schema(engine)
    finally:
        engine.dispose()


def test_the_existing_local_user_becomes_the_workspace_owner(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _database_at(path, PRE_MEMBERSHIP)
    _seed_local_user(path)

    _upgrade(path)

    assert _members(path) == [("local", "usr_abc", "owner", 10.0, None)]
    conn = sqlite3.connect(path)
    try:
        user = conn.execute(
            "SELECT auth_source, oidc_issuer, oidc_subject, avatar_url, last_seen_at"
            " FROM collaboration_users"
        ).fetchone()
        invites = conn.execute("SELECT count(*) FROM workspace_invites").fetchone()
    finally:
        conn.close()
    assert user == ("local", None, None, None, None)
    assert invites == (0,)


def test_running_the_revision_again_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _database_at(path, PRE_MEMBERSHIP)
    _seed_local_user(path)
    _upgrade(path)

    # A database whose stamp was rolled back by hand still upgrades.
    engine = open_engine(path)
    try:
        with engine.connect() as conn:
            command.stamp(_config(conn), PRE_MEMBERSHIP)
        ensure_schema(engine)
    finally:
        engine.dispose()

    assert _members(path) == [("local", "usr_abc", "owner", 10.0, None)]


def test_oidc_identities_are_unique_per_issuer(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _upgrade(path)
    conn = sqlite3.connect(path)
    try:
        insert = (
            "INSERT INTO collaboration_users (id, client_id, username, email, created_at,"
            " updated_at, oidc_issuer, oidc_subject) VALUES (?, ?, ?, ?, 1.0, 1.0, ?, ?)"
        )
        conn.execute(insert, ("u1", "c1", "a", "a@x", "https://idp", "sub-1"))
        conn.execute(insert, ("u2", "c2", "b", "b@x", None, None))
        conn.execute(insert, ("u3", "c3", "c", "c@x", None, None))
        try:
            conn.execute(insert, ("u4", "c4", "d", "d@x", "https://idp", "sub-1"))
        except sqlite3.IntegrityError:
            duplicate_refused = True
        else:
            duplicate_refused = False
    finally:
        conn.close()
    assert duplicate_refused
