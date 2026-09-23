"""Revision 0034: whether a local account's email came from a trusted path.

A database written before the revision holds accounts whose email may have
been changed by whoever holds them, so every one of them reads back with
``email_verified = 0``: none is linkable by an OIDC sign-in until a provider
verifies the address. Running the upgrade again changes nothing, including
what was recorded since the first upgrade.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.unit.test_channel_membership_migration import _head, _query, _stamp, _upgrade_to

BEFORE = "0033"


def _seed(path: Path) -> None:
    """Two local accounts, as a release before the revision wrote them."""
    conn = sqlite3.connect(path)
    try:
        for user_id, client_id, name in (
            ("usr_owner", "local_1", "owner"),
            ("usr_bob", "local_2", "bob"),
        ):
            conn.execute(
                "INSERT INTO collaboration_users (id, client_id, username, email, full_name,"
                " timezone, created_at, updated_at, active)"
                " VALUES (?, ?, ?, ?, ?, 'UTC', 1, 1, 1)",
                (user_id, client_id, name, f"{name}@example.test", name.title()),
            )
        conn.commit()
    finally:
        conn.close()


def _migrated(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    _upgrade_to(path, BEFORE)
    _seed(path)
    _head(path)
    return path


def test_every_existing_account_reads_back_unverified(tmp_path: Path) -> None:
    path = _migrated(tmp_path)
    rows = _query(path, "SELECT id, email_verified FROM collaboration_users ORDER BY id")
    assert rows == [("usr_bob", 0), ("usr_owner", 0)]


def test_running_the_upgrade_again_changes_nothing(tmp_path: Path) -> None:
    path = _migrated(tmp_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE collaboration_users SET email_verified = 1 WHERE id = 'usr_owner'")
        conn.commit()
    finally:
        conn.close()
    before = _query(path, "SELECT * FROM collaboration_users ORDER BY id")

    _stamp(path, BEFORE)
    _head(path)

    assert _query(path, "SELECT * FROM collaboration_users ORDER BY id") == before
