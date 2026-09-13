"""Local profile, channel, message, turn, agent and team resources."""

from __future__ import annotations

import os
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from sbxloop.agentmodels import model_for_phase, refreshed_models
from sbxloop.api.agents import AGENTS, AGENTS_BY_SLUG
from sbxloop.api.auth.deps import Authenticated, current, get_ctx, require
from sbxloop.api.auth.store import AuthError
from sbxloop.api.collaboration import (
    Channel,
    CollaborationError,
    LocalUser,
    Message,
    Preference,
    Team,
    Turn,
    Workflow,
)
from sbxloop.api.collaboration_schemas import (
    AgentOut,
    ChannelCreate,
    ChannelOut,
    ChannelPage,
    ChannelUpdate,
    ConnectionMutation,
    ConnectionOut,
    ConnectionTestOut,
    DetailOut,
    LocalLoginRequest,
    LocalRegisterRequest,
    LocalUserOut,
    LocalUserUpdate,
    MessageOut,
    ParticipantOut,
    PreferenceDefinitionOut,
    PreferenceOut,
    PreferenceUpdate,
    ServiceDefinitionOut,
    TeamCreate,
    TeamOut,
    TeamUpdate,
    TurnAccepted,
    TurnCreate,
    TurnOut,
    WorkflowCreate,
    WorkflowOut,
    WorkflowUpdate,
)
from sbxloop.api.context import PAGE_MAX, ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import TokenResponse, rfc3339
from sbxloop.api.routes.auth import grant_tokens
from sbxloop.config import Config
from sbxloop.engine.harness import ROLE_BY_PHASE

router = APIRouter(prefix="/v1", tags=["collaboration"])
MENTION = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9_-]{0,63})\b", re.IGNORECASE)

PREFERENCE_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "name": "personality",
        "label": "Personality",
        "description": "How would you like Angie to communicate with you?",
        "placeholder": "formal, casual, friendly, brief, etc.",
    },
    {
        "name": "interests",
        "label": "Interests",
        "description": "What are your main interests and areas Angie should know about?",
        "placeholder": "cybersecurity, woodworking, gaming, gardening...",
    },
    {
        "name": "schedule",
        "label": "Schedule",
        "description": "Describe your typical daily schedule (work hours, time zone, routines).",
        "placeholder": "work mon-fri 8-5, dinner at 7, sleep 11-7...",
    },
    {
        "name": "priorities",
        "label": "Priorities",
        "description": "What are your top priorities that Angie should always keep in mind?",
        "placeholder": "monitoring communications, GitHub alerts, email tracking...",
    },
    {
        "name": "communication",
        "label": "Communication",
        "description": "Which communication channels do you prefer and in what order?",
        "placeholder": "Slack, Discord, iMessage, email...",
    },
    {
        "name": "home",
        "label": "Home",
        "description": "Describe your home setup relevant for Angie (devices and location).",
        "placeholder": "Hue lights, Home Assistant, Ubiquiti network...",
    },
    {
        "name": "work",
        "label": "Work",
        "description": "Describe your work context (role, tools, projects, workflows).",
        "placeholder": "security engineer, Python, CI/CD pipelines...",
    },
    {
        "name": "style",
        "label": "Style",
        "description": "How detailed should Angie's responses be? Any tone preferences?",
        "placeholder": "English, kind and light, detailed and verbose...",
    },
)
PREFERENCE_NAMES = frozenset(item["name"] for item in PREFERENCE_DEFINITIONS)

