"""The tool names an agent's ``tools`` list may narrow to.

An agent's ``tools`` only narrows what its role already gets, so the names it
may use are the host tools sbxloop itself serves: the chat session's roster,
the ones a run's sessions are given, and the names reserved for agent tools
that are still to come. ``[[mcp]]`` server names are accepted beside these
(checked against the configuration, not listed here).
"""

from __future__ import annotations

__all__ = ["CONCIERGE_TOOLS", "RESERVED_TOOLS", "RUN_TOOLS", "TOOL_CATALOG"]

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

#: Names held for agent tools that are not served yet.
RESERVED_TOOLS: frozenset[str] = frozenset({"memory", "start_run", "file_issue"})

TOOL_CATALOG: frozenset[str] = CONCIERGE_TOOLS | RUN_TOOLS | RESERVED_TOOLS
