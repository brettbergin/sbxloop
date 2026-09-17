"""The agent directory: the built-ins, ``[[agents]]`` and people's own agents.

Reads cover every source; only a person's own agents are created, edited
(against the revision the caller last read) and archived. A built-in or
configured agent answers 409 ``agent_read_only``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from sbxloop.agentmodels import model_for_phase, refreshed_models
from sbxloop.agents.definition import AgentDefinition as RegistryAgent, AgentSpec
from sbxloop.agents.registry import (
    AgentArchived,
    AgentExists,
    AgentInvalid,
    AgentNotFound,
    AgentRegistry,
    AgentRegistryReadOnly,
    AgentRevisionConflict,
    AgentSlugTaken,
    addressable,
)
from sbxloop.api.agents import AgentDefinition
from sbxloop.api.auth.deps import Authenticated, get_ctx, require
from sbxloop.api.collaboration_schemas import AgentCreate, AgentOut, AgentUpdate
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.config import Config
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
    raise exc


_REFUSALS = (
    AgentInvalid,
    AgentRevisionConflict,
    AgentNotFound,
    AgentExists,
    AgentArchived,
    AgentRegistryReadOnly,
    AgentSlugTaken,
)


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
    try:
        agent = await ctx.call(ctx.agents.create, spec, auth.principal.id)
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
    try:
        agent = await ctx.call(ctx.agents.update, slug, patch, expected, auth.principal.id)
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
    try:
        agent = await ctx.call(ctx.agents.archive, slug, auth.principal.id)
    except _REFUSALS as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return agent_out(agent, await ctx.call(refreshed_models, ctx.config))
