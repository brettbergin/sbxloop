"""Revision 0021: channel membership, participants and authorship.

A database written before the revision holds a single owner's channels,
messages and turns with no author. After the upgrade every row carries the
author it had all along, every channel has its owner as a member, and running
the upgrade again changes nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command

from sbxloop.db import ensure_schema, open_engine
from sbxloop.db.schema import _config

OWNER = "usr_owner"
CHANNEL = "chn_one"


def _upgrade_to(path: Path, revision: str) -> None:
    engine = open_engine(path)
    try:
        with engine.begin() as conn:
            command.upgrade(_config(conn), revision)
    finally:
        engine.dispose()


def _stamp(path: Path, revision: str) -> None:
    engine = open_engine(path)
    try:
        with engine.begin() as conn:
            command.stamp(_config(conn), revision)
    finally:
        engine.dispose()


def _head(path: Path) -> None:
    engine = open_engine(path)
    try:
        ensure_schema(engine)
    finally:
        engine.dispose()


def _seed(path: Path) -> None:
    """What a release before the revision wrote for one conversation."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO collaboration_users (id, client_id, username, email, full_name,"
            " timezone, created_at, updated_at, active)"
            " VALUES (?, 'local_1', 'owner', 'o@example.test', 'Local Owner', 'UTC', 1, 1, 1)",
            (OWNER,),
        )
        conn.execute(
            "INSERT INTO collaboration_channels (id, workspace_id, user_id, title, state,"
            " revision, created_at, updated_at) VALUES (?, 'local', ?, 'T', 'active', 1, 1, 1)",
            (CHANNEL, OWNER),
        )
        conn.execute(
            "INSERT INTO collaboration_channels (id, workspace_id, user_id, title, state,"
            " revision, created_at, updated_at, deleted_at)"
            " VALUES ('chn_gone', 'local', ?, 'Gone', 'deleted', 2, 1, 2, 2)",
            (OWNER,),
        )
        rows = [
            # id, role, kind, agent_slug
            ("msg_user", "user", "message", None),
            ("msg_angie", "assistant", "message", None),
            ("msg_planner", "assistant", "agent_result", "planner"),
            ("msg_handoff", "assistant", "agent_handoff", "critic"),
            ("msg_error", "assistant", "turn_error", None),
            ("msg_cancel", "assistant", "turn_cancelled", None),
            ("msg_work", "assistant", "work_result", None),
            ("msg_work_op", "assistant", "work_result", "operator"),
        ]
        for sequence, (message_id, role, kind, slug) in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO collaboration_messages (id, channel_id, turn_id, sequence, role,"
                " kind, content, agent_slug, created_at) VALUES (?, ?, 'trn_one', ?, ?, ?, 'x',"
                " ?, 1)",
                (message_id, CHANNEL, sequence, role, kind, slug),
            )
        conn.execute(
            "INSERT INTO collaboration_turns (id, channel_id, input_message_id, status,"
            " created_at) VALUES ('trn_one', ?, 'msg_user', 'completed', 1)",
            (CHANNEL,),
        )
        conn.commit()
    finally:
        conn.close()


def _query(path: Path, sql: str) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path)
    try:
        return list(conn.execute(sql).fetchall())
    finally:
        conn.close()


def _migrated(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    _upgrade_to(path, "0020")
    _seed(path)
    _head(path)
    return path


def test_every_existing_message_gets_the_author_it_always_had(tmp_path: Path) -> None:
    path = _migrated(tmp_path)
    authors = {
        row[0]: (row[1], row[2])
        for row in _query(path, "SELECT id, author_kind, author_id FROM collaboration_messages")
    }
    assert authors == {
        "msg_user": ("human", OWNER),
        "msg_angie": ("agent", "concierge"),
        "msg_planner": ("agent", "planner"),
        "msg_handoff": ("agent", "critic"),
        "msg_error": ("system", None),
        "msg_cancel": ("system", None),
        "msg_work": ("agent", "concierge"),
        "msg_work_op": ("agent", "operator"),
    }


def test_existing_turns_are_the_channel_owners_and_human_triggered(tmp_path: Path) -> None:
    path = _migrated(tmp_path)
    assert _query(
        path,
        "SELECT author_kind, author_id, trigger, parent_turn_id, source_message_id,"
        " chain_depth FROM collaboration_turns",
    ) == [("human", OWNER, "human", None, None, 0)]


def test_existing_channels_are_private_created_by_and_owned_by_their_user(
    tmp_path: Path,
) -> None:
    path = _migrated(tmp_path)
    assert sorted(
        _query(
            path,
            "SELECT id, visibility, created_by, silenced_until, settings_json"
            " FROM collaboration_channels",
        )
    ) == [
        ("chn_gone", "private", OWNER, None, None),
        (CHANNEL, "private", OWNER, None, None),
    ]
    assert sorted(
        _query(
            path,
            "SELECT channel_id, user_id, role, added_by, last_read_sequence"
            " FROM collaboration_channel_members",
        )
    ) == [
        ("chn_gone", OWNER, "owner", None, 0),
        (CHANNEL, OWNER, "owner", None, 0),
    ]
    assert _query(path, "SELECT joined_at FROM collaboration_channel_members") == [(1.0,), (1.0,)]
    assert _query(path, "SELECT * FROM collaboration_channel_participants") == []


def test_visibility_and_roles_refuse_unknown_values(tmp_path: Path) -> None:
    path = _migrated(tmp_path)
    conn = sqlite3.connect(path)
    try:
        for statement in (
            f"UPDATE collaboration_channels SET visibility = 'public' WHERE id = '{CHANNEL}'",
            "UPDATE collaboration_channel_members SET role = 'admin'"
            f" WHERE channel_id = '{CHANNEL}'",
            "INSERT INTO collaboration_channel_participants (channel_id, agent_slug, mode,"
            f" added_by_kind, created_at) VALUES ('{CHANNEL}', 'planner', 'loud', 'human', 1)",
        ):
            try:
                conn.execute(statement)
            except sqlite3.IntegrityError:
                continue
            raise AssertionError(f"accepted: {statement}")
    finally:
        conn.close()


def test_running_the_upgrade_again_changes_nothing(tmp_path: Path) -> None:
    path = _migrated(tmp_path)
    tables = (
        "collaboration_channels",
        "collaboration_channel_members",
        "collaboration_channel_participants",
        "collaboration_messages",
        "collaboration_turns",
    )
    before = {table: sorted(_query(path, f"SELECT * FROM {table}")) for table in tables}
    _stamp(path, "0020")
    _head(path)
    after = {table: sorted(_query(path, f"SELECT * FROM {table}")) for table in tables}
    assert after == before
