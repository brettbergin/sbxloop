"""Durable local collaboration resources served by the remote API.

These tables deliberately sit beside the existing execution and API tables.
They give product clients a small, durable collaboration model without
changing the run-oriented contract sbxloop already serves.
"""

from __future__ import annotations

from sqlalchemy import (
    REAL,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from sbxloop.db.base import Base


class LocalUserRow(Base):
    __tablename__ = "collaboration_users"
    __table_args__ = (
        # One local account per identity-provider subject; accounts that
        # never signed in through a provider carry neither column.
        Index(
            "idx_collaboration_users_oidc",
            "oidc_issuer",
            "oidc_subject",
            unique=True,
            sqlite_where=text("oidc_issuer IS NOT NULL AND oidc_subject IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    client_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    full_name: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'UTC'"))
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)
    active: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    auth_source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'local'"))
    oidc_issuer: Mapped[str | None] = mapped_column(Text)
    oidc_subject: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[float | None] = mapped_column(REAL)


class WorkspaceMemberRow(Base):
    """A user's role in a workspace. Every local user of an installation is
    a member of its one workspace; the first is its owner."""

    __tablename__ = "workspace_members"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_workspace_members_role"),
    )

    workspace_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("collaboration_users.id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    invited_by: Mapped[str | None] = mapped_column(Text)


class WorkspaceInviteRow(Base):
    """A pending invitation. Only the SHA-256 of the token is kept: the raw
    token is shown once, to whoever created the invite."""

    __tablename__ = "workspace_invites"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[float] = mapped_column(REAL, nullable=False)
    accepted_at: Mapped[float | None] = mapped_column(REAL)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)


class ChannelRow(Base):
    __tablename__ = "collaboration_channels"
    __table_args__ = (Index("idx_collaboration_channels_user_updated", "user_id", "updated_at"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'local'"))
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)
    deleted_at: Mapped[float | None] = mapped_column(REAL)
    visibility: Mapped[str] = mapped_column(
        Text,
        CheckConstraint("visibility IN ('private', 'workspace')", name="visibility"),
        nullable=False,
        server_default=text("'private'"),
    )
    created_by: Mapped[str | None] = mapped_column(Text)
    silenced_until: Mapped[float | None] = mapped_column(REAL)
    settings_json: Mapped[str | None] = mapped_column(Text)


class ChannelMemberRow(Base):
    """A person in a channel. The channel's creator is its first owner."""

    __tablename__ = "collaboration_channel_members"
    __table_args__ = (
        PrimaryKeyConstraint("channel_id", "user_id"),
        CheckConstraint("role IN ('owner', 'member')", name="role"),
        Index("idx_collaboration_channel_members_user", "user_id"),
    )

    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'member'"))
    added_by: Mapped[str | None] = mapped_column(Text)
    joined_at: Mapped[float] = mapped_column(REAL, nullable=False)
    last_read_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )


class ChannelParticipantRow(Base):
    """An agent in a channel: answering when mentioned, or listening in."""

    __tablename__ = "collaboration_channel_participants"
    __table_args__ = (
        PrimaryKeyConstraint("channel_id", "agent_slug"),
        CheckConstraint("mode IN ('mention', 'ambient')", name="mode"),
    )

    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_slug: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'mention'"))
    added_by_kind: Mapped[str | None] = mapped_column(Text)
    added_by_id: Mapped[str | None] = mapped_column(Text)
    muted_until: Mapped[float | None] = mapped_column(REAL)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)


class MessageRow(Base):
    __tablename__ = "collaboration_messages"
    __table_args__ = (
        UniqueConstraint("channel_id", "sequence"),
        UniqueConstraint("channel_id", "client_message_id"),
        Index("idx_collaboration_messages_channel", "channel_id", "sequence"),
        Index("idx_collaboration_messages_turn", "turn_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    turn_id: Mapped[str | None] = mapped_column(Text)
    client_message_id: Mapped[str | None] = mapped_column(Text)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'message'"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    agent_slug: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    work_json: Mapped[str | None] = mapped_column(Text)
    reactions_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'[]'"))
    author_kind: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[str | None] = mapped_column(Text)


class TurnRow(Base):
    __tablename__ = "collaboration_turns"
    __table_args__ = (
        UniqueConstraint("channel_id", "client_turn_id"),
        Index("idx_collaboration_turns_channel", "channel_id", "created_at"),
        Index("idx_collaboration_turns_status", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_turn_id: Mapped[str | None] = mapped_column(Text)
    input_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    targets_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'[]'"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    started_at: Mapped[float | None] = mapped_column(REAL)
    completed_at: Mapped[float | None] = mapped_column(REAL)
    intent: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'conversation'"))
    participants_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'[]'")
    )
    author_kind: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[str | None] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'human'"))
    parent_turn_id: Mapped[str | None] = mapped_column(Text)
    source_message_id: Mapped[str | None] = mapped_column(Text)
    chain_depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class TeamRow(Base):
    __tablename__ = "collaboration_teams"
    __table_args__ = (
        UniqueConstraint("user_id", "slug"),
        Index("idx_collaboration_teams_user", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    goal: Mapped[str | None] = mapped_column(Text)
    agent_slugs_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'[]'"))
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)


class PreferenceRow(Base):
    __tablename__ = "collaboration_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "name"),
        Index("idx_collaboration_preferences_user", "user_id", "name"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)


class WorkflowRow(Base):
    __tablename__ = "collaboration_workflows"
    __table_args__ = (
        UniqueConstraint("user_id", "slug"),
        Index("idx_collaboration_workflows_user", "user_id", "name"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    trigger_event: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)


class AgentRow(Base):
    """A person's own agent. ``spec_json`` is the agent spec as saved; the
    built-ins and ``[[agents]]`` never land here."""

    __tablename__ = "agents"

    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
