"""Merging one person's two accounts into one.

A provider that sends no verified email cannot be linked to the local
account that already exists, so the first sign-in through it provisions a
second account for the same person. ``CollaborationStore.merge_users`` (and
``sbxloop users merge`` over it) folds that account into the one it should
have been: everything it made moves, its provider identity signs in as the
target from then on, and it keeps no way in.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from sbxloop.api.auth import oidc
from sbxloop.api.collaboration import CollaborationError, CollaborationStore
from sbxloop.cli.app import app
from sbxloop.daemon.store import DaemonStore
from sbxloop.db.api_models import ApiEventRow
from sbxloop.db.collaboration_models import (
    AgentMemoryRow,
    ChannelMemberRow,
    ChannelRow,
    LocalUserRow,
    MessageRow,
    PreferenceRow,
    TeamRow,
    TurnRow,
    WorkflowRow,
    WorkspaceMemberRow,
)
from sbxloop.paths import SbxloopHome
from tests.api.conftest import Api
from tests.api.test_auth_oidc import ISSUER, SECRET, FakeIdP, _me, _serve, _sign_in

LOCAL = {
    "email": "bergs@example.test",
    "username": "bergs",
    "password": "correct horse battery staple",
}


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> FakeIdP:
    from tests.unit.test_daemon_loop import Clock

    fake = FakeIdP(clock=Clock())
    monkeypatch.setattr(oidc, "http_request", fake)
    monkeypatch.setenv("SBXLOOP_OIDC_CLIENT_SECRET", SECRET)
    return fake


@pytest.fixture
def served(tmp_path: Path, idp: FakeIdP) -> Iterator[Api]:
    yield from _serve(tmp_path, idp)


def _store(api: Api) -> CollaborationStore:
    return api.ctx.collaboration


def _user(api: Api, client_id: str) -> LocalUserRow:
    with _store(api).dstore.read() as session:
        row = session.scalars(select(LocalUserRow).where(LocalUserRow.client_id == client_id)).one()
        session.expunge(row)
        return row


def _twins(api: Api, idp: FakeIdP) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    """The local owner, and the second account the provider's first sign-in
    made for the same person because it shared no email."""
    registered = api.client.post("/v1/auth/local/register", json=LOCAL)
    assert registered.status_code == 201, registered.text
    local = dict(registered.json())
    tokens = _sign_in(api, idp, "bergs", email=None)
    assert tokens["client_id"] != local["client_id"]
    target = _user(api, local["client_id"])
    source = _user(api, tokens["client_id"])
    assert source.username == "bergs2" and source.auth_source == "oidc"
    assert source.email.endswith("@users.invalid")
    return local, tokens, str(source.id), str(target.id)


def _furnish(api: Api, source_id: str, target_id: str) -> dict[str, str]:
    """What the second account did while it was in use."""
    store = _store(api)
    now = api.clock()
    channel = store.create_channel(source_id, "made while signed in via SSO", now)
    turn, message, _ = store.accept_turn(
        source_id,
        channel.id,
        content="hello from the sso account",
        targets=(),
        client_turn_id=None,
        client_message_id=None,
        actor=None,
        now=now,
    )
    # The target is already in that channel as a plain member.
    with store.dstore.transaction() as session:
        session.add(
            ChannelMemberRow(
                channel_id=channel.id,
                user_id=target_id,
                role="member",
                added_by=source_id,
                joined_at=now,
                last_read_sequence=0,
            )
        )
        session.add(
            AgentMemoryRow(
                id="mem_merge_test",
                agent_slug="angie",
                kind="fact",
                content="prefers short answers",
                author=f"user:{source_id}",
                created_at=now,
                updated_at=now,
            )
        )
        # The source has read the channel; the target has not.
        own = session.get(ChannelMemberRow, (channel.id, source_id))
        assert own is not None
        own.last_read_sequence = message.sequence
    team = store.create_team(
        source_id,
        name="Reviewers",
        slug="reviewers",
        description=None,
        goal=None,
        agent_slugs=(),
        enabled=True,
        now=now,
    )
    store.upsert_preference(source_id, "tone", "terse", now)
    store.upsert_preference(source_id, "language", "en", now)
    store.upsert_preference(target_id, "tone", "friendly", now)
    workflow = store.create_workflow(
        source_id,
        name="Nightly",
        slug="nightly",
        description=None,
        trigger_event=None,
        enabled=True,
        now=now,
    )
    return {
        "channel": channel.id,
        "turn": turn.id,
        "message": message.id,
        "team": team.id,
        "workflow": workflow.id,
    }


def _merged_events(api: Api) -> list[ApiEventRow]:
    with _store(api).dstore.read() as session:
        rows = list(
            session.scalars(
                select(ApiEventRow).where(ApiEventRow.type == "collaboration.user.merged")
            ).all()
        )
        session.expunge_all()
        return rows


def test_merge_moves_everything_the_second_account_made(served: Api, idp: FakeIdP) -> None:
    _local, _tokens, source_id, target_id = _twins(served, idp)
    made = _furnish(served, source_id, target_id)

    report = _store(served).merge_users(source_id, target_id, served.clock())

    assert report.source_id == source_id and report.target_id == target_id
    assert report.preference_conflicts == ("tone",)
    with _store(served).dstore.read() as session:
        channel = session.get(ChannelRow, made["channel"])
        assert channel is not None
        assert (channel.user_id, channel.created_by) == (target_id, target_id)
        members = session.scalars(
            select(ChannelMemberRow).where(ChannelMemberRow.channel_id == made["channel"])
        ).all()
        # One membership, the stronger role.
        assert [(m.user_id, m.role) for m in members] == [(target_id, "owner")]
        message_row = session.get(MessageRow, made["message"])
        assert message_row is not None
        assert members[0].last_read_sequence == message_row.sequence
        message = session.get(MessageRow, made["message"])
        assert message is not None
        assert (message.author_kind, message.author_id) == ("human", target_id)
        turn = session.get(TurnRow, made["turn"])
        assert turn is not None and turn.author_id == target_id
        team = session.get(TeamRow, made["team"])
        assert team is not None and team.user_id == target_id
        workflow = session.get(WorkflowRow, made["workflow"])
        assert workflow is not None and workflow.user_id == target_id
        preferences = {
            row.name: row.content
            for row in session.scalars(
                select(PreferenceRow).where(PreferenceRow.user_id == target_id)
            ).all()
        }
        # The target's own answer wins a clash; the rest moves.
        assert preferences == {"tone": "friendly", "language": "en"}
        assert not session.scalar(
            select(func.count())
            .select_from(PreferenceRow)
            .where(PreferenceRow.user_id == source_id)
        )
        memory = session.get(AgentMemoryRow, "mem_merge_test")
        assert memory is not None and memory.author == f"user:{target_id}"
        # The target keeps the stronger workspace role; the source is gone.
        target_member = session.get(WorkspaceMemberRow, ("local", target_id))
        assert target_member is not None and target_member.role == "owner"
        assert session.get(WorkspaceMemberRow, ("local", source_id)) is None
        source = session.get(LocalUserRow, source_id)
        assert source is not None
        assert source.active == 0
        assert (source.oidc_issuer, source.oidc_subject) == (None, None)
        target = session.get(LocalUserRow, target_id)
        assert target is not None
        # The password keeps working, so the account stays ``local``.
        assert (target.oidc_issuer, target.oidc_subject) == (ISSUER, "bergs")
        assert (target.auth_source, target.username, target.email) == (
            "local",
            "bergs",
            LOCAL["email"],
        )

    (event,) = _merged_events(served)
    assert json.loads(event.data_json or "{}") == {
        "source_user_id": source_id,
        "target_user_id": target_id,
    }


def test_the_provider_identity_signs_in_as_the_target_afterwards(served: Api, idp: FakeIdP) -> None:
    local, _tokens, source_id, target_id = _twins(served, idp)

    _store(served).merge_users(source_id, target_id, served.clock())

    again = _sign_in(served, idp, "bergs", email=None)
    assert again["client_id"] == local["client_id"]
    me = _me(served, again)
    assert me.status_code == 200, me.text
    assert me.json()["username"] == "bergs"
    # The password still signs in too.
    login = served.client.post(
        "/v1/auth/local/login",
        json={"username": "bergs", "password": LOCAL["password"]},
    )
    assert login.status_code == 200, login.text
    # No third account appeared.
    with _store(served).dstore.read() as session:
        assert session.scalar(select(func.count()).select_from(LocalUserRow)) == 2


def test_the_source_accounts_tokens_stop_working(served: Api, idp: FakeIdP) -> None:
    _local, tokens, source_id, target_id = _twins(served, idp)
    assert _me(served, tokens).status_code == 200

    _store(served).merge_users(source_id, target_id, served.clock())

    refreshed = served.client.post(
        "/v1/auth/token",
        json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
    )
    assert refreshed.status_code == 401, refreshed.text
    assert _me(served, tokens).status_code in {401, 403}


def test_a_dry_run_reports_and_writes_nothing(served: Api, idp: FakeIdP) -> None:
    _local, tokens, source_id, target_id = _twins(served, idp)
    made = _furnish(served, source_id, target_id)

    def snapshot() -> dict[str, Any]:
        with _store(served).dstore.read() as session:
            return {
                "users": [
                    (r.id, r.active, r.oidc_subject, r.auth_source)
                    for r in session.scalars(select(LocalUserRow).order_by(LocalUserRow.id))
                ],
                "channel": session.get(ChannelRow, made["channel"]).user_id,  # type: ignore[union-attr]
                "members": session.scalar(select(func.count()).select_from(WorkspaceMemberRow)),
                "prefs": session.scalar(
                    select(func.count())
                    .select_from(PreferenceRow)
                    .where(PreferenceRow.user_id == source_id)
                ),
                "events": session.scalar(select(func.count()).select_from(ApiEventRow)),
            }

    before = snapshot()
    report = _store(served).merge_users(source_id, target_id, served.clock(), dry_run=True)

    assert report.dry_run is True
    assert report.moved["channels"] == 1
    assert report.moved["messages"] == 1
    assert report.moved["teams"] == 1
    assert report.preference_conflicts == ("tone",)
    assert snapshot() == before
    assert _me(served, tokens).status_code == 200


def test_the_target_keeps_the_stronger_workspace_role(served: Api, idp: FakeIdP) -> None:
    _local, _tokens, source_id, target_id = _twins(served, idp)
    store = _store(served)
    # A second owner, so the local one may be demoted for the test.
    store.set_role(source_id, "owner")
    store.set_role(target_id, "member")

    report = store.merge_users(source_id, target_id, served.clock())

    assert report.role == "owner"
    member = store.member_for_user(target_id)
    assert member is not None and member.role == "owner"


def test_a_clashing_team_or_workflow_moves_under_a_new_slug(served: Api, idp: FakeIdP) -> None:
    _local, _tokens, source_id, target_id = _twins(served, idp)
    store = _store(served)
    now = served.clock()
    for owner in (source_id, target_id):
        store.create_team(
            owner,
            name="Reviewers",
            slug="reviewers",
            description=None,
            goal=None,
            agent_slugs=(),
            enabled=True,
            now=now,
        )
        store.create_workflow(
            owner,
            name="Nightly",
            slug="nightly",
            description=None,
            trigger_event=None,
            enabled=True,
            now=now,
        )

    report = store.merge_users(source_id, target_id, served.clock())

    assert report.renamed_teams == (("reviewers", "reviewers-merged"),)
    assert report.renamed_workflows == (("nightly", "nightly-merged"),)
    assert sorted(t.slug for t in store.list_teams(target_id)) == ["reviewers", "reviewers-merged"]
    assert sorted(w.slug for w in store.list_workflows(target_id)) == [
        "nightly",
        "nightly-merged",
    ]


def test_self_merge_is_refused(served: Api, idp: FakeIdP) -> None:
    _local, _tokens, _source_id, target_id = _twins(served, idp)
    with pytest.raises(CollaborationError) as caught:
        _store(served).merge_users(target_id, target_id, served.clock())
    assert caught.value.code == "merge_same_user"


def test_a_missing_user_is_refused(served: Api, idp: FakeIdP) -> None:
    _local, _tokens, source_id, _target_id = _twins(served, idp)
    with pytest.raises(CollaborationError) as caught:
        _store(served).merge_users(source_id, "usr_nobody", served.clock())
    assert caught.value.code == "user_not_found"


def test_a_target_with_another_provider_identity_is_refused(served: Api, idp: FakeIdP) -> None:
    _local, _tokens, source_id, target_id = _twins(served, idp)
    with _store(served).dstore.transaction() as session:
        target = session.get(LocalUserRow, target_id)
        assert target is not None
        target.oidc_issuer = ISSUER
        target.oidc_subject = "someone-else"

    with pytest.raises(CollaborationError) as caught:
        _store(served).merge_users(source_id, target_id, served.clock())

    assert caught.value.code == "merge_identity_conflict"
    assert _merged_events(served) == []
    with _store(served).dstore.read() as session:
        source = session.get(LocalUserRow, source_id)
        assert source is not None and source.active == 1 and source.oidc_subject == "bergs"


# -- the command -------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SbxloopHome:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "200")
    return SbxloopHome(tmp_path / ".sbxloop")


def _seed(home: SbxloopHome) -> tuple[str, str]:
    dstore = DaemonStore(home.state_db)
    try:
        store = CollaborationStore(dstore)
        target = store.register_user(
            username="bergs",
            email=LOCAL["email"],
            password=LOCAL["password"],
            full_name=None,
            timezone="UTC",
            now=1.0,
        )
        source = store.sign_in_external(
            issuer=ISSUER,
            subject="bergs",
            username="bergs",
            email=None,
            email_verified=False,
            full_name=None,
            role_from_groups=None,
            default_role="admin",
            auto_provision=True,
            now=2.0,
        )
        store.create_channel(source.id, "sso channel", 3.0)
        return source.id, target.id
    finally:
        dstore.close()


def _source_state(home: SbxloopHome, source_id: str) -> tuple[int, str | None]:
    dstore = DaemonStore(home.state_db)
    try:
        with dstore.read() as session:
            row = session.get(LocalUserRow, source_id)
            assert row is not None
            return int(row.active), row.oidc_subject
    finally:
        dstore.close()


def test_the_command_without_yes_only_prints_what_would_move(home: SbxloopHome) -> None:
    source_id, _target_id = _seed(home)

    result = runner.invoke(app, ["users", "merge", "--from", "bergs2", "--into", "bergs"])

    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower()
    assert "channels" in result.output
    assert "--yes" in result.output
    assert _source_state(home, source_id) == (1, "bergs")


def test_the_command_with_yes_merges_by_username_or_id(home: SbxloopHome) -> None:
    source_id, target_id = _seed(home)

    result = runner.invoke(app, ["users", "merge", "--from", source_id, "--into", "bergs", "--yes"])

    assert result.exit_code == 0, result.output
    assert "merged" in result.output.lower()
    assert _source_state(home, source_id) == (0, None)
    dstore = DaemonStore(home.state_db)
    try:
        member = CollaborationStore(dstore).member_for_user(target_id)
        assert member is not None and member.role == "owner"
    finally:
        dstore.close()


def test_the_command_refuses_an_unknown_user(home: SbxloopHome) -> None:
    _seed(home)
    result = runner.invoke(app, ["users", "merge", "--from", "nobody", "--into", "bergs"])
    assert result.exit_code == 2
    assert "nobody" in result.output
