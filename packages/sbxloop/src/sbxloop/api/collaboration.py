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
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import case, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from sbxloop.agents.posts import POST_KINDS, TERMINAL_POST_KINDS, PostKind
from sbxloop.api.agents import AGENTS, ANGIE_SLUG
from sbxloop.api.auth.store import hash_secret
from sbxloop.api.channel_access import MANAGING_ROLES, ChannelAccess, ChannelRole, Need
from sbxloop.api.publicids import run_public_id

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
    AgentMemoryRow,
    ChannelLinkRow,
    ChannelMemberRow,
    ChannelParticipantRow,
    ChannelRow,
    ChannelRunPostRow,
    ChannelSummaryRow,
    ExternalIdentityRow,
    LocalUserRow,
    MessageArtifactRow,
    MessageRow,
    PreferenceRow,
    TeamRow,
    TurnRow,
    WorkflowRow,
    WorkspaceInviteRow,
    WorkspaceMemberRow,
)
from sbxloop.db.daemon_models import WorkItemRow
from sbxloop.db.event_scope import channel_for_run, turn_for_item
from sbxloop.db.job_scope import external_metadata
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
#: Messages one turn's history may carry, newest first.
HISTORY_MESSAGES = 200
#: Characters one turn's history may spend on those messages.
HISTORY_CHARS = 60_000
#: How long a bridge identity link code is worth typing.
LINK_CODE_TTL_S = 600.0


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
    #: Messages past the reader's last read sequence; None for a reader
    #: with no membership to track it against (a plain API client).
    unread_count: int | None = None
    #: Stable presentation identity and the baseline for imported messages.
    external_work: dict[str, Any] | None = None


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


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A file a message carries, by catalog identity. ``run_id`` is the
    run's public id; the bytes are served by the artifact routes."""

    id: str
    run_id: str
    relpath: str
    media_type: str
    size: int


@dataclass(frozen=True, slots=True)
class ChannelSummary:
    """What a channel said up to ``through_sequence``, in a few sentences."""

    channel_id: str
    through_sequence: int
    content: str
    created_at: float


@dataclass(frozen=True, slots=True)
class ChannelLink:
    """A bridge surface that mirrors a channel."""

    id: str
    channel_id: str
    backend: str
    surface_id: str
    thread_id: str | None
    allow_guests: bool
    created_by: str | None
    created_at: float
    active: bool


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    """Who a local user is on a bridge."""

    backend: str
    external_user_id: str
    user_id: str
    display_name: str | None
    verified_at: float


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
    artifacts: tuple[ArtifactRef, ...] = ()
    origin: dict[str, Any] | None = None
    #: What an ``agent_update`` a run posted is; None for every other message.
    post_kind: PostKind | None = None


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
    #: The run this turn steered instead of answering from scratch (S-A11).
    steered_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTurnRecord:
    """One agent-authored turn, as the guardrails count it."""

    turn_id: str
    source_slug: str | None
    targets: tuple[str, ...]
    trigger: str
    created_at: float


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


@dataclass(frozen=True, slots=True)
class MergeReport:
    """What :meth:`CollaborationStore.merge_users` moved, or would move.

    ``moved`` counts rows per kind, in a stable order. A preference both
    accounts hold keeps the target's value and is named in
    ``preference_conflicts``; a team or workflow whose slug the target
    already uses moves under a new slug, listed as ``(old, new)``.
    """

    source_id: str
    source_username: str
    target_id: str
    target_username: str
    dry_run: bool
    moved: dict[str, int]
    preference_conflicts: tuple[str, ...]
    renamed_teams: tuple[tuple[str, str], ...]
    renamed_workflows: tuple[tuple[str, str], ...]
    #: The provider identity that moved to the target, if the source had one.
    identity: tuple[str, str] | None
    #: The target's workspace role before and after the merge.
    previous_role: Role | None
    role: Role


#: Workspace roles, weakest first: a merge keeps the stronger of two.
_ROLE_RANK: dict[str, int] = {"member": 0, "admin": 1, "owner": 2}
_CHANNEL_ROLE_RANK: dict[str, int] = {"member": 0, "owner": 1}


class _DryRun(Exception):
    """Unwinds a dry-run merge's transaction, carrying what it found."""

    def __init__(self, report: MergeReport) -> None:
        super().__init__("dry run")
        self.report = report


def _free_slug(taken: set[str], slug: str) -> str:
    candidate = f"{slug}-merged"
    suffix = 2
    while candidate in taken:
        candidate = f"{slug}-merged{suffix}"
        suffix += 1
    return candidate


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


def guest_user(display_name: str | None) -> LocalUser:
    """The stand-in a guest's turn runs for: a name, and nothing else. Its
    empty id belongs to no member, so every check that reads it refuses."""
    name = (display_name or "guest").strip() or "guest"
    return LocalUser(
        id="",
        client_id="",
        username=name,
        email="",
        full_name=name,
        timezone="UTC",
        active=True,
        created_at=0.0,
        updated_at=0.0,
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


def _channel(
    row: ChannelRow,
    my_role: ChannelRole | None = None,
    unread_count: int | None = None,
    external_work: dict[str, Any] | None = None,
) -> Channel:
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
        unread_count=unread_count,
        external_work=external_work,
    )


def message_author(role: str, kind: str, agent_slug: str | None, owner_id: str | None) -> Author:
    """The author of a message stored without one: the rules the authorship
    migration backfilled with, for rows an older release wrote since."""
    if role == "user":
        return Author("human", owner_id)
    if kind in SYSTEM_MESSAGE_KINDS:
        return SYSTEM_AUTHOR
    return Author("agent", agent_slug or ANGIE_SLUG)


def bridge_origin(
    backend: str, surface_id: str, external_message_id: str, author_name: str | None = None
) -> dict[str, Any]:
    """Where a message that arrived over a bridge came from. ``author_name``
    rides along for a guest, who has no account to read a name from."""
    origin: dict[str, Any] = {
        "backend": backend,
        "surface_id": surface_id,
        "external_message_id": external_message_id,
    }
    if author_name:
        origin["author_name"] = author_name
    return origin


def _origin_name(origin: dict[str, Any] | None) -> str | None:
    if not isinstance(origin, dict):
        return None
    name = origin.get("author_name")
    return str(name) if name else None


@dataclass(frozen=True, slots=True)
class _Directory:
    """The people and channels a page of messages names, read once.

    Attributing a message asks who wrote it and, when the row records no
    author, who owns its channel. Asked row by row that is a query per
    author and a query per channel, under the store's single lock, for
    every history a turn builds and every messages page the browser polls.
    :func:`_directory` answers all of them in two queries; a lookup that
    misses falls back to the row-at-a-time path, so a page is never wrong,
    only slower.
    """

    #: User id -> display name, for the ids the page's rows carry.
    names: Mapping[str, str | None]
    #: Channel id -> owning user id.
    owners: Mapping[str, str | None]


#: Nothing read ahead: every lookup falls back to its own query.
_NO_DIRECTORY = _Directory(names={}, owners={})


def _directory(session: Any, rows: Sequence[Any]) -> _Directory:
    """The authors and channel owners ``rows`` name, in one query each."""
    user_ids = {
        str(row.author_id)
        for row in rows
        if row.author_kind == "human" and row.author_id is not None
    }
    names: dict[str, str | None] = {}
    if user_ids:
        for user in session.scalars(
            select(LocalUserRow).where(LocalUserRow.id.in_(sorted(user_ids)))
        ):
            names[str(user.id)] = str(user.full_name or user.username)
    channel_ids = {str(row.channel_id) for row in rows if row.channel_id is not None}
    owners: dict[str, str | None] = {}
    if channel_ids:
        for channel in session.scalars(
            select(ChannelRow).where(ChannelRow.id.in_(sorted(channel_ids)))
        ):
            owners[str(channel.id)] = str(channel.user_id)
    return _Directory(names=names, owners=owners)


def _human_name(
    session: Any, user_id: str | None, directory: _Directory = _NO_DIRECTORY
) -> str | None:
    if user_id is None:
        return None
    if user_id in directory.names:
        return directory.names[user_id]
    user = session.get(LocalUserRow, user_id)
    if user is None:
        return None
    return str(user.full_name or user.username)


def _author(
    session: Any,
    kind: str | None,
    author_id: str | None,
    directory: _Directory = _NO_DIRECTORY,
) -> Author | None:
    if kind == "human":
        return Author("human", author_id, _human_name(session, author_id, directory))
    if kind == "agent":
        return Author("agent", author_id)
    if kind == "system":
        return Author("system", author_id)
    return None


def _owner_id(session: Any, channel_id: str, directory: _Directory = _NO_DIRECTORY) -> str | None:
    if channel_id in directory.owners:
        return directory.owners[channel_id]
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


def _read_baseline(external_work: dict[str, Any] | None) -> int:
    return 0 if external_work is None else int(external_work["read_baseline"])


