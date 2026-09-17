"""Durable collaboration state for local product clients.

The execution API remains run-oriented. This store adds the resources a
chat product needs around it: a local profile, channels, messages, turns,
agent teams, preferences, and workflow definitions. Every write uses the
daemon store's lock and transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from sbxloop.api.agents import AGENTS, ANGIE_SLUG
from sbxloop.api.auth.store import hash_secret
from sbxloop.api.channel_access import ChannelAccess, ChannelRole, Need

# The role names belong to this module's membership contract, so they are
# re-exported explicitly (``X as X``) for strictly type-checked consumers.
from sbxloop.daemon.controls.principal import (
    ALL_CAPABILITIES,
    CAPABILITIES,
    ROLE_CAPABILITIES as ROLE_CAPABILITIES,
    ROLES as ROLES,
    WORKSPACE_ID,
    Capability,
    Role as Role,
)
from sbxloop.daemon.store import DaemonStore
from sbxloop.db.api_models import ApiEventRow, ClientRow, RefreshTokenRow
from sbxloop.db.collaboration_models import (
    ChannelMemberRow,
    ChannelParticipantRow,
    ChannelRow,
    LocalUserRow,
    MessageRow,
    PreferenceRow,
    TeamRow,
    TurnRow,
    WorkflowRow,
    WorkspaceInviteRow,
    WorkspaceMemberRow,
)
from sbxloop.ids import _token
from sbxloop.log import get_logger

log = get_logger(__name__)

#: Characters a provider-suggested username may not keep.
_USERNAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_HANDOFFS_PER_TURN = 6
MAX_HANDOFFS_PER_RESPONSE = 2
# Four hops admit a bounded review return path such as coordinator -> author
# -> reviewer -> author -> coordinator. The agents choose the path; this is a
# circuit breaker, not a workflow definition.
MAX_HANDOFF_DEPTH = 4


class CollaborationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


AuthorKind = Literal["human", "agent", "system"]


@dataclass(frozen=True, slots=True)
class Author:
    """Who wrote a message or started a turn."""

    kind: AuthorKind
    id: str | None
    display_name: str | None = None


SYSTEM_AUTHOR = Author("system", None)
#: Kinds of assistant message the transport writes on its own behalf.
SYSTEM_MESSAGE_KINDS = frozenset({"turn_error", "turn_cancelled"})


@dataclass(frozen=True, slots=True)
class LocalUser:
    id: str
    client_id: str
    username: str
    email: str
    full_name: str | None
    timezone: str
    active: bool
    created_at: float
    updated_at: float
    auth_source: str = "local"
    avatar_url: str | None = None
    last_seen_at: float | None = None


@dataclass(frozen=True, slots=True)
class Member:
    """A local user and their standing in the workspace."""

    user: LocalUser
    role: Role
    workspace_id: str


@dataclass(frozen=True, slots=True)
class Invite:
    """A workspace invitation, never carrying its token."""

    id: str
    workspace_id: str
    email: str | None
    role: Role
    expires_at: float
    accepted_at: float | None
    created_by: str
    created_at: float


@dataclass(frozen=True, slots=True)
class Channel:
    id: str
    workspace_id: str
    user_id: str
    title: str
    state: str
    revision: int
    created_at: float
    updated_at: float
    deleted_at: float | None
    visibility: str = "private"
    created_by: str | None = None
    silenced_until: float | None = None
    #: The reader's role in the channel, for a read made on someone's behalf.
    my_role: ChannelRole | None = None


@dataclass(frozen=True, slots=True)
class ChannelMember:
    """A person in a channel."""

    channel_id: str
    user: LocalUser
    role: ChannelRole
    joined_at: float
    last_read_sequence: int
    added_by: str | None


ParticipantMode = Literal["mention", "ambient"]


@dataclass(frozen=True, slots=True)
class ChannelParticipant:
    """An agent in a channel."""

    channel_id: str
    agent_slug: str
    mode: ParticipantMode
    added_by: Author
    muted_until: float | None
    created_at: float


#: Who a store read or write is made for: a workspace member (or their user
#: id, resolved to the member), or ``None`` for a plain API client or the
#: daemon itself, which keep full access.
type Viewer = Member | str | None


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    channel_id: str
    turn_id: str | None
    client_message_id: str | None
    sequence: int
    role: str
    kind: str
    content: str
    agent_slug: str | None
    created_at: float
    work: dict[str, Any] | None = None
    reactions: tuple[str, ...] = ()
    author: Author = SYSTEM_AUTHOR
    artifacts: tuple[Any, ...] = ()
    origin: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Turn:
    id: str
    channel_id: str
    client_turn_id: str | None
    input_message_id: str
    status: str
    targets: tuple[str, ...]
    error: str | None
    created_at: float
    started_at: float | None
    completed_at: float | None
    intent: str = "conversation"
    participants: tuple[dict[str, Any], ...] = ()
    author: Author | None = None
    trigger: str = "human"
    parent_turn_id: str | None = None
    source_message_id: str | None = None
    chain_depth: int = 0


@dataclass(frozen=True, slots=True)
class Team:
    id: str
    user_id: str
    name: str
    slug: str
    description: str | None
    goal: str | None
    agent_slugs: tuple[str, ...]
    enabled: bool
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class Preference:
    id: str
    user_id: str
    name: str
    content: str
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class Workflow:
    id: str
    user_id: str
    name: str
    slug: str
    description: str | None
    trigger_event: str | None
    enabled: bool
    created_at: float
    updated_at: float


def _user(row: LocalUserRow) -> LocalUser:
    return LocalUser(
        id=str(row.id),
        client_id=str(row.client_id),
        username=str(row.username),
        email=str(row.email),
        full_name=None if row.full_name is None else str(row.full_name),
        timezone=str(row.timezone),
        active=bool(row.active),
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
        auth_source=str(row.auth_source or "local"),
        avatar_url=None if row.avatar_url is None else str(row.avatar_url),
        last_seen_at=None if row.last_seen_at is None else float(row.last_seen_at),
    )


def _role(value: str) -> Role:
    for role in ROLES:
        if role == value:
            return role
    raise CollaborationError("invalid_role", f"unknown workspace role: {value}")


def _member(user: LocalUserRow, row: WorkspaceMemberRow) -> Member:
    return Member(user=_user(user), role=_role(str(row.role)), workspace_id=str(row.workspace_id))


def _invite(row: WorkspaceInviteRow) -> Invite:
    return Invite(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        email=None if row.email is None else str(row.email),
        role=_role(str(row.role)),
        expires_at=float(row.expires_at),
        accepted_at=None if row.accepted_at is None else float(row.accepted_at),
        created_by=str(row.created_by),
        created_at=float(row.created_at),
    )


def _capabilities_json(capabilities: frozenset[Capability]) -> str:
    return json.dumps([cap for cap in CAPABILITIES if cap in capabilities])


def invite_token_hash(raw_token: str) -> str:
    """The only form of an invite token the store keeps."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _channel(row: ChannelRow, my_role: ChannelRole | None = None) -> Channel:
    return Channel(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        user_id=str(row.user_id),
        title=str(row.title),
        state=str(row.state),
        revision=int(row.revision),
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
        deleted_at=None if row.deleted_at is None else float(row.deleted_at),
        visibility=str(row.visibility or "private"),
        created_by=None if row.created_by is None else str(row.created_by),
        silenced_until=None if row.silenced_until is None else float(row.silenced_until),
        my_role=my_role,
    )


def message_author(role: str, kind: str, agent_slug: str | None, owner_id: str | None) -> Author:
    """The author of a message stored without one: the rules the authorship
    migration backfilled with, for rows an older release wrote since."""
    if role == "user":
        return Author("human", owner_id)
    if kind in SYSTEM_MESSAGE_KINDS:
        return SYSTEM_AUTHOR
    return Author("agent", agent_slug or ANGIE_SLUG)


def _human_name(session: Any, user_id: str | None) -> str | None:
    if user_id is None:
        return None
    user = session.get(LocalUserRow, user_id)
    if user is None:
        return None
    return str(user.full_name or user.username)


def _author(session: Any, kind: str | None, author_id: str | None) -> Author | None:
    if kind == "human":
        return Author("human", author_id, _human_name(session, author_id))
    if kind == "agent":
        return Author("agent", author_id)
    if kind == "system":
        return Author("system", author_id)
    return None


def _owner_id(session: Any, channel_id: str) -> str | None:
    channel = session.get(ChannelRow, channel_id)
    return None if channel is None else str(channel.user_id)


def _member_in(session: Any, user_id: str) -> Member | None:
    row = session.execute(
        select(LocalUserRow, WorkspaceMemberRow)
        .join(WorkspaceMemberRow, WorkspaceMemberRow.user_id == LocalUserRow.id)
        .where(WorkspaceMemberRow.workspace_id == WORKSPACE_ID, LocalUserRow.id == user_id)
    ).first()
    return None if row is None else _member(row[0], row[1])


def _resolve(session: Any, viewer: Viewer) -> Member | None:
    """The member a viewer stands for. A user id that is no longer an
    active workspace member sees nothing: it never falls back to ``None``."""
    if viewer is None or isinstance(viewer, Member):
        return viewer
    member = _member_in(session, viewer)
    if member is None or not member.user.active:
        raise CollaborationError("channel_not_found", "channel not found")
    return member


def _access(
    session: Any,
    channel_id: str,
    viewer: Viewer,
    need: Need,
    *,
    now: float | None = None,
    include_deleted: bool = False,
) -> tuple[ChannelRow, Member | None]:
    """The channel, once ``viewer`` may ``need`` it; raises otherwise."""
    row: ChannelRow | None = session.get(ChannelRow, channel_id)
    if row is None or (not include_deleted and row.state != "active"):
        raise CollaborationError("channel_not_found", "channel not found")
    member = _resolve(session, viewer)
    ChannelAccess.check(session, row, member, need, now=now)
    return row, member


def _my_role(session: Any, channel_id: str, member: Member | None) -> ChannelRole | None:
    return None if member is None else ChannelAccess.role(session, channel_id, member.user.id)


def _other_owner(session: Any, channel_id: str, user_id: str) -> bool:
    """Whether the channel has an owner besides ``user_id``."""
    found = session.scalar(
        select(ChannelMemberRow.user_id)
        .where(
            ChannelMemberRow.channel_id == channel_id,
            ChannelMemberRow.user_id != user_id,
            ChannelMemberRow.role == "owner",
        )
        .limit(1)
    )
    return found is not None


