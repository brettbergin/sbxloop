"""Durable collaboration state for local product clients.

The execution API remains run-oriented. This store adds the resources a
chat product needs around it: a local profile, channels, messages, turns,
agent teams, preferences, and workflow definitions. Every write uses the
daemon store's lock and transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from sbxloop.api.auth.store import hash_secret
from sbxloop.daemon.controls.principal import ALL_CAPABILITIES, CAPABILITIES, WORKSPACE_ID
from sbxloop.daemon.store import DaemonStore
from sbxloop.db.api_models import ApiEventRow, ClientRow
from sbxloop.db.collaboration_models import (
    ChannelRow,
    LocalUserRow,
    MessageRow,
    PreferenceRow,
    TeamRow,
    TurnRow,
    WorkflowRow,
)
from sbxloop.ids import _token


class CollaborationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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
    )


def _channel(row: ChannelRow) -> Channel:
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
    )


def _message(row: MessageRow) -> Message:
    return Message(
        id=str(row.id),
        channel_id=str(row.channel_id),
        turn_id=None if row.turn_id is None else str(row.turn_id),
        client_message_id=(None if row.client_message_id is None else str(row.client_message_id)),
        sequence=int(row.sequence),
        role=str(row.role),
        kind=str(row.kind),
        content=str(row.content),
        agent_slug=None if row.agent_slug is None else str(row.agent_slug),
        created_at=float(row.created_at),
    )


def _turn(row: TurnRow) -> Turn:
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
    ) -> LocalUser:
        """Create the installation's one local user and its API principal."""
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
                if session.scalar(select(func.count()).select_from(LocalUserRow)):
                    raise CollaborationError(
                        "local_user_exists", "this installation already has a local user"
                    )
                session.execute(
                    insert(ClientRow).values(
                        id=client_id,
                        name=username,
                        secret_hash=hash_secret(password),
                        capabilities_json=json.dumps(list(CAPABILITIES)),
                        created_at=now,
                        created_by="local-onboarding",
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
                row = session.get(LocalUserRow, user_id)
                assert row is not None  # nosec B101 - inserted in this transaction
                _event(
                    session,
                    "collaboration.user.created",
                    now,
                    actor={"kind": "client", "id": client_id, "display": username, "via": "api"},
                    data={"user_id": user_id},
                )
                return _user(row)
        except IntegrityError as exc:
            raise CollaborationError(
                "profile_conflict", "username or email is already in use"
            ) from exc

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
                )
            )
            row = session.get(ChannelRow, channel_id)
            assert row is not None  # nosec B101
            _event(session, "collaboration.channel.created", now, data={"channel_id": channel_id})
            return _channel(row)

    def get_channel(
        self, user_id: str, channel_id: str, *, include_deleted: bool = False
    ) -> Channel | None:
        with self.dstore.read() as session:
            row = session.get(ChannelRow, channel_id)
            if row is None or row.user_id != user_id:
                return None
            if not include_deleted and row.state != "active":
                return None
            return _channel(row)

    def list_channels(self, user_id: str, *, limit: int, offset: int) -> tuple[list[Channel], int]:
        with self.dstore.read() as session:
            condition = (ChannelRow.user_id == user_id, ChannelRow.state == "active")
            total = int(
                session.scalar(select(func.count()).select_from(ChannelRow).where(*condition)) or 0
            )
            rows = session.scalars(
                select(ChannelRow)
                .where(*condition)
                .order_by(ChannelRow.updated_at.desc(), ChannelRow.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return [_channel(row) for row in rows], total

    def update_channel(
        self, user_id: str, channel_id: str, title: str, now: float
    ) -> Channel | None:
        with self.dstore.transaction() as session:
            row = session.get(ChannelRow, channel_id)
            if row is None or row.user_id != user_id or row.state != "active":
                return None
            row.title = title.strip()[:200] or row.title
            row.updated_at = now
            row.revision += 1
            session.flush()
            _event(session, "collaboration.channel.updated", now, data={"channel_id": channel_id})
            return _channel(row)

    def delete_channel(self, user_id: str, channel_id: str, now: float) -> bool:
        """Tombstone a channel so a late agent result cannot recreate it."""
        with self.dstore.transaction() as session:
            row = session.get(ChannelRow, channel_id)
            if row is None or row.user_id != user_id or row.state != "active":
                return False
            row.state = "deleted"
            row.deleted_at = now
            row.updated_at = now
            row.revision += 1
            _event(session, "collaboration.channel.deleted", now, data={"channel_id": channel_id})
            return True

    # -- messages and turns --------------------------------------------------------

    @staticmethod
    def _next_sequence(session: Any, channel_id: str) -> int:
        newest = session.scalar(
            select(func.max(MessageRow.sequence)).where(MessageRow.channel_id == channel_id)
        )
        return int(newest or 0) + 1

    def list_messages(
        self, user_id: str, channel_id: str, *, after: int = 0
    ) -> list[Message] | None:
        with self.dstore.read() as session:
            channel = session.get(ChannelRow, channel_id)
            if channel is None or channel.user_id != user_id or channel.state != "active":
                return None
            rows = session.scalars(
                select(MessageRow)
                .where(MessageRow.channel_id == channel_id, MessageRow.sequence > after)
                .order_by(MessageRow.sequence.asc())
            )
            return [_message(row) for row in rows]

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
    ) -> tuple[Turn, Message, bool]:
        """Append the user message and accepted turn atomically.

        A repeated client turn id returns the original resources. A reused id
        with different text is rejected instead of silently changing meaning.
        """
        with self.dstore.immediate_transaction() as session:
            channel = session.get(ChannelRow, channel_id)
            if channel is None or channel.user_id != user_id or channel.state != "active":
                raise CollaborationError("channel_not_found", "channel not found")
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
                    return _turn(existing), _message(message), False
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
                    created_at=now,
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
                            {"agent_slug": target, "status": "queued", "error": None}
                            for target in (targets or (None,))
                        ]
                    ),
                    created_at=now,
                )
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
                },
            )
            return _turn(turn_row), _message(message_row), True

    def start_turn(self, turn_id: str, now: float) -> bool:
        with self.dstore.transaction() as session:
            turn = session.get(TurnRow, turn_id)
            if turn is None or turn.status != "accepted":
                return False
            channel = session.get(ChannelRow, turn.channel_id)
            if channel is None or channel.state != "active":
                turn.status = "cancelled"
                turn.completed_at = now
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
                turn.status = "cancelled"
                turn.completed_at = now
                return None
            message_id = "msg_" + _token(16)
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
                )
            )
            channel.updated_at = now
            channel.revision += 1
            if participant_index is not None:
                progress = json.loads(turn.participants_json)
                progress[participant_index]["status"] = "completed"
                turn.participants_json = json.dumps(progress)
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
                },
            )
            return _message(row)

    def finish_turn(self, turn_id: str, *, error: str | None, now: float) -> Turn | None:
        with self.dstore.immediate_transaction() as session:
            row = session.get(TurnRow, turn_id)
            if row is None or row.status in {"completed", "failed", "cancelled"}:
                return None if row is None else _turn(row)
            cancelled = row.status == "cancelling"
            row.status = "cancelled" if cancelled else ("failed" if error else "completed")
            if cancelled:
                error = "Stopped remaining responses. Work already started may still have effects."
            progress = json.loads(row.participants_json)
            for participant in progress:
                if participant["status"] in {"queued", "running"}:
                    participant["status"] = (
                        "cancelled" if cancelled else ("failed" if error else "completed")
                    )
            row.participants_json = json.dumps(progress)
            row.error = error
            row.completed_at = now
            channel = session.get(ChannelRow, row.channel_id)
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
                    },
                )
            session.flush()
            _event(
                session,
                f"collaboration.turn.{row.status}",
                now,
                data={"channel_id": row.channel_id, "turn_id": row.id, "error": error},
            )
            return _turn(row)

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
                user = session.get(LocalUserRow, channel.user_id) if channel else None
                message = session.get(MessageRow, row.input_message_id)
                if channel and channel.state == "active" and user and user.active and message:
                    queued.append((message.sequence, _turn(row), _user(user), message.content))
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

    def get_turn(self, user_id: str, channel_id: str, turn_id: str) -> Turn | None:
        with self.dstore.read() as session:
            channel = session.get(ChannelRow, channel_id)
            row = session.get(TurnRow, turn_id)
            if (
                channel is None
                or channel.user_id != user_id
                or channel.state != "active"
                or row is None
                or row.channel_id != channel_id
            ):
                return None
            return _turn(row)

    def list_turns(self, user_id: str, channel_id: str, *, active_only: bool = False) -> list[Turn]:
        with self.dstore.read() as session:
            channel = session.get(ChannelRow, channel_id)
            if channel is None or channel.user_id != user_id or channel.state != "active":
                raise CollaborationError("channel_not_found", "channel not found")
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
                _turn(row)
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
                    {"agent_slug": target, "status": "queued", "error": None}
                    for target in (json.loads(row.targets_json) or [None])
                ]
            progress[index]["status"] = "running"
            row.participants_json = json.dumps(progress)
            _event(
                session,
                "collaboration.participant.running",
                now,
                data={"channel_id": row.channel_id, "turn_id": turn_id, "index": index},
            )
            return True

    def participant_failed(self, turn_id: str, index: int, error: str) -> None:
        with self.dstore.immediate_transaction() as session:
            row = session.get(TurnRow, turn_id)
            if row is None or row.status not in {"running", "cancelling"}:
                return
            progress = json.loads(row.participants_json)
            progress[index].update(status="failed", error=error)
            row.participants_json = json.dumps(progress)

    def cancel_turn(self, user_id: str, channel_id: str, turn_id: str, now: float) -> Turn | None:
        settle = False
        with self.dstore.immediate_transaction() as session:
            channel = session.get(ChannelRow, channel_id)
            row = session.get(TurnRow, turn_id)
            if (
                channel is None
                or channel.user_id != user_id
                or channel.state != "active"
                or row is None
                or row.channel_id != channel_id
            ):
                return None
            if row.status not in {"accepted", "running"}:
                return _turn(row)
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
            result = _turn(row)
        return self.finish_turn(turn_id, error=None, now=now) if settle else result

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
