"""Revision 0026: public events know their channel and their audience.

Collaboration events recorded before the revision name their channel only in
their data, and agent memory events name the channel the memory came from as
``source_channel_id``. After the upgrade each carries it in ``channel_id``
too, every other event keeps ``NULL``, nobody is an audience yet, and
running the upgrade again changes nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import inspect

from sbxloop.db import open_engine
from tests.unit.test_channel_membership_migration import _head, _query, _stamp, _upgrade_to

BEFORE = "0025"
EVENTS = [
    # type, data_json
    ("collaboration.channel.created", '{"channel_id": "chn_one"}'),
    ("collaboration.message.created", '{"channel_id": "chn_two", "sequence": 1}'),
    ("collaboration.team.created", '{"team_id": "team_1"}'),
    ("collaboration.turn.accepted", '{"channel_id": 7}'),
    ("collaboration.channel.updated", "not json"),
    ("collaboration.channel.deleted", None),
    ("daemon.notice", '{"channel_id": "chn_one"}'),
    ("run.started", '{"kind": "code"}'),
    ("agent.memory.created", '{"memory_id": "mem_1", "source_channel_id": "chn_one"}'),
    ("agent.memory.updated", '{"memory_id": "mem_2", "source_channel_id": null}'),
    ("agent.memory.deleted", "not json"),
    ("agent.run.started", '{"source_channel_id": "chn_one"}'),
]


def _seed(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        for type_, data in EVENTS:
            conn.execute(
                "INSERT INTO api_events (recorded_at, occurred_at, type, data_json)"
                " VALUES (1, 1, ?, ?)",
                (type_, data),
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


def test_collaboration_events_take_the_channel_their_data_names(tmp_path: Path) -> None:
    path = _migrated(tmp_path)
    assert _query(
        path, "SELECT type, channel_id, audience_user_id FROM api_events ORDER BY seq"
    ) == [
        ("collaboration.channel.created", "chn_one", None),
        ("collaboration.message.created", "chn_two", None),
        ("collaboration.team.created", None, None),
        ("collaboration.turn.accepted", None, None),
        ("collaboration.channel.updated", None, None),
        ("collaboration.channel.deleted", None, None),
        ("daemon.notice", None, None),
        ("run.started", None, None),
        ("agent.memory.created", "chn_one", None),
        ("agent.memory.updated", None, None),
        ("agent.memory.deleted", None, None),
        ("agent.run.started", None, None),
    ]


def test_events_are_indexed_by_channel_then_sequence(tmp_path: Path) -> None:
    path = _migrated(tmp_path)
    engine = open_engine(path)
    try:
        indexes = {
            index["name"]: index["column_names"]
            for index in inspect(engine).get_indexes("api_events")
        }
    finally:
        engine.dispose()
    assert indexes["idx_api_events_channel"] == ["channel_id", "seq"]


def test_running_the_upgrade_again_changes_nothing(tmp_path: Path) -> None:
    path = _migrated(tmp_path)
    before = _query(path, "SELECT * FROM api_events ORDER BY seq")
    _stamp(path, BEFORE)
    _head(path)
    assert _query(path, "SELECT * FROM api_events ORDER BY seq") == before
