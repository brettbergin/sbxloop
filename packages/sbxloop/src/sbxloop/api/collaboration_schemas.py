"""Public request and response shapes for local collaboration."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from sbxloop.agents.definition import AgentRoleName, AgentSpec, AgentStartKind
from sbxloop.api.models import ApiModel

WorkspaceRole = Literal["owner", "admin", "member"]
AuthSource = Literal["local", "oidc"]


class LocalRegisterRequest(ApiModel):
    email: str = Field(min_length=3, max_length=320)
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=1024)
    full_name: str | None = Field(default=None, max_length=160)
    timezone: str = Field(default="UTC", min_length=1, max_length=100)
    #: Required for every user after the installation's first: the token of
    #: a workspace invite, which sets the new user's role.
    invite_token: str | None = Field(default=None, min_length=1, max_length=256)


class LocalLoginRequest(ApiModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class LocalUserOut(ApiModel):
    id: str
    email: str
    username: str
    full_name: str | None
    timezone: str
    is_active: bool
    created_at: str
    updated_at: str
    #: The caller's workspace role (feature ``workspace.members``).
    role: WorkspaceRole | None = None
    #: The identity provider's picture URL, when it supplied one.
    avatar_url: str | None = None
    #: How the user signs in.
    auth_source: AuthSource | None = None


class WorkspaceUserOut(ApiModel):
    """One person in the workspace directory."""

    id: str
    username: str
    email: str
    full_name: str | None
    avatar_url: str | None
    role: WorkspaceRole
    is_active: bool
    auth_source: AuthSource
    last_seen_at: str | None


class WorkspaceUserPage(ApiModel):
    data: list[WorkspaceUserOut]


class WorkspaceMemberUpdate(ApiModel):
    role: WorkspaceRole | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _something(self) -> Self:
        if self.role is None and self.is_active is None:
            raise ValueError("name a role or is_active to change")
        return self


class WorkspaceInviteCreate(ApiModel):
    role: WorkspaceRole
    #: When given, only a user registering with this email (in any case)
    #: can spend the invite. Surrounding whitespace is trimmed first, and an
    #: empty or all-whitespace email counts as no email at all.
    email: str | None = Field(default=None, min_length=3, max_length=320)
    ttl_hours: int = Field(default=72, ge=1, le=720)

    @field_validator("email", mode="before")
    @classmethod
    def _trim_email(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class WorkspaceInviteCreated(ApiModel):
    """A new invite. ``token`` appears here and nowhere else."""

    id: str
    token: str
    expires_at: str
    role: WorkspaceRole
    email: str | None


class WorkspaceInviteOut(ApiModel):
    id: str
    role: WorkspaceRole
    email: str | None
    expires_at: str
    accepted_at: str | None
    created_by: str | None


class WorkspaceInvitePage(ApiModel):
    data: list[WorkspaceInviteOut]


class LocalUserUpdate(ApiModel):
    email: str | None = Field(default=None, min_length=3, max_length=320)
    full_name: str | None = Field(default=None, max_length=160)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)


class AgentOut(ApiModel):
    slug: str
    name: str
    description: str
    capabilities: list[str]
    category: str
    instructions: str = ""
    system_prompt: str = ""
    module_path: str = "sbxloop.api.agents"
    backend: str = ""
    model: str = ""
    model_source: str = ""
    phase_models: dict[str, str] = Field(default_factory=dict)
    execution_mode: str = "chat_and_managed_runs"
    read_only: bool = False
    avatar: str = ""
    color: str = ""
    roles: list[str] = Field(default_factory=list)
    tools: list[str] | None = None
    skills: list[str] | None = None
    mcp: list[str] | None = None
    credentials: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    can_start: list[str] = Field(default_factory=list)
    max_runs_per_day: int | None = None
    aliases: list[str] = Field(default_factory=list)
    enabled: bool = True
    source: Literal["builtin", "config", "user"] = "builtin"
    editable: bool = False
    revision: int = 0


class AgentCreate(AgentSpec):
    """A new agent: the agent spec itself. Unknown keys (hosts, egress) are refused."""


class AgentUpdate(ApiModel):
    """Changes to a person's agent, made against ``expected_revision``.
    Keys left out keep their value; ``slug`` cannot change."""

    expected_revision: int = Field(ge=0)
    slug: str | None = None
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    avatar: str | None = None
    color: str | None = None
    instructions: str | None = Field(default=None, max_length=8000)
    model: str | None = None
    roles: list[AgentRoleName] | None = None
    tools: list[str] | None = None
    skills: list[str] | None = None
    mcp: list[str] | None = None
    credentials: list[str] | None = None
    interests: list[str] | None = None
    can_start: list[AgentStartKind] | None = None
    max_runs_per_day: int | None = Field(default=None, ge=0)
    aliases: list[str] | None = None
    enabled: bool | None = None


class MemoryOut(ApiModel):
    id: str
    agent_slug: str
    kind: Literal["fact", "preference", "procedure"]
    content: str
    source_channel_id: str | None = None
    source_run_id: str | None = None
    source_message_id: str | None = None
    author: str
    pinned: bool
    created_at: str
    updated_at: str
    last_used_at: str | None = None
    revision: int


class MemoryCreate(ApiModel):
    content: str = Field(min_length=1, max_length=16000)
    kind: Literal["fact", "preference", "procedure"] = "fact"
    pinned: bool = False
    channel_id: str | None = Field(default=None, min_length=1, max_length=128)


class MemoryUpdate(ApiModel):
    content: str | None = Field(default=None, min_length=1, max_length=16000)
    pinned: bool | None = None
    expected_revision: int = Field(ge=1)


class TeamCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    description: str | None = Field(default=None, max_length=1000)
    goal: str | None = Field(default=None, max_length=4000)
    agent_slugs: list[str] = Field(default_factory=list, max_length=16)
    is_enabled: bool = True


class TeamUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    description: str | None = Field(default=None, max_length=1000)
    goal: str | None = Field(default=None, max_length=4000)
    agent_slugs: list[str] | None = Field(default=None, max_length=16)
    is_enabled: bool | None = None


class TeamOut(ApiModel):
    id: str
    name: str
    slug: str
    description: str | None
    goal: str | None
    agent_slugs: list[str]
    is_enabled: bool
    created_at: str
    updated_at: str


class PreferenceDefinitionOut(ApiModel):
    name: str
    label: str
    description: str
    placeholder: str


class PreferenceUpdate(ApiModel):
    content: str = Field(max_length=10_000)


class PreferenceOut(ApiModel):
    name: str
    content: str


class DetailOut(ApiModel):
    detail: str


class ServiceFieldOut(ApiModel):
    key: str
    label: str
    type: str


class ServiceDefinitionOut(ApiModel):
    key: str
    name: str
    description: str
    auth_type: Literal["oauth2", "api_key", "token", "credentials"]
    color: str
    fields: list[ServiceFieldOut]
    agent_slug: str | None
    available: bool = True
    unavailable_reason: str | None = None


class ConnectionOut(ApiModel):
    id: str
    service_type: str
    display_name: str | None
    auth_type: Literal["oauth2", "api_key", "token", "credentials"]
    status: Literal["connected", "expired", "error", "disconnected"]
    masked_credentials: dict[str, str]
    scopes: str | None = None
    token_expires_at: str | None = None
    last_used_at: str | None = None
    last_tested_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ConnectionMutation(ApiModel):
    service_type: str | None = None
    credentials: dict[str, str] = Field(default_factory=dict)
    display_name: str | None = None


class ConnectionTestOut(ApiModel):
    success: bool
    message: str
    status: Literal["connected", "expired", "error", "disconnected"]


class WorkflowCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    description: str | None = Field(default=None, max_length=2000)
    trigger_event: str | None = Field(default=None, max_length=120)
    is_enabled: bool = True


class WorkflowUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    description: str | None = Field(default=None, max_length=2000)
    trigger_event: str | None = Field(default=None, max_length=120)
    is_enabled: bool | None = None


class WorkflowOut(ApiModel):
    id: str
    name: str
    slug: str
    description: str | None
    trigger_event: str | None
    is_enabled: bool
    created_at: str
    updated_at: str


class ChannelCreate(ApiModel):
    title: str | None = Field(default=None, max_length=200)


ChannelVisibility = Literal["private", "workspace"]
ChannelRoleName = Literal["owner", "member"]


class ChannelUpdate(ApiModel):
    """Only the fields sent change. Changing either one takes managing the
    channel: its owner, or a workspace owner or admin."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    visibility: ChannelVisibility | None = None


