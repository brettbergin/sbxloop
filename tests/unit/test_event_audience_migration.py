"""Revision 0039: events recorded for one person before 0026 get their audience.

Revision 0026 gave ``api_events`` an ``audience_user_id`` but left every
recorded event without one, so a person's own team, preference, workflow and
profile events from an older release reached every workspace member. After
the upgrade each carries the person it is about: the ``user_id`` its data
names, or the owner of the team, preference or workflow its data names. One
whose person can no longer be told (its team, preference or workflow is
gone) is for nobody, so no workspace member sees it. Events that are not
about one person, and events that already have an audience, keep what they
had, and running the upgrade again changes nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.unit.test_channel_membership_migration import _head, _query, _stamp, _upgrade_to

BEFORE = "0038"
NOBODY = ""
EVENTS = [
    # type, data_json, audience_user_id already recorded
    ("collaboration.team.created", '{"team_id": "team_ann"}', None),
    ("collaboration.team.updated", '{"team_id": "team_bob"}', None),
    ("collaboration.team.deleted", '{"team_id": "team_gone"}', None),
    ("collaboration.preference.created", '{"preference_id": "pref_ann", "name": "s"}', None),
    ("collaboration.preference.updated", '{"preference_id": "pref_ann", "name": "s"}', None),
    ("collaboration.preference.deleted", '{"preference_id": "pref_gone", "name": "s"}', None),
    ("collaboration.preferences.reset", '{"user_id": "usr_bob", "deleted": 2}', None),
    ("collaboration.workflow.created", '{"workflow_id": "wf_bob", "slug": "w"}', None),
    ("collaboration.workflow.updated", '{"workflow_id": "wf_bob", "slug": "w"}', None),
    ("collaboration.workflow.deleted", "not json", None),
    ("collaboration.user.updated", '{"user_id": "usr_ann"}', None),
    ("collaboration.user.updated", '{"user_id": 7}', None),
    ("collaboration.channel.read", '{"channel_id": "chn_one", "user_id": "usr_bob"}', None),
    ("collaboration.team.created", '{"team_id": "team_ann"}', "usr_ann"),
    # Not about one person: the sign-in audit, a merge, channel activity.
    ("collaboration.user.created", '{"user_id": "usr_ann"}', None),
    ("collaboration.user.merged", '{"source_user_id": "usr_ann"}', None),
    ("collaboration.channel.created", '{"channel_id": "chn_one", "user_id": "usr_ann"}', None),
    ("daemon.notice", '{"user_id": "usr_ann"}', None),
]


def _seed(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO collaboration_teams (id, user_id, name, slug, created_at, updated_at)"
            " VALUES ('team_ann', 'usr_ann', 'T', 't', 1, 1),"
            " ('team_bob', 'usr_bob', 'T', 't', 1, 1)"
        )
        conn.execute(
            "INSERT INTO collaboration_preferences"
            " (id, user_id, name, content, created_at, updated_at)"
            " VALUES ('pref_ann', 'usr_ann', 's', 'brief', 1, 1)"
        )
        conn.execute(
            "INSERT INTO collaboration_workflows (id, user_id, name, slug, created_at, updated_at)"
            " VALUES ('wf_bob', 'usr_bob', 'W', 'w', 1, 1)"
        )
        for type_, data, audience in EVENTS:
            conn.execute(
                "INSERT INTO api_events (recorded_at, occurred_at, type, data_json,"
                " audience_user_id) VALUES (1, 1, ?, ?, ?)",
                (type_, data, audience),
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


def test_per_person_events_take_the_person_they_are_about(tmp_path: Path) -> None:
    path = _migrated(tmp_path)
    assert _query(path, "SELECT type, audience_user_id FROM api_events ORDER BY seq") == [
        ("collaboration.team.created", "usr_ann"),
        ("collaboration.team.updated", "usr_bob"),
        ("collaboration.team.deleted", NOBODY),
        ("collaboration.preference.created", "usr_ann"),
        ("collaboration.preference.updated", "usr_ann"),
        ("collaboration.preference.deleted", NOBODY),
        ("collaboration.preferences.reset", "usr_bob"),
        ("collaboration.workflow.created", "usr_bob"),
        ("collaboration.workflow.updated", "usr_bob"),
        ("collaboration.workflow.deleted", NOBODY),
        ("collaboration.user.updated", "usr_ann"),
        ("collaboration.user.updated", NOBODY),
        ("collaboration.channel.read", "usr_bob"),
        ("collaboration.team.created", "usr_ann"),
        ("collaboration.user.created", None),
        ("collaboration.user.merged", None),
        ("collaboration.channel.created", None),
        ("daemon.notice", None),
    ]


def test_running_the_upgrade_again_changes_nothing(tmp_path: Path) -> None:
    path = _migrated(tmp_path)
    before = _query(path, "SELECT * FROM api_events ORDER BY seq")
    _stamp(path, BEFORE)
    _head(path)
    assert _query(path, "SELECT * FROM api_events ORDER BY seq") == before
