"""Local profile, channel, message, turn, agent and team resources."""

from __future__ import annotations

import os
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from sbxloop.agents.registry import AgentRegistry
from sbxloop.api.auth.deps import (
    Authenticated,
    current,
    current_member,
    get_ctx,
    member_of,
    require,
)
from sbxloop.api.auth.store import AuthError
from sbxloop.api.collaboration import (
    Author,
    Channel,
    ChannelMember,
    ChannelParticipant,
    CollaborationError,
    LocalUser,
    Member,
    Message,
    Preference,
    Role,
    Team,
    Turn,
    Workflow,
)
from sbxloop.api.collaboration_schemas import (
    AuthorOut,
    ChannelCreate,
    ChannelMemberCreate,
    ChannelMemberOut,
    ChannelMemberPage,
    ChannelMemberUserOut,
    ChannelOut,
    ChannelPage,
    ChannelParticipantOut,
    ChannelParticipantPage,
    ChannelParticipantUpdate,
    ChannelUpdate,
    ChannelWorkOut,
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
    ReactionSet,
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
from sbxloop.api.routes.agents import addressable
from sbxloop.api.routes.auth import grant_tokens

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
        "key": "gitlab",
        "name": "GitLab",
        "description": "GitLab repositories, merge requests, and issues",
        "auth_type": "api_key",
        "color": "#FC6D26",
        "fields": [],
        "agent_slug": None,
        "available": False,
        "unavailable_reason": (
            "GitLab execution is not available in this SBXLOOP version. "
            "Connection setup will be available when its forge backend is implemented."
        ),
    },
    {
        "key": "gitea",
        "name": "Gitea",
        "description": "Self-hosted Gitea repositories, pull requests, and issues",
        "auth_type": "api_key",
        "color": "#609926",
        "fields": [],
        "agent_slug": None,
        "available": False,
        "unavailable_reason": (
            "Gitea execution is not available in this SBXLOOP version. "
            "Connection setup will be available when its forge backend is implemented."
        ),
    },
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
    if exc.code in {
        "invite_invalid",
        "invite_expired",
        "invite_email_mismatch",
        "owner_required",
        "channel_forbidden",
    }:
        status = 403
    return Problem(status, exc.code, exc.message)


def _user_out(user: LocalUser, role: Role | None = None) -> LocalUserOut:
    return LocalUserOut(
        id=user.id,
        email=user.email,
        username=user.username,
        full_name=user.full_name,
        timezone=user.timezone,
        is_active=user.active,
        created_at=rfc3339(user.created_at) or "",
        updated_at=rfc3339(user.updated_at) or "",
        role=role,
        avatar_url=user.avatar_url,
        auth_source="oidc" if user.auth_source == "oidc" else "local",
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
        visibility="workspace" if channel.visibility == "workspace" else "private",
        created_by=channel.created_by,
        silenced_until=channel.silenced_until,
        my_role=channel.my_role,
    )


def _channel_member_out(member: ChannelMember) -> ChannelMemberOut:
    return ChannelMemberOut(
        user_id=member.user.id,
        role=member.role,
        joined_at=rfc3339(member.joined_at) or "",
        last_read_sequence=member.last_read_sequence,
        user=ChannelMemberUserOut(
            id=member.user.id,
            username=member.user.username,
            full_name=member.user.full_name,
            avatar_url=member.user.avatar_url,
        ),
    )


def _participant_out(
    participant: ChannelParticipant,
    ctx: ApiContext,
    thinking: set[str],
    working: dict[str, str],
) -> ChannelParticipantOut:
    slug = participant.agent_slug
    status: Literal["idle", "thinking", "working"] = "idle"
    activity = None
    if slug in working:
        status, activity = "working", working[slug]
    elif slug in thinking:
        status = "thinking"
    return ChannelParticipantOut(
        agent_slug=slug,
        mode=participant.mode,
        added_by=_author_out(participant.added_by, ctx),
        muted_until=participant.muted_until,
        created_at=rfc3339(participant.created_at) or "",
        status=status,
        activity=activity,
    )