def _channel_member(row: ChannelMemberRow, user: LocalUserRow) -> ChannelMember:
    return ChannelMember(
        channel_id=str(row.channel_id),
        user=_user(user),
        role="owner" if row.role == "owner" else "member",
        joined_at=float(row.joined_at),
        last_read_sequence=int(row.last_read_sequence or 0),
        added_by=None if row.added_by is None else str(row.added_by),
    )


def _participant(session: Any, row: ChannelParticipantRow) -> ChannelParticipant:
    added_by = _author(session, row.added_by_kind, row.added_by_id) or SYSTEM_AUTHOR
    return ChannelParticipant(
        channel_id=str(row.channel_id),
        agent_slug=str(row.agent_slug),
        mode="ambient" if row.mode == "ambient" else "mention",
        added_by=added_by,
        muted_until=None if row.muted_until is None else float(row.muted_until),
        created_at=float(row.created_at),
    )


def _participant_activity(
    session: Any, channel_id: str, agent_slug: str | None, status: str, now: float
) -> None:
    """An agent started (``thinking``) or stopped (``idle``) answering."""
    _event(
        session,
        "collaboration.participant.activity",
        now,
        data={"channel_id": channel_id, "agent_slug": agent_slug or ANGIE_SLUG, "status": status},
    )


def _message(session: Any, row: MessageRow) -> Message:
    agent_slug = None if row.agent_slug is None else str(row.agent_slug)
    work = json.loads(row.work_json) if row.work_json else None
    if row.kind == "work_result":
        # Runner results stored before attribution carry no author; they were Angie's.
        agent_slug = agent_slug or ANGIE_SLUG
        if isinstance(work, dict) and work.get("agent_slug") is None:
            work["agent_slug"] = ANGIE_SLUG
    author = _author(session, row.author_kind, row.author_id)
    if author is None:
        # Written by a release that recorded no author.
        derived = message_author(
            str(row.role), str(row.kind), agent_slug, _owner_id(session, str(row.channel_id))
        )
        author = _author(session, derived.kind, derived.id) or derived
    return Message(
        id=str(row.id),
        channel_id=str(row.channel_id),
        turn_id=None if row.turn_id is None else str(row.turn_id),
        client_message_id=(None if row.client_message_id is None else str(row.client_message_id)),
        sequence=int(row.sequence),
        role=str(row.role),
        kind=str(row.kind),
        content=str(row.content),
        agent_slug=agent_slug,
        created_at=float(row.created_at),
        work=work,
        reactions=tuple(str(value) for value in json.loads(row.reactions_json or "[]")),
        author=author,
    )


def _turn(session: Any, row: TurnRow) -> Turn:
    author = _author(session, row.author_kind, row.author_id) or _author(
        session, "human", _owner_id(session, str(row.channel_id))
    )
    return Turn(
        id=str(row.id),
        channel_id=str(row.channel_id),
        client_turn_id=None if row.client_turn_id is None else str(row.client_turn_id),
        input_message_id=str(row.input_message_id),
        status=str(row.status),
        targets=tuple(str(value) for value in json.loads(row.targets_json or "[]")),
        error=None if row.error is None else str(row.error),
        created_at=float(row.created_at),
        started_at=None if row.started_at is None else float(row.started_at),
        completed_at=None if row.completed_at is None else float(row.completed_at),
        intent=str(row.intent),
        participants=tuple(json.loads(row.participants_json)),
        author=author,
        trigger=str(row.trigger or "human"),
        parent_turn_id=None if row.parent_turn_id is None else str(row.parent_turn_id),
        source_message_id=None if row.source_message_id is None else str(row.source_message_id),
        chain_depth=int(row.chain_depth or 0),
    )


def _team(row: TeamRow) -> Team:
    return Team(
        id=str(row.id),
        user_id=str(row.user_id),
        name=str(row.name),
        slug=str(row.slug),
        description=None if row.description is None else str(row.description),
        goal=None if row.goal is None else str(row.goal),
        agent_slugs=tuple(str(value) for value in json.loads(row.agent_slugs_json or "[]")),
        enabled=bool(row.enabled),
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
    )


def _preference(row: PreferenceRow) -> Preference:
    return Preference(
        id=str(row.id),
        user_id=str(row.user_id),
        name=str(row.name),
        content=str(row.content),
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
    )


def _workflow(row: WorkflowRow) -> Workflow:
    return Workflow(
        id=str(row.id),
        user_id=str(row.user_id),
        name=str(row.name),
        slug=str(row.slug),
        description=None if row.description is None else str(row.description),
        trigger_event=None if row.trigger_event is None else str(row.trigger_event),
        enabled=bool(row.enabled),
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
    )


