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
    #: Whether ``email`` came from a path its holder could not steer to
    #: someone else's address: the installation's first registration, an
    #: invite addressed to it, or a provider claim marked verified. Cleared
    #: when the person changes it themselves. A provider identity is linked
    #: to a local account on a first sign-in only when this is set.
    email_verified: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


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
    #: Where a message that arrived over a bridge came from, when it did.
    origin_json: Mapped[str | None] = mapped_column(Text)
    #: What an ``agent_update`` message is (revision 0030); null otherwise.
    post_kind: Mapped[str | None] = mapped_column(Text)


class ChannelLinkRow(Base):
    """A bridge surface mirroring a channel (revision 0028).

    ``surface_id`` is the service's own channel id and ``thread_id`` the
    thread within it, when the link is to a thread. SQLite treats NULLs in
    a unique index as distinct, so the store also refuses a second link to
    a surface explicitly; the constraint is the backstop.
    """

    __tablename__ = "collaboration_channel_links"
    __table_args__ = (
        UniqueConstraint("backend", "surface_id", "thread_id"),
        Index("idx_collaboration_channel_links_channel", "channel_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    backend: Mapped[str] = mapped_column(Text, nullable=False)
    surface_id: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(Text)
    allow_guests: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    active: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class ExternalIdentityRow(Base):
    """Who someone is on a bridge: their service account, mapped to a local
    one by a code they typed there themselves (revision 0028)."""

    __tablename__ = "collaboration_external_identities"
    __table_args__ = (
        PrimaryKeyConstraint("backend", "external_user_id"),
        Index("idx_collaboration_external_identities_user", "user_id"),
    )

    backend: Mapped[str] = mapped_column(Text, nullable=False)
    external_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[float] = mapped_column(REAL, nullable=False)


class ChannelRunPostRow(Base):
    """One post a run made in a channel (revision 0030).

    The dedupe key is the run's own name for the moment it is posting
    about, so a replayed, resumed or re-observed run finds its own row
    instead of writing a second message.
    """

    __tablename__ = "channel_run_posts"
    __table_args__ = (Index("idx_channel_run_posts_run", "run_id", "posted_at"),)

    dedupe_key: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    posted_at: Mapped[float] = mapped_column(REAL, nullable=False)


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
    #: The run this turn steered instead of answering (S-A11): a mention of
    #: an agent working live work in this channel goes to that run.
    steered_run_id: Mapped[str | None] = mapped_column(Text)


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


class AgentMemoryRow(Base):
    """One thing an agent keeps beyond a conversation (revision 0023).

    ``source_channel_id`` scopes it: null is global, otherwise it is shown
    only in that channel unless the platform says the channel is visible to
    the whole workspace. A forgotten memory keeps its row with
    ``deleted_at`` set.
    """

    __tablename__ = "agent_memories"
    __table_args__ = (Index("idx_agent_memories_agent", "agent_slug", "deleted_at", "updated_at"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_slug: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_channel_id: Mapped[str | None] = mapped_column(Text)
    source_run_id: Mapped[str | None] = mapped_column(Text)
    source_message_id: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    pinned: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[float | None] = mapped_column(REAL)
    updated_at: Mapped[float | None] = mapped_column(REAL)
    last_used_at: Mapped[float | None] = mapped_column(REAL)
    deleted_at: Mapped[float | None] = mapped_column(REAL)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class MessageArtifactRow(Base):
    """A file a message carries (revision 0028).

    The row is written in the same transaction as the message, so a result
    and the files it delivered are never half-recorded. ``channel_id`` is
    denormalised from the message: it is what scopes a channel's file list
    and the ``read_channel_artifact`` tool, and it is indexed for both.
    ``run_id`` is the run's public id, as the work snapshot carries it.
    """

    __tablename__ = "collaboration_message_artifacts"
    __table_args__ = (
        PrimaryKeyConstraint("message_id", "artifact_id"),
        Index("idx_collaboration_message_artifacts_channel", "channel_id"),
    )

    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str | None] = mapped_column(Text)
    relpath: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)


class ChannelInputFileRow(Base):
    """An immutable user upload, independent of generated run artifacts.

    The original lives under ``SbxloopHome.channel_files`` at a path derived
    only from this opaque id. ``reserved`` rows have no committed bytes yet;
    only the uploader may see them. A later message association will make an
    ``uploaded`` file visible to the rest of its channel.
    """

    __tablename__ = "collaboration_input_files"
    __table_args__ = (
        UniqueConstraint("channel_id", "uploader_id", "client_upload_id"),
        Index("idx_collaboration_input_files_channel", "channel_id", "created_at"),
        Index("idx_collaboration_input_files_workspace", "workspace_id", "status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    uploader_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_upload_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    declared_size: Mapped[int | None] = mapped_column(Integer)
    size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
    uploaded_at: Mapped[float | None] = mapped_column(REAL)
    deleted_at: Mapped[float | None] = mapped_column(REAL)


class ChannelSummaryRow(Base):
    """What a channel said before its history window (revision 0028).

    One row per compaction: ``through_sequence`` is the last message the
    summary covers, so the newest row is the one a trimmed history opens
    with and the watermark the next compaction starts from.
    """

    __tablename__ = "collaboration_channel_summaries"
    __table_args__ = (PrimaryKeyConstraint("channel_id", "through_sequence"),)

    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    through_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(REAL, nullable=False)