def _author_out(author: Author, ctx: ApiContext) -> AuthorOut:
    display_name = author.display_name
    if author.kind == "agent" and display_name is None and author.id is not None:
        agent = ctx.agents.get(author.id)
        display_name = author.id if agent is None else agent.spec.name
    return AuthorOut(kind=author.kind, id=author.id, display_name=display_name)


def _messages_out(messages: list[Message], ctx: ApiContext) -> list[MessageOut]:
    """Project messages; an agent author's name may need a registry read
    (saved agents live in the store), so callers run this through ctx.call."""
    return [_message_out(message, ctx) for message in messages]


def _message_out(message: Message, ctx: ApiContext) -> MessageOut:
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
        work=ChannelWorkOut.model_validate(message.work) if message.work else None,
        reactions=list(message.reactions),
        author=_author_out(message.author, ctx),
    )


def _turn_out(turn: Turn) -> TurnOut:
    return TurnOut(
        id=turn.id,
        channel_id=turn.channel_id,
        input_message_id=turn.input_message_id,
        status=turn.status,
        targets=list(turn.targets),
        participants=[
            ParticipantOut.model_validate(
                {key: value for key, value in p.items() if key in ParticipantOut.model_fields}
            )
            for p in turn.participants
        ],
        error=turn.error,
        created_at=rfc3339(turn.created_at) or "",
        started_at=rfc3339(turn.started_at),
        completed_at=rfc3339(turn.completed_at),
        author_id=None if turn.author is None else turn.author.id,
        trigger=turn.trigger,
        parent_turn_id=turn.parent_turn_id,
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
    """The calling member's user; kept for callers of the old helper."""
    del ctx
    return member_of(auth).user


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
            invite_token=body.invite_token,
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
    member: Member = Depends(current_member),  # noqa: B008
) -> LocalUserOut:
    return _user_out(member.user, member.role)


@router.patch("/users/me", response_model=LocalUserOut)
async def update_local_user(
    body: LocalUserUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> LocalUserOut:
    user = member.user
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
    return _user_out(updated, member.role)


# -- teams ---------------------------------------------------------------------------


def _validate_agents(registry: AgentRegistry, slugs: list[str]) -> tuple[str, ...]:
    ordered = tuple(dict.fromkeys(slugs))
    unknown = sorted(slug for slug in ordered if not addressable(registry.get(slug), slug))
    if unknown:
        raise Problem(422, "unknown_agent", f"unknown agent(s): {', '.join(unknown)}")
    return ordered


def _refuse_agent_name(registry: AgentRegistry, slug: str) -> None:
    """A mention resolves an agent before a team, so a team may not take a
    name an agent (enabled or not) answers to."""
    if registry.get(slug) is not None:
        raise Problem(409, "slug_taken", f"an agent is already called {slug}")


@router.get("/teams", response_model=list[TeamOut])
async def list_teams(
    enabled_only: bool = False,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> list[TeamOut]:
    user = member.user
    teams = await ctx.call(ctx.collaboration.list_teams, user.id, enabled_only=enabled_only)
    return [_team_out(team) for team in teams]


@router.get("/teams/{team_id}", response_model=TeamOut)
async def get_team(
    team_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> TeamOut:
    user = member.user
    team = await ctx.call(ctx.collaboration.get_team, user.id, team_id)
    if team is None:
        raise Problem(404, "team_not_found", "team not found")
    return _team_out(team)


@router.post("/teams", response_model=TeamOut, status_code=201)
async def create_team(
    body: TeamCreate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> TeamOut:
    user = member.user
    await ctx.call(_refuse_agent_name, ctx.agents, body.slug)
    agents = await ctx.call(_validate_agents, ctx.agents, body.agent_slugs)
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
    member: Member = Depends(current_member),  # noqa: B008
) -> TeamOut:
    user = member.user
    values = body.model_dump(exclude_unset=True)
    if values.get("slug") is not None:
        await ctx.call(_refuse_agent_name, ctx.agents, values["slug"])
    if "agent_slugs" in values:
        values["agent_slugs"] = await ctx.call(_validate_agents, ctx.agents, values["agent_slugs"])
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
    member: Member = Depends(current_member),  # noqa: B008
) -> None:
    user = member.user
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
    member: Member = Depends(current_member),  # noqa: B008
) -> list[PreferenceOut]:
    user = member.user
    values = await ctx.call(ctx.collaboration.list_preferences, user.id)
    return [_preference_out(value) for value in values]


@router.post("/prompts/reset", response_model=DetailOut)
async def reset_preferences(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> DetailOut:
    user = member.user
    await ctx.call(ctx.collaboration.reset_preferences, user.id, ctx.clock())
    ctx.hub.notify()
    return DetailOut(detail="Preferences reset")


@router.get("/prompts/{name}", response_model=PreferenceOut)
async def get_preference(
    name: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> PreferenceOut:
    user = member.user
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
    member: Member = Depends(current_member),  # noqa: B008
) -> PreferenceOut:
    user = member.user
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
    member: Member = Depends(current_member),  # noqa: B008
) -> DetailOut:
    user = member.user
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
    member: Member = Depends(current_member),  # noqa: B008
) -> list[WorkflowOut]:
    user = member.user
    values = await ctx.call(ctx.collaboration.list_workflows, user.id)
    return [_workflow_out(value) for value in values]