class ChannelOut(ApiModel):
    id: str
    workspace_id: str
    title: str
    state: str
    revision: int
    created_at: str
    updated_at: str
    #: ``private``: channel members only; ``workspace``: every workspace member.
    visibility: ChannelVisibility = "private"
    created_by: str | None = None
    silenced_until: float | None = None
    #: Messages past the reader's last read sequence. Null for a caller
    #: with no channel membership to measure against.
    unread_count: int | None = None
    #: The caller's role in the channel; ``null`` when not a member.
    my_role: ChannelRoleName | None = None


class ChannelPage(ApiModel):
    items: list[ChannelOut]
    total: int
    has_more: bool


class ChannelMemberUserOut(ApiModel):
    id: str
    username: str
    full_name: str | None = None
    avatar_url: str | None = None


class ChannelMemberOut(ApiModel):
    user_id: str
    role: ChannelRoleName
    joined_at: str
    last_read_sequence: int = 0
    user: ChannelMemberUserOut


class ChannelMemberPage(ApiModel):
    data: list[ChannelMemberOut]


class ChannelMemberCreate(ApiModel):
    user_id: str = Field(min_length=1, max_length=128)
    role: ChannelRoleName = "member"


ParticipantModeName = Literal["mention", "ambient"]


class ArtifactRefOut(ApiModel):
    """A file a run delivered, by catalog identity; its bytes are served by
    ``GET /v1/artifacts/{id}``."""

    id: str
    run_id: str
    relpath: str
    media_type: str
    size: int


