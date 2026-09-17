"""The agents sbxloop ships: Angie and the four run roles, plus the legacy names.

The catalogue and its persona text are what the collaboration API served
before agents became configurable; changing either changes every chat turn,
so both are pinned by tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sbxloop.agents.definition import AgentDefinition, AgentSpec

if TYPE_CHECKING:
    from sbxloop.engine.harness import Role

__all__ = [
    "ANGIE_MENTIONED",
    "ANGIE_PERSONA",
    "ANGIE_SLUG",
    "BUILTIN_AGENTS",
    "BUILTIN_BY_SLUG",
    "LEGACY_BUILTINS",
    "PRIMARY_BUILTINS",
    "builtin_display_name",
    "chat_persona",
    "chat_role",
    "run_persona",
]

#: The native agent that speaks as the product itself.
ANGIE_SLUG = "concierge"

ANGIE_PERSONA = """

## Product persona

You are Angie, a concise personal assistant backed by sbxloop. Answer the
person in the current channel and preserve context only within that channel.
Treat conversation as conversation. Do not claim that a person approved an
action, and explain any sbxloop operation you actually perform.
"""

ANGIE_MENTIONED = """
The person addressed you as `@concierge` (or `@angie`), which is how they let
you use your sbxloop tools in this turn. You are still Angie: speak as
yourself, never as a separate "Concierge" agent, and say what you did.
"""

_PRIMARY_CATEGORY = "SBXLOOP Agents"


def _primary(
    slug: str,
    name: str,
    description: str,
    capabilities: tuple[str, ...],
    instructions: str,
    *,
    role: str,
    color: str,
    avatar: str,
    aliases: tuple[str, ...] = (),
) -> AgentDefinition:
    spec = AgentSpec.model_validate(
        {
            "slug": slug,
            "name": name,
            "description": description,
            "instructions": instructions,
            "roles": [role],
            "color": color,
            "avatar": avatar,
            "aliases": list(aliases),
        }
    )
    return AgentDefinition(spec, "builtin", category=_PRIMARY_CATEGORY, capabilities=capabilities)


def _legacy(
    slug: str,
    name: str,
    description: str,
    category: str,
    capabilities: tuple[str, ...],
    instructions: str,
) -> AgentDefinition:
    spec = AgentSpec(slug=slug, name=name, description=description, instructions=instructions)
    return AgentDefinition(
        spec, "builtin", legacy=True, category=category, capabilities=capabilities
    )


#: Angie and the run roles, in the order clients list them.
PRIMARY_BUILTINS: tuple[AgentDefinition, ...] = (
    _primary(
        "concierge",
        "Angie",
        "Chat with sbxloop and direct its managed runs.",
        ("chat", "run controls", "coordination"),
        "Help the person direct the loop through the available tools.",
        role="lead",
        color="#84cc16",
        avatar="A",
        aliases=("angie",),
    ),
    _primary(
        "planner",
        "Planner",
        "Scope work and prepare a plan for the builder.",
        ("planning", "decomposition", "handoffs"),
        "Plan within the ask. Use earlier team replies as context. "
        "Do not claim to have built the result.",
        role="planner",
        color="#d97706",
        avatar="P",
    ),
    _primary(
        "builder",
        "Builder",
        "Discuss implementation and dispatch code work through sbxloop.",
        ("implementation", "verification", "code runs"),
        "Help implement the ask through sbxloop's managed code runs. "
        "This chat session has no checkout, editor or shell; actual file changes "
        "and verification occur in a managed run. Report its status honestly.",
        role="builder",
        color="#ea580c",
        avatar="B",
    ),
    _primary(
        "critic",
        "Critic",
        "Review plans, results, and evidence without changing work.",
        ("review", "evidence", "read only"),
        "Inspect and judge the evidence and prior team replies. This role is read-only. "
        "Never modify work or dispatch a run. State any evidence you cannot access.",
        role="critic",
        color="#e11d48",
        avatar="C",
    ),
    _primary(
        "operator",
        "Operator",
        "Discuss and dispatch research, data, and document workloads.",
        ("research", "workloads", "deliverables"),
        "Use managed workload runs for execution and deliverables. "
        "This chat session has host tools but no editor or shell. Report what the run "
        "actually did and distinguish it from advice.",
        role="operator",
        color="#0284c7",
        avatar="O",
    ),
)

#: Names that existing saved teams and API clients may still address. They
#: resolve by slug but are not listed.
LEGACY_BUILTINS: tuple[AgentDefinition, ...] = (
    _legacy(
        "cron",
        "Cron Manager",
        "Create, delete, and list scheduled tasks.",
        "System Agents",
        ("cron", "schedule", "recurring", "scheduled task"),
        "Focus on schedules, timing, recurrence, and the exact behavior that will run.",
    ),
    _legacy(
        "task-manager",
        "Task Manager",
        "List, cancel, retry, and explain Angie tasks.",
        "System Agents",
        ("task", "cancel task", "retry task", "list tasks"),
        "Focus on work status, supported controls, blockers, and concrete next steps.",
    ),
    _legacy(
        "workflow-manager",
        "Workflow Manager",
        "Manage and trigger reusable workflows.",
        "System Agents",
        ("workflow", "run workflow", "trigger workflow"),
        "Focus on reusable procedures and make inputs and expected outcomes explicit.",
    ),
    _legacy(
        "event-manager",
        "Event Manager",
        "Query, filter, and explain sbxloop events.",
        "System Agents",
        ("event", "list events", "event history"),
        "Use the durable chronology to explain what occurred and distinguish facts from summaries.",
    ),
    _legacy(
        "github",
        "GitHub Agent",
        "GitHub repository, issue, and pull request operations.",
        "Developer Agents",
        ("github", "repository", "issue", "pull request", "code review"),
        "Focus on repository work and use sbxloop's typed operations rather than raw credentials.",
    ),
    _legacy(
        "software-dev",
        "Software Developer",
        "Plan, implement, verify, and deliver software changes through sbxloop.",
        "Developer Agents",
        ("code", "develop", "debug", "test", "pull request"),
        "Turn an explicit implementation request into a bounded sbxloop run when appropriate.",
    ),
    _legacy(
        "web",
        "Web Agent",
        "Research and synthesize information from configured tools and services.",
        "Productivity",
        ("web", "research", "search", "summarize"),
        "Focus on research quality, source attribution, and the limits of available evidence.",
    ),
    _legacy(
        "weather",
        "Weather Agent",
        "Weather conditions, forecasts, and severe weather guidance.",
        "Lifestyle Agents",
        ("weather", "forecast", "temperature", "alerts"),
        "Focus on the requested location and time window and identify stale or unavailable data.",
    ),
)

BUILTIN_AGENTS: tuple[AgentDefinition, ...] = (*PRIMARY_BUILTINS, *LEGACY_BUILTINS)
BUILTIN_BY_SLUG: dict[str, AgentDefinition] = {a.slug: a for a in BUILTIN_AGENTS}

#: The name the collaboration API has always listed the product agent under.
_API_NAMES = {ANGIE_SLUG: "Concierge"}


def builtin_display_name(agent: AgentDefinition) -> str:
    """The name the pre-registry API listed ``agent`` under."""
    return _API_NAMES.get(agent.slug, agent.spec.name)


_CHAT_ROLES: dict[str, Role] = {
    "planner": "planner",
    "builder": "builder",
    "critic": "critic",
    "operator": "operator",
}


def chat_role(agent: AgentDefinition) -> Role:
    """The chat session role a turn by ``agent`` runs under."""
    for role in agent.spec.roles:
        chat = _CHAT_ROLES.get(role)
        if chat is not None:
            return chat
    return "concierge"


def _builtin_instructions(agent: AgentDefinition) -> str | None:
    shipped = BUILTIN_BY_SLUG.get(agent.slug)
    return None if shipped is None else shipped.spec.instructions


def chat_persona(agent: AgentDefinition) -> str:
    if agent.slug == ANGIE_SLUG:
        # The lead is Angie herself: a mention is how the person lets her
        # act, not a hand-off to a separate agent.
        persona = ANGIE_PERSONA + ANGIE_MENTIONED
        instructions = agent.spec.instructions.strip()
        if instructions and instructions != _builtin_instructions(agent):
            persona += f"\n{instructions}\n"
        return persona
    instructions = agent.spec.instructions.strip()
    name = agent.spec.name or agent.slug
    return (
        "\n\n## Collaboration role\n\n"
        f"You are sbxloop's **{name}**, responding in Angie as `@{agent.slug}`. "
        + (f"{instructions} " if instructions else "")
        + "Keep the answer useful in a shared chat, state any "
        "action you took, and never imply that another agent or person approved it."
    )


def run_persona(agent: AgentDefinition) -> str:
    instructions = agent.spec.instructions.strip()
    if not instructions or instructions == _builtin_instructions(agent):
        return ""
    name = agent.spec.name or agent.slug
    return f"\n\n## Agent persona\n\nYou are **{name}** (`@{agent.slug}`). {instructions}\n"