def _unread(
    session: Any,
    channel_id: str,
    member: Member | None,
    external_work: dict[str, Any] | None = None,
) -> int | None:
    """Messages after the member's last read sequence. ``None`` when there
    is no membership to measure against, except for external jobs whose
    durable import baseline applies even before someone joins."""
    if member is None:
        return None
    row = session.get(ChannelMemberRow, (channel_id, member.user.id))
    if row is None and external_work is None:
        return None
    sequence = max(
        _read_baseline(external_work), 0 if row is None else int(row.last_read_sequence or 0)
    )
    statement = (
        select(func.count())
        .select_from(MessageRow)
        .where(MessageRow.channel_id == channel_id, MessageRow.sequence > sequence)
    )
    # Replay can append history to an existing private conversation, or
    # after live progress. Exclude those entries individually in every
    # channel: raising the baseline would also read intervening live
    # messages. Ordinary messages have no historical provenance.
    historical = case(
        (
            func.json_valid(MessageRow.origin_json) == 1,
            func.json_extract(MessageRow.origin_json, "$.historical"),
        ),
        else_=None,
    )
    statement = statement.where(func.coalesce(historical, 0) != 1)
    return int(session.scalar(statement) or 0)


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


def _channel_link(row: ChannelLinkRow) -> ChannelLink:
    return ChannelLink(
        id=str(row.id),
        channel_id=str(row.channel_id),
        backend=str(row.backend),
        surface_id=str(row.surface_id),
        thread_id=None if row.thread_id is None else str(row.thread_id),
        allow_guests=bool(row.allow_guests),
        created_by=None if row.created_by is None else str(row.created_by),
        created_at=float(row.created_at),
        active=bool(row.active),
    )


def _identity(row: ExternalIdentityRow) -> ExternalIdentity:
    return ExternalIdentity(
        backend=str(row.backend),
        external_user_id=str(row.external_user_id),
        user_id=str(row.user_id),
        display_name=None if row.display_name is None else str(row.display_name),
        verified_at=float(row.verified_at),
    )


def _active_link(
    session: Any, backend: str, surface_id: str, thread_id: str | None
) -> ChannelLinkRow | None:
    """The one active link for a surface. ``thread_id`` is matched exactly,
    including its absence, which SQLite's unique index cannot do for NULL."""
    condition = (
        ChannelLinkRow.thread_id.is_(None)
        if thread_id is None
        else ChannelLinkRow.thread_id == thread_id
    )
    found: ChannelLinkRow | None = session.scalars(
        select(ChannelLinkRow)
        .where(
            ChannelLinkRow.backend == backend,
            ChannelLinkRow.surface_id == surface_id,
            condition,
            ChannelLinkRow.active == 1,
        )
        .limit(1)
    ).first()
    return found


def _latest_summary(session: Any, channel_id: str) -> ChannelSummary | None:
    """The newest compaction of a channel's history, if it has one."""
    row = session.scalars(
        select(ChannelSummaryRow)
        .where(ChannelSummaryRow.channel_id == channel_id)
        .order_by(ChannelSummaryRow.through_sequence.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return ChannelSummary(
        channel_id=str(row.channel_id),
        through_sequence=int(row.through_sequence),
        content=str(row.content),
        created_at=float(row.created_at),
    )


def _artifact_ref(row: MessageArtifactRow) -> ArtifactRef:
    return ArtifactRef(
        id=str(row.artifact_id),
        run_id="" if row.run_id is None else str(row.run_id),
        relpath=str(row.relpath),
        media_type=str(row.media_type),
        size=int(row.size),
    )


def _history_line(
    session: Any,
    row: Any,
    files: tuple[ArtifactRef, ...],
    directory: _Directory = _NO_DIRECTORY,
) -> str:
    """One message as a turn's history carries it.

    The single place that shape is written, so what counts against the
    history's character budget and what counts against the compaction
    window's are the same measure.
    """
    line: dict[str, Any] = {
        "seq": int(row.sequence),
        "author_kind": None,
        "author": None,
        "role": str(row.role),
        "kind": str(row.kind),
        "content": str(row.content),
    }
    author = _author(session, row.author_kind, row.author_id, directory)
    if author is None:
        derived = message_author(
            str(row.role),
            str(row.kind),
            None if row.agent_slug is None else str(row.agent_slug),
            _owner_id(session, str(row.channel_id), directory),
        )
        author = _author(session, derived.kind, derived.id, directory) or derived
    line["author_kind"] = author.kind
    line["author"] = author.id
    if files:
        line["artifacts"] = [
            {"id": ref.id, "name": ref.relpath, "media_type": ref.media_type, "size": ref.size}
            for ref in files
        ]
    return json.dumps(line, ensure_ascii=False)


def _attachments(session: Any, message_ids: Sequence[str]) -> dict[str, tuple[ArtifactRef, ...]]:
    """The files each of ``message_ids`` carries, in one read: a channel's
    whole history is projected without a query per message."""
    found: dict[str, list[ArtifactRef]] = {}
    if not message_ids:
        return {}
    rows = session.scalars(
        select(MessageArtifactRow)
        .where(MessageArtifactRow.message_id.in_(list(message_ids)))
        .order_by(MessageArtifactRow.relpath.asc())
    )
    for row in rows:
        found.setdefault(str(row.message_id), []).append(_artifact_ref(row))
    return {key: tuple(value) for key, value in found.items()}


def _message(
    session: Any,
    row: MessageRow,
    attachments: Mapping[str, tuple[ArtifactRef, ...]] | None = None,
    directory: _Directory = _NO_DIRECTORY,
) -> Message:
    agent_slug = None if row.agent_slug is None else str(row.agent_slug)
    work = json.loads(row.work_json) if row.work_json else None
    origin = json.loads(row.origin_json) if row.origin_json else None
    if row.kind == "work_result":
        # Runner results stored before attribution carry no author; they were Angie's.
        agent_slug = agent_slug or ANGIE_SLUG
        if isinstance(work, dict) and work.get("agent_slug") is None:
            work["agent_slug"] = ANGIE_SLUG
    author = _author(session, row.author_kind, row.author_id, directory)
    if author is not None and author.kind == "human" and author.id is None:
        # A guest on a linked surface: no account, so the name they use
        # there is the only one there is, and it rides with the origin.
        author = Author("human", None, _origin_name(origin))
    if author is None:
        # Written by a release that recorded no author.
        derived = message_author(
            str(row.role),
            str(row.kind),
            agent_slug,
            _owner_id(session, str(row.channel_id), directory),
        )
        author = _author(session, derived.kind, derived.id, directory) or derived
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
        artifacts=(
            _attachments(session, [str(row.id)]).get(str(row.id), ())
            if attachments is None
            else attachments.get(str(row.id), ())
        ),
        origin=origin,
        # A kind this build does not know (a later build's) reads as none:
        # one row must never fail the whole channel's message list.
        post_kind=cast(PostKind, str(row.post_kind)) if row.post_kind in POST_KINDS else None,
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
        steered_run_id=None if row.steered_run_id is None else str(row.steered_run_id),
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
    audience: str | None = None,
) -> None:
    """Record a collaboration event. It belongs to the channel its data
    names; ``audience`` is the one user it is for (their own teams,
    preferences, workflows and profile)."""
    channel_id = (data or {}).get("channel_id")
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
            channel_id=channel_id if isinstance(channel_id, str) and channel_id else None,
            audience_user_id=audience,
        )
    )


