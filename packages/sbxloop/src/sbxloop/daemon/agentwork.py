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

Every start is written to a small durable ledger in ``daemon_state`` the
moment it happens, because the two things the guardrails ask about cannot
be answered from the work itself. A filed issue leaves no row in the
store until a poll discovers it minutes or hours later, so counting items
would let an agent file all day before the cap noticed; and a service is
built fresh for every participant of every turn, so an in-memory note of
what was already filed is empty again on the next message. The ledger is
what makes ``runs_started_today`` and :meth:`duplicate` answer across both.

Refusals are :class:`ToolRejectedError`, whose text the agent reads and
can act on; nothing here raises a bare exception at a session.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from sbxloop.agents.origin import origin_footer, origin_marker, strip_origin_markers
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

#: The ``daemon_state`` key prefix of the ledger: one row per piece of work
#: an agent started, keyed by its dedupe key (which already folds in the
#: agent and the parent, so the key alone identifies the ask).
_LEDGER_PREFIX = "agent_work:started:"


class AgentWorkService:
    """An agent's ``start_run`` and ``file_issue``, over a daemon loop."""

    def __init__(self, loop: Any, clock: Any = time.time) -> None:
        self.loop = loop
        self.clock = clock

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
        """How much work ``agent_slug`` has started since the pool's day
        began -- read from the ledger, so a restart does not hand an agent a
        fresh allowance and a queued issue counts from the moment it is
        filed rather than from whenever a poll happens to discover it."""
        start, _ = self.loop.usage_pool.day(self.clock())
        return sum(
            1
            for entry in self._ledger().values()
            if entry.get("agent") == agent_slug and float(entry.get("ts") or 0.0) >= start
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
        raw = self.loop.dstore.get_value(f"{_LEDGER_PREFIX}{dedupe_key}")
        if raw is not None:
            entry = self._entry(raw)
            ref = entry.get("ref") if entry else None
            if isinstance(ref, str) and ref:
                return ref
        item = self.loop.dstore.get(f"api:{_KEY_PREFIX}{dedupe_key}")
        return item.item_id if item is not None else None

    # -- the ledger -----------------------------------------------------------

    @staticmethod
    def _entry(raw: str) -> dict[str, Any]:
        try:
            entry = json.loads(raw)
        except ValueError:
            return {}
        return entry if isinstance(entry, dict) else {}

    def _ledger(self) -> dict[str, dict[str, Any]]:
        """Every start this daemon has recorded, by dedupe key."""
        rows = self.loop.dstore.values_with_prefix(_LEDGER_PREFIX)
        return {
            key[len(_LEDGER_PREFIX) :]: entry
            for key, raw in rows.items()
            if (entry := self._entry(raw))
        }

    def _record(self, request: StartRequest | IssueRequest, ref: str, kind: str) -> None:
        """Note that this ask has been started, before the tool answers.

        This is what the daily cap counts and what :meth:`duplicate` finds,
        so it lands for every shape of start -- a workload, a queued issue
        and an unqueued one alike.
        """
        self.loop.dstore.set_value(
            f"{_LEDGER_PREFIX}{request.dedupe_key}",
            json.dumps(
                {
                    "agent": request.origin.agent_slug,
                    "ts": self.clock(),
                    "ref": ref,
                    "kind": kind,
                },
                sort_keys=True,
            ),
        )

    # -- doing ----------------------------------------------------------------

    def start(self, request: StartRequest) -> str:
        if request.kind == "workload":
            return self._start_workload(request)
        return self._start_code(request)

    def _start_workload(self, request: StartRequest) -> str:
        # The ask is the agent's own text: any marker in it is stripped
        # before the daemon's own is appended, so the ask a person reads
        # cannot claim it came from somebody else.
        written = strip_origin_markers(request.ask).strip()
        ask = (
            f"{written}\n\n{origin_footer(request.origin.agent_slug, request.on_behalf_of)}\n"
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
        self._record(request, outcome.item.item_id, "workload")
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
        # The title comes off the ask, so it is stripped here too: a marker
        # the agent wrote must not become the title of the issue either.
        written = strip_origin_markers(request.ask).strip()
        title = next((ln.strip() for ln in written.splitlines() if ln.strip()), "")[:200]
        issue = IssueRequest(
            repo=repo,
            title=title or "agent-requested change",
            body=written,
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
        # The body is the agent's own text and discovery reads the origin
        # back out of it, so every marker in it goes before the daemon's
        # own is appended: an agent may not name another agent as the one
        # who asked, nor reset the depth its chain is already at.
        body = (
            f"{strip_origin_markers(request.body).strip()}\n\n---\n"
            f"{origin_footer(request.origin.agent_slug, request.on_behalf_of)}\n"
            f"{origin_marker(request.origin)}\n"
        )
        try:
            ref = github.call(
                lambda ops: ops.issue_create(request.repo, request.title, body, labels=labels)
            )
        except (GithubOpsError, WorkerError, SbxError, DaemonError) as exc:
            raise ToolRejectedError(f"filing the issue failed: {exc}") from exc
        self._record(request, str(ref.url or ref.number), "code" if request.queue else "issue")
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
