"""The agent directory: the built-ins, ``[[agents]]`` and people's own agents.

Reads cover every source; only a person's own agents are created, edited
(against the revision the caller last read) and archived, and only by the
person who saved one or a workspace owner or admin (403 ``agent_forbidden``
for anyone else). A built-in or configured agent answers 409
``agent_read_only``. Every agent, from any
source, also has a long-term memory a person can list, add to, edit and
forget (``/agents/{slug}/memories``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from sbxloop.agentmodels import model_for_phase, refreshed_models
from sbxloop.agents.definition import AgentDefinition as RegistryAgent, AgentSpec
from sbxloop.agents.memory import AgentMemoryError, Memory, WorkspaceChannelVisibility
from sbxloop.agents.registry import (
    AgentArchived,
    AgentExists,
    AgentForbidden,
    AgentInvalid,
    AgentNotFound,
    AgentRegistry,
    AgentRegistryReadOnly,
    AgentRevisionConflict,
    AgentSlugTaken,
    addressable,
)
from sbxloop.api.agents import AgentDefinition
from sbxloop.api.auth.deps import Authenticated, get_ctx, require, role_of
from sbxloop.api.channel_access import MANAGING_ROLES
from sbxloop.api.collaboration_schemas import (
    AgentCreate,
    AgentOut,
    AgentUpdate,
    MemoryCreate,
    MemoryOut,
    MemoryUpdate,
)
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import rfc3339
from sbxloop.config import Config
from sbxloop.db.collaboration_models import ChannelRow
from sbxloop.engine.harness import ROLE_BY_PHASE
from sbxloop.errors import SbxloopError

__all__ = ["addressable", "agent_out", "router"]

router = APIRouter(prefix="/v1", tags=["collaboration"])


def agent_out(agent: RegistryAgent, config: Config) -> AgentOut:
    view = AgentDefinition.from_registry(agent)
    selection = model_for_phase(config, view.phase)
    spec = agent.spec
    return AgentOut(
        slug=view.slug,
        name=view.name,
        description=view.description,
        capabilities=list(view.capabilities),
        category=view.category,
        instructions=view.instructions,
        system_prompt=view.persona.strip(),
        backend=config.agent.backend,
        model=spec.model or selection.model,
        model_source="agent.model" if spec.model else selection.source,
        phase_models={
            phase: model_for_phase(config, phase).model
            for phase in (*ROLE_BY_PHASE, "concierge")
            if ROLE_BY_PHASE.get(phase, "concierge") == view.role
        },
        read_only=view.role == "critic",
        avatar=spec.avatar,
        color=spec.color,
        roles=list(spec.roles),
        tools=None if spec.tools is None else list(spec.tools),
        skills=None if spec.skills is None else list(spec.skills),
        mcp=None if spec.mcp is None else list(spec.mcp),
        credentials=list(spec.credentials),
        interests=list(spec.interests),
        can_start=list(spec.can_start),
        max_runs_per_day=spec.max_runs_per_day,
        aliases=list(spec.aliases),
        enabled=agent.active,
        source=agent.source,
        editable=not agent.read_only and not agent.archived,
        revision=agent.revision,
    )


def _problem(exc: SbxloopError) -> Problem:
    if isinstance(exc, AgentInvalid):
        return Problem(422, "invalid_agent", str(exc), problems=exc.problems)
    if isinstance(exc, AgentRevisionConflict):
        return Problem(409, "agent_revision_conflict", str(exc), current_revision=exc.current)
    if isinstance(exc, AgentNotFound):
        return Problem(404, "agent_not_found", "agent not found")
    if isinstance(exc, AgentExists):
        return Problem(409, "agent_exists", str(exc))
    if isinstance(exc, AgentSlugTaken):
        return Problem(409, "slug_taken", str(exc))
    if isinstance(exc, AgentArchived):
        return Problem(409, "agent_archived", str(exc))
    if isinstance(exc, AgentRegistryReadOnly):
        return Problem(409, "agent_read_only", str(exc))
    if isinstance(exc, AgentForbidden):
        return Problem(403, "agent_forbidden", str(exc))
    raise exc


_REFUSALS = (
    AgentInvalid,
    AgentRevisionConflict,
    AgentNotFound,
    AgentExists,
    AgentArchived,
    AgentRegistryReadOnly,
    AgentSlugTaken,
    AgentForbidden,
)


def _saver(auth: Authenticated) -> tuple[str, bool]:
    """Who saves, edits or archives an agent: the local user behind the
    client (so a person keeps their agents from every client they sign in
    with), or the client itself when no user stands behind it. The flag
    says whether they may change anyone's agent: a workspace owner or
    admin, which for a plain API client means holding ``daemon:manage``."""
    member = auth.member
    who = member.user.id if member is not None else auth.client.id
    return who, role_of(auth) in MANAGING_ROLES


def _found(registry: AgentRegistry, slug: str) -> RegistryAgent:
    agent = registry.get(slug)
    if agent is None:
        raise Problem(404, "agent_not_found", "agent not found")
    return agent


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(
    include_disabled: bool = False,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> list[AgentOut]:
    """The agents a client shows. ``include_disabled`` (for clients that may
    edit agents, ``collaboration:write``) adds disabled and archived agents
    and the legacy built-in names, so an agent switched off can be found and
    switched back on."""
    if include_disabled and not auth.principal.can("collaboration:write"):
        raise Problem(
            403,
            "forbidden",
            f"{auth.principal.id} lacks collaboration:write",
            capability="collaboration:write",
        )
    config = await ctx.call(refreshed_models, ctx.config)
    agents = await ctx.call(ctx.agents.list, include_disabled)
    return [agent_out(agent, config) for agent in agents]


@router.get("/agents/{slug}", response_model=AgentOut)
async def get_agent(
    slug: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> AgentOut:
    agent = await ctx.call(_found, ctx.agents, slug)
    return agent_out(agent, await ctx.call(refreshed_models, ctx.config))


@router.post("/agents", response_model=AgentOut, status_code=201)
async def create_agent(
    body: AgentCreate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> AgentOut:
    spec = AgentSpec.model_validate(body.model_dump())
    by, _ = _saver(auth)
    try:
        agent = await ctx.call(ctx.agents.create, spec, by)
    except _REFUSALS as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return agent_out(agent, await ctx.call(refreshed_models, ctx.config))


@router.patch("/agents/{slug}", response_model=AgentOut)
async def update_agent(
    slug: str,
    body: AgentUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> AgentOut:
    patch = body.model_dump(exclude_unset=True)
    expected = patch.pop("expected_revision")
    by, manager = _saver(auth)
    try:
        agent = await ctx.call(ctx.agents.update, slug, patch, expected, by, manager=manager)
    except _REFUSALS as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return agent_out(agent, await ctx.call(refreshed_models, ctx.config))


@router.post("/agents/{slug}/archive", response_model=AgentOut)
async def archive_agent(
    slug: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> AgentOut:
    by, manager = _saver(auth)
    try:
        agent = await ctx.call(ctx.agents.archive, slug, by, manager=manager)
    except _REFUSALS as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return agent_out(agent, await ctx.call(refreshed_models, ctx.config))


#: Member roles that may read memories learned in channels they cannot open.
_PRIVATE_READERS = frozenset({"owner", "admin"})


def _memory_out(memory: Memory) -> MemoryOut:
    return MemoryOut(
        id=memory.id,
        agent_slug=memory.agent_slug,
        kind=memory.kind,  # type: ignore[arg-type]
        content=memory.content,
        source_channel_id=memory.source_channel_id,
        source_run_id=memory.source_run_id,
        source_message_id=memory.source_message_id,
        author=memory.author,
        pinned=memory.pinned,
        created_at=rfc3339(memory.created_at) or "",
        updated_at=rfc3339(memory.updated_at) or "",
        last_used_at=rfc3339(memory.last_used_at),
        revision=memory.revision,
    )


def _memory_problem(exc: AgentMemoryError) -> Problem:
    status = {
        "memory_not_found": 404,
        "invalid_memory": 422,
    }.get(exc.code, 409)
    return Problem(status, exc.code, exc.message)


def _agent_slug(ctx: ApiContext, slug: str) -> str:
    """The canonical slug ``slug`` (or an alias) names in the registry,
    saved agents included; 404 otherwise."""
    return _found(ctx.agents, slug).slug


def _author(ctx: ApiContext, auth: Authenticated) -> str:
    """The person behind the request: the local user, or the client itself
    for a plain API client."""
    user = ctx.collaboration.user_by_client(auth.client.id)
    if user is not None:
        if not user.active:
            raise Problem(403, "local_profile_required", "this local profile is inactive")
        return f"user:{user.id}"
    return f"user:{auth.client.id}"


def _check_channel(ctx: ApiContext, auth: Authenticated, channel_id: str) -> None:
    """A channel the caller may address: its own for the local user, any
    live channel for a plain API client."""
    user = ctx.collaboration.user_by_client(auth.client.id)
    if user is not None:
        found = ctx.collaboration.get_channel(user.id, channel_id) is not None
    else:
        with ctx.loop.dstore.read() as session:
            row = session.get(ChannelRow, channel_id)
            found = row is not None and row.state == "active"
    if not found:
        raise Problem(404, "channel_not_found", "channel not found")


def _reader(ctx: ApiContext, auth: Authenticated) -> Callable[[str], bool] | None:
    """The channels whose memories the caller may see: every channel for a
    plain API client or a workspace owner or admin (None), otherwise the
    workspace channels and the ones the member created or belongs to."""
    member = auth.member
    if member is None or member.role in _PRIVATE_READERS:
        return None
    return WorkspaceChannelVisibility(ctx.loop.dstore).readable_by(member.user.id)


@router.get("/agents/{slug}/memories", response_model=list[MemoryOut])
async def list_memories(
    slug: str,
    channel_id: Annotated[str | None, Query(max_length=128)] = None,
    q: Annotated[str | None, Query(max_length=1000)] = None,
    include_private: bool = False,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:read")),  # noqa: B008
) -> list[MemoryOut]:
    agent = await ctx.call(_agent_slug, ctx, slug)
    if channel_id is not None:
        await ctx.call(_check_channel, ctx, auth, channel_id)
    readable = await ctx.call(_reader, ctx, auth)
    memories = await ctx.call(
        ctx.memory.list,
        agent,
        channel_id=channel_id,
        include_private=include_private,
        query=q,
        readable=readable,
    )
    return [_memory_out(memory) for memory in memories]


@router.post("/agents/{slug}/memories", response_model=MemoryOut, status_code=201)
async def create_memory(
    slug: str,
    body: MemoryCreate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> MemoryOut:
    agent = await ctx.call(_agent_slug, ctx, slug)
    if body.channel_id is not None:
        await ctx.call(_check_channel, ctx, auth, body.channel_id)
    author = await ctx.call(_author, ctx, auth)
    try:
        memory = await ctx.call(
            ctx.memory.remember,
            agent,
            body.content,
            kind=body.kind,
            channel_id=body.channel_id,
            author=author,
            pinned=body.pinned,
        )
    except AgentMemoryError as exc:
        raise _memory_problem(exc) from exc
    ctx.hub.notify()
    return _memory_out(memory)


@router.patch("/agents/{slug}/memories/{memory_id}", response_model=MemoryOut)
async def update_memory(
    slug: str,
    memory_id: str,
    body: MemoryUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> MemoryOut:
    agent = await ctx.call(_agent_slug, ctx, slug)
    author = await ctx.call(_author, ctx, auth)
    try:
        memory = await ctx.call(
            ctx.memory.update,
            memory_id,
            content=body.content,
            pinned=body.pinned,
            expected_revision=body.expected_revision,
            author=author,
            agent=agent,
        )
    except AgentMemoryError as exc:
        raise _memory_problem(exc) from exc
    ctx.hub.notify()
    return _memory_out(memory)


@router.delete("/agents/{slug}/memories/{memory_id}", status_code=204)
async def delete_memory(
    slug: str,
    memory_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("collaboration:write")),  # noqa: B008
) -> None:
    agent = await ctx.call(_agent_slug, ctx, slug)
    author = await ctx.call(_author, ctx, auth)
    try:
        await ctx.call(ctx.memory.forget, agent, memory_id, author=author)
    except AgentMemoryError as exc:
        raise _memory_problem(exc) from exc
    ctx.hub.notify()
