"""The public shapes built from the daemon's own records (#1036).

Everything here runs on the context's executor — it reads the stores and
asks the loop for its live state — and returns models the routes hand
back untouched. Public ids are assigned as a side effect of a read, in
one write per page. ``available_actions`` is the eligibility module's
answer for the resource as it stands now; the command that follows
rechecks it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import (
    Actor,
    GateSummary,
    Item,
    ItemDetail,
    Origin,
    OriginKind,
    Profile,
    PublishedOut,
    PullRequest,
    QueueEntry,
    Recipe,
    RepoHealth,
    Repository,
    Rounds,
    Run,
    Task,
    TaskOutputOut,
    rfc3339,
)
from sbxloop.api.publicids import PublicIds, item_key, parse_run_id, run_public_id, split_item_key
from sbxloop.daemon.controls.eligibility import Subject, available_actions
from sbxloop.daemon.controls.intake import RECIPE_PARAMETERS
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import dispatch_eligible_at
from sbxloop.engine.model import RunRecord, TaskRecord
from sbxloop.errors import SbxloopError
from sbxloop.ghids import is_api_id, is_chat_id, is_schedule_id, try_parse_gh_id
from sbxloop.recipes import RECIPES

ITEM_ACTIONS: tuple[str, ...] = ("retry", "requeue", "abandon")
RUN_ACTIONS: tuple[str, ...] = (
    "cancel",
    "resume",
    "steer",
    "grant_rounds",
    "gate_approve",
    "review_wait_resume",
)
_REVIEW_WAIT_ITEM_STATES = frozenset({"awaiting_review", "paused_review"})

NOT_FOUND = "no such resource"


def not_found() -> Problem:
    """One answer for an id of any kind that names nothing: never which
    kind it was not."""
    return Problem(404, "not_found", NOT_FOUND)


class Views:
    """The projections over one context, built per request."""

    def __init__(self, ctx: ApiContext) -> None:
        self.ctx = ctx
        self.loop: Any = ctx.loop
        self.dstore: Any = self.loop.dstore
        self.store: Any = self.loop.store
        self.config: Any = self.loop.config
        self.ids: PublicIds = ctx.public_ids
        self.now = ctx.clock()
        self._status: dict[str, Any] | None = None

    # -- live state ----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        if self._status is None:
            self._status = dict(self.loop.status())
        return self._status

    def current_run_id(self) -> str | None:
        current = self.status().get("current")
        return str(current["run_id"]) if current else None

    # -- lookups -------------------------------------------------------------------

    def run_record(self, run_id: str) -> RunRecord | None:
        try:
            record: RunRecord = self.store.get_run(run_id)
        except SbxloopError:
            return None
        return record

    def item_by_public_id(self, public_id: str) -> WorkItem:
        resolved = self.ids.resolve(public_id)
        if resolved is None or resolved.kind != "item":
            raise not_found()
        repo, item_id = split_item_key(resolved.key)
        item: WorkItem | None = self.dstore.get(item_id)
        if item is None or (item.repo or None) != repo:
            raise not_found()
        return item

    def run_by_public_id(self, public_id: str) -> RunRecord:
        run_id = parse_run_id(public_id)
        record = self.run_record(run_id) if run_id else None
        if record is None:
            raise not_found()
        return record

    def repository_by_public_id(self, public_id: str) -> Any:
        resolved = self.ids.resolve(public_id)
        if resolved is None or resolved.kind != "repository":
            raise not_found()
        entry = self.config.github.find_repo(resolved.key)
        if entry is None:
            raise not_found()
        return entry

    # -- eligibility ---------------------------------------------------------------

    def _subject(self, item: WorkItem | None, run: RunRecord | None) -> Subject:
        run_id = run.run_id if run is not None else (item.run_id if item else None)
        gate_state: str | None = None
        hold_state: str | None = None
        if run_id is not None and item is not None and item.state == "gated":
            gate = self.dstore.merge_gate_for(run_id)
            gate_state = gate.state if gate is not None else None
        if run_id is not None and item is not None and item.state in _REVIEW_WAIT_ITEM_STATES:
            hold = self.dstore.review_hold_for(run_id)
            hold_state = hold.state if hold is not None else None
        return Subject(
            run_kind=(run.kind if run is not None else item.kind if item else "code"),
            run_state=run.state if run is not None else None,
            item_state=item.state if item is not None else None,
            is_current=run_id is not None and run_id == self.current_run_id(),
            pinned=item is not None and run is not None and item.run_id == run.run_id,
            exhausted=run is not None and run.exhausted is not None,
            gate_state=gate_state,
            review_hold_state=hold_state,
        )

    # -- items ---------------------------------------------------------------------

    def _origin(self, item: WorkItem, repo_ids: dict[str, str]) -> Origin:
        parsed = try_parse_gh_id(item.item_id)
        kind: OriginKind
        if parsed is not None:
            kind = "issue"
        elif is_chat_id(item.item_id):
            kind = "chat"
        elif is_schedule_id(item.item_id):
            kind = "schedule"
        elif is_api_id(item.item_id):
            kind = "api"
        else:
            kind = "other"
        repo = item.repo or (parsed.repo if parsed is not None else None)
        return Origin(
            kind=kind,
            repository_id=repo_ids.get(repo) if repo else None,
            repository=repo,
            number=parsed.number if parsed is not None else None,
            url=item.url or None,
            ref=item.item_id,
        )

    def items(self, rows: Sequence[WorkItem]) -> list[Item]:
        """A page of items, ids assigned in one write."""
        public = self.ids.item_ids(rows, self.now)
        repos = {
            r
            for r in (i.repo or getattr(try_parse_gh_id(i.item_id), "repo", None) for i in rows)
            if r
        }
        repo_ids = self.ids.repository_ids(repos, self.now) if repos else {}
        return [self._item(item, public[item_key(item)], repo_ids) for item in rows]

    def item(self, item: WorkItem) -> Item:
        return self.items([item])[0]

    def _item(self, item: WorkItem, public_id: str, repo_ids: dict[str, str]) -> Item:
        run = self.run_record(item.run_id) if item.run_id else None
        actions = available_actions(self._subject(item, run))
        return Item(
            id=public_id,
            kind=item.kind,
            state=item.state,
            title=item.title,
            origin=self._origin(item, repo_ids),
            profile=item.profile,
            recipe=item.recipe,
            recipe_target=item.recipe_target,
            attempts=item.attempts,
            run_id=run_public_id(item.run_id) if item.run_id else None,
            last_error=item.last_error,
            pending_report=item.pending_report,
            not_before=rfc3339(item.not_before),
            created_at=rfc3339(item.created_at) or "",
            updated_at=rfc3339(item.updated_at) or "",
            revision=item.revision,
            available_actions=[a for a in ITEM_ACTIONS if a in actions],
        )

    def item_detail(self, item: WorkItem) -> ItemDetail:
        base = self.item(item)
        runs = [run_public_id(r) for r in self.dstore.runs_for_item(item.item_id)]
        return ItemDetail(
            **base.model_dump(),
            body=item.body,
            runs=runs,
            admitted_by=self._admitted_by(item),
        )

    def _admitted_by(self, item: WorkItem) -> Actor | None:
        operations = getattr(self.loop, "operations", None)
        if operations is None:
            return None
        keys = [item.item_id]
        parsed = try_parse_gh_id(item.item_id)
        if parsed is not None and item.repo:
            keys.append(f"{item.repo}#{parsed.number}")
        for key in keys:
            for op in operations.page(target=("item", key), limit=20):
                if op.action == "item.admit" and op.state == "succeeded":
                    actor = op.actor
                    return Actor(
                        kind=str(actor.get("kind", "operator")),
                        id=str(actor.get("id", "")),
                        display=actor.get("display"),
                        via=str(actor.get("via", "")),
                    )
        return None

    # -- queue ---------------------------------------------------------------------

    def queue(self, limit: int) -> tuple[list[QueueEntry], bool]:
        rows: list[WorkItem] = list(self.dstore.queued_in_order())
        more = len(rows) > limit
        rows = rows[:limit]
        backoff = float(self.config.daemon.retry_backoff_s)
        entries = []
        for position, (item, view) in enumerate(zip(rows, self.items(rows), strict=True), 1):
            eligible_at = dispatch_eligible_at(item, backoff)
            eligible = eligible_at <= self.now
            reason: str | None = None
            if item.run_id is not None:
                reason = "interrupted run awaiting resume; goes first"
            elif not eligible and item.not_before is not None and eligible_at == item.not_before:
                reason = "scheduled retry; waits its own clock"
            elif not eligible:
                reason = f"retry backoff after {item.attempts} attempt(s)"
            entries.append(
                QueueEntry(
                    position=position,
                    item=view,
                    eligible_at=rfc3339(eligible_at) if eligible_at > 0 else None,
                    eligible=eligible,
                    reason=reason,
                )
            )
        return entries, more

    # -- runs ----------------------------------------------------------------------

    def runs(self, rows: Sequence[RunRecord]) -> list[Run]:
        return [self.run(record) for record in rows]

    def run(self, record: RunRecord) -> Run:
        item_id = self.dstore.item_for_run(record.run_id)
        item = self.dstore.get(item_id) if item_id else None
        gate = None
        review_wait = None
        if item is not None:
            if item.state == "gated":
                found = self.dstore.merge_gate_for(record.run_id)
                if found is not None:
                    gate = GateSummary(kind=found.kind, state=found.state, revision=found.revision)
            if item.state in _REVIEW_WAIT_ITEM_STATES:
                hold = self.dstore.review_hold_for(record.run_id)
                review_wait = hold.state if hold is not None else None
        actions = available_actions(self._subject(item, record))
        pr = (
            PullRequest(
                number=record.pr_number,
                url=record.pr_url,
                branch=record.branch,
                head_sha=record.head_sha,
                title=record.pr_title,
            )
            if record.pr_number is not None or record.branch
            else None
        )
        return Run(
            id=run_public_id(record.run_id),
            kind=record.kind,
            state=record.state,
            stage=record.stage,
            outcome=record.outcome,
            reason=record.reason,
            item_id=self.ids.item_id(item, self.now) if item is not None else None,
            created_at=rfc3339(record.created_at) or "",
            updated_at=rfc3339(record.updated_at) or "",
            pull_request=pr,
            rounds=Rounds(
                review=record.review_rounds,
                ci=record.ci_rounds,
                granted=record.granted_rounds,
                exhausted=record.exhausted,
            ),
            published=[
                PublishedOut(sink=p.sink, location=p.location, tasks=list(p.tasks), files=p.files)
                for p in record.published
            ],
            gate=gate,
            review_wait=review_wait,
            revision=record.revision,
            available_actions=[a for a in RUN_ACTIONS if a in actions],
        )

    def tasks(self, run_id: str) -> list[Task]:
        records: list[TaskRecord] = self.store.get_tasks(run_id)
        return [
            Task(
                id=t.spec.id,
                title=t.spec.title,
                description=t.spec.description,
                state=t.state,
                depends_on=list(t.spec.depends_on),
                revisions=t.revisions,
                replans=t.replans,
                verify_suspect=t.verify_suspect,
                verify_reauthors=t.verify_reauthors,
                output=(
                    TaskOutputOut(summary=t.output.summary, files=list(t.output.files))
                    if t.output is not None
                    else None
                ),
            )
            for t in records
        ]

    # -- the catalog ---------------------------------------------------------------

    def repositories(self) -> list[Repository]:
        entries = list(self.config.github.repo_list())
        ids = self.ids.repository_ids([e.repo for e in entries], self.now) if entries else {}
        health = {
            str(h["repo"]): h
            for h in self.status().get("repos") or []
            if isinstance(h, dict) and "repo" in h and "state" in h
        }
        daemon = self.config.daemon
        return [
            Repository(
                id=ids[entry.repo],
                repository=entry.repo,
                forge=str(self.config.vcs_kind_for(entry.repo)),
                enabled=entry.enabled,
                deliver_base=entry.deliver_base,
                trigger_label=entry.trigger_label or daemon.trigger_label,
                workload_label=entry.workload_label or daemon.workload_label,
                health=(
                    RepoHealth.model_validate(health[entry.repo]) if entry.repo in health else None
                ),
            )
            for entry in entries
        ]

    def profiles(self) -> list[Profile]:
        default = self.config.workload.default
        return [
            Profile(
                id=p.name,
                name=p.name,
                description=p.description,
                sinks=[str(s) for s in p.sinks],
                publish=str(p.publish),
                repo=p.repo,
                default=p.name == default,
            )
            for p in self.config.workloads
        ]

    def recipes(self) -> list[Recipe]:
        enabled = bool(self.config.entrygraph.enabled)
        return [
            Recipe(
                id=name,
                name=name,
                parameters=list(RECIPE_PARAMETERS.get(name, ())),
                enabled=enabled if name == "entrygraph" else True,
            )
            for name in sorted(RECIPES)
        ]