class ChannelWorkOut(ApiModel):
    item_id: str
    #: The turn the work hangs on; null for a run a channel asked for
    #: outside any turn of its own.
    turn_id: str | None = None
    agent_slug: str | None
    title: str
    kind: str
    state: str
    run_id: str | None = None
    stage: str | None = None
    item_revision: int = 0
    run_revision: int | None = None
    item_actions: list[str] = Field(default_factory=list)
    run_actions: list[str] = Field(default_factory=list)
    #: Available files the run delivered, by path, at most
    #: ``WORK_ARTIFACTS_MAX``; empty for results written before this field.
    artifacts: list[ArtifactRefOut] = Field(default_factory=list)


class ChannelArtifactPage(ApiModel):
    """Every file a channel's messages carry."""

    data: list[ArtifactRefOut]


class AuthorOut(ApiModel):
    """Who wrote a message: a person, an agent, or sbxloop itself."""

    kind: Literal["human", "agent", "system"]
    id: str | None = None
    display_name: str | None = None


BridgeBackendName = Literal["discord", "slack", "mattermost"]


class MessageOriginOut(ApiModel):
    """The bridge surface a message arrived on, for a message that did."""

    backend: str
    surface_id: str
    external_message_id: str | None = None


class BridgeOut(ApiModel):
    """A chat service this release can bridge, and whether it is set up."""

    backend: BridgeBackendName
    configured: bool
    label: str


class BridgePage(ApiModel):
    data: list[BridgeOut]


class ChannelLinkCreate(ApiModel):
    """Link a surface of a chat service to this channel.

    ``allow_guests`` admits people on that surface who have linked no
    account: their messages are stored under the name they use there.
    """

    backend: BridgeBackendName
    surface_id: str = Field(min_length=1, max_length=200)
    thread_id: str | None = Field(default=None, min_length=1, max_length=200)
    allow_guests: bool = False


class ChannelLinkOut(ApiModel):
    id: str
    channel_id: str
    backend: BridgeBackendName
    surface_id: str
    thread_id: str | None = None
    allow_guests: bool = False
    created_by: str | None = None
    created_at: str
    active: bool = True


class ChannelLinkPage(ApiModel):
    data: list[ChannelLinkOut]


