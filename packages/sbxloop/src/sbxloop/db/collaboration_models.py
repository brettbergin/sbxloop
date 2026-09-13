"""Durable local collaboration resources served by the remote API.

These tables deliberately sit beside the existing execution and API tables.
They give product clients a small, durable collaboration model without
changing the run-oriented contract sbxloop already serves.
"""

from __future__ import annotations

from sqlalchemy import REAL, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from sbxloop.db.base import Base


class LocalUserRow(Base):
    __tablename__ = "collaboration_users"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    client_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    full_name: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'UTC'"))
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    updated_at: Mapped[float] = mapped_column(REAL, nullable=False)
    active: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


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
