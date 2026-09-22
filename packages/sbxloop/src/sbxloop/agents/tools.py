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

:func:`work_tools` (``start_run`` and ``file_issue``) is not a group: an
agent is offered it because its spec declares ``can_start``, and a
``tools`` list only narrows that further. Every guardrail an agent's own
initiative needs is applied here, before the host is asked to admit
anything -- see :func:`work_tools`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol, cast

from sbxloop.agents.origin import WorkOrigin
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
    "RUN_TOOLS",
    "TOOL_CATALOG",
    "UNGUARDED_START_TOOLS",
    "WORK_TOOL_NAMES",
    "AgentTool",
    "IssueRequest",
    "StartRequest",
    "WorkHost",
    "WorkLimits",
    "agent_tool_handler",
    "chat_memory_granted",
    "memory_tools",
    "work_dedupe_key",
    "work_granted",
    "work_tools",
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

#: The tools an agent that declares ``can_start`` is offered (S-A12).
#: Not a group: ``can_start`` is what grants them, and a ``tools`` list
#: only narrows what the agent already gets.
WORK_TOOL_NAMES: frozenset[str] = frozenset({"start_run", "file_issue"})

#: The concierge tools that put work in the queue without asking any of an
#: agent's guardrails. An agent offered :data:`WORK_TOOL_NAMES` is not also
#: offered these (``daemon/concierge.py``), so ``start_run`` and
#: ``file_issue`` are the only way it starts work.
UNGUARDED_START_TOOLS: frozenset[str] = frozenset(
    {
        "create_issue",
        "create_schedule",
        "label_issue_for_run",
        "start_entrygraph",
        "start_workload",
    }
)

TOOL_CATALOG: frozenset[str] = CONCIERGE_TOOLS | RUN_TOOLS | AGENT_TOOL_GROUPS | WORK_TOOL_NAMES

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


#: The kinds of run an agent may ask for on its own.
AgentStartKind = Literal["code", "workload"]

_ASK_MAX = 4000
_TITLE_MAX = 200
_BODY_MAX = 8000


@dataclass(frozen=True, slots=True)
class WorkLimits:
    """The ``[agent_team]`` bounds that apply to every agent."""

    max_chain_depth: int
    max_agent_runs_per_day: int


@dataclass(frozen=True, slots=True)
class StartRequest:
    """A run an agent asked for, past every guardrail."""

    kind: AgentStartKind
    ask: str
    repo: str | None
    profile: str | None
    origin: WorkOrigin
    channel_id: str | None
    dedupe_key: str
    on_behalf_of: str | None


@dataclass(frozen=True, slots=True)
class IssueRequest:
    """An issue an agent asked to file, past every guardrail."""

    repo: str
    title: str
    body: str
    queue: bool
    origin: WorkOrigin
    channel_id: str | None
    dedupe_key: str
    on_behalf_of: str | None


class WorkHost(Protocol):
    """The host side of an agent's initiative.

    Everything that reads the daemon's configuration, store, budget or the
    forge lives behind this, so the guardrails in :func:`work_tools` are
    one readable sequence and the platform can be swapped in a test.
    """

    def limits(self) -> WorkLimits: ...

    def repository(self, repo: str | None) -> str:
        """The canonical ``owner/name`` for ``repo``; a refusal when it is
        not configured or not enabled."""

    def runs_started_today(self, agent_slug: str) -> int: ...

    def budget_refusal(self) -> str | None:
        """Why the workspace pool will not admit another run now, or None."""

    def duplicate(self, dedupe_key: str) -> str | None:
        """What this exact ask already produced, or None."""

    def start(self, request: StartRequest) -> str: ...

    def file_issue(self, request: IssueRequest) -> str: ...


def work_dedupe_key(agent_slug: str, parent_item_id: str | None, ask: str) -> str:
    """The key that makes one agent asking one parent for one thing idempotent."""
    folded = " ".join(ask.split()).casefold()
    digest = hashlib.sha256(folded.encode()).hexdigest()
    material = "\x00".join((agent_slug, parent_item_id or "", digest))
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def work_granted(agent: AgentDefinition) -> bool:
    """Whether ``agent`` is offered ``start_run`` and ``file_issue``: it
    declares ``can_start``, is active, and its ``tools`` list (when it has
    one) does not narrow them away."""
    if not agent.active or not agent.spec.can_start:
        return False
    tools = agent.spec.tools
    return tools is None or bool(WORK_TOOL_NAMES & set(tools))