class CollaborationStore:
    def __init__(self, dstore: DaemonStore) -> None:
        self.dstore = dstore
        #: Called with every message appended to a channel, after commit.
        self._message_observers: list[Callable[[Message], None]] = []
        #: Outstanding bridge identity link codes: code -> (user, expiry).
        self._link_codes: dict[str, tuple[str, float]] = {}
        self._link_code_lock = threading.Lock()

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
                        # The first registration is the operator's own, and an
                        # invite addressed to the email vouches for it. An open
                        # invite vouches for the person, not for the address.
                        email_verified=1 if invite is None or invite.email is not None else 0,
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
                    audience=user_id,
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
        with its name refreshed, and its email too when the provider has
        verified it. Otherwise, with ``link_verified_email``, an unlinked
        local account whose email matches is linked, but only when the
        provider has verified the email and the local account's
        ``email_verified`` is set (its address came from the first
        registration, an addressed invite or an earlier verified claim,
        never from the person editing it). Failing both, a new account is
        provisioned when ``auto_provision`` allows: the installation's first
        user owns the workspace, anyone else takes ``role_from_groups`` or
        ``default_role``. An email another account already holds, or one the
        provider has not verified, is not given to the new account, which
        gets an undeliverable one instead. For an existing member,
        ``role_from_groups`` (when not ``None``) replaces the role, except
        that the last owner is never demoted and the sign-in that links
        never changes it. An inactive user, or one no longer in the
        workspace, is refused.
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
            linked = False
            # An address the provider has not checked is never stored: the
            # account keeps the one it has, or gets an undeliverable one.
            new_email = email if email_verified else None
            if row is None and new_email is not None:
                holder: LocalUserRow | None = session.scalars(
                    select(LocalUserRow).where(LocalUserRow.email == new_email)
                ).first()
                if (
                    holder is not None
                    and link_verified_email
                    and holder.oidc_subject is None
                    and holder.email_verified
                ):
                    # Both sides vouch for the address. The password keeps
                    # working, so the account stays ``local``; the provider
                    # identity is recorded beside it.
                    holder.oidc_issuer = issuer
                    holder.oidc_subject = subject
                    holder.updated_at = now
                    row = holder
                    linked = True
                    _event(session, "auth.oidc.linked", now, data={"user_id": holder.id})
                elif holder is not None:
                    # Not linkable: the person gets an account of their own,
                    # and the address stays with the account that holds it.
                    if link_verified_email and holder.oidc_subject is None:
                        # Its holder typed the address in, so it proves
                        # nothing about who the provider is vouching for.
                        log.info(
                            "auth.oidc_link_refused",
                            user_id=holder.id,
                            reason="local_email_unverified",
                        )
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
                self._refresh_identity(session, row, new_email, full_name, now)
            # Groups are followed from the next sign-in on: the sign-in that
            # links never changes what the linked account may do.
            if (
                not created
                and not linked
                and role_from_groups is not None
                and role_from_groups != member.role
            ):
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
        """Follow the provider's name, and its email when ``email`` is the
        address it has verified (``None`` otherwise)."""
        changed = False
        if email is not None and email != row.email:
            clash = session.scalars(
                select(LocalUserRow.id).where(
                    LocalUserRow.email == email, LocalUserRow.id != row.id
                )
            ).first()
            if clash is None:
                row.email = email
                row.email_verified = 1
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
        # Only an address the provider has verified, and nobody else holds,
        # reaches here; anything else is the placeholder below.
        verified = email is not None
        if email is None:
            # The column is required and unique; a provider that shares no
            # address, or no verified one, gets one that can never receive
            # mail.
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
                email_verified=1 if verified else 0,
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

    def find_user(self, selector: str) -> LocalUser | None:
        """The user a selector names: a user id, or else a username."""
        selector = selector.strip()
        with self.dstore.read() as session:
            row = session.get(LocalUserRow, selector)
            if row is None:
                row = session.scalars(
                    select(LocalUserRow).where(LocalUserRow.username == selector)
                ).first()
            return None if row is None else _user(row)

    # -- merging two accounts --------------------------------------------------------

    def merge_users(
        self, source_id: str, target_id: str, now: float, *, dry_run: bool = False
    ) -> MergeReport:
        """Fold ``source_id`` into ``target_id``, in one immediate transaction.

        This is for one person holding two accounts: typically a local
        account and the one a provider's first sign-in created because it
        shared no verified email to link by. Everything the source made or
        belongs to moves to the target: channel ownership and membership
        (the stronger channel role and the further read position are kept),
        human message and turn authorship, teams, preferences (the target's
        value wins a clash), workflows, agent memories the source authored,
        the invites it created, its bridge identities and the events meant
        for it alone. The target keeps the stronger of the two workspace
        roles.

        The source's provider identity moves onto the target, which keeps
        its username, email, password and ``auth_source``: an account that
        still signs in with a password is ``local`` with a provider identity
        recorded beside it, exactly as an email link leaves it. So both
        sign-in methods reach the target afterwards. The source is then
        deactivated, loses its membership, its provider identity and every
        capability, and its refresh tokens are revoked, as a removed member's
        are. The audit event names the two ids and nothing else.

        Refused: merging a user into itself (``merge_same_user``), a user
        that does not exist (``user_not_found``), an inactive target
        (``merge_target_inactive``), and a target already bound to a
        different provider identity than the source's
        (``merge_identity_conflict``). ``dry_run`` does all of it inside the
        transaction and rolls it back, so the report is exactly what a real
        merge would do and nothing is written.
        """
        try:
            with self.dstore.immediate_transaction() as session:
                report = self._merge(session, source_id, target_id, now, dry_run=dry_run)
                if dry_run:
                    raise _DryRun(report)
                return report
        except _DryRun as unwound:
            return unwound.report

    def _merge(
        self, session: Any, source_id: str, target_id: str, now: float, *, dry_run: bool
    ) -> MergeReport:
        if source_id == target_id:
            raise CollaborationError("merge_same_user", "a user cannot be merged into itself")
        source: LocalUserRow | None = session.get(LocalUserRow, source_id)
        target: LocalUserRow | None = session.get(LocalUserRow, target_id)
        if source is None or target is None:
            missing = source_id if source is None else target_id
            raise CollaborationError("user_not_found", f"no user {missing}")
        if not target.active:
            raise CollaborationError(
                "merge_target_inactive", "the account to merge into is deactivated"
            )
        source_identity = (
            None
            if source.oidc_issuer is None or source.oidc_subject is None
            else (str(source.oidc_issuer), str(source.oidc_subject))
        )
        target_identity = (
            None
            if target.oidc_issuer is None or target.oidc_subject is None
            else (str(target.oidc_issuer), str(target.oidc_subject))
        )
        if target_identity is not None and target_identity != source_identity:
            raise CollaborationError(
                "merge_identity_conflict",
                "the account to merge into already signs in through another provider identity",
            )
        moved: dict[str, int] = {}

        def count(kind: str, result: Any) -> None:
            moved[kind] = moved.get(kind, 0) + int(result.rowcount or 0)

        # Channels the source owns or made.
        count(
            "channels",
            session.execute(
                update(ChannelRow).where(ChannelRow.user_id == source_id).values(user_id=target_id)
            ),
        )
        session.execute(
            update(ChannelRow)
            .where(ChannelRow.created_by == source_id)
            .values(created_by=target_id)
        )
        # Channel memberships: one row per channel, the stronger role and
        # the further read position.
        memberships = 0
        for own in session.scalars(
            select(ChannelMemberRow).where(ChannelMemberRow.user_id == source_id)
        ).all():
            theirs: ChannelMemberRow | None = session.get(
                ChannelMemberRow, (own.channel_id, target_id)
            )
            if theirs is None:
                session.execute(
                    update(ChannelMemberRow)
                    .where(
                        ChannelMemberRow.channel_id == own.channel_id,
                        ChannelMemberRow.user_id == source_id,
                    )
                    .values(user_id=target_id)
                )
            else:
                if _CHANNEL_ROLE_RANK.get(str(own.role), 0) > _CHANNEL_ROLE_RANK.get(
                    str(theirs.role), 0
                ):
                    theirs.role = own.role
                theirs.last_read_sequence = max(
                    int(theirs.last_read_sequence), int(own.last_read_sequence)
                )
                session.delete(own)
            memberships += 1
        session.flush()
        session.expire_all()
        moved["channel_memberships"] = memberships
        session.execute(
            update(ChannelMemberRow)
            .where(ChannelMemberRow.added_by == source_id)
            .values(added_by=target_id)
        )
        session.execute(
            update(ChannelParticipantRow)
            .where(
                ChannelParticipantRow.added_by_kind == "human",
                ChannelParticipantRow.added_by_id == source_id,
            )
            .values(added_by_id=target_id)
        )
        session.execute(
            update(ChannelLinkRow)
            .where(ChannelLinkRow.created_by == source_id)
            .values(created_by=target_id)
        )
        # What the source said.
        count(
            "messages",
            session.execute(
                update(MessageRow)
                .where(MessageRow.author_kind == "human", MessageRow.author_id == source_id)
                .values(author_id=target_id)
            ),
        )
        count(
            "turns",
            session.execute(
                update(TurnRow)
                .where(TurnRow.author_kind == "human", TurnRow.author_id == source_id)
                .values(author_id=target_id)
            ),
        )
        # Teams and workflows are unique per user by slug; a clash moves
        # under a new slug rather than losing either.
        renamed: dict[str, list[tuple[str, str]]] = {"teams": [], "workflows": []}
        for kind, model in (("teams", TeamRow), ("workflows", WorkflowRow)):
            taken = set(session.scalars(select(model.slug).where(model.user_id == target_id)).all())
            rows = session.scalars(select(model).where(model.user_id == source_id)).all()
            for row in rows:
                slug = str(row.slug)
                if slug in taken:
                    new_slug = _free_slug(taken, slug)
                    renamed[kind].append((slug, new_slug))
                    row.slug = new_slug
                    slug = new_slug
                taken.add(slug)
                row.user_id = target_id
                row.updated_at = now
            moved[kind] = len(rows)
        session.flush()
        # Preferences: the target's own answer wins.
        target_names = set(
            session.scalars(
                select(PreferenceRow.name).where(PreferenceRow.user_id == target_id)
            ).all()
        )
        conflicts: list[str] = []
        preferences = 0
        for pref in session.scalars(
            select(PreferenceRow).where(PreferenceRow.user_id == source_id)
        ).all():
            if pref.name in target_names:
                conflicts.append(str(pref.name))
                session.delete(pref)
            else:
                pref.user_id = target_id
                pref.updated_at = now
                preferences += 1
        moved["preferences"] = preferences
        session.flush()
        count(
            "memories",
            session.execute(
                update(AgentMemoryRow)
                .where(AgentMemoryRow.author == f"user:{source_id}")
                .values(author=f"user:{target_id}")
            ),
        )
        count(
            "invites",
            session.execute(
                update(WorkspaceInviteRow)
                .where(WorkspaceInviteRow.created_by == source_id)
                .values(created_by=target_id)
            ),
        )
        session.execute(
            update(WorkspaceMemberRow)
            .where(WorkspaceMemberRow.invited_by == source_id)
            .values(invited_by=target_id)
        )
        count(
            "bridge_identities",
            session.execute(
                update(ExternalIdentityRow)
                .where(ExternalIdentityRow.user_id == source_id)
                .values(user_id=target_id)
            ),
        )
        count(
            "private_events",
            session.execute(
                update(ApiEventRow)
                .where(ApiEventRow.audience_user_id == source_id)
                .values(audience_user_id=target_id)
            ),
        )
        session.expire_all()
        source = session.get(LocalUserRow, source_id)
        target = session.get(LocalUserRow, target_id)
        assert source is not None and target is not None  # nosec B101 - read above
        # Workspace membership: the target keeps the stronger role.
        source_member = self._member_row(session, source_id)
        target_member = self._member_row(session, target_id)
        previous_role: Role | None = (
            None if target_member is None else _role(str(target_member.role))
        )
        candidates = [str(m.role) for m in (source_member, target_member) if m is not None]
        role = _role(max(candidates, key=lambda r: _ROLE_RANK[r]) if candidates else "member")
        if target_member is None:
            session.add(
                WorkspaceMemberRow(
                    workspace_id=WORKSPACE_ID,
                    user_id=target_id,
                    role=role,
                    created_at=now,
                    invited_by=None,
                )
            )
        else:
            target_member.role = role
        if source_member is not None:
            session.delete(source_member)
        session.flush()
        # The provider identity moves: the source's row gives it up first,
        # so the unique index never sees it twice.
        source.oidc_issuer = None
        source.oidc_subject = None
        source.active = 0
        source.updated_at = now
        session.flush()
        if source_identity is not None:
            target.oidc_issuer, target.oidc_subject = source_identity
        target.updated_at = now
        self._grant_role(session, target, role)
        self._grant_role(session, source, None)
        self._revoke_refresh(session, str(source.client_id), now)
        session.flush()
        if not dry_run:
            _event(
                session,
                "collaboration.user.merged",
                now,
                data={"source_user_id": source_id, "target_user_id": target_id},
            )
        return MergeReport(
            source_id=source_id,
            source_username=str(source.username),
            target_id=target_id,
            target_username=str(target.username),
            dry_run=dry_run,
            moved=moved,
            preference_conflicts=tuple(conflicts),
            renamed_teams=tuple(renamed["teams"]),
            renamed_workflows=tuple(renamed["workflows"]),
            identity=source_identity,
            previous_role=previous_role,
            role=role,
        )

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
                if email is not None and email.strip().casefold() != row.email:
                    # Typed in by its holder: nobody has shown the address is
                    # theirs, so no provider identity may be linked to it.
                    row.email = email.strip().casefold()
                    row.email_verified = 0
                if full_name is not None:
                    row.full_name = full_name.strip() or None
                if timezone is not None:
                    row.timezone = timezone.strip() or "UTC"
                row.updated_at = now
                session.flush()
                _event(
                    session,
                    "collaboration.user.updated",
                    now,
                    data={"user_id": row.id},
                    audience=str(row.id),
                )
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
            metadata = external_metadata(session, channel_id)
            return _channel(
                row,
                _my_role(session, channel_id, member),
                _unread(session, channel_id, member, metadata),
                metadata,
            )

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
            channels = []
            for row, role in rows:
                metadata = external_metadata(session, str(row.id))
                channels.append(
                    _channel(
                        row,
                        None if role is None else ("owner" if role == "owner" else "member"),
                        _unread(session, str(row.id), member, metadata),
                        metadata,
                    )
                )
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
            metadata = external_metadata(session, channel_id)
            return _channel(
                row,
                _my_role(session, channel_id, member),
                _unread(session, channel_id, member, metadata),
                metadata,
            )

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
        *,
        added_by: Author | None = None,
    ) -> ChannelParticipant:
        """Add an agent to the channel or change how it takes part. ``values``
        holds only the fields the caller set (``mode``, ``muted_until``).
        ``added_by`` credits a caller that is not a person: the agent whose
        reply addressed this one."""
        mode = values.get("mode")
        if mode is not None and mode not in {"mention", "ambient"}:
            raise CollaborationError("invalid_participant", "mode is mention or ambient")
        with self.dstore.transaction() as session:
            _, member = _access(session, channel_id, viewer, "post", now=now)
            author = Author("human", member.user.id) if member is not None else added_by
            row = session.get(ChannelParticipantRow, (channel_id, agent_slug))
            if row is None:
                row = ChannelParticipantRow(
                    channel_id=channel_id,
                    agent_slug=agent_slug,
                    mode=mode or "mention",
                    added_by_kind=None if author is None else author.kind,
                    added_by_id=None if author is None else author.id,
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

    # -- bridge links and external identities --------------------------------------

    def add_message_observer(self, observer: Callable[[Message], None]) -> None:
        """Hear every message appended to a channel, after it is committed.

        The mirror that posts channel traffic out to linked bridge surfaces
        is one; an observer that raises is logged and never fails the write.
        """
        self._message_observers.append(observer)

    def _appended(self, message: Message | None) -> Message | None:
        """Tell the observers about ``message``; returns it, so a caller can
        end with ``return self._appended(...)``."""
        if message is not None:
            for observer in tuple(self._message_observers):
                try:
                    observer(message)
                except Exception:
                    log.warning("collaboration.message_observer_failed", exc_info=True)
        return message

    def list_channel_links(self, viewer: Viewer, channel_id: str) -> list[ChannelLink]:
        with self.dstore.read() as session:
            _access(session, channel_id, viewer, "manage")
            rows = session.scalars(
                select(ChannelLinkRow)
                .where(ChannelLinkRow.channel_id == channel_id, ChannelLinkRow.active == 1)
                .order_by(ChannelLinkRow.created_at.asc())
            )
            return [_channel_link(row) for row in rows]

    def create_channel_link(
        self,
        viewer: Viewer,
        channel_id: str,
        *,
        backend: str,
        surface_id: str,
        thread_id: str | None,
        allow_guests: bool,
        created_by: str | None,
        now: float,
    ) -> ChannelLink:
        """Link a bridge surface to the channel.

        Takes managing the channel *and* administering the workspace: a link
        makes the channel capture what everyone on that surface says and post
        its own traffic there, which reaches past the channel. A run's thread
        belongs to its run and is refused.
        """
        if backend == "discord" and thread_id is not None:
            # A Discord thread is a channel of its own: messages typed there
            # arrive with the thread's id as their channel, and a post to it
            # goes to that id. So the thread is the surface.
            surface_id, thread_id = thread_id, None
        # A run's thread is recorded once, when the run opens it, so reading
        # it ahead of the write transaction races nothing that matters.
        run_thread = self.dstore.run_for_thread(thread_id or surface_id, backend) is not None
        with self.dstore.immediate_transaction() as session:
            _, member = _access(session, channel_id, viewer, "manage", now=now)
            if member is not None and member.role not in MANAGING_ROLES:
                raise CollaborationError(
                    "channel_forbidden", "linking a surface takes a workspace admin"
                )
            if run_thread:
                raise CollaborationError(
                    "link_run_thread", "that surface is a run's thread and cannot be linked"
                )
            if _active_link(session, backend, surface_id, thread_id) is not None:
                raise CollaborationError(
                    "link_exists", "that surface is already linked to a channel"
                )
            if thread_id is not None:
                # A retired link to this thread would trip the unique index
                # (NULL threads never do). Nothing reads a retired row, since
                # each message carries its own origin, so it gives way.
                session.execute(
                    delete(ChannelLinkRow).where(
                        ChannelLinkRow.backend == backend,
                        ChannelLinkRow.surface_id == surface_id,
                        ChannelLinkRow.thread_id == thread_id,
                        ChannelLinkRow.active == 0,
                    )
                )
            link_id = "lnk_" + _token(16)
            session.execute(
                insert(ChannelLinkRow).values(
                    id=link_id,
                    channel_id=channel_id,
                    backend=backend,
                    surface_id=surface_id,
                    thread_id=thread_id,
                    allow_guests=1 if allow_guests else 0,
                    created_by=created_by,
                    created_at=now,
                    active=1,
                )
            )
            _event(
                session,
                "collaboration.link.added",
                now,
                data={"channel_id": channel_id, "link_id": link_id, "backend": backend},
            )
            row = session.get(ChannelLinkRow, link_id)
            assert row is not None  # nosec B101 - just inserted
            return _channel_link(row)

    def delete_channel_link(
        self, viewer: Viewer, channel_id: str, link_id: str, now: float
    ) -> None:
        """Retire a link. The row stays, inactive, so the messages that named
        the surface keep an origin that can still be read back."""
        with self.dstore.immediate_transaction() as session:
            _access(session, channel_id, viewer, "manage", now=now)
            row = session.get(ChannelLinkRow, link_id)
            if row is None or str(row.channel_id) != channel_id or not row.active:
                raise CollaborationError("link_not_found", "link not found")
            row.active = 0
            _event(
                session,
                "collaboration.link.removed",
                now,
                data={"channel_id": channel_id, "link_id": link_id, "backend": str(row.backend)},
            )

    def link_for_surface(
        self, backend: str, surface_id: str, thread_id: str | None = None
    ) -> ChannelLink | None:
        """The active link for a surface, or None when it is not linked."""
        with self.dstore.read() as session:
            row = _active_link(session, backend, surface_id, thread_id)
            return None if row is None else _channel_link(row)

    def create_link_code(self, user_id: str, now: float) -> tuple[str, float]:
        """A short code the person types on a bridge to prove who they are.

        Codes live in this process only: they are single use and expire in
        minutes, so a daemon restart costs one retyped code rather than a
        table of half-finished identities.
        """
        expires_at = now + LINK_CODE_TTL_S
        code = _token(8)
        with self._link_code_lock:
            self._link_codes = {
                value: pending for value, pending in self._link_codes.items() if pending[1] > now
            }
            self._link_codes[code] = (user_id, expires_at)
        return code, expires_at

    def redeem_link_code(
        self,
        code: str,
        *,
        backend: str,
        external_user_id: str,
        display_name: str | None,
        now: float,
    ) -> ExternalIdentity | None:
        """Spend a code: map this bridge account to the user who asked for
        it. An unknown, spent or expired code maps nothing."""
        with self._link_code_lock:
            pending = self._link_codes.pop(code.strip(), None)
        if pending is None or pending[1] <= now:
            return None
        return self.link_identity(
            pending[0],
            backend=backend,
            external_user_id=external_user_id,
            display_name=display_name,
            now=now,
        )

    def link_identity(
        self,
        user_id: str,
        *,
        backend: str,
        external_user_id: str,
        display_name: str | None,
        now: float,
    ) -> ExternalIdentity:
        """Map a bridge account to a local one, replacing any earlier map."""
        with self.dstore.immediate_transaction() as session:
            row = session.get(ExternalIdentityRow, (backend, external_user_id))
            if row is None:
                row = ExternalIdentityRow(
                    backend=backend,
                    external_user_id=external_user_id,
                    user_id=user_id,
                    display_name=display_name,
                    verified_at=now,
                )
                session.add(row)
            else:
                row.user_id = user_id
                row.display_name = display_name
                row.verified_at = now
            return _identity(row)

    def identity_user(self, backend: str, external_user_id: str) -> str | None:
        """The local user a bridge account belongs to, or None.

        A map outlives the membership it was made under, so the membership
        is what answers: a user who has been removed from the workspace or
        deactivated is no longer anybody here, exactly as ``_resolve`` has
        it. The caller then treats the author as unmapped and the link's
        own ``allow_guests`` rule decides what happens to the message.
        """
        with self.dstore.read() as session:
            row = session.get(ExternalIdentityRow, (backend, external_user_id))
            if row is None:
                return None
            member = _member_in(session, str(row.user_id))
            if member is None or not member.user.active:
                return None
            return str(row.user_id)

    def list_identities(self, user_id: str) -> list[ExternalIdentity]:
        with self.dstore.read() as session:
            rows = session.scalars(
                select(ExternalIdentityRow)
                .where(ExternalIdentityRow.user_id == user_id)
                .order_by(ExternalIdentityRow.backend.asc())
            )
            return [_identity(row) for row in rows]

    def unlink_identity(self, user_id: str, backend: str) -> bool:
        with self.dstore.immediate_transaction() as session:
            rows = session.scalars(
                select(ExternalIdentityRow).where(
                    ExternalIdentityRow.user_id == user_id,
                    ExternalIdentityRow.backend == backend,
                )
            ).all()
            for row in rows:
                session.delete(row)
            return bool(rows)

    # -- agent follow-ups, silence and read state -----------------------------------

    def message_content(self, message_id: str) -> str | None:
        """One message's text by id: what a follow-up turn is answering."""
        with self.dstore.read() as session:
            row = session.get(MessageRow, message_id)
            return None if row is None else str(row.content)

    def get_message(self, channel_id: str, message_id: str) -> Message | None:
        """One message of a channel, read for the daemon rather than for a
        viewer: the subject an ambient decision is made about."""
        with self.dstore.read() as session:
            row = session.get(MessageRow, message_id)
            if row is None or str(row.channel_id) != channel_id:
                return None
            return _message(session, row)

    def silenced_until(self, channel_id: str) -> float | None:
        """When the channel's silence lifts, or ``None``. Read by the
        guardrails, which run for the daemon and not for a viewer."""
        with self.dstore.read() as session:
            row = session.get(ChannelRow, channel_id)
            if row is None or row.silenced_until is None:
                return None
            return float(row.silenced_until)

    def may_post(self, viewer: Viewer, channel_id: str, now: float) -> bool:
        """Whether ``viewer`` may post in the channel, by the same check
        :meth:`set_silence` and a new turn make: the rule a channel stop
        answers to, from chat as from ``POST /v1/channels/{id}/stop``."""
        with self.dstore.transaction() as session:
            try:
                _access(session, channel_id, viewer, "post", now=now)
            except CollaborationError:
                return False
            return True

    def set_silence(
        self, viewer: Viewer, channel_id: str, until: float | None, now: float
    ) -> Channel | None:
        """Silence the channel until ``until`` (``None`` lifts it). Anyone
        who may post may quiet the agents in a channel they are in."""
        with self.dstore.transaction() as session:
            try:
                row, member = _access(session, channel_id, viewer, "post", now=now)
            except CollaborationError as exc:
                if exc.code == "channel_not_found":
                    return None
                raise
            row.silenced_until = None if until is None else float(until)
            row.updated_at = now
            row.revision += 1
            _event(
                session,
                "collaboration.channel.silenced"
                if until is not None
                else "collaboration.channel.resumed",
                now,
                data={"channel_id": channel_id, "silenced_until": row.silenced_until},
            )
            metadata = external_metadata(session, channel_id)
            return _channel(
                row,
                _my_role(session, channel_id, member),
                _unread(session, channel_id, member, metadata),
                metadata,
            )

    def set_read_sequence(
        self, viewer: Viewer, channel_id: str, sequence: int, now: float
    ) -> ChannelMember | None:
        """Record how far the reader has read. The sequence only moves
        forward, so a stale client cannot un-read a channel."""
        with self.dstore.transaction() as session:
            _, member = _access(session, channel_id, viewer, "post", now=now)
            if member is None:
                return None
            entry = session.get(ChannelMemberRow, (channel_id, member.user.id))
            if entry is None:
                return None
            newest = session.scalar(
                select(func.max(MessageRow.sequence)).where(MessageRow.channel_id == channel_id)
            )
            baseline = _read_baseline(external_metadata(session, channel_id))
            capped = min(max(int(sequence), baseline), int(newest or 0))
            entry.last_read_sequence = max(int(entry.last_read_sequence or 0), capped)
            _event(
                session,
                "collaboration.channel.read",
                now,
                data={
                    "channel_id": channel_id,
                    "user_id": member.user.id,
                    "sequence": entry.last_read_sequence,
                },
                audience=member.user.id,
            )
            user = session.get(LocalUserRow, member.user.id)
            assert user is not None  # nosec B101 - membership invariant
            return _channel_member(entry, user)

    def agent_turns_since(self, channel_id: str, since: float) -> list[AgentTurnRecord]:
        """The channel's agent-started turns created at or after ``since``,
        oldest first: what the rate caps and the pair cooldown count. A turn
        a person asked for is not one of them, whoever it addresses."""
        with self.dstore.read() as session:
            rows = session.scalars(
                select(TurnRow)
                .where(
                    TurnRow.channel_id == channel_id,
                    TurnRow.trigger.in_(("mention", "ambient")),
                    TurnRow.created_at >= since,
                )
                .order_by(TurnRow.created_at.asc())
            )
            return [
                AgentTurnRecord(
                    turn_id=str(row.id),
                    source_slug=None if row.author_id is None else str(row.author_id),
                    targets=tuple(str(v) for v in json.loads(row.targets_json or "[]")),
                    trigger=str(row.trigger or "human"),
                    created_at=float(row.created_at),
                )
                for row in rows
            ]

    def ambient_turns_since(self, channel_id: str, agent_slug: str, since: float) -> int:
        """How often ``agent_slug`` has spoken unprompted in the channel at
        or after ``since``: what ``ambient_max_per_hour`` counts."""
        with self.dstore.read() as session:
            rows = session.scalars(
                select(TurnRow.targets_json).where(
                    TurnRow.channel_id == channel_id,
                    TurnRow.trigger == "ambient",
                    TurnRow.created_at >= since,
                )
            )
            return sum(1 for value in rows if agent_slug in json.loads(value or "[]"))

    def recent_messages(self, channel_id: str, limit: int) -> list[Message]:
        """The channel's newest messages, oldest first: the window an
        ambient decision reads."""
        with self.dstore.read() as session:
            rows = list(
                session.scalars(
                    select(MessageRow)
                    .where(MessageRow.channel_id == channel_id)
                    .order_by(MessageRow.sequence.desc())
                    .limit(max(1, limit))
                )
            )
            return [_message(session, row) for row in reversed(rows)]

    def record_followup_decision(
        self,
        channel_id: str,
        *,
        source_slug: str | None,
        target_slug: str,
        trigger: str,
        depth: int,
        admission: Any,
        now: float,
    ) -> None:
        """Audit one guardrail decision. The reason travels; the message
        that caused it never does."""
        with self.dstore.transaction() as session:
            _event(
                session,
                "collaboration.followup.queued"
                if admission.ok
                else "collaboration.followup.suppressed",
                now,
                data={
                    "channel_id": channel_id,
                    "agent_slug": target_slug,
                    "source_agent_slug": source_slug,
                    "trigger": trigger,
                    "chain_depth": depth,
                    "reason": admission.reason,
                    "retry_at": admission.retry_at,
                },
            )

    def accept_agent_turn(
        self,
        channel_id: str,
        source_message_id: str,
        *,
        author: Author,
        targets: tuple[str, ...],
        trigger: str,
        parent_turn_id: str | None,
        chain_depth: int,
        now: float,
    ) -> Turn | None:
        """Accept a turn one agent started by addressing another.

        No new message is appended: the agent's own reply, named by
        ``source_message_id``, is the turn's input. ``None`` when the
        channel is gone.
        """
        with self.dstore.immediate_transaction() as session:
            channel = session.get(ChannelRow, channel_id)
            if channel is None or channel.state != "active":
                return None
            turn_id = "trn_" + _token(16)
            session.execute(
                insert(TurnRow).values(
                    id=turn_id,
                    channel_id=channel_id,
                    client_turn_id=None,
                    input_message_id=source_message_id,
                    status="accepted",
                    targets_json=json.dumps(list(targets)),
                    intent="conversation",
                    participants_json=json.dumps(
                        [
                            {
                                "agent_slug": target,
                                "status": "queued",
                                "error": None,
                                "read_only": target == "critic",
                            }
                            for target in targets
                        ]
                    ),
                    created_at=now,
                    author_kind=author.kind,
                    author_id=author.id,
                    trigger=trigger,
                    parent_turn_id=parent_turn_id,
                    source_message_id=source_message_id,
                    chain_depth=chain_depth,
                )
            )
            channel.updated_at = now
            channel.revision += 1
            row = session.get(TurnRow, turn_id)
            assert row is not None  # nosec B101 - just inserted
            _event(
                session,
                "collaboration.turn.accepted",
                now,
                data={
                    "channel_id": channel_id,
                    "turn_id": turn_id,
                    "targets": list(targets),
                    "trigger": trigger,
                },
            )
            return _turn(session, row)

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
            rows = list(
                session.scalars(
                    select(MessageRow)
                    .where(MessageRow.channel_id == channel_id, MessageRow.sequence > after)
                    .order_by(MessageRow.sequence.asc())
                )
            )
            attachments = _attachments(session, [str(row.id) for row in rows])
            known = _directory(session, rows)
            return [_message(session, row, attachments, known) for row in rows]

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
        assignees: dict[str, str] | None = None,
    ) -> tuple[Turn, Message, bool]:
        """Append the user message and accepted turn atomically.

        A repeated client turn id returns the original resources. A reused id
        with different text is rejected instead of silently changing meaning.
        ``participants`` are the agents the message mentions: each one not in
        the channel yet joins it, answering when mentioned. ``assignees`` are
        the run roles those mentions declare on a turn that may start managed
        work; they ride the first participant slot, where admission reads them.
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
                                **(
                                    {"assignees": dict(assignees)}
                                    if assignees and index == 0
                                    else {}
                                ),
                            }
                            for index, target in enumerate(targets or (None,))
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
            accepted = (_turn(session, turn_row), _message(session, message_row), True)
        self._appended(accepted[1])
        return accepted

    def accept_linked_turn(
        self,
        link: ChannelLink,
        *,
        content: str,
        author_user_id: str | None,
        display_name: str | None,
        external_message_id: str,
        now: float,
        targets: tuple[str, ...] = (),
        participants: tuple[str, ...] = (),
    ) -> tuple[Turn, Message]:
        """Append a message that arrived on a linked bridge surface, and the
        turn that answers it.

        The link is the authorization: whoever may post on the surface the
        channel's owner linked posts here, with no ``post`` check against
        the channel, and :meth:`recover_turns` honours the same rule after
        a restart. A mapped author is credited to their account; a guest
        (only where the link admits one) is a human with no account, named
        by the handle they use on that service. ``targets`` are the agents
        the message addresses, as :meth:`accept_turn` records them, and
        ``participants`` the agents it mentions: each joins the channel.
        """
        origin = bridge_origin(
            link.backend,
            link.surface_id,
            external_message_id,
            None if author_user_id else display_name,
        )
        with self.dstore.immediate_transaction() as session:
            channel = session.get(ChannelRow, link.channel_id)
            if channel is None or channel.state != "active":
                raise CollaborationError("channel_not_found", "channel not found")
            turn_id = "trn_" + _token(16)
            message_id = "msg_" + _token(16)
            session.execute(
                insert(MessageRow).values(
                    id=message_id,
                    channel_id=link.channel_id,
                    turn_id=turn_id,
                    sequence=self._next_sequence(session, link.channel_id),
                    role="user",
                    kind="message",
                    content=content,
                    reactions_json=json.dumps(["⏳"]),
                    created_at=now,
                    author_kind="human",
                    author_id=author_user_id,
                    origin_json=json.dumps(origin),
                )
            )
            session.execute(
                insert(TurnRow).values(
                    id=turn_id,
                    channel_id=link.channel_id,
                    input_message_id=message_id,
                    status="accepted",
                    targets_json=json.dumps(list(targets)),
                    intent="conversation",
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
                    author_id=author_user_id,
                    trigger="human",
                    chain_depth=0,
                )
            )
            for slug in dict.fromkeys(participants):
                if session.get(ChannelParticipantRow, (link.channel_id, slug)) is None:
                    session.add(
                        ChannelParticipantRow(
                            channel_id=link.channel_id,
                            agent_slug=slug,
                            mode="mention",
                            added_by_kind="human",
                            added_by_id=author_user_id,
                            created_at=now,
                        )
                    )
                    _event(
                        session,
                        "collaboration.participant.added",
                        now,
                        data={"channel_id": link.channel_id, "agent_slug": slug},
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
                data={"channel_id": link.channel_id, "turn_id": turn_id, "targets": list(targets)},
            )
            _event(
                session,
                "collaboration.message.created",
                now,
                data={
                    "channel_id": link.channel_id,
                    "message_id": message_id,
                    "sequence": message_row.sequence,
                    "author_kind": "human",
                    "author_id": author_user_id,
                },
            )
            accepted = (_turn(session, turn_row), _message(session, message_row))
        self._appended(accepted[1])
        return accepted

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
            appended = _message(session, row)
        return self._appended(appended)

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
        artifacts: Sequence[Mapping[str, Any]] = (),
    ) -> Message | None:
        """Append one server-owned work result, idempotently.

        ``artifacts`` are the files the run delivered, as the work snapshot
        names them. They are attached in the same transaction as the
        message, so a result never exists without the files it announced.
        """
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
            self._attach_artifacts(session, message_id, channel_id, artifacts, now)
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
            delivered = _message(session, row)
        return self._appended(delivered)

    @staticmethod
    def _attach_artifacts(
        session: Any,
        message_id: str,
        channel_id: str,
        artifacts: Sequence[Mapping[str, Any]],
        now: float,
    ) -> None:
        """Record the files a message carries, inside the caller's
        transaction. A file named twice on one message is one row."""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for artifact in artifacts:
            artifact_id = artifact.get("id")
            relpath = artifact.get("relpath")
            if not isinstance(artifact_id, str) or not isinstance(relpath, str):
                continue
            if artifact_id in seen:
                continue
            seen.add(artifact_id)
            run_id = artifact.get("run_id")
            size = artifact.get("size")
            rows.append(
                {
                    "message_id": message_id,
                    "artifact_id": artifact_id,
                    "channel_id": channel_id,
                    "run_id": run_id if isinstance(run_id, str) else None,
                    "relpath": relpath,
                    "media_type": str(artifact.get("media_type") or "application/octet-stream"),
                    "size": size if isinstance(size, int) and not isinstance(size, bool) else 0,
                    "created_at": now,
                }
            )
        if rows:
            session.execute(insert(MessageArtifactRow).prefix_with("OR IGNORE"), rows)

    def channel_artifacts(self, viewer: Viewer, channel_id: str) -> list[ArtifactRef] | None:
        """Every file the channel's messages carry, newest message first;
        ``None`` when the channel is not this viewer's to read."""
        with self.dstore.read() as session:
            try:
                _access(session, channel_id, viewer, "read")
            except CollaborationError:
                return None
            rows = session.scalars(
                select(MessageArtifactRow)
                .join(MessageRow, MessageRow.id == MessageArtifactRow.message_id)
                .where(MessageArtifactRow.channel_id == channel_id)
                .order_by(MessageRow.sequence.desc(), MessageArtifactRow.relpath.asc())
            )
            found: dict[str, ArtifactRef] = {}
            for row in rows:
                found.setdefault(str(row.artifact_id), _artifact_ref(row))
            return list(found.values())

    def artifact_attached(self, channel_id: str, artifact_id: str) -> bool:
        """Whether a message in this channel carries that file."""
        with self.dstore.read() as session:
            return (
                session.scalars(
                    select(MessageArtifactRow.artifact_id)
                    .where(
                        MessageArtifactRow.channel_id == channel_id,
                        MessageArtifactRow.artifact_id == artifact_id,
                    )
                    .limit(1)
                ).first()
                is not None
            )

    def channel_owns_run(self, channel_id: str, run_id: str) -> bool:
        """Whether a work item admitted for this channel produced ``run_id``.

        A run linked to the channel is part of its shared context even
        before its result message lands, so its files are readable there.
        """
        with self.dstore.read() as session:
            return (
                session.scalars(
                    select(WorkItemRow.item_id)
                    .where(WorkItemRow.channel_id == channel_id, WorkItemRow.run_id == run_id)
                    .limit(1)
                ).first()
                is not None
            )

    def put_channel_summary(
        self, channel_id: str, through_sequence: int, content: str, now: float
    ) -> ChannelSummary:
        """Record what the channel said up to ``through_sequence``.

        Each summary continues the one before it, so only the newest is
        ever read: the channel's older rows are deleted in the same
        transaction rather than kept forever.
        """
        with self.dstore.immediate_transaction() as session:
            session.execute(
                delete(ChannelSummaryRow).where(
                    ChannelSummaryRow.channel_id == channel_id,
                    ChannelSummaryRow.through_sequence < through_sequence,
                )
            )
            row = session.get(ChannelSummaryRow, (channel_id, through_sequence))
            if row is None:
                session.execute(
                    insert(ChannelSummaryRow).values(
                        channel_id=channel_id,
                        through_sequence=through_sequence,
                        content=content,
                        created_at=now,
                    )
                )
            else:
                row.content = content
                row.created_at = now
        return ChannelSummary(channel_id, through_sequence, content, now)

    def latest_channel_summary(self, channel_id: str) -> ChannelSummary | None:
        with self.dstore.read() as session:
            return _latest_summary(session, channel_id)

    def summary_backlog(
        self, channel_id: str, *, keep: int = HISTORY_MESSAGES, max_chars: int = HISTORY_CHARS
    ) -> tuple[int, str | None, str] | None:
        """What a compaction job has to summarise, or ``None`` when nothing
        has fallen out of the channel's history window yet.

        Returns the last sequence covered, the previous summary (so the new
        one continues it) and the transcript of the messages that fell out,
        oldest first and bounded by ``max_chars``.

        The window is the one :meth:`turn_history` keeps: ``keep`` messages
        *and* ``max_chars`` characters, whichever binds first. A channel far
        under the message cap still trims on the character budget, and a
        summary has to exist for the trimmed history to open with.

        The sequence returned is the last one the transcript actually
        carries, never the last one that fell out: a backlog too big for one
        excerpt is summarised over several compactions instead of having its
        tail marked covered without ever being read.
        """
        with self.dstore.read() as session:
            newest = list(
                session.scalars(
                    select(MessageRow)
                    .where(MessageRow.channel_id == channel_id)
                    .order_by(MessageRow.sequence.desc())
                    .limit(keep)
                )
            )
            carried = _attachments(session, [str(message.id) for message in newest])
            known = _directory(session, newest)
            kept: list[int] = []
            held = 0
            for message in newest:
                line = _history_line(session, message, carried.get(str(message.id), ()), known)
                held += len(line) + 1
                if kept and held > max_chars:
                    break
                kept.append(int(message.sequence))
            if not kept:
                return None
            previous = _latest_summary(session, channel_id)
            covered = 0 if previous is None else previous.through_sequence
            rows = list(
                session.scalars(
                    select(MessageRow)
                    .where(
                        MessageRow.channel_id == channel_id,
                        MessageRow.sequence < min(kept),
                        MessageRow.sequence > covered,
                    )
                    .order_by(MessageRow.sequence.asc())
                )
            )
            if not rows:
                return None
            through: int | None = None
            lines: list[str] = []
            spent = 0
            for row in rows:
                author = _author(session, row.author_kind, row.author_id)
                who = (author.display_name or author.id or author.kind) if author else str(row.role)
                line = f"{who}: {' '.join(str(row.content).split())}"
                if lines and spent + len(line) > max_chars:
                    break
                # One message longer than the whole budget is cut rather
                # than refused: the watermark has to be able to move past it.
                lines.append(line[:max_chars])
                spent += len(lines[-1]) + 1
                through = int(row.sequence)
            if through is None:
                return None
            return through, None if previous is None else previous.content, "\n".join(lines)

    def member_reads_run(self, member: Member, run_id: str) -> bool:
        """Whether ``member`` may read what run ``run_id`` (internal id)
        produced: the run was asked for by a channel they can read. A run
        no channel asked for, or whose channel is gone, is not theirs."""
        with self.dstore.read() as session:
            channel_id = channel_for_run(session, run_id)
            if channel_id is None:
                return False
            try:
                _access(session, channel_id, member, "read")
            except CollaborationError:
                return False
            return True

    def turn_for_post(
        self,
        channel_id: str,
        reply_to_message_id: str | None,
        item_id: str | None = None,
    ) -> str | None:
        """The turn a run's post belongs to: the turn that asked the message
        it answers when that message is in this channel, and otherwise the
        turn that asked for its work, by the identities the work's result
        is delivered on. A channel with no such turn has nowhere to hang
        the post, and it hangs on none."""
        with self.dstore.read() as session:
            if reply_to_message_id is not None:
                message = session.get(MessageRow, reply_to_message_id)
                if (
                    message is not None
                    and message.turn_id is not None
                    and str(message.channel_id) == channel_id
                ):
                    return str(message.turn_id)
            return None if item_id is None else turn_for_item(session, item_id, channel_id)

    def post_agent_update(
        self,
        *,
        channel_id: str,
        author_agent: str,
        kind: PostKind,
        text: str,
        run_id: str,
        dedupe_key: str,
        now: float,
        turn_id: str | None = None,
        work: dict[str, Any] | None = None,
    ) -> str | None:
        """Record one post a run made, idempotently on ``dedupe_key``.

        Returns the message id, or None when the channel is gone or
        silenced. A silenced channel still hears a run that has finished
        or stopped: ``delivery`` and ``notice`` are posted anyway.
        """
        if kind not in POST_KINDS:
            # ``PostKind`` is a type, not a check: a caller naming a kind
            # this build does not know gets nothing stored, and its key
            # stays free for a post that is readable.
            log.warning(
                "api.channel_post_unknown_kind", channel=channel_id, key=dedupe_key, kind=kind
            )
            return None
        with self.dstore.immediate_transaction() as session:
            posted = session.get(ChannelRunPostRow, dedupe_key)
            if posted is not None:
                return str(posted.message_id)
            channel = session.get(ChannelRow, channel_id)
            if channel is None or channel.state != "active":
                return None
            if turn_id is not None:
                # A post hangs on a turn of its own channel or on none:
                # another channel's turn would carry it to its members.
                turn = session.get(TurnRow, turn_id)
                if turn is None or str(turn.channel_id) != channel_id:
                    turn_id = None
                    if work is not None and "turn_id" in work:
                        work = {**work, "turn_id": None}
            silenced = channel.silenced_until is not None and float(channel.silenced_until) > now
            if silenced and kind not in TERMINAL_POST_KINDS:
                return None
            message_id = "msg_" + _token(16)
            session.execute(
                insert(MessageRow).values(
                    id=message_id,
                    channel_id=channel_id,
                    turn_id=turn_id,
                    sequence=self._next_sequence(session, channel_id),
                    role="assistant",
                    kind="agent_update",
                    content=text,
                    agent_slug=author_agent,
                    work_json=None if work is None else json.dumps(work, default=str),
                    created_at=now,
                    author_kind="agent",
                    author_id=author_agent,
                    post_kind=kind,
                )
            )
            session.execute(
                insert(ChannelRunPostRow).values(
                    dedupe_key=dedupe_key,
                    run_id=run_id,
                    message_id=message_id,
                    kind=kind,
                    posted_at=now,
                )
            )
            channel.updated_at = now
            channel.revision += 1
            row = session.get(MessageRow, message_id)
            assert row is not None  # nosec B101 - just inserted
            _event(
                session,
                "collaboration.message.created",
                now,
                data={
                    "channel_id": channel_id,
                    "turn_id": turn_id,
                    "message_id": message_id,
                    "sequence": row.sequence,
                    "agent_slug": author_agent,
                    "author_kind": "agent",
                    "author_id": author_agent,
                    "post_kind": kind,
                    "run_id": run_public_id(run_id) if run_id else None,
                },
            )
            return message_id

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

    def record_steered_run(self, turn_id: str, run_id: str, now: float) -> None:
        """Note that this turn steered ``run_id`` rather than answering from
        scratch (S-A11), so a client can show the mention as direction to
        work already in flight."""
        with self.dstore.immediate_transaction() as session:
            row = session.get(TurnRow, turn_id)
            if row is not None:
                row.steered_run_id = run_id

    def finish_turn(self, turn_id: str, *, error: str | None, now: float) -> Turn | None:
        # A turn that ends badly leaves the only answer the asker gets, and
        # a linked surface hears about it the same way it hears about a
        # reply: through the observers, once the write has committed.
        appended: Message | None = None
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
                message_row = session.get(MessageRow, message_id)
                if message_row is not None:
                    appended = _message(session, message_row)
            session.flush()
            _event(
                session,
                f"collaboration.turn.{row.status}",
                now,
                data={"channel_id": row.channel_id, "turn_id": row.id, "error": error},
            )
            finished = _turn(session, row)
        self._appended(appended)
        return finished

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
                message = session.get(MessageRow, row.input_message_id)
                stored_origin = message.origin_json if message is not None else None
                origin = json.loads(stored_origin) if stored_origin else None
                if row.author_kind == "human" and row.author_id is None and origin:
                    # A guest on a linked surface: the turn runs for the same
                    # stand-in it would have run for live, never for the
                    # channel's owner, whose identity would answer a stranger.
                    if channel and channel.state == "active" and message:
                        queued.append(
                            (
                                message.sequence,
                                _turn(session, row),
                                guest_user(_origin_name(origin)),
                                message.content,
                            )
                        )
                    else:
                        interrupted.append((row.id, False))
                    continue
                if row.author_kind == "human" and row.author_id is not None and origin:
                    # A mapped author on a linked surface: the link was the
                    # authorization when the turn was accepted, with no
                    # channel access check, and a restart keeps that rule.
                    # The membership behind the map still has to hold, as
                    # it did when the bridge mapped them.
                    author = _member_in(session, row.author_id)
                    if (
                        channel is not None
                        and channel.state == "active"
                        and message is not None
                        and author is not None
                        and author.user.active
                    ):
                        queued.append(
                            (message.sequence, _turn(session, row), author.user, message.content)
                        )
                    else:
                        interrupted.append((row.id, False))
                    continue
                author_id = row.author_id if row.author_kind == "human" else None
                user_id = author_id or (channel.user_id if channel else None)
                user = session.get(LocalUserRow, user_id) if user_id else None
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

    def turn_history(self, turn: Turn, *, max_chars: int = HISTORY_CHARS) -> str:
        """Prior turns and completed peers in this turn, without future input.

        One JSON object per line, oldest last written first: the sequence,
        who wrote it, the message kind, the text and the files it carried.
        Bounded to :data:`HISTORY_MESSAGES` messages and ``max_chars``
        characters; when anything was dropped the channel's latest summary
        opens the history as a ``channel_summary`` line, so the agent still
        knows what came before rather than silently losing it.
        """
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
            eligible = (
                select(MessageRow)
                .where(
                    MessageRow.channel_id == turn.channel_id,
                    MessageRow.turn_id.in_(prior_turns)
                    | ((MessageRow.turn_id == turn.id) & (MessageRow.role == "assistant")),
                )
                .order_by(MessageRow.sequence.desc())
            )
            # One more than the cap tells the reader whether anything was left out.
            rows = list(session.scalars(eligible.limit(HISTORY_MESSAGES + 1)))
            dropped = len(rows) > HISTORY_MESSAGES
            rows = rows[:HISTORY_MESSAGES]
            attachments = _attachments(session, [str(row.id) for row in rows])
            known = _directory(session, rows)
            chunks: list[str] = []
            remaining = max_chars
            for row in rows:
                chunk = _history_line(session, row, attachments.get(str(row.id), ()), known)
                if len(chunk) > remaining:
                    dropped = True
                    break
                chunks.append(chunk)
                remaining -= len(chunk) + 1
            if dropped:
                summary = _latest_summary(session, turn.channel_id)
                if summary is not None:
                    chunks.append(
                        json.dumps(
                            {
                                "seq": summary.through_sequence,
                                "author_kind": "system",
                                "author": None,
                                "role": "assistant",
                                "kind": "channel_summary",
                                "content": summary.content,
                            },
                            ensure_ascii=False,
                        )
                    )
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

    def participant_failed(self, turn_id: str, index: int, error: str, now: float) -> None:
        """``now`` stamps the agent's ``idle`` activity, so it never predates
        the ``thinking`` the same participant recorded when it started."""
        with self.dstore.immediate_transaction() as session:
            row = session.get(TurnRow, turn_id)
            if row is None or row.status not in {"running", "cancelling"}:
                return
            progress = json.loads(row.participants_json)
            if progress[index]["status"] == "running":
                _participant_activity(
                    session, row.channel_id, progress[index]["agent_slug"], "idle", now
                )
            progress[index].update(status="failed", error=error)
            row.participants_json = json.dumps(progress)

    def cancel_turn(self, viewer: Viewer, channel_id: str, turn_id: str, now: float) -> Turn | None:
        """Stop a turn. The person who asked, or someone who manages the
        channel, may stop it; ``None`` when either cannot be seen."""
        return self._cancel_turn(turn_id, now, viewer=viewer, channel_id=channel_id)[0]

    def request_turn_cancel(self, turn_id: str, now: float) -> bool:
        """Cancel a turn the daemon itself is stopping (its channel's lane).

        The same transition as :meth:`cancel_turn`: an unstarted turn is
        settled as cancelled, a running one is marked ``cancelling`` for its
        participant loop to observe. No ownership check: the caller is the
        scheduler, not a person. Unlike a person's cancel it also applies
        when the channel is no longer active, because the scheduler drops
        the turn from its lane and nothing else would settle it. Returns
        whether the turn was settled or moved to ``cancelling``.
        """
        return self._cancel_turn(turn_id, now, scheduler=True)[1]

    def _cancel_turn(
        self,
        turn_id: str,
        now: float,
        *,
        viewer: Viewer | None = None,
        channel_id: str | None = None,
        scheduler: bool = False,
    ) -> tuple[Turn | None, bool]:
        """The turn after the request, and whether this call changed it."""
        settle = False
        with self.dstore.immediate_transaction() as session:
            member: Member | None = None
            if not scheduler and channel_id is not None:
                try:
                    _, member = _access(session, channel_id, viewer, "read")
                except CollaborationError:
                    return None, False
            row = session.get(TurnRow, turn_id)
            if row is None or (channel_id is not None and row.channel_id != channel_id):
                return None, False
            channel_id = row.channel_id
            channel = session.get(ChannelRow, channel_id)
            active = channel is not None and channel.state == "active"
            if not scheduler:
                if not active:
                    return None, False
                if member is not None and not (
                    row.author_kind in {"human", None} and row.author_id == member.user.id
                ):
                    _access(session, channel_id, member, "manage")
            if row.status not in {"accepted", "running"}:
                return _turn(session, row), False
            if not active and row.status == "accepted":
                # Nothing will start this turn once its lane drops it.
                self._cancel_deleted_channel_turn(session, row, now)
                return _turn(session, row), True
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
        if settle:
            return self.finish_turn(turn_id, error=None, now=now), True
        return result, True

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
        # One agent asking another is channel traffic like any other, so the
        # observers — and through them a linked surface — hear it too.
        appended: Message | None = None
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
            session.flush()
            message_row = session.get(MessageRow, message_id)
            if message_row is not None:
                appended = _message(session, message_row)
            queued = f"Queued @{agent_slug} as handoff {index}. Its reply will appear in this chat."
        self._appended(appended)
        return queued

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
                _event(
                    session,
                    "collaboration.team.created",
                    now,
                    data={"team_id": team_id},
                    audience=user_id,
                )
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
                _event(
                    session,
                    "collaboration.team.updated",
                    now,
                    data={"team_id": team_id},
                    audience=user_id,
                )
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
            _event(
                session,
                "collaboration.team.deleted",
                now,
                data={"team_id": team_id},
                audience=user_id,
            )
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
                audience=user_id,
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
                audience=user_id,
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
                audience=user_id,
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
                    audience=user_id,
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
                    audience=user_id,
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
                audience=user_id,
            )
            return True


LOCAL_USER_CAPABILITIES = ALL_CAPABILITIES
