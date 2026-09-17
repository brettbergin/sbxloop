"""Native sbxloop roles exposed through its collaboration transport.

The catalogue itself lives in :mod:`sbxloop.agents`; these are the shapes the
collaboration routes and the chat turn have always read, derived from the
registry's built-ins.
"""

from __future__ import annotations

from dataclasses import dataclass

from sbxloop.agents.builtin import (
    ANGIE_MENTIONED,
    ANGIE_PERSONA,
    ANGIE_SLUG,
    LEGACY_BUILTINS,
    PRIMARY_BUILTINS,
    builtin_display_name,
    chat_role,
)
from sbxloop.agents.definition import AgentDefinition as RegistryAgent
from sbxloop.agents.registry import AgentRegistry, default_registry
from sbxloop.engine.harness import ROLE_BY_PHASE, Role

__all__ = [
    "AGENTS",
    "AGENTS_BY_SLUG",
    "ANGIE_MENTIONED",
    "ANGIE_PERSONA",
    "ANGIE_SLUG",
    "LEGACY_AGENTS",
    "AgentDefinition",
    "AgentRegistry",
    "default_registry",
]


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    slug: str
    name: str
    description: str
    category: str
    capabilities: tuple[str, ...]
    instructions: str
    role: Role = "concierge"
    agent: RegistryAgent | None = None

    @classmethod
    def from_registry(cls, agent: RegistryAgent) -> AgentDefinition:
        return cls(
            agent.slug,
            builtin_display_name(agent),
            agent.spec.description,
            agent.category,
            agent.capabilities,
            agent.spec.instructions,
            chat_role(agent),
            agent,
        )

    @property
    def phase(self) -> str:
        return next(
            (phase for phase, role in ROLE_BY_PHASE.items() if role == self.role), "concierge"
        )

    @property
    def persona(self) -> str:
        if self.agent is not None:
            return self.agent.chat_persona()
        return (
            "\n\n## Collaboration role\n\n"
            f"You are sbxloop's **{self.name}**, responding in Angie as `@{self.slug}`. "
            f"{self.instructions} Keep the answer useful in a shared chat, state any "
            "action you took, and never imply that another agent or person approved it."
        )


LEGACY_AGENTS: tuple[AgentDefinition, ...] = tuple(
    AgentDefinition.from_registry(agent) for agent in LEGACY_BUILTINS
)

AGENTS: tuple[AgentDefinition, ...] = tuple(
    AgentDefinition.from_registry(agent) for agent in PRIMARY_BUILTINS
)

# Existing saved teams and API clients may still address these names.
AGENTS_BY_SLUG = {agent.slug: agent for agent in (*LEGACY_AGENTS, *AGENTS)}
