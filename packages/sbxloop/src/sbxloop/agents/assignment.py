"""Which named agent takes each part of a run.

An :class:`AgentAssignment` is decided by the host before a run starts and
travels with it: the lead that speaks for the run, the agent in each run
role, and, optionally, the agent a single task is given to. Each agent is
carried as an :class:`AgentBinding`, a snapshot of what the run needs from
it (persona, memory, model, narrowing), so a run keeps the agent it was
given even when the registry changes under it.

The engine consults the assignment through :meth:`AgentAssignment.binding_for`.
A *default* assignment (the built-in team, unchanged) is indistinguishable
from no assignment: no prompt, tool list or event changes because of it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, get_args

from sbxloop.agents.builtin import ANGIE_SLUG, BUILTIN_BY_SLUG, PRIMARY_BUILTINS
from sbxloop.agents.definition import AgentDefinition, AgentRoleName
from sbxloop.engine.harness import ROLE_BY_PHASE

if TYPE_CHECKING:
    from sbxloop.agents.registry import AgentRegistry
    from sbxloop.engine.model import RunKind

__all__ = [
    "RUN_ROLES",
    "WORKING_PHASES",
    "AgentAssignment",
    "AgentBinding",
    "MemoryBlocks",
    "RunRole",
    "agent_memory_block",
    "binding_from_definition",
    "memory_section",
    "plan_assignment",
]

#: The roles a run's phases are taken in (``lead`` is not one: the lead
#: speaks for the run rather than running a phase).
RunRole = Literal["planner", "builder", "critic", "operator"]
RUN_ROLES: tuple[RunRole, ...] = get_args(RunRole)

#: The phases that do a task's work, and so go to that task's assignee when
#: it has one. Every other phase (planning, judging, reviewing, steering)
#: stays with the agent in its role, whatever task it looks at.
WORKING_PHASES = frozenset({"build", "operator_execute"})

#: The built-in agent for each role.
_BUILTIN_FOR_ROLE: dict[str, str] = {
    role: agent.slug for agent in PRIMARY_BUILTINS for role in agent.spec.roles
}


class MemoryBlocks(Protocol):
    """Where an agent's remembered context comes from (the memory service).

    The parameter names and kinds are :meth:`MemoryService.prompt_block`'s
    own, so the service satisfies the protocol structurally rather than by
    happening to be called positionally.
    """

    def prompt_block(self, agent: str, *, channel_id: str | None) -> str:
        """The block appended to the agent's system message; ``""`` for none."""


