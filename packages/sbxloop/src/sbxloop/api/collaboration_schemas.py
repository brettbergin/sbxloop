"""Public request and response shapes for local collaboration."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from sbxloop.api.models import ApiModel


class LocalRegisterRequest(ApiModel):
    email: str = Field(min_length=3, max_length=320)
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=1024)
    full_name: str | None = Field(default=None, max_length=160)
    timezone: str = Field(default="UTC", min_length=1, max_length=100)


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


class ChannelUpdate(ApiModel):
    title: str = Field(min_length=1, max_length=200)


class ChannelOut(ApiModel):
    id: str
    workspace_id: str
    title: str
    state: str
    revision: int
    created_at: str
    updated_at: str


class ChannelPage(ApiModel):
    items: list[ChannelOut]
    total: int
    has_more: bool


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


class TurnCreate(ApiModel):
    content: str = Field(min_length=1, max_length=100_000)
    target_slugs: list[str] = Field(default_factory=list, max_length=16)
    client_turn_id: str | None = Field(default=None, max_length=128)
    client_message_id: str | None = Field(default=None, max_length=128)
    intent: Literal["conversation", "delegate"] = "conversation"


class ParticipantOut(ApiModel):
    agent_slug: str | None
    status: str
    error: str | None = None
    requested_by: str | None = None
    parent_index: int | None = None
    request: str | None = None
    read_only: bool = False


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


class TurnAccepted(ApiModel):
    turn: TurnOut
    message: MessageOut
    replayed: bool = False