def work_tools(
    host: WorkHost,
    agent: AgentDefinition,
    *,
    channel_id: str | None = None,
    parent_item_id: str | None = None,
    parent_depth: int = 0,
    on_behalf_of: str | None = None,
) -> list[AgentTool]:
    """``agent``'s own initiative: start a run, file an issue.

    ``parent_depth`` is how deep the work the agent is doing *now* sits;
    what it starts is one hop further. Work a person asked for is depth 0,
    so the first run an agent starts is depth 1.

    Every guardrail is here, in one sequence, before ``host`` is asked to
    admit or file anything:

    1. the kind must be one the agent's spec declares in ``can_start`` --
       an agent that may research may not open pull requests;
    2. the repository named (or the default, for a code run) must be
       configured *and* enabled -- the refusal names what is configured;
    3. ``parent_depth`` must be below ``[agent_team] max_chain_depth`` --
       this is what ends a chain of agents starting each other's work;
    4. the agent must be under its daily cap (``max_runs_per_day`` on its
       spec, else ``[agent_team] max_agent_runs_per_day``) -- one agent
       cannot spend the whole workspace;
    5. the workspace pool must admit another run;
    6. the ask must not be one this agent already started under this
       parent.

    Filing an issue nobody queued runs nothing, so it is excused 1 and 5 --
    there is no kind to declare and no run for the pool to admit -- and
    nothing else: writing on a repository is work the agent started, and
    an agent that could file unqueued issues without answering to 3, 4 and
    6 would have no cap at all.

    A refusal is a :class:`ToolRejectedError` naming the knob that refused,
    so the agent can say why it stopped instead of trying again.
    """
    can_start = tuple(agent.spec.can_start)
    if not can_start:
        return []
    slug = agent.slug
    declared = ", ".join(can_start)

    def guard(kind: str | None, repo: str | None, *, needs_repo: bool, budget: bool = True) -> str:
        """Every check, in order; the canonical repository when one applies.

        ``kind`` is None for work that starts no run of its own (filing an
        issue nobody queued): there is no kind to declare, and the
        workspace pool has no run to admit, but everything else still
        applies -- an agent writing on a repository is work it started,
        and it answers to the chain and to its daily cap like the rest.
        """
        if kind is not None and kind not in can_start:
            raise ToolRejectedError(
                f"you may not start a {kind} run (your can_start declares: {declared})"
            )
        resolved = host.repository(repo) if needs_repo or repo else ""
        limits = host.limits()
        if parent_depth >= limits.max_chain_depth:
            hops = "hop" if parent_depth == 1 else "hops"
            raise ToolRejectedError(
                f"this work is already {parent_depth} agent-started {hops} deep, and "
                f"[agent_team] max_chain_depth is {limits.max_chain_depth}: say what "
                "should happen instead of starting more work"
            )
        cap = agent.spec.max_runs_per_day
        knob = "your max_runs_per_day"
        if cap is None:
            cap, knob = limits.max_agent_runs_per_day, "[agent_team] max_agent_runs_per_day"
        started = host.runs_started_today(slug)
        if started >= cap:
            raise ToolRejectedError(
                f"you have started {started} today and {knob} is {cap}: nothing more "
                "starts until the day rolls over"
            )
        refusal = host.budget_refusal() if budget else None
        if refusal is not None:
            raise ToolRejectedError(f"the workspace will not admit another run: {refusal}")
        return resolved

    def deduped(ask: str) -> str:
        key = work_dedupe_key(slug, parent_item_id, ask)
        existing = host.duplicate(key)
        if existing is not None:
            raise ToolRejectedError(
                f"you already asked for this: {existing}. Say what it produced rather "
                "than asking again"
            )
        return key

    def origin() -> WorkOrigin:
        return WorkOrigin(
            agent_slug=slug,
            parent_item_id=parent_item_id,
            chain_depth=parent_depth + 1,
        )

    def start_run(args: Mapping[str, Any]) -> str:
        kind = _text(args, "kind").strip()
        if kind not in ("code", "workload"):
            raise ToolRejectedError("kind must be 'code' or 'workload'")
        ask = _text(args, "ask").strip()[:_ASK_MAX]
        if not ask:
            raise ToolRejectedError("an ask is required: say what the run should produce")
        repo = _text(args, "repo").strip() or None
        profile = _text(args, "profile").strip() or None
        resolved = guard(kind, repo, needs_repo=kind == "code")
        key = deduped(ask)
        return host.start(
            StartRequest(
                kind=cast("AgentStartKind", kind),
                ask=ask,
                repo=resolved or None,
                profile=profile,
                origin=origin(),
                channel_id=channel_id,
                dedupe_key=key,
                on_behalf_of=on_behalf_of,
            )
        )

    def file_issue(args: Mapping[str, Any]) -> str:
        title = " ".join(_text(args, "title").split())[:_TITLE_MAX]
        body = _text(args, "body").strip()[:_BODY_MAX]
        if not title or not body:
            raise ToolRejectedError("both title and body are required")
        queue = bool(args.get("queue"))
        if queue and "code" not in can_start:
            raise ToolRejectedError(
                "queue=true starts a code run, and your can_start does not include code "
                f"(it declares: {declared}). File it unqueued and say why it matters"
            )
        repo = _text(args, "repo").strip() or None
        # An unqueued issue runs nothing, so only a queued one answers to
        # `can_start` and to the workspace pool. Everything else holds
        # either way: filing on a repository is work the agent started, so
        # it counts against the chain and against its own day.
        resolved = (
            guard("code", repo, needs_repo=True)
            if queue
            else guard(None, repo, needs_repo=True, budget=False)
        )
        key = deduped(f"issue:{title}")
        return host.file_issue(
            IssueRequest(
                repo=resolved,
                title=title,
                body=body,
                queue=queue,
                origin=origin(),
                channel_id=channel_id,
                dedupe_key=key,
                on_behalf_of=on_behalf_of,
            )
        )

    return [
        AgentTool(
            HostToolSpec(
                name="start_run",
                description=(
                    "Start a run of your own when the work is bigger than an answer: a "
                    "`workload` produces files or a report, a `code` run produces a pull "
                    "request on a repository. Say what you want out of it, not how to do "
                    "it. Only start work nobody has asked for yet, and say in the "
                    "conversation that you started it."
                ),
                parameters=_schema(
                    {
                        "kind": {"type": "string", "enum": list(can_start)},
                        "ask": {
                            "type": "string",
                            "description": "What the run should produce, in a sentence or two.",
                            "minLength": 1,
                            "maxLength": _ASK_MAX,
                        },
                        "repo": {
                            "type": "string",
                            "description": "`owner/name`; omit for the default repository.",
                        },
                        "profile": {
                            "type": "string",
                            "description": "A workload profile name; omit for the default.",
                        },
                    },
                    ["kind", "ask"],
                ),
            ),
            start_run,
        ),
        AgentTool(
            HostToolSpec(
                name="file_issue",
                description=(
                    "File an issue on a configured repository for something real that is "
                    "out of scope here. It is not queued unless you ask for that, and a "
                    "person decides what happens to it. Write the symptom first."
                ),
                parameters=_schema(
                    {
                        "repo": {
                            "type": "string",
                            "description": "`owner/name`; omit for the default repository.",
                        },
                        "title": {"type": "string", "minLength": 1, "maxLength": _TITLE_MAX},
                        "body": {"type": "string", "minLength": 1, "maxLength": _BODY_MAX},
                        "queue": {
                            "type": "boolean",
                            "description": (
                                "Add the trigger label so the daemon runs it. Needs `code` "
                                "in your can_start."
                            ),
                        },
                    },
                    ["title", "body"],
                ),
            ),
            file_issue,
        ),
    ]
