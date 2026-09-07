"""Coordinate serial campaigns without teaching the run engine about epics."""

from __future__ import annotations

from collections.abc import Callable

from sbxloop.config import Config
from sbxloop.daemon.campaign_source import CampaignSource, CampaignSourceError, source_for_campaign
from sbxloop.daemon.campaigns import (
    CampaignError,
    CampaignPlan,
    CampaignSnapshot,
    CampaignStepSnapshot,
    CampaignStore,
    DeliveryEvidence,
    PublishedReceipt,
    member_key,
)
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.sources import WorkSource
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import RunRecord
from sbxloop.engine.sinks import sink_of
from sbxloop.engine.store import StateStore
from sbxloop.errors import SbxloopError
from sbxloop.ghids import is_local_id


class CampaignCoordinator:
    """Called under the loop's campaign lock, never around a whole engine run.

    Source preparation is finite and happens only after durable admission.
    Reconciliation reads delivery proof and checkpoints it independently of
    source reporting, so a failed issue-close report cannot undo delivery.
    """

    def __init__(
        self,
        config: Config,
        store: StateStore,
        dstore: DaemonStore,
        source: Callable[[], WorkSource],
        clock: Callable[[], float],
        notice: Callable[..., None],
        context: Callable[[WorkItem], str],
    ) -> None:
        self.config, self.store, self.dstore = config, store, dstore
        self.source, self.clock, self.notice = source, clock, notice
        self.context = context
        self.campaigns = CampaignStore(dstore.path)

    def close(self) -> None:
        self.campaigns.close()

    def _require(self, campaign_id: str) -> CampaignSnapshot:
        campaign = self.campaigns.get(campaign_id)
        if campaign is None:
            raise CampaignError(f"unknown campaign: {campaign_id}")
        return campaign

    def _adapter(self, item: WorkItem) -> CampaignSource | None:
        source = source_for_campaign(self.source(), item)
        if source is None:
            if not is_local_id(item.item_id) or item.kind != "workload":
                raise CampaignError(f"{item.item_id} has no supported campaign source")
            return None
        return CampaignSource(source)

    def admit(self, plan: CampaignPlan) -> str:
        existing = self.campaigns.get(plan.campaign_id)
        if existing is not None:
            contexts = {member_key(step.item): step.context for step in existing.plan.steps}
            plan = plan.model_copy(
                update={
                    "steps": tuple(
                        step.model_copy(
                            update={
                                "context": step.context or contexts.get(member_key(step.item), "")
                            }
                        )
                        for step in plan.steps
                    )
                }
            )
            self.campaigns.admit(plan, self.clock())  # verifies replay identity
            return self.status(plan.campaign_id)
        for step in plan.steps:
            if step.item.profile is not None and not any(
                profile.name == step.item.profile for profile in self.config.workloads
            ):
                raise CampaignError(f"unknown workload profile: {step.item.profile}")
            adapter = self._adapter(step.item)
            if adapter is not None:
                adapter.validate_campaign_item(step.item)
        plan = plan.model_copy(
            update={
                "steps": tuple(
                    step.model_copy(update={"context": self.context(step.item)})
                    if not step.context and not is_local_id(step.item.item_id)
                    else step
                    for step in plan.steps
                )
            }
        )
        self.campaigns.admit(plan, self.clock())
        self.notice(
            "campaign.admitted",
            f"{plan.requested_by} admitted campaign {plan.campaign_id}: "
            f"{len(plan.steps)} ordered steps",
            campaign=plan.campaign_id,
            by=plan.requested_by,
        )
        self._prepare(self._require(plan.campaign_id))
        return self.status(plan.campaign_id)

    def _prepare(self, campaign: CampaignSnapshot) -> None:
        if campaign.prepared or campaign.held or campaign.complete:
            return
        try:
            for step in campaign.steps:
                if step.complete:
                    continue
                existing = self._live_item(step)
                if existing is not None and (
                    existing.state != "queued"
                    or existing.claimed
                    or existing.claim_token
                    or existing.run_id
                    or existing.attempts
                    or self.dstore.runs_for_item(existing.item_id)
                ):
                    if step != campaign.next_step:
                        raise CampaignError(f"later step {step.item.item_id} has already started")
                    continue  # The frontier retains the normal claim/recovery path.
                adapter = self._adapter(existing or step.item)
                if adapter is not None:
                    adapter.park_campaign_item(existing or step.item)
                if existing is not None and step != campaign.next_step:
                    self.dstore.discard(existing.item_id)
        except (CampaignError, CampaignSourceError, SbxloopError) as exc:
            self.hold(campaign.campaign_id, "system", f"source preparation: {exc}")
            return
        self.campaigns.mark_prepared(campaign.campaign_id, now=self.clock())

    def hold(self, campaign_id: str, by: str | None = None, reason: str = "operator hold") -> str:
        who = by or "operator"
        if self.campaigns.set_hold(campaign_id, True, reason=reason, actor=who, now=self.clock()):
            self.notice(
                "campaign.held",
                f"{who} held campaign {campaign_id}: {reason}",
                campaign=campaign_id,
                by=who,
                reason=reason,
            )
        return self.status(campaign_id)

    def resume(self, campaign_id: str, by: str | None = None) -> str:
        who = by or "operator"
        if self.campaigns.set_hold(
            campaign_id, False, reason="resumed", actor=who, now=self.clock()
        ):
            self.notice(
                "campaign.resumed",
                f"{who} resumed campaign {campaign_id}",
                campaign=campaign_id,
                by=who,
            )
        # The next tick prepares sources; a control response never starts work.
        self.reconcile()
        return self.status(campaign_id)

    def move(
        self,
        campaign_id: str,
        item_id: str,
        *,
        before: str | None = None,
        after: str | None = None,
        by: str | None = None,
    ) -> str:
        who = by or "operator"
        if self.campaigns.move(
            campaign_id, item_id, before=before, after=after, actor=who, now=self.clock()
        ):
            self.notice(
                "campaign.moved",
                f"{who} moved {item_id} "
                f"{'before' if before is not None else 'after'} {before or after} "
                f"in campaign {campaign_id}",
                campaign=campaign_id,
                by=who,
            )
            self._prepare(self._require(campaign_id))
        return self.status(campaign_id)

    def _live_item(self, step: CampaignStepSnapshot) -> WorkItem | None:
        key = member_key(step.item)
        matching: list[WorkItem] = []
        for item in self.dstore.items():
            try:
                if member_key(item) == key:
                    matching.append(item)
            except CampaignError:
                continue  # unrelated legacy rows are attributed by normal recovery
        if len(matching) > 1:
            raise CampaignError(f"{step.item.item_id} has multiple work-item rows")
        return matching[0] if matching else None

    def snapshot_item(self, item: WorkItem) -> WorkItem:
        step = self.campaigns.member(item)
        if step is None:
            return item
        return item.model_copy(
            update={
                field: getattr(step.item, field)
                for field in (
                    "item_id",
                    "source_key",
                    "title",
                    "body",
                    "url",
                    "repo",
                    "kind",
                    "profile",
                    "requested_by",
                )
            }
        )

    def allows(self, item: WorkItem) -> bool:
        return self.campaigns.allows(item)

    def _block(self, campaign_id: str, step: CampaignStepSnapshot, reason: str | None) -> None:
        if (
            self.campaigns.set_blocker(
                campaign_id, step.item.item_id if reason else None, reason, now=self.clock()
            )
            and reason
        ):
            self.notice(
                "campaign.waiting",
                f"campaign {campaign_id} waits for {step.item.item_id}: {reason}",
                campaign=campaign_id,
                item=step.item.item_id,
                reason=reason,
            )

    def _evidence(self, step: CampaignStepSnapshot, run: RunRecord) -> DeliveryEvidence:
        if run.kind != step.item.kind:
            raise CampaignError("run kind disagrees with the admitted step")
        if step.item.kind == "code":
            adapter = self._adapter(step.item)
            if adapter is None:
                raise CampaignError("code delivery has no repository source")
            proof = adapter.verify_code_delivery(
                step.item.repo or "", step.expected_base or "", run
            )
            return DeliveryEvidence(
                kind="code",
                run_id=run.run_id,
                verified_at=self.clock(),
                repo=proof.repo,
                base=proof.base,
                pr_number=proof.pr_number,
                commit_sha=proof.merge_commit_sha,
            )
        if run.state != "completed" or not run.published:
            raise CampaignError("workload has no completed publication with receipts")
        tasks = self.store.get_tasks(run.run_id)
        completed = [task for task in tasks if task.state == "done"]
        if not completed or any(task.state not in ("done", "skipped") for task in tasks):
            raise CampaignError("workload publication has no complete task record")
        for task in completed:
            if task.output is None or not any(
                receipt.sink == sink_of(task) and task.spec.id in receipt.tasks and receipt.location
                for receipt in run.published
            ):
                raise CampaignError(f"workload publication receipt missing for task {task.spec.id}")
        return DeliveryEvidence(
            kind="workload",
            run_id=run.run_id,
            verified_at=self.clock(),
            published=tuple(
                PublishedReceipt.model_validate(receipt.model_dump()) for receipt in run.published
            ),
        )

    def reconcile(self) -> None:
        """Read evidence even while held; never claim or change source labels."""
        for campaign in self.campaigns.list():
            step = campaign.next_step
            if step is None:
                continue
            try:
                item = self._live_item(step)
                if item is None:
                    if self.dstore.runs_for_item(step.item.item_id):
                        self._block(
                            campaign.campaign_id,
                            step,
                            "work item missing after execution; reconciliation required",
                        )
                    continue
                if item.run_id is not None:
                    run = self.store.get_run(item.run_id)
                    if run.state in ("merged", "completed") and item.state not in (
                        "failed",
                        "cancelled",
                    ):
                        evidence = self._evidence(step, run)
                        if self.campaigns.record_success(
                            campaign.campaign_id, step.item.item_id, evidence, self.clock()
                        ):
                            self.notice(
                                "campaign.step_completed",
                                f"campaign {campaign.campaign_id}: {step.item.item_id} delivered",
                                campaign=campaign.campaign_id,
                                item=step.item.item_id,
                                run=run.run_id,
                            )
                            if self._require(campaign.campaign_id).complete:
                                self.notice(
                                    "campaign.completed",
                                    f"campaign {campaign.campaign_id} completed",
                                    campaign=campaign.campaign_id,
                                )
                        continue
                if item.state in ("queued", "running"):
                    self._block(campaign.campaign_id, step, None)
                else:
                    self._block(
                        campaign.campaign_id, step, item.last_error or f"step is {item.state}"
                    )
            except (CampaignError, CampaignSourceError, SbxloopError) as exc:
                self._block(campaign.campaign_id, step, str(exc))

    def enqueue_ready(self) -> int:
        """Prepare sources and enqueue only the first unfinished step."""
        for campaign in self.campaigns.list():
            self._prepare(campaign)
        added = 0
        for step in self.campaigns.ready():
            try:
                existing = self._live_item(step)
                if existing is not None and existing.state != "queued":
                    continue
                if existing is None and self.dstore.runs_for_item(step.item.item_id):
                    self._block(
                        step.campaign_id,
                        step,
                        "work item missing after execution; reconciliation required",
                    )
                    continue
                pending = existing or WorkItem.model_validate(step.item.model_dump())
                if not pending.claimed:
                    if pending.claim_token:
                        if self.source().settle_claim(pending):
                            self.dstore.mark_claimed(pending.item_id, self.clock())
                            continue
                        self.dstore.clear_claim(pending.item_id, self.clock())
                        pending = self.dstore.get(pending.item_id) or pending
                    adapter = self._adapter(pending)
                    if adapter is not None:
                        adapter.prepare_campaign_item(pending)
                if existing is None:
                    added += int(self.dstore.upsert_new(pending, self.clock()))
            except (CampaignError, CampaignSourceError, SbxloopError) as exc:
                self.hold(step.campaign_id, "system", f"source preparation: {exc}")
        return added

    def claim_failed(self, item: WorkItem) -> bool:
        step = self.campaigns.member(item)
        if step is None:
            return False
        self.hold(
            step.campaign_id,
            "system",
            f"could not claim {item.item_id}; resolve ownership and resume",
        )
        return True

    def status(self, campaign_id: str | None = None) -> str:
        campaigns = (
            [self._require(campaign_id)] if campaign_id is not None else self.campaigns.list()
        )
        if not campaigns:
            return "no campaigns"
        lines: list[str] = []
        for campaign in campaigns:
            count = sum(step.complete for step in campaign.steps)
            state = (
                "complete"
                if campaign.complete
                else "held"
                if campaign.held
                else "waiting"
                if campaign.blocker_reason
                else "preparing"
                if not campaign.prepared
                else "active"
            )
            lines.append(
                f"{campaign.campaign_id}: {count}/{len(campaign.steps)} delivered · {state}"
            )
            if campaign.held:
                lines.append(f"  held by {campaign.hold_actor}: {campaign.hold_reason}")
            if campaign.blocker_reason:
                lines.append(f"  waiting: {campaign.blocker_reason}")
            for step in campaign.steps:
                item = self._live_item(step)
                state = (
                    "done"
                    if step.complete
                    else item.state
                    if item
                    else "ready"
                    if step == campaign.next_step
                    else "waiting for predecessor"
                )
                lines.append(
                    f"  {step.position}. {step.item.item_id} · {step.item.title} · {state}"
                )
        return "\n".join(lines)
