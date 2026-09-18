"""Revision 0030: what a run has already said in a channel.

A database written before the revision gains an empty ``channel_run_posts``
table, indexed by run, and every message it already held reads back with no
``post_kind``. Running the upgrade again changes nothing, including the posts
recorded since the first upgrade.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import inspect

from sbxloop.db import open_engine
from tests.unit.test_channel_membership_migration import _head, _query, _stamp, _upgrade_to

BEFORE = "0029"


def _seed(path: Path) -> None:
    """One channel with one message, as a release before the revision wrote it."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO collaboration_users (id, client_id, username, email, full_name,"
            " timezone, created_at, updated_at, active)"
            " VALUES ('usr_owner', 'local_1', 'owner', 'o@example.test', 'Owner', 'UTC',"
            " 1, 1, 1)"
        )
        conn.execute(
            "INSERT INTO collaboration_channels (id, workspace_id, user_id, title, state,"
            " revision, created_at, updated_at)"
            " VALUES ('chn_one', 'local', 'usr_owner', 'T', 'active', 1, 1, 1)"
        )
        conn.execute(
            "INSERT INTO collaboration_messages (id, channel_id, sequence, role, kind,"
            " content, created_at) VALUES ('msg_old', 'chn_one', 1, 'user', 'message',"
            " 'hello', 1)"
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


def test_the_upgrade_adds_an_empty_post_table_and_leaves_messages_unkinded(
    tmp_path: Path,
) -> None:
    path = _migrated(tmp_path)
    assert _query(path, "SELECT * FROM channel_run_posts") == []
    assert _query(path, "SELECT id, post_kind FROM collaboration_messages") == [("msg_old", None)]
    engine = open_engine(path)
    try:
        indexes = {
            index["name"]: index["column_names"]
            for index in inspect(engine).get_indexes("channel_run_posts")
        }
    finally:
        engine.dispose()
    assert indexes["idx_channel_run_posts_run"] == ["run_id", "posted_at"]


def test_running_the_upgrade_again_changes_nothing(tmp_path: Path) -> None:
    path = _migrated(tmp_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO channel_run_posts (dedupe_key, run_id, message_id, kind, posted_at)"
            " VALUES ('r1:plan', 'r1', 'msg_old', 'plan', 2)"
        )
        conn.execute("UPDATE collaboration_messages SET post_kind = 'plan'")
        conn.commit()
    finally:
        conn.close()
    posts = _query(path, "SELECT * FROM channel_run_posts")
    messages = _query(path, "SELECT * FROM collaboration_messages")

    _stamp(path, BEFORE)
    _head(path)

    assert _query(path, "SELECT * FROM channel_run_posts") == posts
    assert _query(path, "SELECT * FROM collaboration_messages") == messages
