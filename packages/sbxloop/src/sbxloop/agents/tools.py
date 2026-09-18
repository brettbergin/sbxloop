"""The tool names an agent's ``tools`` list may narrow to, and the agent's own tools.

An agent's ``tools`` only narrows what its role already gets, so the names it
may use are the host tools sbxloop itself serves: the chat session's roster,
the ones a run's sessions are given, the agent tool groups, and the names
reserved for agent tools that are still to come. ``[[mcp]]`` server names are
accepted beside these (checked against the configuration, not listed here).

An agent tool group is one ``tools`` entry that grants several tools the
agent uses on its own behalf. ``memory`` grants :func:`memory_tools`
(``remember``, ``recall`` and ``forget``) over the agent's long-term memory,
answered on the host and scoped to the channel the agent is working in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple

from sbxloop.errors import ToolRejectedError
from sbxloop.log import get_logger
from sbxloop_worker.protocol import HostToolCall, HostToolResponse, HostToolSpec

if TYPE_CHECKING:
    from sbxloop.agents.definition import AgentDefinition
    from sbxloop.agents.memory import MemoryService

__all__ = [
    "AGENT_TOOL_GROUPS",
    "CONCIERGE_TOOLS",
    "MEMORY_TOOL_GROUP",
    "MEMORY_TOOL_NAMES",
    "RESERVED_TOOLS",
    "RUN_TOOLS",
    "TOOL_CATALOG",
    "AgentTool",
    "agent_tool_handler",
    "chat_memory_granted",
    "memory_tools",
]

log = get_logger(__name__)

#: Host tools the chat session may be given (``daemon/concierge.py``).
CONCIERGE_TOOLS: frozenset[str] = frozenset(
    {
        "handoff_agent",
        "sbx_control",
        "list_runs",
        "run_detail",
        "watch_run",
        "run_events",
        "item_detail",
        "version_status",
        "run_usage",
        "usage_today",
        "agent_rate_limits",
        "daemon_log",
        "start_workload",
        "start_entrygraph",
        "create_schedule",
        "delete_schedule",
        "config_keys",
        "set_config",
        "list_repos",
        "github_get",
        "pr_status",
        "create_issue",
        "list_issues",
        "label_issue_for_run",
        "comment_on_issue",
        "close_issue",
        "load_skill",
        "read_channel_artifact",
    }
)

#: Host tools a run's sessions may be given (``engine/``).
RUN_TOOLS: frozenset[str] = frozenset(
    {
        "call_service",
        "fetch_dependencies",
        "lookup_followup",
        "load_skill",
    }
)

#: The ``tools`` entry that grants an agent its memory tools.
MEMORY_TOOL_GROUP = "memory"
#: The tools the ``memory`` entry grants.
MEMORY_TOOL_NAMES: frozenset[str] = frozenset({"remember", "recall", "forget"})

#: ``tools`` entries that each grant a group of the agent's own tools.
AGENT_TOOL_GROUPS: frozenset[str] = frozenset({MEMORY_TOOL_GROUP})

#: Names held for agent tools that are not served yet.
RESERVED_TOOLS: frozenset[str] = frozenset({"start_run", "file_issue"})

TOOL_CATALOG: frozenset[str] = CONCIERGE_TOOLS | RUN_TOOLS | AGENT_TOOL_GROUPS | RESERVED_TOOLS

#: The most memories one ``recall`` returns, and how many when not asked.
_RECALL_MAX = 20
_RECALL_DEFAULT = 5
_QUERY_MAX = 1000
_ID_MAX = 64


class AgentTool(NamedTuple):
    """One tool an agent uses on its own behalf: what the session is told,
    and the host-side answer. ``impl`` raises :class:`ToolRejectedError`
    with a reason the agent can act on."""

    spec: HostToolSpec
    impl: Callable[[Mapping[str, Any]], str]


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _text(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ToolRejectedError(f"{name} must be text")
    return value


def chat_memory_granted(agent: AgentDefinition) -> bool:
    """Whether ``agent`` gets its memory tools in a chat turn: when its
    ``tools`` name ``memory``, or when it is a person's own agent with no
    tool list (nothing narrowed, so its own tools are part of what it gets).
    A built-in or ``[[agents]]`` entry with no list gets none, so a turn by
    the shipped team is unchanged."""
    tools = agent.spec.tools
    if tools is None:
        return agent.source == "user"
    return MEMORY_TOOL_GROUP in tools


def memory_tools(
    service: MemoryService,
    agent_slug: str,
    *,
    channel_id: str | None,
    run_id: str | None,
    message_id: str | None,
    writable: bool = True,
) -> list[AgentTool]:
    """``agent_slug``'s memory tools while it works in ``channel_id``.

    What it remembers is written as ``agent:<slug>``, learned in
    ``channel_id`` (None: kept for every channel) from ``run_id`` and
    ``message_id``; what it recalls or forgets is only what that channel may
    see. ``writable=False`` (a read-only turn or session) offers ``recall``
    alone. Empty when memory is turned off.

    ``channel_id=None`` — a run that was not started from a channel — keeps
    what the agent remembers for the whole workspace, so it is readable in
    *every* channel. ``remember`` says so in its own description rather than
    leaving the agent to assume its note stays where it was working.
    """
    from sbxloop.agents.memory import MEMORY_KINDS, AgentMemoryError

    if not service.cfg.enabled:
        return []
    author = f"agent:{agent_slug}"
    kinds = sorted(MEMORY_KINDS)
    cap = service.cfg.max_item_chars
    kept_where = (
        "It is kept for the place you are working in now."
        if channel_id is not None
        else (
            "You are not working in one channel, so it is kept for the whole workspace "
            "and anyone may see it in any channel."
        )
    )

    def remember(args: Mapping[str, Any]) -> str:
        content = _text(args, "content")
        kind = _text(args, "kind") or "fact"
        if kind not in MEMORY_KINDS:
            raise ToolRejectedError(f"kind must be one of: {', '.join(kinds)}")
        try:
            memory = service.remember(
                agent_slug,
                content,
                kind=kind,  # type: ignore[arg-type]
                channel_id=channel_id,
                run_id=run_id,
                message_id=message_id,
                author=author,
            )
        except AgentMemoryError as exc:
            raise ToolRejectedError(exc.message) from exc
        cut = " (cut to fit)" if len(content.strip()) > len(memory.content) else ""
        return f"Remembered {memory.id} ({memory.kind}){cut}: {memory.content}"

    def recall(args: Mapping[str, Any]) -> str:
        query = _text(args, "query")[:_QUERY_MAX]
        # A backend that fills every optional parameter sends an omitted
        # limit as an explicit null; that is the default, not a bad call.
        raw_limit = args.get("limit")
        if raw_limit is None:
            raw_limit = _RECALL_DEFAULT
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            raise ToolRejectedError("limit must be a whole number")
        limit = max(1, min(raw_limit, _RECALL_MAX))
        memories = service.recall(agent_slug, query=query, channel_id=channel_id, limit=limit)
        if not memories:
            return "No memories match."
        lines = [
            f"- {memory.id} ({memory.kind}{', pinned' if memory.pinned else ''}) "
            f"{' '.join(memory.content.split())}"
            for memory in memories
        ]
        return "Memories you can use here:\n" + "\n".join(lines)

    def forget(args: Mapping[str, Any]) -> str:
        memory_id = _text(args, "memory_id").strip()
        if not memory_id:
            raise ToolRejectedError("memory_id is required: recall first to find it")
        visible = {
            memory.id
            for memory in service.list(agent_slug, channel_id=channel_id, include_private=False)
        }
        if memory_id not in visible:
            raise ToolRejectedError(f"memory {memory_id[:_ID_MAX]} not found")
        try:
            service.forget(agent_slug, memory_id, author=author)
        except AgentMemoryError as exc:
            raise ToolRejectedError(exc.message) from exc
        return f"Forgot {memory_id}."

    tools = [
        AgentTool(
            HostToolSpec(
                name="remember",
                description=(
                    "Keep one short, durable note for later conversations and runs: a fact, a "
                    "preference someone stated, or a procedure that worked. "
                    f"{kept_where} Never store secrets or anything a person "
                    "asked you not to keep."
                ),
                parameters=_schema(
                    {
                        "content": {
                            "type": "string",
                            "description": "The note, in one or two sentences.",
                            "minLength": 1,
                            "maxLength": cap,
                        },
                        "kind": {"type": "string", "enum": kinds},
                    },
                    ["content"],
                ),
            ),
            remember,
        ),
        AgentTool(
            HostToolSpec(
                name="recall",
                description=(
                    "Look up notes you kept earlier that you may use here, each with its id. "
                    "A blank query returns the most recent ones."
                ),
                parameters=_schema(
                    {
                        "query": {"type": "string", "maxLength": _QUERY_MAX},
                        "limit": {"type": "integer", "minimum": 1, "maximum": _RECALL_MAX},
                    },
                    [],
                ),
            ),
            recall,
        ),
        AgentTool(
            HostToolSpec(
                name="forget",
                description="Drop a note you kept, by the id recall showed you.",
                parameters=_schema(
                    {"memory_id": {"type": "string", "minLength": 1, "maxLength": _ID_MAX}},
                    ["memory_id"],
                ),
            ),
            forget,
        ),
    ]
    if not writable:
        return [tool for tool in tools if tool.spec.name == "recall"]
    return tools


def agent_tool_handler(
    tools: Sequence[AgentTool],
    delegate: Callable[[HostToolCall], HostToolResponse] | None,
    *,
    agent_slug: str,
) -> Callable[[HostToolCall], HostToolResponse]:
    """A host-tool handler answering ``tools`` itself and passing every
    other call to ``delegate`` (an unknown tool when there is none)."""
    by_name = {tool.spec.name: tool for tool in tools}

    def handler(call: HostToolCall) -> HostToolResponse:
        tool = by_name.get(call.name)
        if tool is None:
            if delegate is None:
                return HostToolResponse(
                    call_id=call.call_id, ok=False, error=f"unknown tool {call.name!r}"
                )
            return delegate(call)
        try:
            text = tool.impl(dict(call.arguments))
        except ToolRejectedError as exc:
            return HostToolResponse(
                call_id=call.call_id,
                ok=False,
                text=f"tool {call.name} rejected: {exc}",
                error=str(exc),
            )
        except Exception as exc:
            log.warning(
                "agent.tool_failed",
                tool=call.name,
                agent=agent_slug,
                error=f"{type(exc).__name__}: {exc}"[:300],
                exc_info=True,
            )
            return HostToolResponse(
                call_id=call.call_id,
                ok=False,
                text=f"tool {call.name} failed: {type(exc).__name__}",
                error=type(exc).__name__,
            )
        return HostToolResponse(call_id=call.call_id, ok=True, text=text)

    return handler