def _frozen(mapping: Mapping[Any, Any]) -> Mapping[Any, Any]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class AgentBinding:
    """One agent as a run uses it.

    ``persona`` and ``memory_block`` are appended to the system message of
    each session the agent takes; ``tools`` (None: no narrowing) and
    ``credentials`` (empty: no narrowing) only ever narrow what the phase
    already gets; ``model`` (None: the phase's own model) sits below the
    run override and the repository's per-phase model.
    """

    slug: str
    name: str
    role: AgentRoleName
    model: str | None
    persona: str
    memory_block: str
    tools: frozenset[str] | None
    credentials: tuple[str, ...]
    revision: int

    def is_default(self) -> bool:
        """A built-in in its own role, adding nothing and narrowing nothing."""
        return (
            _BUILTIN_FOR_ROLE.get(self.role) == self.slug
            and not self.persona
            and not self.memory_block
            and self.model is None
            and self.tools is None
            and not self.credentials
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "role": self.role,
            "model": self.model,
            "persona": self.persona,
            "memory_block": self.memory_block,
            "tools": None if self.tools is None else sorted(self.tools),
            "credentials": list(self.credentials),
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgentBinding:
        tools = data.get("tools")
        return cls(
            slug=str(data["slug"]),
            name=str(data["name"]),
            role=data["role"],
            model=None if data.get("model") is None else str(data["model"]),
            persona=str(data.get("persona") or ""),
            memory_block=str(data.get("memory_block") or ""),
            tools=None if tools is None else frozenset(str(t) for t in tools),
            credentials=tuple(str(c) for c in data.get("credentials") or ()),
            revision=int(data.get("revision") or 0),
        )


@dataclass(frozen=True)
class AgentAssignment:
    """The agents a run was given. ``roles`` maps a run role to a slug in
    ``agents``; ``tasks`` maps a task id to the slug that does its work;
    ``lead`` is the slug that speaks for the run."""

    lead: str
    roles: Mapping[RunRole, str]
    agents: Mapping[str, AgentBinding]
    tasks: Mapping[str, str] = field(default_factory=dict)
    channel_id: str | None = None
    origin_agent: str | None = None
    chain_depth: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", _frozen(self.roles))
        object.__setattr__(self, "agents", _frozen(self.agents))
        object.__setattr__(self, "tasks", _frozen(self.tasks))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AgentAssignment):
            return NotImplemented
        return self.to_json() == other.to_json()

    def __hash__(self) -> int:
        return hash(self.to_json())

    def binding_for(self, phase: str, task_id: str | None = None) -> AgentBinding | None:
        """The agent taking ``phase`` (a prompt name), for ``task_id`` when
        the phase works on one; None when nobody was assigned to it."""
        try:
            role = ROLE_BY_PHASE[phase]
        except KeyError:
            known = ", ".join(sorted(ROLE_BY_PHASE))
            raise ValueError(f"no harness role for phase {phase!r} (known: {known})") from None
        if task_id is not None and phase in WORKING_PHASES:
            slug = self.tasks.get(task_id)
            if slug is not None and slug in self.agents:
                return self.agents[slug]
        slug = self.roles.get(cast("RunRole", role))
        return None if slug is None else self.agents.get(slug)

    def lead_binding(self) -> AgentBinding | None:
        return self.agents.get(self.lead)

    def is_default(self) -> bool:
        """True when this assignment changes nothing about a run: every
        agent is a built-in in its own role, and no task is handed to an
        agent other than its role's."""
        workers = {self.roles.get("builder"), self.roles.get("operator")}
        return (
            self.lead == ANGIE_SLUG
            and all(binding.is_default() for binding in self.agents.values())
            and all(self.roles.get(role) == _BUILTIN_FOR_ROLE[role] for role in self.roles)
            and all(slug in workers for slug in self.tasks.values())
        )

    def with_tasks(self, tasks: Mapping[str, str]) -> AgentAssignment:
        """This assignment with ``tasks`` laid over its task mapping; slugs
        that name no agent in the assignment are ignored."""
        merged = dict(self.tasks)
        merged.update({task: slug for task, slug in tasks.items() if slug in self.agents})
        return AgentAssignment(
            lead=self.lead,
            roles=self.roles,
            agents=self.agents,
            tasks=merged,
            channel_id=self.channel_id,
            origin_agent=self.origin_agent,
            chain_depth=self.chain_depth,
        )

    def to_json(self) -> str:
        """A stable encoding: fixed top-level order, maps sorted by key."""
        payload = {
            "lead": self.lead,
            "roles": {role: self.roles[role] for role in sorted(self.roles)},
            "agents": {slug: self.agents[slug].to_dict() for slug in sorted(self.agents)},
            "tasks": {task: self.tasks[task] for task in sorted(self.tasks)},
            "channel_id": self.channel_id,
            "origin_agent": self.origin_agent,
            "chain_depth": self.chain_depth,
        }
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> AgentAssignment:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("an agent assignment is a JSON object")
        roles = {str(role): str(slug) for role, slug in (data.get("roles") or {}).items()}
        unknown = sorted(set(roles) - set(RUN_ROLES))
        if unknown:
            raise ValueError(f"unknown run role(s) {unknown} in agent assignment")
        return cls(
            lead=str(data["lead"]),
            roles=roles,  # type: ignore[arg-type]
            agents={
                str(slug): AgentBinding.from_dict(binding)
                for slug, binding in (data.get("agents") or {}).items()
            },
            tasks={str(task): str(slug) for task, slug in (data.get("tasks") or {}).items()},
            channel_id=data.get("channel_id"),
            origin_agent=data.get("origin_agent"),
            chain_depth=int(data.get("chain_depth") or 0),
        )