@router.post("/workflows", response_model=WorkflowOut, status_code=201)
async def create_workflow(
    body: WorkflowCreate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> WorkflowOut:
    user = member.user
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
    member: Member = Depends(current_member),  # noqa: B008
) -> WorkflowOut:
    user = member.user
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
    member: Member = Depends(current_member),  # noqa: B008
) -> WorkflowOut:
    user = member.user
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
    member: Member = Depends(current_member),  # noqa: B008
) -> None:
    user = member.user
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
    member: Member = Depends(current_member),  # noqa: B008
) -> ChannelPage:
    channels, total = await ctx.call(
        ctx.collaboration.list_channels, member, limit=limit, offset=offset
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
    member: Member = Depends(current_member),  # noqa: B008
) -> ChannelOut:
    user = member.user
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
    member: Member = Depends(current_member),  # noqa: B008
) -> ChannelOut:
    channel = await ctx.call(ctx.collaboration.get_channel, member, channel_id)
    if channel is None:
        raise Problem(404, "channel_not_found", "channel not found")
    return _channel_out(channel)


@router.patch("/channels/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: str,
    body: ChannelUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> ChannelOut:
    try:
        channel = await ctx.call(
            ctx.collaboration.update_channel,
            member,
            channel_id,
            body.title,
            ctx.clock(),
            visibility=body.visibility,
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if channel is None:
        raise Problem(404, "channel_not_found", "channel not found")
    ctx.hub.notify()
    return _channel_out(channel)


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> None:
    try:
        deleted = await ctx.call(ctx.collaboration.delete_channel, member, channel_id, ctx.clock())
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if not deleted:
        raise Problem(404, "channel_not_found", "channel not found")
    ctx.hub.notify()


@router.get("/channels/{channel_id}/messages", response_model=list[MessageOut])
async def list_messages(
    channel_id: str,
    after: Annotated[int, Query(ge=0)] = 0,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> list[MessageOut]:
    channel = await ctx.call(ctx.collaboration.get_channel, member, channel_id)
    if channel is None:
        raise Problem(404, "channel_not_found", "channel not found")
    await ctx.call(ctx.project_work, channel_id)
    messages = await ctx.call(ctx.collaboration.list_messages, member, channel_id, after=after)
    if messages is None:
        raise Problem(404, "channel_not_found", "channel not found")
    return await ctx.call(_messages_out, list(messages), ctx)


@router.put("/channels/{channel_id}/messages/{message_id}/reaction", response_model=MessageOut)
async def set_message_reaction(
    channel_id: str,
    message_id: str,
    body: ReactionSet,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> MessageOut:
    try:
        message = await ctx.call(
            ctx.collaboration.set_message_reaction,
            member,
            channel_id,
            message_id,
            emoji=body.emoji,
            active=body.active,
            now=ctx.clock(),
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if message is None:
        raise Problem(404, "message_not_found", "message not found")
    ctx.hub.notify()
    return await ctx.call(_message_out, message, ctx)


@router.get("/channels/{channel_id}/work", response_model=list[ChannelWorkOut])
async def channel_work(
    channel_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> list[ChannelWorkOut]:
    channel = await ctx.call(ctx.collaboration.get_channel, member, channel_id)
    if channel is None:
        raise Problem(404, "channel_not_found", "channel not found")
    return await ctx.call(ctx.project_work, channel_id)


async def _targets(
    ctx: ApiContext, user: LocalUser, content: str, requested: list[str]
) -> tuple[str, ...]:
    selectors = list(requested)
    selectors.extend(match.group(1).casefold() for match in MENTION.finditer(content))
    result: list[str] = []
    for selector in dict.fromkeys(selectors):
        agent = await ctx.call(ctx.agents.get, selector)
        if addressable(agent, selector):
            result.append(selector)
            continue
        team = await ctx.call(ctx.collaboration.get_team, user.id, selector)
        if team is not None and team.enabled:
            for slug in team.agent_slugs:
                if addressable(await ctx.call(ctx.agents.get, slug), slug):
                    result.append(slug)
            continue
        if selector in requested:
            raise Problem(422, "unknown_target", f"unknown agent or team: {selector}")
    return tuple(dict.fromkeys(result))


def _mentioned_agents(ctx: ApiContext, content: str, targets: tuple[str, ...]) -> tuple[str, ...]:
    """The agents a turn names, by ``@slug`` or as a target: each joins the
    channel. A runner turn's mentions count too, though they seed no reply.
    Reads the registry, so it runs through ``ctx.call``."""
    slugs: list[str] = []
    for selector in (*targets, *(m.group(1).casefold() for m in MENTION.finditer(content))):
        agent = _addressable(ctx, selector)
        if agent is not None:
            slugs.append(agent)
    return tuple(dict.fromkeys(slugs))


def _addressable(ctx: ApiContext, slug: str) -> str | None:
    """``slug``, when it names an agent a mention may reach. Reads the
    registry, so it runs through ``ctx.call``."""
    key = slug.strip().casefold()
    return key if addressable(ctx.agents.get(key), key) else None


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
    # The member is checked after availability, so a stopped concierge is
    # reported as such whoever asks.
    user = member_of(auth).user
    runner_selected = body.intent in {"code", "workload"}
    if runner_selected and body.target_slugs:
        raise Problem(
            422,
            "runner_target_conflict",
            "Code and workload runner turns are coordinated by Angie; omit target_slugs.",
        )
    # Agent mentions remain part of an explicit runner ask, but do not seed
    # parallel chat participants. The runner owns its own internal roles.
    targets = () if runner_selected else await _targets(ctx, user, body.content, body.target_slugs)
    intent = "delegate" if targets else body.intent
    participants = await ctx.call(_mentioned_agents, ctx, body.content, targets)
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
            participants=participants,
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if created:
        ctx.hub.notify()
    message_out = await ctx.call(_message_out, message, ctx)
    return TurnAccepted(turn=_turn_out(turn), message=message_out, replayed=not created)


@router.get("/channels/{channel_id}/turns/{turn_id}", response_model=TurnOut)
async def get_turn(
    channel_id: str,
    turn_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> TurnOut:
    turn = await ctx.call(ctx.collaboration.get_turn, member, channel_id, turn_id)
    if turn is None:
        raise Problem(404, "turn_not_found", "turn not found")
    return _turn_out(turn)


@router.get("/channels/{channel_id}/turns", response_model=list[TurnOut])
async def list_turns(
    channel_id: str,
    active_only: bool = False,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> list[TurnOut]:
    try:
        turns = await ctx.call(
            ctx.collaboration.list_turns, member, channel_id, active_only=active_only
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
    member: Member = Depends(current_member),  # noqa: B008
) -> TurnOut:
    try:
        turn = await ctx.call(
            ctx.collaboration.cancel_turn, member, channel_id, turn_id, ctx.clock()
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if turn is None:
        raise Problem(404, "turn_not_found", "turn not found")
    ctx.hub.notify()
    return _turn_out(turn)


# -- channel members and participants --------------------------------------------


@router.get("/channels/{channel_id}/members", response_model=ChannelMemberPage)
async def list_channel_members(
    channel_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> ChannelMemberPage:
    try:
        members = await ctx.call(ctx.collaboration.list_channel_members, member, channel_id)
    except CollaborationError as exc:
        raise _problem(exc) from exc
    return ChannelMemberPage(data=[_channel_member_out(value) for value in members])


@router.post(
    "/channels/{channel_id}/members",
    response_model=ChannelMemberOut,
    status_code=201,
    responses={200: {"model": ChannelMemberOut, "description": "A current member's role changed"}},
)
async def add_channel_member(
    channel_id: str,
    body: ChannelMemberCreate,
    response: Response,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> ChannelMemberOut:
    """Add a workspace member to the channel (201); takes managing the
    channel. An explicit ``role`` for a current member with another role
    changes it in place (200)."""
    try:
        entry, added = await ctx.call(
            ctx.collaboration.add_channel_member,
            member,
            channel_id,
            body.user_id,
            body.role,
            ctx.clock(),
            change_role="role" in body.model_fields_set,
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    if not added:
        response.status_code = 200
    ctx.hub.notify()
    return _channel_member_out(entry)


@router.delete("/channels/{channel_id}/members/{user_id}", status_code=204)
async def remove_channel_member(
    channel_id: str,
    user_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> Response:
    """Remove someone from the channel (managing it), or leave it (one's own
    id). The last owner cannot leave while anyone else remains."""
    try:
        await ctx.call(
            ctx.collaboration.remove_channel_member, member, channel_id, user_id, ctx.clock()
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return Response(status_code=204)


async def _participants_out(
    ctx: ApiContext, channel_id: str, participants: list[ChannelParticipant]
) -> list[ChannelParticipantOut]:
    """Participants with their current activity: ``thinking`` from the
    channel's running turns, ``working`` from its live, credited runs."""
    thinking = await ctx.call(ctx.collaboration.thinking_agents, channel_id)
    working: dict[str, str] = {}
    for work in await ctx.call(ctx.project_work, channel_id):
        snapshot = work if isinstance(work, dict) else work.model_dump()
        if (
            snapshot.get("run_id")
            and snapshot.get("agent_slug")
            and snapshot.get("state") not in TERMINAL_WORK_STATES
        ):
            working.setdefault(str(snapshot["agent_slug"]), str(snapshot.get("title") or ""))
    return [_participant_out(value, ctx, thinking, working) for value in participants]


#: Work states after which a run no longer keeps its agent busy.
TERMINAL_WORK_STATES = frozenset({"merged", "completed", "failed", "blocked", "cancelled", "gated"})


@router.get("/channels/{channel_id}/participants", response_model=ChannelParticipantPage)
async def list_channel_participants(
    channel_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> ChannelParticipantPage:
    try:
        participants = await ctx.call(ctx.collaboration.list_participants, member, channel_id)
    except CollaborationError as exc:
        raise _problem(exc) from exc
    return ChannelParticipantPage(data=await _participants_out(ctx, channel_id, participants))


@router.put("/channels/{channel_id}/participants/{slug}", response_model=ChannelParticipantOut)
async def put_channel_participant(
    channel_id: str,
    slug: str,
    body: ChannelParticipantUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> ChannelParticipantOut:
    """Add an agent to the channel, or change how it takes part."""
    channel = await ctx.call(ctx.collaboration.get_channel, member, channel_id)
    if channel is None:
        raise Problem(404, "channel_not_found", "channel not found")
    agent = await ctx.call(_addressable, ctx, slug)
    if agent is None:
        raise Problem(404, "agent_not_found", "agent not found")
    try:
        participant = await ctx.call(
            ctx.collaboration.put_participant,
            member,
            channel_id,
            agent,
            body.model_dump(exclude_unset=True),
            ctx.clock(),
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return (await _participants_out(ctx, channel_id, [participant]))[0]


@router.delete("/channels/{channel_id}/participants/{slug}", status_code=204)
async def remove_channel_participant(
    channel_id: str,
    slug: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
    member: Member = Depends(current_member),  # noqa: B008
) -> Response:
    try:
        await ctx.call(
            ctx.collaboration.remove_participant,
            member,
            channel_id,
            slug.strip().casefold(),
            ctx.clock(),
        )
    except CollaborationError as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return Response(status_code=204)


__all__ = ["router"]
