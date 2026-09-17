"""The host side of an agent's own initiative (S-A12).

:func:`sbxloop.agents.tools.work_tools` applies the guardrails; this is
what it calls once they all pass. Nothing here decides whether an agent
*may* start work -- it resolves a repository against the configuration,
counts what the agent already started today, asks the workspace pool, and
then admits the work through the same :class:`ControlService` a person's
API client uses, under a :meth:`Principal.for_agent` that holds
``items:create`` and nothing else.

Two shapes of start:

* a **workload** becomes an inline admission, exactly the shape the
  concierge's ``start_workload`` queues, carrying the channel and the
  origin columns so the run answers in the right place and the chain stays
  countable;
* a **code** run has no issue to run against, so one is filed carrying the
  repository's trigger label. Discovery picks it up like any other labelled
  issue, and reads the origin back out of the marker in its body.

Refusals are :class:`ToolRejectedError`, whose text the agent reads and
can act on; nothing here raises a bare exception at a session.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from sbxloop.agents.origin import origin_footer, origin_marker
from sbxloop.agents.tools import (
    AgentTool,
    IssueRequest,
    StartRequest,
    WorkLimits,
    work_granted,
    work_tools,
)
from sbxloop.daemon.controls.intake import WorkloadAdmission
from sbxloop.daemon.controls.principal import Principal
from sbxloop.daemon.controls.results import ControlError
from sbxloop.daemon.controls.service import ControlService
from sbxloop.errors import DaemonError, GithubOpsError, SbxError, ToolRejectedError, WorkerError
from sbxloop.log import get_logger

if TYPE_CHECKING:
    from sbxloop.agents.definition import AgentDefinition
    from sbxloop.config import Config

__all__ = ["AgentWorkService"]

log = get_logger(__name__)

#: The item id an agent-started workload is minted under: the dedupe key
#: rides the id, so the store itself makes one ask idempotent forever.
_KEY_PREFIX = "agent-"


class AgentWorkService:
    """An agent's ``start_run`` and ``file_issue``, over a daemon loop."""

    def __init__(self, loop: Any, clock: Any = time.time) -> None:
        self.loop = loop
        self.clock = clock
        #: Issues filed this process, by dedupe key. A filed issue leaves
        #: no row in the daemon's store until a poll discovers it, so this
        #: is what stops a session filing the same issue twice in a row;
        #: the durable guard is the item id a started workload is minted
        #: under.
        self._filed: dict[str, str] = {}

    # -- offering -------------------------------------------------------------

    def tools(
        self,
        agent: AgentDefinition,
        *,
        channel_id: str | None = None,
        parent_item_id: str | None = None,
        parent_depth: int = 0,
        on_behalf_of: str | None = None,
    ) -> list[AgentTool]:
        """``agent``'s work tools, or none when its spec declares no
        ``can_start`` (so the shipped team's turns are unchanged)."""
        if not work_granted(agent):
            return []
        return work_tools(
            self,
            agent,
            channel_id=channel_id,
            parent_item_id=parent_item_id,
            parent_depth=parent_depth,
            on_behalf_of=on_behalf_of,
        )

    # -- WorkHost -------------------------------------------------------------

    def limits(self) -> WorkLimits:
        team = self.loop.config.agent_team
        return WorkLimits(
            max_chain_depth=team.max_chain_depth,
            max_agent_runs_per_day=team.max_agent_runs_per_day,
        )

    def repository(self, repo: str | None) -> str:
        config: Config = self.loop.config
        entry = config.github.find_repo(repo)
        if entry is None:
            known = ", ".join(sorted(r.repo for r in config.github.repos)) or "none"
            named = repr(repo) if repo else "a default"
            raise ToolRejectedError(
                f"{named} repository is not configured here (configured: {known})"
            )
        if not entry.enabled:
            raise ToolRejectedError(f"{entry.repo} is configured but not enabled")
        name: str = entry.repo
        return name

    def runs_started_today(self, agent_slug: str) -> int:
        """How much work ``agent_slug`` has put in the queue since the
        pool's day began -- counted from the items themselves, so a restart
        does not hand an agent a fresh allowance."""
        start, _ = self.loop.usage_pool.day(self.clock())
        return sum(
            1
            for item in self.loop.dstore.items()
            if item.origin_agent == agent_slug and item.created_at >= start
        )

    def budget_refusal(self) -> str | None:
        admission = self.loop.usage_pool.admit_run(None, self.clock())
        if admission.ok:
            return None
        which = (
            "the workspace has spent its daily token budget"
            if admission.reason == "token_budget"
            else "the workspace has started every run it may start today"
        )
        return f"{which} ([daemon] {admission.reason})"

    def duplicate(self, dedupe_key: str) -> str | None:
        filed = self._filed.get(dedupe_key)
        if filed is not None:
            return filed
        item = self.loop.dstore.get(f"api:{_KEY_PREFIX}{dedupe_key}")
        return item.item_id if item is not None else None

    # -- doing ----------------------------------------------------------------

    def start(self, request: StartRequest) -> str:
        if request.kind == "workload":
            return self._start_workload(request)
        return self._start_code(request)

    def _start_workload(self, request: StartRequest) -> str:
        ask = (
            f"{request.ask}\n\n{origin_footer(request.origin.agent_slug, request.on_behalf_of)}\n"
            f"{origin_marker(request.origin)}"
        )
        admission = WorkloadAdmission(
            ask=ask,
            profile=request.profile,
            key=f"{_KEY_PREFIX}{request.dedupe_key}",
            channel_id=request.channel_id,
            origin_agent=request.origin.agent_slug,
            parent_item_id=request.origin.parent_item_id,
            chain_depth=request.origin.chain_depth,
        )
        principal = Principal.for_agent(request.origin.agent_slug, request.on_behalf_of)
        try:
            outcome = ControlService(self.loop).admit(principal, admission)
        except ControlError as exc:
            raise ToolRejectedError(exc.message) from exc
        log.info(
            "agent_work.started",
            agent=request.origin.agent_slug,
            item=outcome.item.item_id,
            kind="workload",
            depth=request.origin.chain_depth,
            parent=request.origin.parent_item_id,
            channel=request.channel_id,
        )
        return (
            f"queued workload `{outcome.item.item_id}` ({outcome.item.title}). It runs after "
            "anything already queued and reports where this conversation is."
        )

    def _start_code(self, request: StartRequest) -> str:
        """A code run needs an issue to run against, so one is filed with
        the repository's trigger label; the poll claims it as it would a
        person's."""
        repo = self.repository(request.repo)
        title = next((ln.strip() for ln in request.ask.splitlines() if ln.strip()), "")[:200]
        issue = IssueRequest(
            repo=repo,
            title=title or "agent-requested change",
            body=request.ask,
            queue=True,
            origin=request.origin,
            channel_id=request.channel_id,
            dedupe_key=request.dedupe_key,
            on_behalf_of=request.on_behalf_of,
        )
        return self.file_issue(issue)

    def file_issue(self, request: IssueRequest) -> str:
        github = getattr(self.loop, "github", None)
        if github is None:
            raise ToolRejectedError("this daemon has no GitHub access, so it cannot file issues")
        trigger = self.loop.config.labels_for(request.repo).trigger
        labels = [trigger] if request.queue else []
        body = (
            f"{request.body}\n\n---\n"
            f"{origin_footer(request.origin.agent_slug, request.on_behalf_of)}\n"
            f"{origin_marker(request.origin)}\n"
        )
        try:
            ref = github.call(
                lambda ops: ops.issue_create(request.repo, request.title, body, labels=labels)
            )
        except (GithubOpsError, WorkerError, SbxError, DaemonError) as exc:
            raise ToolRejectedError(f"filing the issue failed: {exc}") from exc
        self._filed[request.dedupe_key] = str(ref.url or ref.number)
        log.info(
            "agent_work.issue_filed",
            agent=request.origin.agent_slug,
            repo=request.repo,
            number=ref.number,
            queued=request.queue,
            depth=request.origin.chain_depth,
        )
        if request.queue:
            return (
                f"filed and queued issue #{ref.number} {ref.url} with the `{trigger}` label -- "
                "the daemon claims it on its next poll and runs it after anything already queued."
            )
        return (
            f"filed issue #{ref.number} {ref.url} -- NOT queued: it carries no `{trigger}` "
            "label, so nothing runs until a person adds one."
        )