def memory_section(block: str) -> str:
    """A memory block as a section appended to a system message or persona,
    set off by a blank line; ``""`` for an empty block, so what it is
    appended to is unchanged."""
    return "\n\n" + block.lstrip("\n") if block.strip() else ""


def agent_memory_block(memory: MemoryBlocks | None, agent: str, *, channel_id: str | None) -> str:
    """``agent``'s memories visible in ``channel_id`` as a prompt section,
    and ``""`` without a memory source, so the prompt is unchanged. A run
    takes it once, when it is planned, and keeps that snapshot, resume
    included."""
    if memory is None:
        return ""
    return memory_section(memory.prompt_block(agent, channel_id=channel_id))


def binding_from_definition(
    agent: AgentDefinition,
    role: AgentRoleName,
    *,
    memory: MemoryBlocks | None = None,
    channel_id: str | None = None,
) -> AgentBinding:
    spec = agent.spec
    return AgentBinding(
        slug=agent.slug,
        name=spec.name or agent.slug,
        role=role,
        model=spec.model,
        persona=agent.run_persona(),
        memory_block=agent_memory_block(memory, agent.slug, channel_id=channel_id),
        tools=None if spec.tools is None else frozenset(spec.tools),
        credentials=tuple(spec.credentials),
        revision=agent.revision,
    )


def _usable(
    registry: AgentRegistry, slug: str | None, role: AgentRoleName
) -> AgentDefinition | None:
    if not slug:
        return None
    agent = registry.get(slug)
    if agent is None or not agent.active or agent.legacy or role not in agent.spec.roles:
        return None
    return agent


def _builtin(registry: AgentRegistry, role: AgentRoleName) -> AgentDefinition:
    slug = _BUILTIN_FOR_ROLE[role]
    # The operator's toml may adjust a built-in (its model, say); the run
    # takes the adjusted one. A built-in is never missing from a registry.
    return registry.get(slug) or BUILTIN_BY_SLUG[slug]


def plan_assignment(
    registry: AgentRegistry,
    *,
    kind: RunKind,
    lead: str | None,
    requested: Mapping[RunRole, str],
    memory: MemoryBlocks | None = None,
    channel_id: str | None,
) -> AgentAssignment:
    """The assignment a run of ``kind`` starts with.

    Each role takes the requested agent when it exists, is enabled, is not
    archived and declares the role, and the built-in for that role otherwise; the lead
    is ``lead`` on the same terms, Angie otherwise. A ``tool`` run has no
    agent phases and so no roles. ``memory`` fills each agent's memory
    block for ``channel_id``; without it the blocks are empty.
    """
    roles: dict[RunRole, str] = {}
    agents: dict[str, AgentBinding] = {}
    wanted: tuple[RunRole, ...] = () if kind == "tool" else RUN_ROLES
    for role in wanted:
        agent = _usable(registry, requested.get(role), role) or _builtin(registry, role)
        roles[role] = agent.slug
        if agent.slug not in agents:
            agents[agent.slug] = binding_from_definition(
                agent, role, memory=memory, channel_id=channel_id
            )
    leader = _usable(registry, lead, "lead") or _builtin(registry, "lead")
    if leader.slug not in agents:
        agents[leader.slug] = binding_from_definition(
            leader, "lead", memory=memory, channel_id=channel_id
        )
    return AgentAssignment(lead=leader.slug, roles=roles, agents=agents, channel_id=channel_id)