SERVICE_DEFINITIONS: tuple[dict[str, object], ...] = (
    {
        "key": "github",
        "name": "GitHub",
        "description": "Repository management — pull requests, issues, and code review",
        "auth_type": "api_key",
        "color": "#333333",
        "fields": [
            {
                "key": "personal_access_token",
                "label": "Personal Access Token",
                "type": "password",
            }
        ],
        "agent_slug": "github",
    },
    {
        "key": "slack",
        "name": "Slack",
        "description": "Team messaging through sbxloop's Slack bridge",
        "auth_type": "token",
        "color": "#4A154B",
        "fields": [
            {"key": "bot_token", "label": "Bot Token (xoxb-…)", "type": "password"},
            {"key": "app_token", "label": "App Token (xapp-…)", "type": "password"},
        ],
        "agent_slug": None,
    },
    {
        "key": "discord",
        "name": "Discord",
        "description": "Community messaging through sbxloop's Discord bridge",
        "auth_type": "token",
        "color": "#5865F2",
        "fields": [{"key": "bot_token", "label": "Bot Token", "type": "password"}],
        "agent_slug": None,
    },
    {
        "key": "mattermost",
        "name": "Mattermost",
        "description": "Self-hosted messaging through sbxloop's Mattermost bridge",
        "auth_type": "token",
        "color": "#0058CC",
        "fields": [{"key": "bot_token", "label": "Bot Token", "type": "password"}],
        "agent_slug": None,
    },
)


def _problem(exc: CollaborationError) -> Problem:
    status = 404 if exc.code.endswith("not_found") else 409
    if exc.code in {"invalid_profile", "weak_password", "unknown_agent"}:
        status = 422
    return Problem(status, exc.code, exc.message)


def _user_out(user: LocalUser) -> LocalUserOut:
    return LocalUserOut(
        id=user.id,
        email=user.email,
        username=user.username,
        full_name=user.full_name,
        timezone=user.timezone,
        is_active=user.active,
        created_at=rfc3339(user.created_at) or "",
        updated_at=rfc3339(user.updated_at) or "",
    )


def _channel_out(channel: Channel) -> ChannelOut:
    return ChannelOut(
        id=channel.id,
        workspace_id=channel.workspace_id,
        title=channel.title,
        state=channel.state,
        revision=channel.revision,
        created_at=rfc3339(channel.created_at) or "",
        updated_at=rfc3339(channel.updated_at) or "",
    )


def _message_out(message: Message) -> MessageOut:
    return MessageOut(
        id=message.id,
        channel_id=message.channel_id,
        turn_id=message.turn_id,
        sequence=message.sequence,
        role=message.role,  # type: ignore[arg-type]
        kind=message.kind,
        content=message.content,
        agent_slug=message.agent_slug,
        created_at=rfc3339(message.created_at) or "",
    )


def _turn_out(turn: Turn) -> TurnOut:
    return TurnOut(
        id=turn.id,
        channel_id=turn.channel_id,
        input_message_id=turn.input_message_id,
        status=turn.status,
        targets=list(turn.targets),
        participants=[ParticipantOut.model_validate(p) for p in turn.participants],
        error=turn.error,
        created_at=rfc3339(turn.created_at) or "",
        started_at=rfc3339(turn.started_at),
        completed_at=rfc3339(turn.completed_at),
    )


def _team_out(team: Team) -> TeamOut:
    return TeamOut(
        id=team.id,
        name=team.name,
        slug=team.slug,
        description=team.description,
        goal=team.goal,
        agent_slugs=list(team.agent_slugs),
        is_enabled=team.enabled,
        created_at=rfc3339(team.created_at) or "",
        updated_at=rfc3339(team.updated_at) or "",
    )


def _preference_out(preference: Preference) -> PreferenceOut:
    return PreferenceOut(name=preference.name, content=preference.content)


def _workflow_out(workflow: Workflow) -> WorkflowOut:
    return WorkflowOut(
        id=workflow.id,
        name=workflow.name,
        slug=workflow.slug,
        description=workflow.description,
        trigger_event=workflow.trigger_event,
        is_enabled=workflow.enabled,
        created_at=rfc3339(workflow.created_at) or "",
        updated_at=rfc3339(workflow.updated_at) or "",
    )


def _local_user(ctx: ApiContext, auth: Authenticated) -> LocalUser:
    user = ctx.collaboration.user_by_client(auth.client.id)
    if user is None or not user.active:
        raise Problem(403, "local_profile_required", "this client is not the local Angie user")
    return user