class LinkCodeOut(ApiModel):
    """A code to type on a bridge, once, to prove an account is yours."""

    code: str
    expires_at: str


class ExternalIdentityOut(ApiModel):
    backend: BridgeBackendName
    external_user_id: str
    display_name: str | None = None
    verified_at: str


class ExternalIdentityPage(ApiModel):
    data: list[ExternalIdentityOut]


#: What an ``agent_update`` a run posted is.
PostKindName = Literal["plan", "progress", "review", "delivery", "reply", "notice"]


class MessageOut(ApiModel):
    id: str
    channel_id: str
    turn_id: str | None
    sequence: int
    role: Literal["user", "assistant"]
    kind: str
    content: str
    agent_slug: str | None
    created_at: str
    work: ChannelWorkOut | None = None
    reactions: list[str] = Field(default_factory=list)
    author: AuthorOut | None = None
    #: Files this message carries, readable by anyone who can read the
    #: channel (feature ``collaboration.message_artifacts``).
    artifacts: list[ArtifactRefOut] = Field(default_factory=list)
    #: Where the message arrived from, when it came over a bridge.
    origin: MessageOriginOut | None = None
    #: Set on the ``agent_update`` messages a run posts; null otherwise.
    post_kind: PostKindName | None = None


class ReactionSet(ApiModel):
    emoji: Literal["👍", "👎", "❤️", "🎉", "😄", "😕"]
    active: bool = True


class TurnCreate(ApiModel):
    content: str = Field(min_length=1, max_length=100_000)
    target_slugs: list[str] = Field(default_factory=list, max_length=16)
    client_turn_id: str | None = Field(default=None, max_length=128)
    client_message_id: str | None = Field(default=None, max_length=128)
    intent: Literal["conversation", "delegate", "code", "workload", "auto"] = "conversation"


class ParticipantOut(ApiModel):
    """One agent's slot in a turn. ``assignees`` is set on the first slot of
    a turn that may start managed work: the run roles the turn's mentions
    declare, as ``role -> agent slug``, which admission uses to assign the
    run."""

    agent_slug: str | None
    status: str
    error: str | None = None
    requested_by: str | None = None
    parent_index: int | None = None
    request: str | None = None
    read_only: bool = False
    assignees: dict[str, str] | None = None


class ChannelParticipantOut(ApiModel):
    """An agent in a channel. ``status`` is ``thinking`` while it answers a
    running turn here, ``working`` while a live run in this channel is
    credited to it, and ``idle`` otherwise; ``activity`` names that run."""

    agent_slug: str
    mode: ParticipantModeName
    added_by: AuthorOut
    muted_until: float | None = None
    created_at: str
    status: Literal["idle", "thinking", "working"] = "idle"
    activity: str | None = None


class ChannelParticipantPage(ApiModel):
    data: list[ChannelParticipantOut]


class ChannelParticipantUpdate(ApiModel):
    """Only the fields sent change; a new participant answers when mentioned."""

    mode: ParticipantModeName | None = None
    muted_until: float | None = None


class TurnOut(ApiModel):
    id: str
    channel_id: str
    input_message_id: str
    status: str
    targets: list[str]
    participants: list[ParticipantOut] = Field(default_factory=list)
    error: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    author_id: str | None = None
    trigger: str | None = None
    parent_turn_id: str | None = None
    intent: str | None = None
    #: How many agent-started turns separate this one from the human turn
    #: that started the chain; zero for a turn a person asked for.
    chain_depth: int = 0


class ChannelSilence(ApiModel):
    """How long the channel's agents stay quiet; null lifts the silence."""

    until: float | None = None


class ChannelReadUpdate(ApiModel):
    """How far the caller has read this channel."""

    sequence: int = Field(ge=0)


class ChannelStopOut(ApiModel):
    """What a stop actually stopped."""

    cancelled_turns: list[str] = Field(default_factory=list)
    cancelled_runs: list[str] = Field(default_factory=list)
    #: Work items the channel queued that had not started, abandoned.
    cancelled_items: list[str] = Field(default_factory=list)
    silenced_until: float | None = None


class TurnAccepted(ApiModel):
    turn: TurnOut
    message: MessageOut
    replayed: bool = False