def _event(
    session: Any,
    type_: str,
    now: float,
    *,
    actor: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    session.execute(
        insert(ApiEventRow).values(
            recorded_at=now,
            occurred_at=now,
            type=type_,
            run_id=None,
            item_id=None,
            operation_id=None,
            actor_json=None if actor is None else json.dumps(actor, default=str),
            source_seq=None,
            data_json=json.dumps(data or {}, default=str),
        )
    )


class CollaborationStore:
    def __init__(self, dstore: DaemonStore) -> None:
        self.dstore = dstore

    # -- local profile -------------------------------------------------------------

    def register_user(
        self,
        *,
        username: str,
        email: str,
        password: str,
        full_name: str | None,
        timezone: str,
        now: float,
        invite_token: str | None = None,
    ) -> LocalUser:
        """Create a local user, its API principal and its membership.

        The installation's first user owns the workspace and holds every
        capability. Every later user needs an unexpired, unspent invite and
        joins with the invite's role and exactly that role's capabilities.
        """
        username = username.strip()
        email = email.strip().casefold()
        if not username or not email:
            raise CollaborationError("invalid_profile", "username and email are required")
        if len(password) < 8:
            raise CollaborationError("weak_password", "password must contain at least 8 characters")
        user_id = "usr_" + _token(12)
        client_id = "local_" + _token(12)
        try:
            with self.dstore.transaction() as session:
                invite: WorkspaceInviteRow | None = None
                role: Role = "owner"
                capabilities_json = json.dumps(list(CAPABILITIES))
                if session.scalar(select(func.count()).select_from(LocalUserRow)):
                    if invite_token is None:
                        raise CollaborationError(
                            "local_user_exists", "this installation already has a local user"
                        )
                    invite = self._open_invite(session, invite_token, now, email=email)
                    role = _role(str(invite.role))
                    capabilities_json = _capabilities_json(ROLE_CAPABILITIES[role])
                session.execute(
                    insert(ClientRow).values(
                        id=client_id,
                        name=username,
                        secret_hash=hash_secret(password),
                        capabilities_json=capabilities_json,
                        created_at=now,
                        created_by="local-onboarding" if invite is None else "workspace-invite",
                    )
                )
                session.execute(
                    insert(LocalUserRow).values(
                        id=user_id,
                        client_id=client_id,
                        username=username,
                        email=email,
                        full_name=full_name.strip() if full_name else None,
                        timezone=timezone.strip() or "UTC",
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.add(
                    WorkspaceMemberRow(
                        workspace_id=WORKSPACE_ID,
                        user_id=user_id,
                        role=role,
                        created_at=now,
                        invited_by=None if invite is None else invite.created_by,
                    )
                )
                if invite is not None:
                    invite.accepted_at = now
                session.flush()
                row = session.get(LocalUserRow, user_id)
                assert row is not None  # nosec B101 - inserted in this transaction
                _event(
                    session,
                    "collaboration.user.created",
                    now,
                    actor={"kind": "client", "id": client_id, "display": username, "via": "api"},
                    data={"user_id": user_id},
                )
                if invite is not None:
                    _event(
                        session,
                        "workspace.invite.accepted",
                        now,
                        data={"invite_id": invite.id, "user_id": user_id, "role": role},
                    )
                return _user(row)
        except IntegrityError as exc:
            raise CollaborationError(
                "profile_conflict", "username or email is already in use"
            ) from exc

    # -- provider sign-in ----------------------------------------------------------

    @staticmethod
    def _free_username(session: Any, hint: str) -> str:
        base = _USERNAME_UNSAFE.sub("-", hint.strip()).strip("-.")[:64] or "user"
        taken = set(
            session.scalars(
                select(LocalUserRow.username).where(
                    LocalUserRow.username.startswith(base, autoescape=True)
                )
            ).all()
        )
        if base not in taken:
            return base
        suffix = 2
        while f"{base}{suffix}" in taken:
            suffix += 1
        return f"{base}{suffix}"

    def sign_in_external(
        self,
        *,
        issuer: str,
        subject: str,
        username: str | None,
        email: str | None,
        email_verified: bool,
        full_name: str | None,
        role_from_groups: Role | None,
        default_role: Role,
        auto_provision: bool,
        link_verified_email: bool = False,
        now: float,
    ) -> LocalUser:
        """The local account behind a provider identity, created or linked
        on first sign-in.

        An account already bound to ``(issuer, subject)`` is used as is,
        with its email and name refreshed. Otherwise, with
        ``link_verified_email``, an unlinked local account whose email
        matches is linked, but only when the provider has verified the
        email. Failing both, a new account is provisioned when
        ``auto_provision`` allows: the installation's first user owns the
        workspace, anyone else takes ``role_from_groups`` or
        ``default_role``. An email another account already holds is not
        given to the new account, which gets an undeliverable one instead.
        For an existing member, ``role_from_groups`` (when not ``None``)
        replaces the role, except that the last owner is never demoted. An
        inactive user, or one no longer in the workspace, is refused.
        """
        email = email.strip().casefold() if email and email.strip() else None
        try:
            return self._sign_in_external(
                issuer=issuer,
                subject=subject,
                username=username,
                email=email,
                email_verified=email_verified,
                full_name=full_name,
                role_from_groups=role_from_groups,
                default_role=default_role,
                auto_provision=auto_provision,
                link_verified_email=link_verified_email,
                now=now,
            )
        except IntegrityError as exc:
            # A concurrent first sign-in of the same person, or a username
            # taken between the check and the insert; the retry finds it.
            raise CollaborationError(
                "oidc_account_conflict", "the account changed during sign-in; try again"
            ) from exc

    def _sign_in_external(
        self,
        *,
        issuer: str,
        subject: str,
        username: str | None,
        email: str | None,
        email_verified: bool,
        full_name: str | None,
        role_from_groups: Role | None,
        default_role: Role,
        auto_provision: bool,
        link_verified_email: bool = False,
        now: float,
    ) -> LocalUser:
        with self.dstore.transaction() as session:
            row: LocalUserRow | None = session.scalars(
                select(LocalUserRow).where(
                    LocalUserRow.oidc_issuer == issuer, LocalUserRow.oidc_subject == subject
                )
            ).first()
            created = False
            new_email = email
            if row is None and email is not None:
                holder: LocalUserRow | None = session.scalars(
                    select(LocalUserRow).where(LocalUserRow.email == email)
                ).first()
                if (
                    holder is not None
                    and link_verified_email
                    and email_verified
                    and holder.oidc_subject is None
                ):
                    # The password keeps working, so the account stays
                    # ``local``; the provider identity is recorded beside it.
                    holder.oidc_issuer = issuer
                    holder.oidc_subject = subject
                    holder.updated_at = now
                    row = holder
                    _event(session, "auth.oidc.linked", now, data={"user_id": holder.id})
                elif holder is not None:
                    # Not linkable: the person gets an account of their own,
                    # and the address stays with the account that holds it.
                    new_email = None
            if row is None:
                if not auto_provision:
                    raise CollaborationError(
                        "oidc_not_provisioned", "no account exists for this sign-in"
                    )
                row = self._provision(
                    session,
                    issuer=issuer,
                    subject=subject,
                    username=username or (email.split("@", 1)[0] if email else None) or "user",
                    email=new_email,
                    full_name=full_name,
                    role=role_from_groups or default_role,
                    now=now,
                )
                created = True
            member = self._member_row(session, str(row.id))
            if not row.active or member is None:
                raise CollaborationError("oidc_account_disabled", "this account is disabled")
            if not created:
                self._refresh_identity(session, row, email, full_name, now)
                if role_from_groups is not None and role_from_groups != member.role:
                    if member.role == "owner" and self._owner_count(session) <= 1:
                        log.info("auth.oidc_last_owner_kept", user_id=row.id)
                    else:
                        member.role = role_from_groups
                        self._grant_role(session, row, role_from_groups)
                        _event(
                            session,
                            "workspace.member.role_changed",
                            now,
                            data={"user_id": row.id, "role": role_from_groups},
                        )
            _event(session, "auth.oidc.login", now, data={"user_id": row.id})
            session.flush()
            return _user(row)

    @staticmethod
    def _refresh_identity(
        session: Any, row: LocalUserRow, email: str | None, full_name: str | None, now: float
    ) -> None:
        changed = False
        if email is not None and email != row.email:
            clash = session.scalars(
                select(LocalUserRow.id).where(
                    LocalUserRow.email == email, LocalUserRow.id != row.id
                )
            ).first()
            if clash is None:
                row.email = email
                changed = True
            else:
                log.info("auth.oidc_email_kept", user_id=row.id)
        name = full_name.strip() if full_name else None
        if name and name != row.full_name:
            row.full_name = name
            changed = True
        if changed:
            row.updated_at = now

    def _provision(
        self,
        session: Any,
        *,
        issuer: str,
        subject: str,
        username: str,
        email: str | None,
        full_name: str | None,
        role: Role,
        now: float,
    ) -> LocalUserRow:
        if not session.scalar(select(func.count()).select_from(LocalUserRow)):
            role = "owner"  # the installation's first user owns it
        user_id = "usr_" + _token(12)
        client_id = "local_" + _token(12)
        name = self._free_username(session, username)
        if email is None:
            # The column is required and unique; a provider that shares no
            # address gets one that can never receive mail.
            digest = hashlib.sha256(f"{issuer}\n{subject}".encode()).hexdigest()[:24]
            email = f"oidc-{digest}@users.invalid"
        session.execute(
            insert(ClientRow).values(
                id=client_id,
                name=name,
                # Nobody knows this secret: the account signs in through its
                # provider, never with client credentials.
                secret_hash=hash_secret(_token(32)),
                capabilities_json=_capabilities_json(ROLE_CAPABILITIES[role]),
                created_at=now,
                created_by="oidc-sign-in",
            )
        )
        session.execute(
            insert(LocalUserRow).values(
                id=user_id,
                client_id=client_id,
                username=name,
                email=email,
                full_name=full_name.strip() if full_name else None,
                timezone="UTC",
                created_at=now,
                updated_at=now,
                auth_source="oidc",
                oidc_issuer=issuer,
                oidc_subject=subject,
            )
        )
        session.add(
            WorkspaceMemberRow(
                workspace_id=WORKSPACE_ID,
                user_id=user_id,
                role=role,
                created_at=now,
                invited_by=None,
            )
        )
        session.flush()
        _event(
            session,
            "collaboration.user.created",
            now,
            actor={"kind": "client", "id": client_id, "display": name, "via": "api"},
            data={"user_id": user_id},
        )
        row: LocalUserRow | None = session.get(LocalUserRow, user_id)
        assert row is not None  # nosec B101 - inserted in this transaction
        return row

    def user_by_username(self, username: str) -> LocalUser | None:
        with self.dstore.read() as session:
            row = session.scalars(
                select(LocalUserRow).where(LocalUserRow.username == username.strip())
            ).first()
            return None if row is None else _user(row)

    def user_by_client(self, client_id: str) -> LocalUser | None:
        with self.dstore.read() as session:
            row = session.scalars(
                select(LocalUserRow).where(LocalUserRow.client_id == client_id)
            ).first()
            return None if row is None else _user(row)

    def update_user(
        self,
        client_id: str,
        *,
        email: str | None,
        full_name: str | None,
        timezone: str | None,
        now: float,
    ) -> LocalUser:
        try:
            with self.dstore.transaction() as session:
                row = session.scalars(
                    select(LocalUserRow).where(LocalUserRow.client_id == client_id)
                ).first()
                if row is None or not row.active:
                    raise CollaborationError("profile_not_found", "local profile not found")
                if email is not None:
                    row.email = email.strip().casefold()
                if full_name is not None:
                    row.full_name = full_name.strip() or None
                if timezone is not None:
                    row.timezone = timezone.strip() or "UTC"
                row.updated_at = now
                session.flush()
                _event(session, "collaboration.user.updated", now, data={"user_id": row.id})
                return _user(row)
        except IntegrityError as exc:
            raise CollaborationError("profile_conflict", "email is already in use") from exc

    # -- workspace membership ------------------------------------------------------

    @staticmethod
    def _open_invite(
        session: Any, raw_token: str, now: float, *, email: str | None
    ) -> WorkspaceInviteRow:
        """The unspent, unexpired invite behind ``raw_token``. An invite
        addressed to an email admits only that address, compared without
        regard to case."""
        row: WorkspaceInviteRow | None = session.scalars(
            select(WorkspaceInviteRow).where(
                WorkspaceInviteRow.token_hash == invite_token_hash(raw_token),
                WorkspaceInviteRow.workspace_id == WORKSPACE_ID,
            )
        ).first()
        if row is None or row.accepted_at is not None:
            raise CollaborationError("invite_invalid", "the invite is unknown or already used")
        if float(row.expires_at) <= now:
            raise CollaborationError("invite_expired", "the invite has expired")
        if row.email is not None and (email or "").strip().casefold() != str(row.email).casefold():
            raise CollaborationError(
                "invite_email_mismatch", "the invite is addressed to another email"
            )
        return row

    @staticmethod
    def _grant_role(session: Any, user: LocalUserRow, role: Role | None) -> None:
        """Rewrite the user's API client to hold what ``role`` grants
        (nothing, for ``None``). Tokens are narrowed by the client's current
        capabilities on every request, so the change applies at once."""
        client = session.get(ClientRow, user.client_id)
        if client is not None:
            client.capabilities_json = _capabilities_json(
                frozenset() if role is None else ROLE_CAPABILITIES[role]
            )

    @staticmethod
    def _revoke_refresh(session: Any, client_id: str, now: float) -> None:
        """Revoke every live refresh token of ``client_id`` inside the
        caller's transaction. The API auth store keeps its tokens in this
        same daemon database, so the revocation commits or rolls back with
        the membership change that asked for it."""
        session.execute(
            update(RefreshTokenRow)
            .where(RefreshTokenRow.client_id == client_id, RefreshTokenRow.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    @staticmethod
    def _owner_count(session: Any, *, besides: str | None = None) -> int:
        """How many active owners the workspace has, ``besides`` one user
        when named."""
        conditions = [
            WorkspaceMemberRow.workspace_id == WORKSPACE_ID,
            WorkspaceMemberRow.role == "owner",
            LocalUserRow.active != 0,
        ]
        if besides is not None:
            conditions.append(WorkspaceMemberRow.user_id != besides)
        count = session.scalar(
            select(func.count())
            .select_from(WorkspaceMemberRow)
            .join(LocalUserRow, LocalUserRow.id == WorkspaceMemberRow.user_id)
            .where(*conditions)
        )
        return int(count or 0)

    @staticmethod
    def _member_row(session: Any, user_id: str) -> WorkspaceMemberRow | None:
        row: WorkspaceMemberRow | None = session.get(WorkspaceMemberRow, (WORKSPACE_ID, user_id))
        return row

    @staticmethod
    def _join(session: Any, *conditions: Any) -> list[Member]:
        rows = session.execute(
            select(LocalUserRow, WorkspaceMemberRow)
            .join(WorkspaceMemberRow, WorkspaceMemberRow.user_id == LocalUserRow.id)
            .where(WorkspaceMemberRow.workspace_id == WORKSPACE_ID, *conditions)
            .order_by(WorkspaceMemberRow.created_at, LocalUserRow.id)
        ).all()
        return [_member(user, member) for user, member in rows]

    def _insert_member(
        self,
        session: Any,
        user_id: str,
        role: Role,
        invited_by: str | None,
        now: float,
    ) -> Member:
        user = session.get(LocalUserRow, user_id)
        if user is None:
            raise CollaborationError("user_not_found", "user not found")
        if self._member_row(session, user_id) is not None:
            raise CollaborationError("already_member", "the user is already a member")
        row = WorkspaceMemberRow(
            workspace_id=WORKSPACE_ID,
            user_id=user_id,
            role=role,
            created_at=now,
            invited_by=invited_by,
        )
        session.add(row)
        self._grant_role(session, user, role)
        session.flush()
        _event(
            session,
            "workspace.member.added",
            now,
            data={"user_id": user_id, "role": role},
        )
        return _member(user, row)

    def add_member(self, user_id: str, role: Role, invited_by: str | None, now: float) -> Member:
        """Make an existing user a member with ``role``."""
        role = _role(role)
        with self.dstore.transaction() as session:
            return self._insert_member(session, user_id, role, invited_by, now)

    def set_role(self, user_id: str, role: Role) -> Member:
        """Change a member's role. The workspace always keeps an owner."""
        role = _role(role)
        with self.dstore.transaction() as session:
            row = self._member_row(session, user_id)
            user = session.get(LocalUserRow, user_id)
            if row is None or user is None:
                raise CollaborationError("member_not_found", "member not found")
            if (
                row.role == "owner"
                and role != "owner"
                and not self._owner_count(session, besides=user_id)
            ):
                raise CollaborationError("last_owner", "the workspace must keep an owner")
            row.role = role
            self._grant_role(session, user, role if user.active else None)
            session.flush()
            return _member(user, row)

    def update_member(
        self,
        user_id: str,
        *,
        role: Role | None = None,
        active: bool | None = None,
        owner_ok: bool = True,
        actor: dict[str, Any] | None = None,
        now: float,
    ) -> Member:
        """Change a member's role, standing or both, in one step.

        A deactivated user's API client holds nothing until the user is
        reactivated, when it holds the role's capabilities again. The
        workspace always keeps an active owner. Without ``owner_ok`` the
        change may neither touch an owner nor grant the owner role
        (``owner_required``).
        """
        role = None if role is None else _role(role)
        with self.dstore.transaction() as session:
            row = self._member_row(session, user_id)
            user = session.get(LocalUserRow, user_id)
            if row is None or user is None:
                raise CollaborationError("user_not_found", "user not found")
            if not owner_ok and (row.role == "owner" or role == "owner"):
                raise CollaborationError("owner_required", "only an owner may do this to an owner")
            losing_owner = row.role == "owner" and (
                (role is not None and role != "owner") or active is False
            )
            if losing_owner and not self._owner_count(session, besides=user_id):
                raise CollaborationError("last_owner", "the workspace must keep an owner")
            data: dict[str, Any] = {"user_id": user_id}
            if role is not None:
                row.role = role
                data["role"] = role
            if active is not None:
                user.active = 1 if active else 0
                data["is_active"] = active
            user.updated_at = now
            current = _role(str(row.role))
            self._grant_role(session, user, current if user.active else None)
            if not user.active:
                self._revoke_refresh(session, str(user.client_id), now)
            session.flush()
            _event(session, "workspace.member.updated", now, actor=actor, data=data)
            return _member(user, row)

    def remove_member(
        self,
        user_id: str,
        *,
        owner_ok: bool = True,
        actor: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> bool:
        """End a membership; the user's API client keeps no capability.
        Without ``owner_ok`` an owner cannot be removed (``owner_required``)."""
        with self.dstore.transaction() as session:
            row = self._member_row(session, user_id)
            if row is None:
                return False
            if not owner_ok and row.role == "owner":
                raise CollaborationError("owner_required", "only an owner may remove an owner")
            if row.role == "owner" and not self._owner_count(session, besides=user_id):
                raise CollaborationError("last_owner", "the workspace must keep an owner")
            role = str(row.role)
            session.delete(row)
            user = session.get(LocalUserRow, user_id)
            if user is not None:
                self._grant_role(session, user, None)
                if now is not None:
                    self._revoke_refresh(session, str(user.client_id), now)
            if now is not None:
                _event(
                    session,
                    "workspace.member.removed",
                    now,
                    actor=actor,
                    data={"user_id": user_id, "role": role},
                )
            return True

    def member_for_user(self, user_id: str) -> Member | None:
        with self.dstore.read() as session:
            found = self._join(session, LocalUserRow.id == user_id)
            return found[0] if found else None

    def member_for_client(self, client_id: str) -> Member | None:
        with self.dstore.read() as session:
            found = self._join(session, LocalUserRow.client_id == client_id)
            return found[0] if found else None

    def list_members(self) -> list[Member]:
        with self.dstore.read() as session:
            return self._join(session)

    def touch_last_seen(self, user_id: str, now: float, min_interval_s: float) -> None:
        """Record that the user was seen, unless that was already recorded
        within ``min_interval_s``."""
        with self.dstore.transaction() as session:
            session.execute(
                update(LocalUserRow)
                .where(
                    LocalUserRow.id == user_id,
                    or_(
                        LocalUserRow.last_seen_at.is_(None),
                        LocalUserRow.last_seen_at <= now - min_interval_s,
                    ),
                )
                .values(last_seen_at=now)
            )

    def create_invite(
        self,
        role: Role,
        email: str | None,
        created_by: str,
        ttl_s: float,
        now: float,
        actor: dict[str, Any] | None = None,
    ) -> tuple[Invite, str]:
        """A new invite and its raw token. The token is returned only here;
        the store keeps its SHA-256."""
        role = _role(role)
        if ttl_s <= 0:
            raise CollaborationError("invalid_invite", "an invite must expire in the future")
        raw = "inv_" + _token(32)
        row = WorkspaceInviteRow(
            id="winv_" + _token(12),
            workspace_id=WORKSPACE_ID,
            email=email.strip().casefold() if email and email.strip() else None,
            role=role,
            token_hash=invite_token_hash(raw),
            expires_at=now + ttl_s,
            accepted_at=None,
            created_by=created_by,
            created_at=now,
        )
        with self.dstore.transaction() as session:
            session.add(row)
            session.flush()
            _event(
                session,
                "workspace.invite.created",
                now,
                actor=actor,
                data={"invite_id": row.id, "role": role, "created_by": created_by},
            )
            return _invite(row), raw

    def accept_invite(self, raw_token: str, user_id: str, now: float) -> Member:
        """Spend an invite on an existing user who is not yet a member."""
        with self.dstore.transaction() as session:
            user = session.get(LocalUserRow, user_id)
            if user is None:
                raise CollaborationError("user_not_found", "user not found")
            invite = self._open_invite(session, raw_token, now, email=str(user.email))
            member = self._insert_member(
                session, user_id, _role(str(invite.role)), str(invite.created_by), now
            )
            invite.accepted_at = now
            _event(
                session,
                "workspace.invite.accepted",
                now,
                data={"invite_id": invite.id, "user_id": user_id, "role": member.role},
            )
            return member

    def list_invites(self) -> list[Invite]:
        """Every invite still on record, newest first."""
        with self.dstore.read() as session:
            rows = session.scalars(
                select(WorkspaceInviteRow)
                .where(WorkspaceInviteRow.workspace_id == WORKSPACE_ID)
                .order_by(WorkspaceInviteRow.created_at.desc(), WorkspaceInviteRow.id)
            ).all()
            return [_invite(row) for row in rows]

    def revoke_invite(
        self, invite_id: str, *, actor: dict[str, Any] | None = None, now: float
    ) -> bool:
        """Withdraw an unspent invite so its token admits nobody.
        ``False`` when there is no such invite; a spent one is kept."""
        with self.dstore.transaction() as session:
            row: WorkspaceInviteRow | None = session.scalars(
                select(WorkspaceInviteRow).where(
                    WorkspaceInviteRow.id == invite_id,
                    WorkspaceInviteRow.workspace_id == WORKSPACE_ID,
                )
            ).first()
            if row is None:
                return False
            if row.accepted_at is not None:
                raise CollaborationError("invite_accepted", "the invite was already used")
            role = str(row.role)
            session.delete(row)
            _event(
                session,
                "workspace.invite.revoked",
                now,
                actor=actor,
                data={"invite_id": invite_id, "role": role},
            )
            return True

    # -- channels ------------------------------------------------------------------

    def create_channel(self, user_id: str, title: str, now: float) -> Channel:
        channel_id = "chn_" + _token(16)
        clean_title = title.strip() or "New conversation"
        with self.dstore.transaction() as session:
            session.execute(
                insert(ChannelRow).values(
                    id=channel_id,
                    workspace_id=WORKSPACE_ID,
                    user_id=user_id,
                    title=clean_title[:200],
                    state="active",
                    revision=1,
                    created_at=now,
                    updated_at=now,
                    visibility="private",
                    created_by=user_id,
                )
            )
            session.execute(
                insert(ChannelMemberRow).values(
                    channel_id=channel_id,
                    user_id=user_id,
                    role="owner",
                    added_by=user_id,
                    joined_at=now,
                )
            )
            row = session.get(ChannelRow, channel_id)
            assert row is not None  # nosec B101
            _event(session, "collaboration.channel.created", now, data={"channel_id": channel_id})
            return _channel(row)

    def get_channel(
        self, viewer: Viewer, channel_id: str, *, include_deleted: bool = False
    ) -> Channel | None:
        with self.dstore.read() as session:
            try:
                row, member = _access(
                    session, channel_id, viewer, "read", include_deleted=include_deleted
                )
            except CollaborationError:
                return None
            return _channel(row, _my_role(session, channel_id, member))

    def list_channels(
        self, viewer: Viewer, *, limit: int, offset: int
    ) -> tuple[list[Channel], int]:
        """The active channels ``viewer`` can see: those they belong to and
        every workspace channel, newest activity first."""
        with self.dstore.read() as session:
            try:
                member = _resolve(session, viewer)
            except CollaborationError:
                return [], 0
            conditions: list[Any] = [ChannelRow.state == "active"]
            visible = ChannelAccess.visible_condition(member)
            if visible is not None:
                conditions.append(visible)
            total = int(
                session.scalar(select(func.count()).select_from(ChannelRow).where(*conditions)) or 0
            )
            viewer_id = None if member is None else member.user.id
            rows = session.execute(
                select(ChannelRow, ChannelMemberRow.role)
                .outerjoin(
                    ChannelMemberRow,
                    (ChannelMemberRow.channel_id == ChannelRow.id)
                    & (ChannelMemberRow.user_id == viewer_id),
                )
                .where(*conditions)
                .order_by(ChannelRow.updated_at.desc(), ChannelRow.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            channels = [
                _channel(row, None if role is None else ("owner" if role == "owner" else "member"))
                for row, role in rows
            ]
            return channels, total

    def update_channel(
        self,
        viewer: Viewer,
        channel_id: str,
        title: str | None,
        now: float,
        *,
        visibility: str | None = None,
    ) -> Channel | None:
        """Rename a channel or change who can see it. ``None`` when the
        viewer cannot see it; ``channel_forbidden`` when they may not manage it."""
        if visibility is not None and visibility not in {"private", "workspace"}:
            raise CollaborationError("invalid_visibility", "visibility is private or workspace")
        with self.dstore.transaction() as session:
            try:
                row, member = _access(session, channel_id, viewer, "manage", now=now)
            except CollaborationError as exc:
                if exc.code == "channel_not_found":
                    return None
                raise
            if title is not None:
                row.title = title.strip()[:200] or row.title
            if visibility is not None:
                row.visibility = visibility
            row.updated_at = now
            row.revision += 1
            session.flush()
            _event(session, "collaboration.channel.updated", now, data={"channel_id": channel_id})
            return _channel(row, _my_role(session, channel_id, member))

    def delete_channel(self, viewer: Viewer, channel_id: str, now: float) -> bool:
        """Tombstone a channel so a late agent result cannot recreate it.
        ``False`` when the viewer cannot see it; ``channel_forbidden`` when
        they may not manage it."""
        with self.dstore.transaction() as session:
            try:
                row, _ = _access(session, channel_id, viewer, "manage", now=now)
            except CollaborationError as exc:
                if exc.code == "channel_not_found":
                    return False
                raise
            row.state = "deleted"
            row.deleted_at = now
            row.updated_at = now
            row.revision += 1
            _event(session, "collaboration.channel.deleted", now, data={"channel_id": channel_id})
            return True

    # -- channel members -----------------------------------------------------------

    def list_channel_members(self, viewer: Viewer, channel_id: str) -> list[ChannelMember]:
        with self.dstore.read() as session:
            _access(session, channel_id, viewer, "read")
            rows = session.execute(
                select(ChannelMemberRow, LocalUserRow)
                .join(LocalUserRow, LocalUserRow.id == ChannelMemberRow.user_id)
                .where(ChannelMemberRow.channel_id == channel_id)
                .order_by(ChannelMemberRow.joined_at, ChannelMemberRow.user_id)
            ).all()
            return [_channel_member(row, user) for row, user in rows]

    def add_channel_member(
        self,
        viewer: Viewer,
        channel_id: str,
        user_id: str,
        role: ChannelRole,
        now: float,
        *,
        change_role: bool = False,
    ) -> tuple[ChannelMember, bool]:
        """Add a workspace member to the channel (manage permission), and
        whether they were added. With ``change_role``, a current member
        whose role differs gets ``role`` instead; the last owner cannot step
        down. Anything else about a current member is
        ``already_channel_member``."""
        with self.dstore.transaction() as session:
            _, member = _access(session, channel_id, viewer, "manage", now=now)
            target = _member_in(session, user_id)
            if target is None or not target.user.active:
                raise CollaborationError("user_not_found", "no such workspace member")
            existing = session.get(ChannelMemberRow, (channel_id, user_id))
            if existing is not None:
                if not change_role or existing.role == role:
                    raise CollaborationError(
                        "already_channel_member", "the user is already in this channel"
                    )
                if existing.role == "owner" and not _other_owner(session, channel_id, user_id):
                    raise CollaborationError(
                        "last_channel_owner",
                        "make someone else an owner before the last owner steps down",
                    )
                existing.role = role
                session.flush()
                _event(
                    session,
                    "collaboration.member.updated",
                    now,
                    data={"channel_id": channel_id, "user_id": user_id},
                )
                user = session.get(LocalUserRow, user_id)
                assert user is not None  # nosec B101 - an active workspace member
                return _channel_member(existing, user), False
            ChannelAccess.join(
                session,
                channel_id,
                user_id,
                None if member is None else member.user.id,
                now,
                role=role,
            )
            row = session.get(ChannelMemberRow, (channel_id, user_id))
            user = session.get(LocalUserRow, user_id)
            assert row is not None and user is not None  # nosec B101 - inserted above
            return _channel_member(row, user), True

    def remove_channel_member(
        self, viewer: Viewer, channel_id: str, user_id: str, now: float
    ) -> None:
        """Remove someone from a channel. Removing oneself (leaving) needs
        only to see the channel; removing anyone else needs manage. The last
        owner cannot go while anyone else remains."""
        with self.dstore.transaction() as session:
            _, member = _access(session, channel_id, viewer, "read")
            if member is not None and member.user.id != user_id:
                _access(session, channel_id, member, "manage")
            row = session.get(ChannelMemberRow, (channel_id, user_id))
            if row is None:
                raise CollaborationError(
                    "channel_member_not_found", "the user is not in this channel"
                )
            if row.role == "owner":
                others = list(
                    session.scalars(
                        select(ChannelMemberRow.role).where(
                            ChannelMemberRow.channel_id == channel_id,
                            ChannelMemberRow.user_id != user_id,
                        )
                    )
                )
                if others and "owner" not in others:
                    raise CollaborationError(
                        "last_channel_owner",
                        "make someone else an owner before the last owner leaves",
                    )
            session.delete(row)
            _event(
                session,
                "collaboration.member.removed",
                now,
                data={"channel_id": channel_id, "user_id": user_id},
            )

    # -- channel participants ------------------------------------------------------

    def list_participants(self, viewer: Viewer, channel_id: str) -> list[ChannelParticipant]:
        with self.dstore.read() as session:
            _access(session, channel_id, viewer, "read")
            rows = session.scalars(
                select(ChannelParticipantRow)
                .where(ChannelParticipantRow.channel_id == channel_id)
                .order_by(ChannelParticipantRow.created_at, ChannelParticipantRow.agent_slug)
            )
            return [_participant(session, row) for row in rows]

    def put_participant(
        self,
        viewer: Viewer,
        channel_id: str,
        agent_slug: str,
        values: dict[str, Any],
        now: float,
    ) -> ChannelParticipant:
        """Add an agent to the channel or change how it takes part. ``values``
        holds only the fields the caller set (``mode``, ``muted_until``)."""
        mode = values.get("mode")
        if mode is not None and mode not in {"mention", "ambient"}:
            raise CollaborationError("invalid_participant", "mode is mention or ambient")
        with self.dstore.transaction() as session:
            _, member = _access(session, channel_id, viewer, "post", now=now)
            row = session.get(ChannelParticipantRow, (channel_id, agent_slug))
            if row is None:
                row = ChannelParticipantRow(
                    channel_id=channel_id,
                    agent_slug=agent_slug,
                    mode=mode or "mention",
                    added_by_kind=None if member is None else "human",
                    added_by_id=None if member is None else member.user.id,
                    muted_until=values.get("muted_until"),
                    created_at=now,
                )
                session.add(row)
                change = "added"
            else:
                if mode is not None:
                    row.mode = mode
                if "muted_until" in values:
                    row.muted_until = values["muted_until"]
                change = "updated"
            session.flush()
            _event(
                session,
                f"collaboration.participant.{change}",
                now,
                data={"channel_id": channel_id, "agent_slug": agent_slug},
            )
            return _participant(session, row)

    def remove_participant(
        self, viewer: Viewer, channel_id: str, agent_slug: str, now: float
    ) -> None:
        with self.dstore.transaction() as session:
            _access(session, channel_id, viewer, "post", now=now)
            row = session.get(ChannelParticipantRow, (channel_id, agent_slug))
            if row is None:
                raise CollaborationError(
                    "participant_not_found", "the agent is not in this channel"
                )
            session.delete(row)
            _event(
                session,
                "collaboration.participant.removed",
                now,
                data={"channel_id": channel_id, "agent_slug": agent_slug},
            )

    def thinking_agents(self, channel_id: str) -> set[str]:
        """The agents answering a running turn in the channel right now."""
        with self.dstore.read() as session:
            rows = session.scalars(
                select(TurnRow.participants_json).where(
                    TurnRow.channel_id == channel_id,
                    TurnRow.status.in_(("running", "cancelling")),
                )
            )
            return {
                str(participant.get("agent_slug") or ANGIE_SLUG)
                for value in rows
                for participant in json.loads(value or "[]")
                if participant.get("status") == "running"
            }

    # -- messages and turns --------------------------------------------------------

    @staticmethod
    def _next_sequence(session: Any, channel_id: str) -> int:
        newest = session.scalar(
            select(func.max(MessageRow.sequence)).where(MessageRow.channel_id == channel_id)
        )
        return int(newest or 0) + 1

    @staticmethod
    def _cancel_deleted_channel_turn(session: Any, turn: TurnRow, now: float) -> None:
        """Settle a turn whose channel was tombstoned during dispatch."""
        reason = "The channel was deleted before this turn finished."
        turn.status = "cancelled"
        turn.error = reason
        turn.completed_at = now
        progress = json.loads(turn.participants_json)
        for participant in progress:
            if participant["status"] in {"queued", "running"}:
                participant["status"] = "cancelled"
        turn.participants_json = json.dumps(progress)
        input_message = session.get(MessageRow, turn.input_message_id)
        if input_message is not None:
            reactions = [
                str(value)
                for value in json.loads(input_message.reactions_json or "[]")
                if value != "⏳"
            ]
            if "⚠" not in reactions:
                reactions.append("⚠")
            input_message.reactions_json = json.dumps(reactions, ensure_ascii=False)
        _event(
            session,
            "collaboration.turn.cancelled",
            now,
            data={"channel_id": turn.channel_id, "turn_id": turn.id, "error": reason},
        )

    def list_messages(
        self, viewer: Viewer, channel_id: str, *, after: int = 0
    ) -> list[Message] | None:
        with self.dstore.read() as session:
            try:
                _access(session, channel_id, viewer, "read")
            except CollaborationError:
                return None
            rows = session.scalars(
                select(MessageRow)
                .where(MessageRow.channel_id == channel_id, MessageRow.sequence > after)
                .order_by(MessageRow.sequence.asc())
            )
            return [_message(session, row) for row in rows]

    def accept_turn(
        self,
        user_id: str,
        channel_id: str,
        *,
        content: str,
        targets: tuple[str, ...],
        client_turn_id: str | None,
        client_message_id: str | None,
        actor: dict[str, Any] | None,
        now: float,
        intent: str = "conversation",
        participants: tuple[str, ...] = (),
    ) -> tuple[Turn, Message, bool]:
        """Append the user message and accepted turn atomically.

        A repeated client turn id returns the original resources. A reused id
        with different text is rejected instead of silently changing meaning.
        ``participants`` are the agents the message mentions: each one not in
        the channel yet joins it, answering when mentioned.
        """
        with self.dstore.immediate_transaction() as session:
            channel, _ = _access(session, channel_id, user_id, "post", now=now)
            if client_turn_id:
                existing = session.scalars(
                    select(TurnRow).where(
                        TurnRow.channel_id == channel_id,
                        TurnRow.client_turn_id == client_turn_id,
                    )
                ).first()
                if existing is not None:
                    message = session.get(MessageRow, existing.input_message_id)
                    assert message is not None  # nosec B101 - turn invariant
                    if (
                        message.content != content
                        or tuple(json.loads(existing.targets_json)) != targets
                        or existing.intent != intent
                        or message.client_message_id != client_message_id
                    ):
                        raise CollaborationError(
                            "idempotency_conflict",
                            "client_turn_id was already used with a different request",
                        )
                    return _turn(session, existing), _message(session, message), False
            turn_id = "trn_" + _token(16)
            message_id = "msg_" + _token(16)
            session.execute(
                insert(MessageRow).values(
                    id=message_id,
                    channel_id=channel_id,
                    turn_id=turn_id,
                    client_message_id=client_message_id,
                    sequence=self._next_sequence(session, channel_id),
                    role="user",
                    kind="message",
                    content=content,
                    reactions_json=json.dumps(["⏳"]),
                    created_at=now,
                    author_kind="human",
                    author_id=user_id,
                )
            )
            session.execute(
                insert(TurnRow).values(
                    id=turn_id,
                    channel_id=channel_id,
                    client_turn_id=client_turn_id,
                    input_message_id=message_id,
                    status="accepted",
                    targets_json=json.dumps(list(targets)),
                    intent=intent,
                    participants_json=json.dumps(
                        [
                            {
                                "agent_slug": target,
                                "status": "queued",
                                "error": None,
                                "read_only": target == "critic",
                            }
                            for target in (targets or (None,))
                        ]
                    ),
                    created_at=now,
                    author_kind="human",
                    author_id=user_id,
                    trigger="human",
                    chain_depth=0,
                )
            )
            for slug in dict.fromkeys(participants):
                if session.get(ChannelParticipantRow, (channel_id, slug)) is None:
                    session.add(
                        ChannelParticipantRow(
                            channel_id=channel_id,
                            agent_slug=slug,
                            mode="mention",
                            added_by_kind="human",
                            added_by_id=user_id,
                            created_at=now,
                        )
                    )
                    _event(
                        session,
                        "collaboration.participant.added",
                        now,
                        data={"channel_id": channel_id, "agent_slug": slug},
                    )
            channel.updated_at = now
            channel.revision += 1
            turn_row = session.get(TurnRow, turn_id)
            message_row = session.get(MessageRow, message_id)
            assert turn_row is not None and message_row is not None  # nosec B101
            _event(
                session,
                "collaboration.turn.accepted",
                now,
                actor=actor,
                data={"channel_id": channel_id, "turn_id": turn_id, "targets": list(targets)},
            )
            _event(
                session,
                "collaboration.message.created",
                now,
                actor=actor,
                data={
                    "channel_id": channel_id,
                    "message_id": message_id,
                    "sequence": message_row.sequence,
                    "author_kind": "human",
                    "author_id": user_id,
                },
            )
            return _turn(session, turn_row), _message(session, message_row), True

    def start_turn(self, turn_id: str, now: float) -> bool:
        with self.dstore.transaction() as session:
            turn = session.get(TurnRow, turn_id)
            if turn is None or turn.status != "accepted":
                return False
            channel = session.get(ChannelRow, turn.channel_id)
            if channel is None or channel.state != "active":
                self._cancel_deleted_channel_turn(session, turn, now)
                return False
            turn.status = "running"
            turn.started_at = now
            _event(
                session,
                "collaboration.turn.running",
                now,
                data={"channel_id": turn.channel_id, "turn_id": turn.id},
            )
            return True

    def append_reply(
        self,
        turn_id: str,
        *,
        content: str,
        agent_slug: str | None,
        now: float,
        participant_index: int | None = None,
    ) -> Message | None:
        with self.dstore.immediate_transaction() as session:
            turn = session.get(TurnRow, turn_id)
            if turn is None or turn.status not in {"accepted", "running", "cancelling"}:
                return None
            channel = session.get(ChannelRow, turn.channel_id)
            if channel is None or channel.state != "active":
                self._cancel_deleted_channel_turn(session, turn, now)
                return None
            message_id = "msg_" + _token(16)
            author_id = agent_slug or ANGIE_SLUG
            session.execute(
                insert(MessageRow).values(
                    id=message_id,
                    channel_id=turn.channel_id,
                    turn_id=turn_id,
                    sequence=self._next_sequence(session, turn.channel_id),
                    role="assistant",
                    kind="agent_result" if agent_slug else "message",
                    content=content,
                    agent_slug=agent_slug,
                    created_at=now,
                    author_kind="agent",
                    author_id=author_id,
                )
            )
            channel.updated_at = now
            channel.revision += 1
            if participant_index is not None:
                progress = json.loads(turn.participants_json)
                was_running = progress[participant_index]["status"] == "running"
                progress[participant_index]["status"] = "completed"
                progress[participant_index]["message_id"] = message_id
                turn.participants_json = json.dumps(progress)
                if was_running:
                    _participant_activity(session, turn.channel_id, agent_slug, "idle", now)
            row = session.get(MessageRow, message_id)
            assert row is not None  # nosec B101
            _event(
                session,
                "collaboration.message.created",
                now,
                data={
                    "channel_id": turn.channel_id,
                    "turn_id": turn_id,
                    "message_id": message_id,
                    "sequence": row.sequence,
                    "agent_slug": agent_slug,
                    "author_kind": "agent",
                    "author_id": author_id,
                },
            )
            return _message(session, row)

    def append_work_result(
        self,
        message_id: str,
        *,
        channel_id: str,
        turn_id: str,
        content: str,
        agent_slug: str | None,
        work: dict[str, Any],
        now: float,
    ) -> Message | None:
        """Append one server-owned work result, idempotently."""
        with self.dstore.immediate_transaction() as session:
            existing = session.get(MessageRow, message_id)
            if existing is not None:
                return _message(session, existing)
            channel = session.get(ChannelRow, channel_id)
            turn = session.get(TurnRow, turn_id)
            if (
                channel is None
                or channel.state != "active"
                or turn is None
                or turn.channel_id != channel_id
            ):
                return None
            session.execute(
                insert(MessageRow).values(
                    id=message_id,
                    channel_id=channel_id,
                    turn_id=turn_id,
                    sequence=self._next_sequence(session, channel_id),
                    role="assistant",
                    kind="work_result",
                    content=content,
                    agent_slug=agent_slug,
                    work_json=json.dumps(work, default=str),
                    created_at=now,
                    author_kind="agent",
                    author_id=agent_slug or ANGIE_SLUG,
                )
            )
            channel.updated_at = now
            channel.revision += 1
            row = session.get(MessageRow, message_id)
            assert row is not None  # nosec B101
            _event(
                session,
                "collaboration.work.delivered",
                now,
                data={"channel_id": channel_id, "turn_id": turn_id, "message_id": message_id},
            )
            return _message(session, row)

    def message_exists(self, message_id: str) -> bool:
        with self.dstore.read() as session:
            return session.get(MessageRow, message_id) is not None

    def set_message_reaction(
        self,
        user_id: Viewer,
        channel_id: str,
        message_id: str,
        *,
        emoji: str,
        active: bool,
        now: float,
    ) -> Message | None:
        """Idempotently add or remove one reaction on a user's channel message."""
        emoji = emoji.strip()
        if not emoji or any(character.isspace() for character in emoji):
            raise CollaborationError("invalid_reaction", "Choose one emoji without spaces.")
        with self.dstore.immediate_transaction() as session:
            try:
                _access(session, channel_id, user_id, "post", now=now)
            except CollaborationError as exc:
                if exc.code == "channel_not_found":
                    return None
                raise
            row = session.get(MessageRow, message_id)
            if row is None or row.channel_id != channel_id:
                return None
            reactions = [str(value) for value in json.loads(row.reactions_json or "[]")]
            changed = False
            if active and emoji not in reactions:
                if len(reactions) >= 16:
                    raise CollaborationError(
                        "reaction_limit", "This message already has 16 reactions."
                    )
                reactions.append(emoji)
                changed = True
            elif not active and emoji in reactions:
                reactions.remove(emoji)
                changed = True
            if changed:
                row.reactions_json = json.dumps(reactions, ensure_ascii=False)
                _event(
                    session,
                    "collaboration.message.reactions.updated",
                    now,
                    data={
                        "channel_id": channel_id,
                        "message_id": message_id,
                        "emoji": emoji,
                        "active": active,
                    },
                )
            session.flush()
            return _message(session, row)

    def finish_turn(self, turn_id: str, *, error: str | None, now: float) -> Turn | None:
        with self.dstore.immediate_transaction() as session:
            row = session.get(TurnRow, turn_id)
            if row is None or row.status in {"completed", "failed", "cancelled"}:
                return None if row is None else _turn(session, row)
            cancelled = row.status == "cancelling"
            row.status = "cancelled" if cancelled else ("failed" if error else "completed")
            if cancelled:
                error = "Stopped remaining responses. Work already started may still have effects."
            progress = json.loads(row.participants_json)
            for participant in progress:
                if participant["status"] == "running":
                    _participant_activity(
                        session, row.channel_id, participant["agent_slug"], "idle", now
                    )
                if participant["status"] in {"queued", "running"}:
                    participant["status"] = (
                        "cancelled" if cancelled else ("failed" if error else "completed")
                    )
            row.participants_json = json.dumps(progress)
            row.error = error
            row.completed_at = now
            channel = session.get(ChannelRow, row.channel_id)
            input_message = session.get(MessageRow, row.input_message_id)
            if input_message is not None:
                reactions = [
                    str(value)
                    for value in json.loads(input_message.reactions_json or "[]")
                    if value != "⏳"
                ]
                outcome = "⚠" if error else "✅"
                if outcome not in reactions:
                    reactions.append(outcome)
                input_message.reactions_json = json.dumps(reactions, ensure_ascii=False)
            if error and channel is not None and channel.state == "active":
                message_id = "msg_" + _token(16)
                sequence = self._next_sequence(session, row.channel_id)
                session.execute(
                    insert(MessageRow).values(
                        id=message_id,
                        channel_id=row.channel_id,
                        turn_id=turn_id,
                        sequence=sequence,
                        role="assistant",
                        kind="turn_cancelled" if cancelled else "turn_error",
                        content=error,
                        created_at=now,
                        author_kind="system",
                        author_id=None,
                    )
                )
                channel.updated_at = now
                channel.revision += 1
                _event(
                    session,
                    "collaboration.message.created",
                    now,
                    data={
                        "channel_id": row.channel_id,
                        "turn_id": turn_id,
                        "message_id": message_id,
                        "sequence": sequence,
                        "author_kind": "system",
                        "author_id": None,
                    },
                )
            session.flush()
            _event(
                session,
                f"collaboration.turn.{row.status}",
                now,
                data={"channel_id": row.channel_id, "turn_id": row.id, "error": error},
            )
            return _turn(session, row)

    def recover_turns(self, now: float) -> list[tuple[Turn, LocalUser, str]]:
        """Settle interrupted execution and return only work that never started.

        Called before the listener admits requests. Never replay a running
        turn: external actions may already have happened before the crash.
        """
        with self.dstore.read() as session:
            rows = list(
                session.scalars(
                    select(TurnRow).where(TurnRow.status.in_(("accepted", "running", "cancelling")))
                )
            )
            interrupted: list[tuple[str, bool]] = []
            queued: list[tuple[int, Turn, LocalUser, str]] = []
            for row in rows:
                if row.status in {"running", "cancelling"}:
                    progress = json.loads(row.participants_json)
                    if any(p.get("parent_index") is not None for p in progress):
                        interrupted.append(
                            (row.id, all(p["status"] == "completed" for p in progress))
                        )
                        continue
                    replies = set(
                        session.scalars(
                            select(MessageRow.agent_slug).where(
                                MessageRow.turn_id == row.id,
                                MessageRow.role == "assistant",
                                MessageRow.kind != "turn_error",
                            )
                        )
                    )
                    expected = set(json.loads(row.targets_json)) or {None}
                    interrupted.append((row.id, expected <= replies))
                    continue
                channel = session.get(ChannelRow, row.channel_id)
                author_id = row.author_id if row.author_kind == "human" else None
                user_id = author_id or (channel.user_id if channel else None)
                user = session.get(LocalUserRow, user_id) if user_id else None
                message = session.get(MessageRow, row.input_message_id)
                if user is not None and not self._may_read(session, row.channel_id, user.id):
                    user = None
                if channel and channel.state == "active" and user and user.active and message:
                    queued.append(
                        (message.sequence, _turn(session, row), _user(user), message.content)
                    )
                else:
                    interrupted.append((row.id, False))
        for turn_id, answered in interrupted:
            self.finish_turn(
                turn_id,
                now=now,
                error=None
                if answered
                else (
                    "The daemon restarted before this turn finished. Its actions may "
                    "already have run; review the activity before explicitly retrying."
                ),
            )
        # Sequence is authoritative inside each channel even when timestamps tie.
        queued.sort(key=lambda item: (item[1].channel_id, item[0]))
        return [(turn, user, content) for _, turn, user, content in queued]

    @staticmethod
    def _may_read(session: Any, channel_id: str, user_id: str) -> bool:
        try:
            _access(session, channel_id, user_id, "read")
        except CollaborationError:
            return False
        return True

    def turn_history(self, turn: Turn, *, max_chars: int = 60_000) -> str:
        """Prior turns and completed peers in this turn, without future input."""
        with self.dstore.read() as session:
            current = session.get(MessageRow, turn.input_message_id)
            if current is None:
                return ""
            # Replies may be appended after a later user message was accepted.
            # Include replies to earlier inputs, not merely earlier sequences.
            prior_turns = select(MessageRow.turn_id).where(
                MessageRow.channel_id == turn.channel_id,
                MessageRow.role == "user",
                MessageRow.sequence < current.sequence,
            )
            rows = session.scalars(
                select(MessageRow)
                .where(
                    MessageRow.channel_id == turn.channel_id,
                    MessageRow.turn_id.in_(prior_turns)
                    | ((MessageRow.turn_id == turn.id) & (MessageRow.role == "assistant")),
                )
                .order_by(MessageRow.sequence.desc())
                .limit(200)
            )
            chunks: list[str] = []
            remaining = max_chars
            for row in rows:
                chunk = json.dumps(
                    {
                        "role": row.role,
                        "agent": row.agent_slug,
                        "content": row.content,
                    },
                    ensure_ascii=False,
                )
                if len(chunk) > remaining:
                    break
                chunks.append(chunk)
                remaining -= len(chunk) + 1
        return "\n".join(reversed(chunks))

    def participant_result(self, turn: Turn, index: int) -> Message | None:
        """Return a completed participant's reply, scoped to this exact turn."""
        if index < 0 or index >= len(turn.participants):
            return None
        message_id = turn.participants[index].get("message_id")
        if not isinstance(message_id, str):
            return None
        with self.dstore.read() as session:
            row = session.get(MessageRow, message_id)
            if (
                row is None
                or row.channel_id != turn.channel_id
                or row.turn_id != turn.id
                or row.role != "assistant"
            ):
                return None
            return _message(session, row)

    def get_turn(self, viewer: Viewer, channel_id: str, turn_id: str) -> Turn | None:
        with self.dstore.read() as session:
            try:
                _access(session, channel_id, viewer, "read")
            except CollaborationError:
                return None
            row = session.get(TurnRow, turn_id)
            if row is None or row.channel_id != channel_id:
                return None
            return _turn(session, row)

    def list_turns(
        self, viewer: Viewer, channel_id: str, *, active_only: bool = False
    ) -> list[Turn]:
        with self.dstore.read() as session:
            _access(session, channel_id, viewer, "read")
            statement = (
                select(TurnRow)
                .join(MessageRow, MessageRow.id == TurnRow.input_message_id)
                .where(TurnRow.channel_id == channel_id)
            )
            if active_only:
                statement = statement.where(
                    TurnRow.status.in_(("accepted", "running", "cancelling"))
                )
            return [
                _turn(session, row)
                for row in session.scalars(statement.order_by(MessageRow.sequence).limit(200))
            ]

    def participant_started(self, turn_id: str, index: int, now: float) -> bool:
        with self.dstore.immediate_transaction() as session:
            row = session.get(TurnRow, turn_id)
            if row is None or row.status != "running":
                return False
            progress = json.loads(row.participants_json)
            if not progress:
                progress = [
                    {
                        "agent_slug": target,
                        "status": "queued",
                        "error": None,
                        "read_only": target == "critic",
                    }
                    for target in (json.loads(row.targets_json) or [None])
                ]
            progress[index]["status"] = "running"
            row.participants_json = json.dumps(progress)
            _participant_activity(
                session, row.channel_id, progress[index]["agent_slug"], "thinking", now
            )
            _event(
                session,
                "collaboration.participant.running",
                now,
                data={
                    "channel_id": row.channel_id,
                    "turn_id": turn_id,
                    "index": index,
                    "agent_slug": progress[index]["agent_slug"],
                },
            )
            return True

    def link_code_work(
        self, turn_id: str, index: int, repo: str, number: int, title: str, now: float
    ) -> None:
        """Keep successful issue dispatch identity with its originating participant."""
        with self.dstore.immediate_transaction() as session:
            row = session.get(TurnRow, turn_id)
            if row is None or row.status not in {"running", "cancelling"}:
                return
            channel = session.get(ChannelRow, row.channel_id)
            if channel is None or channel.state != "active":
                return
            progress = json.loads(row.participants_json)
            if not 0 <= index < len(progress):
                return
            refs = progress[index].setdefault("code_work", [])
            if any(ref["repo"] == repo and ref["source_key"] == str(number) for ref in refs):
                return
            refs.append({"repo": repo, "source_key": str(number), "title": title})
            row.participants_json = json.dumps(progress)
            _event(
                session,
                "collaboration.work.linked",
                now,
                data={
                    "channel_id": row.channel_id,
                    "turn_id": turn_id,
                    "index": index,
                    "kind": "code",
                },
            )

    def record_tool_activity(
        self, turn_id: str, index: int, tool: str, phase: str, ok: bool | None, now: float
    ) -> None:
        """Publish tool lifecycle only, never tool arguments, output, or credentials."""
        if phase not in {"started", "completed"}:
            return
        with self.dstore.immediate_transaction() as session:
            row = session.get(TurnRow, turn_id)
            if row is None or row.status not in {"running", "cancelling"}:
                return
            progress = json.loads(row.participants_json)
            if not 0 <= index < len(progress) or progress[index]["status"] != "running":
                return
            _event(
                session,
                f"collaboration.tool.{phase}",
                now,
                data={
                    "channel_id": row.channel_id,
                    "turn_id": turn_id,
                    "index": index,
                    "agent_slug": progress[index]["agent_slug"],
                    "tool": tool,
                    "ok": ok,
                },
            )

    def participant_failed(
        self, turn_id: str, index: int, error: str, now: float | None = None
    ) -> None:
        with self.dstore.immediate_transaction() as session:
            row = session.get(TurnRow, turn_id)
            if row is None or row.status not in {"running", "cancelling"}:
                return
            progress = json.loads(row.participants_json)
            if progress[index]["status"] == "running":
                _participant_activity(
                    session,
                    row.channel_id,
                    progress[index]["agent_slug"],
                    "idle",
                    float(row.started_at or row.created_at) if now is None else now,
                )
            progress[index].update(status="failed", error=error)
            row.participants_json = json.dumps(progress)

    def cancel_turn(self, viewer: Viewer, channel_id: str, turn_id: str, now: float) -> Turn | None:
        """Stop a turn. The person who asked, or someone who manages the
        channel, may stop it; ``None`` when either cannot be seen."""
        settle = False
        with self.dstore.immediate_transaction() as session:
            try:
                _, member = _access(session, channel_id, viewer, "read")
            except CollaborationError:
                return None
            row = session.get(TurnRow, turn_id)
            if row is None or row.channel_id != channel_id:
                return None
            if member is not None and not (
                row.author_kind in {"human", None} and row.author_id == member.user.id
            ):
                _access(session, channel_id, member, "manage")
            if row.status not in {"accepted", "running"}:
                return _turn(session, row)
            settle = row.status == "accepted"
            row.status = "cancelling"
            progress = json.loads(row.participants_json)
            for participant in progress:
                if participant["status"] == "queued":
                    participant["status"] = "cancelled"
            row.participants_json = json.dumps(progress)
            _event(
                session,
                "collaboration.turn.cancelling",
                now,
                data={"channel_id": channel_id, "turn_id": turn_id},
            )
            result = _turn(session, row)
        return self.finish_turn(turn_id, error=None, now=now) if settle else result

    def queue_handoff(
        self,
        user_id: Viewer,
        channel_id: str,
        turn_id: str,
        source_index: int,
        agent_slug: str,
        message: str,
        now: float,
        *,
        is_agent: Callable[[str], bool] | None = None,
    ) -> str:
        """Persist a bounded peer request without changing the user's root targets.

        ``is_agent`` says whether a slug names an agent that may be asked
        (the caller's registry); without it only the built-ins may.
        """
        known = (
            is_agent(agent_slug) if is_agent is not None else agent_slug in {a.slug for a in AGENTS}
        )
        if not known:
            raise CollaborationError("unknown_agent", "Choose a native sbxloop agent.")
        if not 1 <= len(message.strip()) <= 4000:
            raise CollaborationError(
                "invalid_handoff", "Provide a message of 1 to 4000 characters."
            )
        message = message.strip()
        key = hashlib.sha256(json.dumps([source_index, agent_slug, message]).encode()).hexdigest()
        with self.dstore.immediate_transaction() as session:
            try:
                channel, _ = _access(session, channel_id, user_id, "post", now=now)
            except CollaborationError as exc:
                raise CollaborationError(
                    "handoff_stopped", "This chat turn is no longer running."
                ) from exc
            turn = session.get(TurnRow, turn_id)
            if turn is None or turn.channel_id != channel_id or turn.status != "running":
                raise CollaborationError("handoff_stopped", "This chat turn is no longer running.")
            progress = json.loads(turn.participants_json)
            if not 0 <= source_index < len(progress):
                raise CollaborationError(
                    "invalid_handoff", "The requesting participant is missing."
                )
            source = progress[source_index]
            if source["status"] != "running":
                raise CollaborationError(
                    "handoff_stopped", "The requesting agent is no longer running."
                )
            for index, participant in enumerate(progress):
                if participant.get("request_key") == key:
                    return (
                        f"Queued @{agent_slug} as handoff {index}. "
                        "Its reply will appear in this chat."
                    )
            source_slug = source["agent_slug"] or "concierge"
            if agent_slug == source_slug:
                raise CollaborationError("invalid_handoff", "Address a different agent.")
            handed = [p for p in progress if p.get("parent_index") is not None]
            if len(handed) >= MAX_HANDOFFS_PER_TURN:
                raise CollaborationError(
                    "handoff_limit", "This turn has reached its six-handoff limit."
                )
            if (
                sum(p.get("parent_index") == source_index for p in handed)
                >= MAX_HANDOFFS_PER_RESPONSE
            ):
                raise CollaborationError(
                    "handoff_limit", "Each response can request at most two peers."
                )
            depth = int(source.get("depth", 0)) + 1
            if depth > MAX_HANDOFF_DEPTH:
                raise CollaborationError(
                    "handoff_limit", "This turn has reached its handoff depth limit."
                )
            read_only = (
                bool(source.get("read_only")) or source_slug == "critic" or agent_slug == "critic"
            )
            index = len(progress)
            progress.append(
                {
                    "agent_slug": agent_slug,
                    "status": "queued",
                    "error": None,
                    "parent_index": source_index,
                    "requested_by": source_slug,
                    "request": message,
                    "request_key": key,
                    "depth": depth,
                    "read_only": read_only,
                }
            )
            turn.participants_json = json.dumps(progress)
            message_id = "msg_" + _token(16)
            sequence = self._next_sequence(session, channel_id)
            session.execute(
                insert(MessageRow).values(
                    id=message_id,
                    channel_id=channel_id,
                    turn_id=turn_id,
                    sequence=sequence,
                    role="assistant",
                    kind="agent_handoff",
                    agent_slug=source_slug,
                    content=f"@{source_slug} asked @{agent_slug}:\n\n{message}",
                    created_at=now,
                    author_kind="agent",
                    author_id=source_slug,
                )
            )
            channel.updated_at = now
            channel.revision += 1
            _event(
                session,
                "collaboration.handoff.queued",
                now,
                data={
                    "channel_id": channel_id,
                    "turn_id": turn_id,
                    "index": index,
                    "requested_by": source_slug,
                    "agent_slug": agent_slug,
                    "read_only": read_only,
                    "message_id": message_id,
                },
            )
            return f"Queued @{agent_slug} as handoff {index}. Its reply will appear in this chat."

    # -- teams ---------------------------------------------------------------------

    def list_teams(self, user_id: str, *, enabled_only: bool = False) -> list[Team]:
        with self.dstore.read() as session:
            statement = select(TeamRow).where(TeamRow.user_id == user_id)
            if enabled_only:
                statement = statement.where(TeamRow.enabled == 1)
            rows = session.scalars(statement.order_by(TeamRow.created_at.asc()))
            return [_team(row) for row in rows]

    def get_team(self, user_id: str, selector: str) -> Team | None:
        with self.dstore.read() as session:
            row = session.scalars(
                select(TeamRow).where(
                    TeamRow.user_id == user_id,
                    (TeamRow.id == selector) | (TeamRow.slug == selector),
                )
            ).first()
            return None if row is None else _team(row)

    def create_team(
        self,
        user_id: str,
        *,
        name: str,
        slug: str,
        description: str | None,
        goal: str | None,
        agent_slugs: tuple[str, ...],
        enabled: bool,
        now: float,
    ) -> Team:
        team_id = "team_" + _token(12)
        try:
            with self.dstore.transaction() as session:
                session.execute(
                    insert(TeamRow).values(
                        id=team_id,
                        user_id=user_id,
                        name=name.strip(),
                        slug=slug.strip(),
                        description=description,
                        goal=goal,
                        agent_slugs_json=json.dumps(list(agent_slugs)),
                        enabled=1 if enabled else 0,
                        created_at=now,
                        updated_at=now,
                    )
                )
                row = session.get(TeamRow, team_id)
                assert row is not None  # nosec B101
                _event(session, "collaboration.team.created", now, data={"team_id": team_id})
                return _team(row)
        except IntegrityError as exc:
            raise CollaborationError(
                "team_conflict", "a team with that slug already exists"
            ) from exc

    def update_team(
        self, user_id: str, team_id: str, values: dict[str, Any], now: float
    ) -> Team | None:
        try:
            with self.dstore.transaction() as session:
                row = session.get(TeamRow, team_id)
                if row is None or row.user_id != user_id:
                    return None
                for key in ("name", "slug", "description", "goal"):
                    if key in values:
                        setattr(row, key, values[key])
                if "agent_slugs" in values:
                    row.agent_slugs_json = json.dumps(list(values["agent_slugs"]))
                if "enabled" in values:
                    row.enabled = 1 if values["enabled"] else 0
                row.updated_at = now
                session.flush()
                _event(session, "collaboration.team.updated", now, data={"team_id": team_id})
                return _team(row)
        except IntegrityError as exc:
            raise CollaborationError(
                "team_conflict", "a team with that slug already exists"
            ) from exc

    def delete_team(self, user_id: str, team_id: str, now: float) -> bool:
        with self.dstore.transaction() as session:
            row = session.get(TeamRow, team_id)
            if row is None or row.user_id != user_id:
                return False
            session.delete(row)
            _event(session, "collaboration.team.deleted", now, data={"team_id": team_id})
            return True

    # -- user preferences ---------------------------------------------------------

    def list_preferences(self, user_id: str) -> list[Preference]:
        with self.dstore.read() as session:
            rows = session.scalars(
                select(PreferenceRow)
                .where(PreferenceRow.user_id == user_id)
                .order_by(PreferenceRow.name.asc())
            )
            return [_preference(row) for row in rows]

    def get_preference(self, user_id: str, name: str) -> Preference | None:
        with self.dstore.read() as session:
            row = session.scalars(
                select(PreferenceRow).where(
                    PreferenceRow.user_id == user_id,
                    PreferenceRow.name == name,
                )
            ).first()
            return None if row is None else _preference(row)

    def upsert_preference(self, user_id: str, name: str, content: str, now: float) -> Preference:
        with self.dstore.transaction() as session:
            row = session.scalars(
                select(PreferenceRow).where(
                    PreferenceRow.user_id == user_id,
                    PreferenceRow.name == name,
                )
            ).first()
            if row is None:
                row = PreferenceRow(
                    id="pref_" + _token(12),
                    user_id=user_id,
                    name=name,
                    content=content,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                event = "created"
            else:
                row.content = content
                row.updated_at = now
                event = "updated"
            session.flush()
            _event(
                session,
                f"collaboration.preference.{event}",
                now,
                data={"preference_id": row.id, "name": name},
            )
            return _preference(row)

    def delete_preference(self, user_id: str, name: str, now: float) -> bool:
        with self.dstore.transaction() as session:
            row = session.scalars(
                select(PreferenceRow).where(
                    PreferenceRow.user_id == user_id,
                    PreferenceRow.name == name,
                )
            ).first()
            if row is None:
                return False
            session.delete(row)
            _event(
                session,
                "collaboration.preference.deleted",
                now,
                data={"preference_id": row.id, "name": name},
            )
            return True

    def reset_preferences(self, user_id: str, now: float) -> None:
        with self.dstore.transaction() as session:
            rows = session.scalars(
                select(PreferenceRow).where(PreferenceRow.user_id == user_id)
            ).all()
            for row in rows:
                session.delete(row)
            _event(
                session,
                "collaboration.preferences.reset",
                now,
                data={"user_id": user_id, "deleted": len(rows)},
            )

    # -- workflow definitions -----------------------------------------------------

    def list_workflows(self, user_id: str) -> list[Workflow]:
        with self.dstore.read() as session:
            rows = session.scalars(
                select(WorkflowRow)
                .where(WorkflowRow.user_id == user_id)
                .order_by(WorkflowRow.name.asc())
            )
            return [_workflow(row) for row in rows]

    def get_workflow(self, user_id: str, workflow_id: str) -> Workflow | None:
        with self.dstore.read() as session:
            row = session.get(WorkflowRow, workflow_id)
            if row is None or row.user_id != user_id:
                return None
            return _workflow(row)

    def create_workflow(
        self,
        user_id: str,
        *,
        name: str,
        slug: str,
        description: str | None,
        trigger_event: str | None,
        enabled: bool,
        now: float,
    ) -> Workflow:
        try:
            with self.dstore.transaction() as session:
                row = WorkflowRow(
                    id="wf_" + _token(12),
                    user_id=user_id,
                    name=name.strip(),
                    slug=slug.strip(),
                    description=description,
                    trigger_event=trigger_event,
                    enabled=1 if enabled else 0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                _event(
                    session,
                    "collaboration.workflow.created",
                    now,
                    data={"workflow_id": row.id, "slug": row.slug},
                )
                return _workflow(row)
        except IntegrityError as exc:
            raise CollaborationError(
                "workflow_conflict", "a workflow with that slug already exists"
            ) from exc

    def update_workflow(
        self, user_id: str, workflow_id: str, values: dict[str, Any], now: float
    ) -> Workflow | None:
        try:
            with self.dstore.transaction() as session:
                row = session.get(WorkflowRow, workflow_id)
                if row is None or row.user_id != user_id:
                    return None
                for key in ("name", "slug", "description", "trigger_event"):
                    if key in values:
                        setattr(row, key, values[key])
                if "enabled" in values:
                    row.enabled = 1 if values["enabled"] else 0
                row.updated_at = now
                session.flush()
                _event(
                    session,
                    "collaboration.workflow.updated",
                    now,
                    data={"workflow_id": row.id, "slug": row.slug},
                )
                return _workflow(row)
        except IntegrityError as exc:
            raise CollaborationError(
                "workflow_conflict", "a workflow with that slug already exists"
            ) from exc

    def delete_workflow(self, user_id: str, workflow_id: str, now: float) -> bool:
        with self.dstore.transaction() as session:
            row = session.get(WorkflowRow, workflow_id)
            if row is None or row.user_id != user_id:
                return False
            session.delete(row)
            _event(
                session,
                "collaboration.workflow.deleted",
                now,
                data={"workflow_id": workflow_id, "slug": row.slug},
            )
            return True


LOCAL_USER_CAPABILITIES = ALL_CAPABILITIES