def _agent_out(slug: str, config: Config) -> AgentOut:
    agent = AGENTS_BY_SLUG[slug]
    selection = model_for_phase(config, agent.phase)
    return AgentOut(
        slug=agent.slug,
        name=agent.name,
        description=agent.description,
        capabilities=list(agent.capabilities),
        category=agent.category,
        instructions=agent.instructions,
        system_prompt=agent.persona.strip(),
        backend=config.agent.backend,
        model=selection.model,
        model_source=selection.source,
        phase_models={
            phase: model_for_phase(config, phase).model
            for phase in (*ROLE_BY_PHASE, "concierge")
            if ROLE_BY_PHASE.get(phase, "concierge") == agent.role
        },
        read_only=agent.role == "critic",
    )


# -- local onboarding -------------------------------------------------------------


@router.post("/auth/local/register", response_model=TokenResponse, status_code=201)
async def register_local(
    body: LocalRegisterRequest,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
) -> TokenResponse:
    try:
        user = await ctx.call(
            ctx.collaboration.register_user,
            username=body.username,
            email=body.email,
            password=body.password,
            full_name=body.full_name,
            timezone=body.timezone,
            now=ctx.clock(),
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    client = await ctx.call(ctx.auth.authenticate, user.client_id, body.password, ctx.clock())
    ctx.hub.notify()
    return await ctx.call(grant_tokens, ctx, client, family_id=None)


@router.post("/auth/local/login", response_model=TokenResponse)
async def login_local(
    body: LocalLoginRequest,
    request: Request,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
) -> TokenResponse:
    address = request.client.host if request.client else "unknown"
    keys = [f"local-user:{body.username}", f"addr:{address}"]
    now = ctx.clock()
    for key in keys:
        wait = ctx.limiter.retry_after(key, now)
        if wait is not None:
            raise Problem(
                429,
                "too_many_attempts",
                "too many failed authentication attempts; try again later",
                headers={"Retry-After": str(int(wait) + 1)},
            )
    user = await ctx.call(ctx.collaboration.user_by_username, body.username)
    client_id = user.client_id if user is not None else "local_unknown"
    try:
        client = await ctx.call(ctx.auth.authenticate, client_id, body.password, now)
    except AuthError as exc:
        for key in keys:
            ctx.limiter.record_failure(key, now)
        raise Problem(401, "invalid_credentials", "unknown user or wrong password") from exc
    if user is None or not user.active:
        raise Problem(401, "invalid_credentials", "unknown user or wrong password")
    for key in keys:
        ctx.limiter.reset(key)
    return await ctx.call(grant_tokens, ctx, client, family_id=None)


@router.get("/users/me", response_model=LocalUserOut)
async def get_local_user(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(current),  # noqa: B008
) -> LocalUserOut:
    return _user_out(await ctx.call(_local_user, ctx, auth))


@router.patch("/users/me", response_model=LocalUserOut)
async def update_local_user(
    body: LocalUserUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> LocalUserOut:
    user = await ctx.call(_local_user, ctx, auth)
    try:
        updated = await ctx.call(
            ctx.collaboration.update_user,
            user.client_id,
            email=body.email,
            full_name=body.full_name,
            timezone=body.timezone,
            now=ctx.clock(),
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return _user_out(updated)


# -- agent catalog and teams ------------------------------------------------------


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> list[AgentOut]:
    config = await ctx.call(refreshed_models, ctx.config)
    return [_agent_out(agent.slug, config) for agent in AGENTS]


@router.get("/agents/{slug}", response_model=AgentOut)
async def get_agent(
    slug: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> AgentOut:
    if slug not in AGENTS_BY_SLUG:
        raise Problem(404, "agent_not_found", "agent not found")
    return _agent_out(slug, await ctx.call(refreshed_models, ctx.config))


def _validate_agents(slugs: list[str]) -> tuple[str, ...]:
    ordered = tuple(dict.fromkeys(slugs))
    unknown = sorted(set(ordered) - set(AGENTS_BY_SLUG))
    if unknown:
        raise Problem(422, "unknown_agent", f"unknown agent(s): {', '.join(unknown)}")
    return ordered


@router.get("/teams", response_model=list[TeamOut])
async def list_teams(
    enabled_only: bool = False,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> list[TeamOut]:
    user = await ctx.call(_local_user, ctx, auth)
    teams = await ctx.call(ctx.collaboration.list_teams, user.id, enabled_only=enabled_only)
    return [_team_out(team) for team in teams]


@router.get("/teams/{team_id}", response_model=TeamOut)
async def get_team(
    team_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> TeamOut:
    user = await ctx.call(_local_user, ctx, auth)
    team = await ctx.call(ctx.collaboration.get_team, user.id, team_id)
    if team is None:
        raise Problem(404, "team_not_found", "team not found")
    return _team_out(team)


@router.post("/teams", response_model=TeamOut, status_code=201)
async def create_team(
    body: TeamCreate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> TeamOut:
    user = await ctx.call(_local_user, ctx, auth)
    agents = _validate_agents(body.agent_slugs)
    try:
        team = await ctx.call(
            ctx.collaboration.create_team,
            user.id,
            name=body.name,
            slug=body.slug,
            description=body.description,
            goal=body.goal,
            agent_slugs=agents,
            enabled=body.is_enabled,
            now=ctx.clock(),
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return _team_out(team)


@router.patch("/teams/{team_id}", response_model=TeamOut)
async def update_team(
    team_id: str,
    body: TeamUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> TeamOut:
    user = await ctx.call(_local_user, ctx, auth)
    values = body.model_dump(exclude_unset=True)
    if "agent_slugs" in values:
        values["agent_slugs"] = _validate_agents(values["agent_slugs"])
    if "is_enabled" in values:
        values["enabled"] = values.pop("is_enabled")
    try:
        team = await ctx.call(ctx.collaboration.update_team, user.id, team_id, values, ctx.clock())
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if team is None:
        raise Problem(404, "team_not_found", "team not found")
    ctx.hub.notify()
    return _team_out(team)


@router.delete("/teams/{team_id}", status_code=204)
async def delete_team(
    team_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> None:
    user = await ctx.call(_local_user, ctx, auth)
    if not await ctx.call(ctx.collaboration.delete_team, user.id, team_id, ctx.clock()):
        raise Problem(404, "team_not_found", "team not found")
    ctx.hub.notify()


# -- user preferences ------------------------------------------------------------


def _preference_name(name: str) -> str:
    clean = name.casefold()
    if clean not in PREFERENCE_NAMES:
        raise Problem(
            422,
            "invalid_preference",
            f"unknown preference {name!r}; use one of: {', '.join(sorted(PREFERENCE_NAMES))}",
        )
    return clean


@router.get("/prompts/definitions", response_model=list[PreferenceDefinitionOut])
async def preference_definitions(
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> list[PreferenceDefinitionOut]:
    return [PreferenceDefinitionOut.model_validate(item) for item in PREFERENCE_DEFINITIONS]


@router.get("/prompts", response_model=list[PreferenceOut])
async def list_preferences(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> list[PreferenceOut]:
    user = await ctx.call(_local_user, ctx, auth)
    values = await ctx.call(ctx.collaboration.list_preferences, user.id)
    return [_preference_out(value) for value in values]


@router.post("/prompts/reset", response_model=DetailOut)
async def reset_preferences(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> DetailOut:
    user = await ctx.call(_local_user, ctx, auth)
    await ctx.call(ctx.collaboration.reset_preferences, user.id, ctx.clock())
    ctx.hub.notify()
    return DetailOut(detail="Preferences reset")


@router.get("/prompts/{name}", response_model=PreferenceOut)
async def get_preference(
    name: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> PreferenceOut:
    user = await ctx.call(_local_user, ctx, auth)
    value = await ctx.call(ctx.collaboration.get_preference, user.id, _preference_name(name))
    if value is None:
        raise Problem(404, "preference_not_found", "preference not found")
    return _preference_out(value)


@router.put("/prompts/{name}", response_model=PreferenceOut)
async def update_preference(
    name: str,
    body: PreferenceUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> PreferenceOut:
    user = await ctx.call(_local_user, ctx, auth)
    clean_name = _preference_name(name)
    content = body.content.strip()
    header = f"# {clean_name.replace('_', ' ').title()}"
    lines = content.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    preference_body = "\n".join(lines).strip()
    normalized = f"{header}\n\n{preference_body}\n" if preference_body else f"{header}\n"
    value = await ctx.call(
        ctx.collaboration.upsert_preference,
        user.id,
        clean_name,
        normalized,
        ctx.clock(),
    )
    ctx.hub.notify()
    return _preference_out(value)


@router.delete("/prompts/{name}", response_model=DetailOut)
async def delete_preference(
    name: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> DetailOut:
    user = await ctx.call(_local_user, ctx, auth)
    clean_name = _preference_name(name)
    deleted = await ctx.call(ctx.collaboration.delete_preference, user.id, clean_name, ctx.clock())
    if not deleted:
        raise Problem(404, "preference_not_found", "preference not found")
    ctx.hub.notify()
    return DetailOut(detail=f"Preference {clean_name!r} deleted")


# -- operator-managed connections -----------------------------------------------


def _connections(ctx: ApiContext) -> list[ConnectionOut]:
    values: list[ConnectionOut] = []
    github_enabled = bool(ctx.config.github.repo or ctx.config.github.repos)
    if github_enabled:
        values.append(
            ConnectionOut(
                id="github",
                service_type="github",
                display_name="sbxloop configuration",
                auth_type="api_key",
                status="connected",
                masked_credentials={"credential": "managed by sbxloop"},
            )
        )
    envs = {
        "slack": ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
        "discord": ("DISCORD_BOT_TOKEN",),
        "mattermost": ("MATTERMOST_BOT_TOKEN",),
    }
    for name, required_env in envs.items():
        section = getattr(ctx.config, name)
        if not section.enabled:
            continue
        ready = all(os.environ.get(key) for key in required_env)
        values.append(
            ConnectionOut(
                id=name,
                service_type=name,
                display_name="sbxloop configuration",
                auth_type="token",
                status="connected" if ready else "error",
                masked_credentials={
                    key: "configured" if os.environ.get(key) else "missing" for key in required_env
                },
            )
        )
    return values


def _operator_managed() -> Problem:
    return Problem(
        409,
        "operator_managed_connection",
        "configure credentials with sbxloop's environment and sbxloop.toml; "
        "remote protected credential intake is tracked by sbxloop issue #1043",
    )


@router.get("/connections/services", response_model=list[ServiceDefinitionOut])
async def connection_services(
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> list[ServiceDefinitionOut]:
    return [ServiceDefinitionOut.model_validate(item) for item in SERVICE_DEFINITIONS]


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> list[ConnectionOut]:
    return _connections(ctx)


@router.get("/connections/{connection_id}", response_model=ConnectionOut)
async def get_connection(
    connection_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> ConnectionOut:
    value = next((item for item in _connections(ctx) if item.id == connection_id), None)
    if value is None:
        raise Problem(404, "connection_not_found", "connection not found")
    return value


@router.post("/connections", response_model=ConnectionOut)
async def create_connection(
    _body: ConnectionMutation,
    _auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> ConnectionOut:
    raise _operator_managed()


@router.patch("/connections/{connection_id}", response_model=ConnectionOut)
async def update_connection(
    _connection_id: str,
    _body: ConnectionMutation,
    _auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> ConnectionOut:
    raise _operator_managed()


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(
    _connection_id: str,
    _auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> None:
    raise _operator_managed()


@router.post("/connections/{connection_id}/test", response_model=ConnectionTestOut)
async def test_connection(
    connection_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> ConnectionTestOut:
    value = next((item for item in _connections(ctx) if item.id == connection_id), None)
    if value is None:
        raise Problem(404, "connection_not_found", "connection not found")
    success = value.status == "connected"
    return ConnectionTestOut(
        success=success,
        message=(
            "sbxloop configuration is present"
            if success
            else "the sbxloop connection is configured but a required credential is missing"
        ),
        status=value.status,
    )


# -- workflow definitions --------------------------------------------------------


@router.get("/workflows", response_model=list[WorkflowOut])
async def list_workflows(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> list[WorkflowOut]:
    user = await ctx.call(_local_user, ctx, auth)
    values = await ctx.call(ctx.collaboration.list_workflows, user.id)
    return [_workflow_out(value) for value in values]


@router.post("/workflows", response_model=WorkflowOut, status_code=201)
async def create_workflow(
    body: WorkflowCreate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> WorkflowOut:
    user = await ctx.call(_local_user, ctx, auth)
    try:
        value = await ctx.call(
            ctx.collaboration.create_workflow,
            user.id,
            name=body.name,
            slug=body.slug,
            description=body.description,
            trigger_event=body.trigger_event,
            enabled=body.is_enabled,
            now=ctx.clock(),
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return _workflow_out(value)


@router.get("/workflows/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(
    workflow_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> WorkflowOut:
    user = await ctx.call(_local_user, ctx, auth)
    value = await ctx.call(ctx.collaboration.get_workflow, user.id, workflow_id)
    if value is None:
        raise Problem(404, "workflow_not_found", "workflow not found")
    return _workflow_out(value)


@router.patch("/workflows/{workflow_id}", response_model=WorkflowOut)
async def update_workflow(
    workflow_id: str,
    body: WorkflowUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> WorkflowOut:
    user = await ctx.call(_local_user, ctx, auth)
    values = body.model_dump(exclude_unset=True)
    if "is_enabled" in values:
        values["enabled"] = values.pop("is_enabled")
    try:
        value = await ctx.call(
            ctx.collaboration.update_workflow,
            user.id,
            workflow_id,
            values,
            ctx.clock(),
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if value is None:
        raise Problem(404, "workflow_not_found", "workflow not found")
    ctx.hub.notify()
    return _workflow_out(value)


@router.delete("/workflows/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> None:
    user = await ctx.call(_local_user, ctx, auth)
    deleted = await ctx.call(ctx.collaboration.delete_workflow, user.id, workflow_id, ctx.clock())
    if not deleted:
        raise Problem(404, "workflow_not_found", "workflow not found")
    ctx.hub.notify()


# -- channels, messages, turns ---------------------------------------------------


@router.get("/channels", response_model=ChannelPage)
async def list_channels(
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> ChannelPage:
    user = await ctx.call(_local_user, ctx, auth)
    channels, total = await ctx.call(
        ctx.collaboration.list_channels, user.id, limit=limit, offset=offset
    )
    return ChannelPage(
        items=[_channel_out(channel) for channel in channels],
        total=total,
        has_more=offset + len(channels) < total,
    )


@router.post("/channels", response_model=ChannelOut, status_code=201)
async def create_channel(
    body: ChannelCreate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> ChannelOut:
    user = await ctx.call(_local_user, ctx, auth)
    channel = await ctx.call(
        ctx.collaboration.create_channel, user.id, body.title or "New conversation", ctx.clock()
    )
    ctx.hub.notify()
    return _channel_out(channel)


@router.get("/channels/{channel_id}", response_model=ChannelOut)
async def get_channel(
    channel_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> ChannelOut:
    user = await ctx.call(_local_user, ctx, auth)
    channel = await ctx.call(ctx.collaboration.get_channel, user.id, channel_id)
    if channel is None:
        raise Problem(404, "channel_not_found", "channel not found")
    return _channel_out(channel)


@router.patch("/channels/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: str,
    body: ChannelUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> ChannelOut:
    user = await ctx.call(_local_user, ctx, auth)
    channel = await ctx.call(
        ctx.collaboration.update_channel, user.id, channel_id, body.title, ctx.clock()
    )
    if channel is None:
        raise Problem(404, "channel_not_found", "channel not found")
    ctx.hub.notify()
    return _channel_out(channel)


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> None:
    user = await ctx.call(_local_user, ctx, auth)
    if not await ctx.call(ctx.collaboration.delete_channel, user.id, channel_id, ctx.clock()):
        raise Problem(404, "channel_not_found", "channel not found")
    ctx.hub.notify()


@router.get("/channels/{channel_id}/messages", response_model=list[MessageOut])
async def list_messages(
    channel_id: str,
    after: Annotated[int, Query(ge=0)] = 0,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> list[MessageOut]:
    user = await ctx.call(_local_user, ctx, auth)
    messages = await ctx.call(ctx.collaboration.list_messages, user.id, channel_id, after=after)
    if messages is None:
        raise Problem(404, "channel_not_found", "channel not found")
    return [_message_out(message) for message in messages]


async def _targets(
    ctx: ApiContext, user: LocalUser, content: str, requested: list[str]
) -> tuple[str, ...]:
    selectors = list(requested)
    selectors.extend(match.group(1).casefold() for match in MENTION.finditer(content))
    result: list[str] = []
    for selector in dict.fromkeys(selectors):
        if selector in AGENTS_BY_SLUG:
            result.append(selector)
            continue
        team = await ctx.call(ctx.collaboration.get_team, user.id, selector)
        if team is not None and team.enabled:
            result.extend(team.agent_slugs)
            continue
        if selector in requested:
            raise Problem(422, "unknown_target", f"unknown agent or team: {selector}")
    return tuple(dict.fromkeys(result))


@router.post("/channels/{channel_id}/turns", response_model=TurnAccepted, status_code=202)
async def create_turn(
    channel_id: str,
    body: TurnCreate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:delegate")),  # noqa: B008
) -> TurnAccepted:
    if not ctx.collaboration_available:
        raise Problem(
            503,
            "collaboration_runtime_unavailable",
            "the sbxloop concierge is disabled or still starting",
        )
    user = await ctx.call(_local_user, ctx, auth)
    targets = await _targets(ctx, user, body.content, body.target_slugs)
    intent = "delegate" if targets else body.intent
    try:
        turn, message, created = await ctx.call(
            ctx.accept_collaboration_turn,
            user,
            channel_id,
            content=body.content.strip(),
            targets=targets,
            client_turn_id=body.client_turn_id,
            client_message_id=body.client_message_id,
            actor=auth.principal.audit(),
            intent=intent,
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if created:
        ctx.hub.notify()
    return TurnAccepted(turn=_turn_out(turn), message=_message_out(message), replayed=not created)


@router.get("/channels/{channel_id}/turns/{turn_id}", response_model=TurnOut)
async def get_turn(
    channel_id: str,
    turn_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> TurnOut:
    user = await ctx.call(_local_user, ctx, auth)
    turn = await ctx.call(ctx.collaboration.get_turn, user.id, channel_id, turn_id)
    if turn is None:
        raise Problem(404, "turn_not_found", "turn not found")
    return _turn_out(turn)


@router.get("/channels/{channel_id}/turns", response_model=list[TurnOut])
async def list_turns(
    channel_id: str,
    active_only: bool = False,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> list[TurnOut]:
    user = await ctx.call(_local_user, ctx, auth)
    try:
        turns = await ctx.call(
            ctx.collaboration.list_turns, user.id, channel_id, active_only=active_only
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    return [_turn_out(turn) for turn in turns]


@router.post("/channels/{channel_id}/turns/{turn_id}/cancel", response_model=TurnOut)
async def cancel_turn(
    channel_id: str,
    turn_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:delegate")),  # noqa: B008
) -> TurnOut:
    user = await ctx.call(_local_user, ctx, auth)
    turn = await ctx.call(ctx.collaboration.cancel_turn, user.id, channel_id, turn_id, ctx.clock())
    if turn is None:
        raise Problem(404, "turn_not_found", "turn not found")
    ctx.hub.notify()
    return _turn_out(turn)


__all__ = ["router"]
