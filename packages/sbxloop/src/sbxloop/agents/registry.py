"""Where agents are looked up: the built-ins, then the operator's ``[[agents]]``.

:class:`AgentRegistry` is the one interface the platform and the engine ask.
:class:`ConfigAgentRegistry` answers it from the shipped catalogue merged
with ``sbxloop.toml``; it is read-only. A registry that also stores a
person's own agents implements the same protocol, layered after these two
(a stored agent never shadows a configured slug).
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from sbxloop.agents.builtin import BUILTIN_AGENTS, BUILTIN_BY_SLUG
from sbxloop.agents.definition import AgentDefinition, AgentRoleName, AgentSpec
from sbxloop.agents.tools import TOOL_CATALOG
from sbxloop.errors import SbxloopError

if TYPE_CHECKING:
    from sbxloop.config import Config

__all__ = [
    "AgentRegistry",
    "AgentRegistryReadOnly",
    "ConfigAgentRegistry",
    "config_agent_problems",
    "default_registry",
]


class AgentRegistryReadOnly(SbxloopError):
    """The registry (or the agent) cannot be changed from here."""


class AgentRegistry(Protocol):
    def get(self, slug_or_alias: str) -> AgentDefinition | None:
        """The agent a slug or alias names, enabled or not; case-insensitive."""

    def list(self, include_disabled: bool = False) -> builtins.list[AgentDefinition]:
        """The agents clients show: enabled and current, built-ins first.
        ``include_disabled`` adds the disabled and the legacy names."""

    def for_role(self, role: AgentRoleName) -> builtins.list[AgentDefinition]:
        """The enabled agents that may take ``role``, built-ins first."""

    def create(self, spec: AgentSpec, by: str) -> AgentDefinition: ...

    def update(
        self, slug: str, patch: Mapping[str, Any], expected_revision: int, by: str
    ) -> AgentDefinition: ...

    def archive(self, slug: str, by: str) -> AgentDefinition: ...

    def validate(self, spec: AgentSpec) -> builtins.list[str]:
        """Everything wrong with ``spec`` beside the agents already known;
        empty when it may be saved."""


def _merge(base: AgentDefinition | None, spec: AgentSpec) -> AgentDefinition:
    """``spec`` over ``base``: a configured entry for a known slug changes
    only the keys it sets to something other than their default."""
    if base is None:
        return AgentDefinition(spec, "config")
    merged = AgentSpec.model_validate(
        {**base.spec.model_dump(), **spec.model_dump(exclude_defaults=True)}
    )
    return AgentDefinition(
        merged,
        "config",
        revision=base.revision,
        legacy=base.legacy,
        category=base.category,
        capabilities=base.capabilities,
    )


def _problems(
    spec: AgentSpec,
    others: Iterable[AgentDefinition],
    *,
    credentials: set[str],
    mcp: set[str],
) -> builtins.list[str]:
    label = f"agent {spec.slug!r}"
    problems: builtins.list[str] = []
    if not spec.name and spec.slug not in BUILTIN_BY_SLUG:
        problems.append(f"{label} needs a name")
    unknown_tools = sorted(set(spec.tools or ()) - TOOL_CATALOG - mcp)
    if unknown_tools:
        problems.append(f"{label} names unknown tool(s) {unknown_tools}")
    unknown_credentials = sorted(set(spec.credentials) - credentials)
    if unknown_credentials:
        problems.append(
            f"{label} names credential(s) {unknown_credentials} not declared under [[credentials]]"
        )
    unknown_mcp = sorted(set(spec.mcp or ()) - mcp)
    if unknown_mcp:
        problems.append(f"{label} names mcp server(s) {unknown_mcp} not declared under [[mcp]]")
    if spec.can_start and not spec.roles:
        problems.append(f"{label} sets can_start but takes no roles")
    owners: dict[str, str] = {}
    for other in others:
        if other.slug == spec.slug:
            continue
        owners[other.slug] = other.slug
        for alias in other.spec.aliases:
            owners.setdefault(alias, other.slug)
    if spec.slug in owners:
        problems.append(f"{label}: {spec.slug!r} is already an alias of {owners[spec.slug]!r}")
    for alias in spec.aliases:
        if alias in owners:
            problems.append(f"{label}: alias {alias!r} already names agent {owners[alias]!r}")
    return problems


class ConfigAgentRegistry:
    """The built-in catalogue with ``[[agents]]`` merged in by slug."""

    def __init__(self, config: Config | None = None) -> None:
        self._credentials = {c.name for c in config.credentials} if config else set()
        self._mcp = {m.name for m in config.mcp} if config else set()
        entries: dict[str, AgentDefinition] = {a.slug: a for a in BUILTIN_AGENTS}
        for spec in config.agents if config else ():
            entries[spec.slug] = _merge(entries.get(spec.slug), spec)
        self._entries = entries
        self._aliases: dict[str, str] = {}
        for agent in entries.values():
            for alias in agent.spec.aliases:
                self._aliases.setdefault(alias, agent.slug)

    def get(self, slug_or_alias: str) -> AgentDefinition | None:
        key = slug_or_alias.strip().casefold()
        agent = self._entries.get(key)
        if agent is None and key in self._aliases:
            agent = self._entries[self._aliases[key]]
        return agent

    def list(self, include_disabled: bool = False) -> builtins.list[AgentDefinition]:
        return [
            agent
            for agent in self._entries.values()
            if include_disabled or (agent.spec.enabled and not agent.legacy)
        ]

    def for_role(self, role: AgentRoleName) -> builtins.list[AgentDefinition]:
        return [agent for agent in self.list() if role in agent.spec.roles]

    def create(self, spec: AgentSpec, by: str) -> AgentDefinition:
        raise AgentRegistryReadOnly("agents come from the built-ins and sbxloop.toml here")

    def update(
        self, slug: str, patch: Mapping[str, Any], expected_revision: int, by: str
    ) -> AgentDefinition:
        raise AgentRegistryReadOnly(f"agent {slug!r} is defined by sbxloop or sbxloop.toml")

    def archive(self, slug: str, by: str) -> AgentDefinition:
        raise AgentRegistryReadOnly(f"agent {slug!r} is defined by sbxloop or sbxloop.toml")

    def validate(self, spec: AgentSpec) -> builtins.list[str]:
        return _problems(spec, self._entries.values(), credentials=self._credentials, mcp=self._mcp)


def config_agent_problems(config: Config) -> builtins.list[str]:
    """What is wrong with ``config``'s ``[[agents]]``, each entry judged as
    it resolves (merged over the built-in it adjusts)."""
    registry = ConfigAgentRegistry(config)
    problems: builtins.list[str] = []
    for spec in config.agents:
        resolved = registry.get(spec.slug)
        if resolved is not None:
            problems += registry.validate(resolved.spec)
    return problems


def default_registry(config: Config) -> AgentRegistry:
    """The registry a daemon without stored agents uses."""
    return ConfigAgentRegistry(config)
