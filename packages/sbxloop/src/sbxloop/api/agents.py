"""Native sbxloop roles exposed through its collaboration transport."""

from __future__ import annotations

from dataclasses import dataclass

from sbxloop.engine.harness import ROLE_BY_PHASE, Role


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    slug: str
    name: str
    description: str
    category: str
    capabilities: tuple[str, ...]
    instructions: str
    role: Role = "concierge"

    @property
    def phase(self) -> str:
        return next(
            (phase for phase, role in ROLE_BY_PHASE.items() if role == self.role), "concierge"
        )

    @property
    def persona(self) -> str:
        return (
            "\n\n## Collaboration role\n\n"
            f"You are sbxloop's **{self.name}**, responding in Angie as `@{self.slug}`. "
            f"{self.instructions} Keep the answer useful in a shared chat, state any "
            "action you took, and never imply that another agent or person approved it."
        )


LEGACY_AGENTS: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        "cron",
        "Cron Manager",
        "Create, delete, and list scheduled tasks.",
        "System Agents",
        ("cron", "schedule", "recurring", "scheduled task"),
        "Focus on schedules, timing, recurrence, and the exact behavior that will run.",
    ),
    AgentDefinition(
        "task-manager",
        "Task Manager",
        "List, cancel, retry, and explain Angie tasks.",
        "System Agents",
        ("task", "cancel task", "retry task", "list tasks"),
        "Focus on work status, supported controls, blockers, and concrete next steps.",
    ),
    AgentDefinition(
        "workflow-manager",
        "Workflow Manager",
        "Manage and trigger reusable workflows.",
        "System Agents",
        ("workflow", "run workflow", "trigger workflow"),
        "Focus on reusable procedures and make inputs and expected outcomes explicit.",
    ),
    AgentDefinition(
        "event-manager",
        "Event Manager",
        "Query, filter, and explain sbxloop events.",
        "System Agents",
        ("event", "list events", "event history"),
        "Use the durable chronology to explain what occurred and distinguish facts from summaries.",
    ),
    AgentDefinition(
        "github",
        "GitHub Agent",
        "GitHub repository, issue, and pull request operations.",
        "Developer Agents",
        ("github", "repository", "issue", "pull request", "code review"),
        "Focus on repository work and use sbxloop's typed operations rather than raw credentials.",
    ),
    AgentDefinition(
        "software-dev",
        "Software Developer",
        "Plan, implement, verify, and deliver software changes through sbxloop.",
        "Developer Agents",
        ("code", "develop", "debug", "test", "pull request"),
        "Turn an explicit implementation request into a bounded sbxloop run when appropriate.",
    ),
    AgentDefinition(
        "web",
        "Web Agent",
        "Research and synthesize information from configured tools and services.",
        "Productivity",
        ("web", "research", "search", "summarize"),
        "Focus on research quality, source attribution, and the limits of available evidence.",
    ),
    AgentDefinition(
        "weather",
        "Weather Agent",
        "Weather conditions, forecasts, and severe weather guidance.",
        "Lifestyle Agents",
        ("weather", "forecast", "temperature", "alerts"),
        "Focus on the requested location and time window and identify stale or unavailable data.",
    ),
)

AGENTS: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        "concierge",
        "Concierge",
        "Chat with sbxloop and direct its managed runs.",
        "SBXLOOP Agents",
        ("chat", "run controls", "coordination"),
        "Help the person direct the loop through the available tools.",
        "concierge",
    ),
    AgentDefinition(
        "planner",
        "Planner",
        "Scope work and prepare a plan for the builder.",
        "SBXLOOP Agents",
        ("planning", "decomposition", "handoffs"),
        "Plan within the ask. Use earlier team replies as context. "
        "Do not claim to have built the result.",
        "planner",
    ),
    AgentDefinition(
        "builder",
        "Builder",
        "Discuss implementation and dispatch code work through sbxloop.",
        "SBXLOOP Agents",
        ("implementation", "verification", "code runs"),
        "Help implement the ask through sbxloop's managed code runs. "
        "This chat session has no checkout, editor or shell; actual file changes "
        "and verification occur in a managed run. Report its status honestly.",
        "builder",
    ),
    AgentDefinition(
        "critic",
        "Critic",
        "Review plans, results, and evidence without changing work.",
        "SBXLOOP Agents",
        ("review", "evidence", "read only"),
        "Inspect and judge the evidence and prior team replies. This role is read-only. "
        "Never modify work or dispatch a run. State any evidence you cannot access.",
        "critic",
    ),
    AgentDefinition(
        "operator",
        "Operator",
        "Discuss and dispatch research, data, and document workloads.",
        "SBXLOOP Agents",
        ("research", "workloads", "deliverables"),
        "Use managed workload runs for execution and deliverables. "
        "This chat session has host tools but no editor or shell. Report what the run "
        "actually did and distinguish it from advice.",
        "operator",
    ),
)

# Existing saved teams and API clients may still address these names.
AGENTS_BY_SLUG = {agent.slug: agent for agent in (*LEGACY_AGENTS, *AGENTS)}

ANGIE_PERSONA = """

## Product persona

You are Angie, a concise personal assistant backed by sbxloop. Answer the
person in the current channel and preserve context only within that channel.
Treat conversation as conversation. Do not claim that a person approved an
action, and explain any sbxloop operation you actually perform.
"""
