"""Durable serial campaigns above the ordinary per-item run lifecycle.

Admission pins a complete plan in one transaction and does not enqueue
children. The loop admits only each campaign's first incomplete step into
the ordinary queue, and filters label discovery through ``allows``. A
delivery verifier must supply evidence before that frontier advances.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from sbxloop.daemon.model import WorkItem
from sbxloop.db import begin_immediate, ensure_schema, open_engine
from sbxloop.db.campaign_models import CampaignRow, CampaignStepRow
from sbxloop.db.daemon_models import DaemonRunRow, WorkItemRow
from sbxloop.engine.model import Published, RunKind
from sbxloop.ghids import format_gh_id, is_repo_slug, normalize_item_id, try_parse_gh_id


class CampaignError(ValueError):
    """A plan or control operation cannot preserve campaign invariants."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class WorkItemSnapshot(WorkItem):
    """Copy of the admitted ask; later work-item bookkeeping cannot edit it."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def member_key(item: WorkItem) -> str:
    """One identity across legacy ids, qualified ids and repository casing."""
    parsed = try_parse_gh_id(item.item_id)
    if parsed is None:
        return item.item_id
    repo = item.repo or parsed.repo
    if repo is None or not is_repo_slug(repo):
        raise CampaignError(f"{item.item_id} needs an explicit repository")
    if parsed.repo and parsed.repo.casefold() != repo.casefold():
        raise CampaignError(f"{item.item_id} disagrees with repository {repo}")
    return format_gh_id(parsed.kind, parsed.number, repo=repo.casefold())


class CampaignStepPlan(_Frozen):
    item: WorkItemSnapshot
    expected_base: str | None = None
    prerequisites: tuple[str, ...] = ()
    # Rendered issue discussion/linked context captured with admission. A
    # later source poll must not rewrite the instructions for an accepted ask.
    context: str = ""

    @field_validator("item", mode="before")
    @classmethod
    def _copy_item(cls, value: object) -> object:
        return value.model_dump() if isinstance(value, WorkItem) else value

    @model_validator(mode="after")
    def _validate_step(self) -> Self:
        parsed = try_parse_gh_id(self.item.item_id)
        if self.item.repo is None and parsed is not None and parsed.repo is not None:
            object.__setattr__(self, "item", self.item.model_copy(update={"repo": parsed.repo}))
        identity = member_key(self.item)
        if parsed is not None:
            object.__setattr__(
                self,
                "item",
                self.item.model_copy(
                    update={"item_id": identity, "repo": (self.item.repo or "").casefold()}
                ),
            )
        if self.item.kind == "code" and not is_repo_slug(self.item.repo or ""):
            raise CampaignError(f"{self.item.item_id} needs an explicit repository")
        if self.item.kind == "code" and not (self.expected_base or "").strip():
            raise CampaignError(f"{self.item.item_id} needs its intended base branch")
        if self.item.kind == "workload" and self.expected_base is not None:
            raise CampaignError("workload steps do not have an intended merge base")
        if (
            self.item.state != "queued"
            or self.item.claimed
            or self.item.run_id is not None
            or self.item.attempts
            or self.item.claim_token is not None
            or self.item.pending_report is not None
            or self.item.restarted
        ):
            raise CampaignError(f"{self.item.item_id} already has execution history")
        return self


class CampaignPlan(_Frozen):
    campaign_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)
    source_url: str = ""
    steps: tuple[CampaignStepPlan, ...]

    @field_validator("campaign_id", "title", "requested_by")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise CampaignError("campaign identity, title and requester must not be blank")
        return value

    @model_validator(mode="after")
    def _validate_plan(self) -> Self:
        if not self.steps:
            raise CampaignError("a campaign needs at least one step")
        identities: set[str] = set()
        source_keys: set[tuple[str, str]] = set()
        normalized: list[CampaignStepPlan] = []
        for step in self.steps:
            key = member_key(step.item)
            source_key = (step.item.source_key, (step.item.repo or "").casefold())
            if key in identities or source_key in source_keys:
                raise CampaignError(f"duplicate campaign member: {step.item.item_id}")
            dependencies: list[str] = []
            for dependency in step.prerequisites:
                parsed = try_parse_gh_id(dependency)
                if parsed is not None:
                    repo = parsed.repo or step.item.repo
                    if repo is None:
                        raise CampaignError(f"prerequisite {dependency} needs a repository")
                    dependency = format_gh_id(parsed.kind, parsed.number, repo.casefold())
                else:
                    dependency = normalize_item_id(dependency)
                if dependency not in identities:
                    raise CampaignError(
                        f"{step.item.item_id}: prerequisite {dependency} must be an earlier member"
                    )
                if dependency not in dependencies:
                    dependencies.append(dependency)
            normalized.append(step.model_copy(update={"prerequisites": tuple(dependencies)}))
            identities.add(key)
            source_keys.add(source_key)
        object.__setattr__(self, "steps", tuple(normalized))
        return self

    @property
    def plan_hash(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class PublishedReceipt(_Frozen):
    """Immutable copy of the engine's structured publication receipt."""

    sink: str = Field(min_length=1)
    location: str = Field(min_length=1)
    tasks: tuple[str, ...] = ()
    files: int = Field(default=0, ge=0)


class DeliveryEvidence(_Frozen):
    """Verified delivery facts; the caller checks their external provenance."""

    kind: RunKind
    run_id: str = Field(min_length=1)
    verified_at: float = Field(ge=0)
    repo: str | None = None
    base: str | None = None
    pr_number: int | None = Field(default=None, ge=1)
    commit_sha: str | None = None
    published: tuple[PublishedReceipt, ...] = ()

    @field_validator("published", mode="before")
    @classmethod
    def _copy_receipts(cls, value: object) -> object:
        if isinstance(value, (tuple, list)):
            return tuple(
                receipt.model_dump() if isinstance(receipt, Published) else receipt
                for receipt in value
            )
        return value

    @model_validator(mode="after")
    def _validate_delivery(self) -> Self:
        if self.kind == "code":
            if not self.repo or not is_repo_slug(self.repo) or not self.base:
                raise CampaignError("code delivery needs its repository and base branch")
            if self.pr_number is None or not self.commit_sha:
                raise CampaignError("code delivery needs the merged PR and commit")
            if self.published:
                raise CampaignError("code delivery cannot use workload publication receipts")
        elif not self.published:
            raise CampaignError("workload delivery needs publication receipts")
        elif self.base is not None or self.pr_number is not None or self.commit_sha is not None:
            raise CampaignError("workload publication is not code landing evidence")
        return self


class CampaignStepSnapshot(CampaignStepPlan):
    campaign_id: str
    position: int
    evidence: DeliveryEvidence | None = None
    completed_at: float | None = None

    @property
    def complete(self) -> bool:
        return self.evidence is not None


class CampaignSnapshot(_Frozen):
    plan: CampaignPlan
    created_at: float
    updated_at: float
    held: bool
    prepared: bool
    hold_reason: str | None
    hold_actor: str | None
    blocker_item_id: str | None
    blocker_reason: str | None
    order_actor: str | None
    order_changed_at: float | None
    steps: tuple[CampaignStepSnapshot, ...]

    @property
    def campaign_id(self) -> str:
        return self.plan.campaign_id

    @property
    def complete(self) -> bool:
        return all(step.complete for step in self.steps)

    @property
    def next_step(self) -> CampaignStepSnapshot | None:
        return next((step for step in self.steps if not step.complete), None)


class CampaignStore:
    """Campaign transactions against the daemon's existing state database."""

    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly
        self._lock = threading.RLock()
        self._engine = open_engine(path, readonly=readonly)
        if not readonly:
            ensure_schema(self._engine)

    def close(self) -> None:
        self._engine.dispose()

    @contextmanager
    def _write(self) -> Iterator[Session]:
        with self._lock, begin_immediate(self._engine) as conn, Session(conn) as session:
            yield session
            session.flush()

    @staticmethod
    def _snapshot(session: Session, row: CampaignRow) -> CampaignSnapshot:
        plan = CampaignPlan.model_validate_json(row.plan_json)
        step_rows = session.scalars(
            select(CampaignStepRow)
            .where(CampaignStepRow.campaign_id == row.campaign_id)
            .order_by(CampaignStepRow.position)
        ).all()
        if (
            len(step_rows) != len(plan.steps)
            or {step.member_key for step in step_rows}
            != {member_key(step.item) for step in plan.steps}
            or any(step.position != position for position, step in enumerate(step_rows, 1))
        ):
            raise CampaignError(f"campaign {row.campaign_id} has incomplete membership")
        return CampaignSnapshot(
            plan=plan,
            created_at=row.created_at,
            updated_at=row.updated_at,
            held=bool(row.held),
            prepared=bool(row.prepared),
            hold_reason=row.hold_reason,
            hold_actor=row.hold_actor,
            blocker_item_id=row.blocker_item_id,
            blocker_reason=row.blocker_reason,
            order_actor=row.order_actor,
            order_changed_at=row.order_changed_at,
            steps=tuple(
                CampaignStepSnapshot(
                    **CampaignStepPlan.model_validate_json(step.step_json).model_dump(),
                    campaign_id=step.campaign_id,
                    position=step.position,
                    evidence=(
                        DeliveryEvidence.model_validate_json(step.evidence_json)
                        if step.evidence_json is not None
                        else None
                    ),
                    completed_at=step.completed_at,
                )
                for step in step_rows
            ),
        )

    @staticmethod
    def _existing_rows(session: Session, item: WorkItem) -> list[WorkItemRow]:
        # A legacy repoless row cannot safely be assigned to this campaign
        # until startup's repo backfill establishes where it belongs.
        rows = session.scalars(
            select(WorkItemRow).where(
                or_(
                    WorkItemRow.item_id == item.item_id,
                    func.lower(WorkItemRow.repo).in_(((item.repo or "").casefold(), "")),
                )
            )
        )
        matches: list[WorkItemRow] = []
        identity = member_key(item)
        parsed = try_parse_gh_id(item.item_id)
        for row in rows:
            existing = WorkItem(
                item_id=row.item_id,
                source_key=row.source_key,
                title=row.title,
                repo=row.repo or None,
            )
            existing_parsed = try_parse_gh_id(row.item_id)
            try:
                same_identity = member_key(existing) == identity
            except CampaignError:
                same_identity = (
                    parsed is not None
                    and existing_parsed is not None
                    and (parsed.kind, parsed.number)
                    == (existing_parsed.kind, existing_parsed.number)
                )
            if same_identity or row.source_key == item.source_key:
                matches.append(row)
        return matches

    @staticmethod
    def _execution_history(session: Session) -> set[str]:
        # Mutable queue rows can be discarded after a failed claim or
        # superseded by rediscovery. The run ledger remains the authority
        # on whether this source has already executed. Old unqualified
        # GitHub rows cannot prove which repo they belong to, so matching
        # them requires reconciliation rather than guessing a new run.
        history: set[str] = set()
        for historic_id in session.scalars(select(DaemonRunRow.item_id).distinct()):
            parsed = try_parse_gh_id(historic_id)
            history.add(
                format_gh_id(
                    parsed.kind,
                    parsed.number,
                    parsed.repo.casefold() if parsed.repo is not None else None,
                )
                if parsed is not None
                else historic_id
            )
        return history

    @staticmethod
    def _has_history(item: WorkItem, history: set[str]) -> bool:
        key = member_key(item)
        parsed = try_parse_gh_id(key)
        return key in history or (
            parsed is not None and format_gh_id(parsed.kind, parsed.number) in history
        )

    def admit(self, plan: CampaignPlan, now: float) -> bool:
        """Persist every member or none; a replay cannot edit the pinned scope.

        Pristine queued rows are absorbed into the pinned plan. A member
        with execution history must be reconciled explicitly instead of
        silently restarted by intake. Only the frontier is later enqueued.
        """
        # Revalidate even model_copy callers before beginning the transaction.
        plan = CampaignPlan.model_validate(plan.model_dump())
        with self._write() as session:
            previous = session.get(CampaignRow, plan.campaign_id)
            if previous is not None:
                if previous.plan_hash != plan.plan_hash:
                    raise CampaignError(f"campaign {plan.campaign_id} already has a different plan")
                return False
            history = self._execution_history(session)
            absorbed: dict[str, WorkItemRow] = {}
            for step in plan.steps:
                key = member_key(step.item)
                if self._has_history(step.item, history):
                    raise CampaignError(f"{step.item.item_id} already has execution history")
                repo = (step.item.repo or "").casefold()
                occupied = session.scalars(
                    select(CampaignStepRow).where(
                        or_(
                            CampaignStepRow.member_key == key,
                            (CampaignStepRow.source_key == step.item.source_key)
                            & (CampaignStepRow.repo == repo),
                        )
                    )
                ).first()
                if occupied is not None:
                    raise CampaignError(
                        f"{step.item.item_id} already belongs to campaign {occupied.campaign_id}"
                    )
                for existing in self._existing_rows(session, step.item):
                    if not existing.repo and step.item.repo:
                        raise CampaignError(f"{existing.item_id} needs repository reconciliation")
                    if self._started(existing):
                        raise CampaignError(f"{existing.item_id} already has execution history")
                    absorbed[existing.item_id] = existing
            session.add(
                CampaignRow(
                    campaign_id=plan.campaign_id,
                    plan_hash=plan.plan_hash,
                    plan_json=plan.model_dump_json(),
                    created_at=now,
                    updated_at=now,
                    held=0,
                    prepared=0,
                )
            )
            session.flush()
            for position, step in enumerate(plan.steps, 1):
                session.add(
                    CampaignStepRow(
                        member_key=member_key(step.item),
                        campaign_id=plan.campaign_id,
                        position=position,
                        source_key=step.item.source_key,
                        repo=(step.item.repo or "").casefold(),
                        step_json=step.model_dump_json(),
                    )
                )
            for existing in absorbed.values():
                session.delete(existing)
            return True

    def get(self, campaign_id: str) -> CampaignSnapshot | None:
        with self._lock, Session(self._engine) as session:
            row = session.get(CampaignRow, campaign_id)
            return self._snapshot(session, row) if row is not None else None

    def list(self) -> list[CampaignSnapshot]:
        with self._lock, Session(self._engine) as session:
            rows = session.scalars(
                select(CampaignRow).order_by(CampaignRow.created_at, CampaignRow.campaign_id)
            ).all()
            return [self._snapshot(session, row) for row in rows]

    def member(self, item: WorkItem) -> CampaignStepSnapshot | None:
        with self._lock, Session(self._engine) as session:
            try:
                key = member_key(item)
            except CampaignError:
                parsed = try_parse_gh_id(item.item_id)
                if item.repo is not None or parsed is None or parsed.repo is not None:
                    raise
                # Standalone legacy fixtures/items remain usable while no
                # campaign owns that source identity. A possible campaign
                # collision needs repo backfill before dispatch can decide.
                occupied = session.scalars(
                    select(CampaignStepRow).where(CampaignStepRow.source_key == item.source_key)
                ).first()
                if occupied is None:
                    return None
                raise
            row = session.scalars(
                select(CampaignStepRow).where(
                    or_(
                        CampaignStepRow.member_key == key,
                        (CampaignStepRow.source_key == item.source_key)
                        & (CampaignStepRow.repo == (item.repo or "").casefold()),
                    )
                )
            ).first()
            if row is None:
                return None
            campaign = session.get(CampaignRow, row.campaign_id)
            if campaign is None:
                raise CampaignError(f"campaign missing for {item.item_id}")
            return self._snapshot(session, campaign).steps[row.position - 1]

    def ready(self) -> builtins.list[CampaignStepSnapshot]:
        """Each unheld campaign's first incomplete step, never later children."""
        return [
            step
            for campaign in self.list()
            if campaign.prepared and not campaign.held and campaign.blocker_reason is None
            if (step := campaign.next_step) is not None
        ]

    @staticmethod
    def _step_for_id(snapshot: CampaignSnapshot, item_id: str) -> CampaignStepSnapshot:
        normalized = normalize_item_id(item_id)
        parsed = try_parse_gh_id(normalized)
        matches: list[CampaignStepSnapshot] = []
        for step in snapshot.steps:
            candidate = try_parse_gh_id(step.item.item_id)
            if step.item.item_id == normalized or (
                parsed is not None
                and candidate is not None
                and (parsed.kind, parsed.number) == (candidate.kind, candidate.number)
                and (
                    parsed.repo is None
                    or parsed.repo.casefold() == (step.item.repo or "").casefold()
                )
            ):
                matches.append(step)
        if len(matches) != 1:
            raise CampaignError(
                f"{item_id} does not uniquely identify a member of campaign {snapshot.campaign_id}"
            )
        return matches[0]

    @staticmethod
    def _started(row: WorkItemRow) -> bool:
        return bool(
            row.state != "queued"
            or row.claimed
            or row.run_id is not None
            or row.attempts
            or row.claim_token is not None
            or row.pending_report is not None
            or row.prior_run_id is not None
            or row.prior_branch is not None
            or row.prior_pr_number is not None
        )

    def move(
        self,
        campaign_id: str,
        item_id: str,
        *,
        before: str | None = None,
        after: str | None = None,
        actor: str,
        now: float,
    ) -> bool:
        """Move pending work without rewriting scope or explicit prerequisites.

        Completed or started positions stay fixed, including the running
        frontier. Strict serial readiness comes from these positions;
        implicit predecessor edges are deliberately not part of the plan.
        """
        if (before is None) == (after is None):
            raise CampaignError("campaign move needs exactly one of before or after")
        if not actor.strip():
            raise CampaignError("campaign move needs an actor")
        with self._write() as session:
            row = session.get(CampaignRow, campaign_id)
            if row is None:
                raise CampaignError(f"unknown campaign: {campaign_id}")
            snapshot = self._snapshot(session, row)
            moving = self._step_for_id(snapshot, item_id)
            anchor = self._step_for_id(snapshot, before if before is not None else after or "")
            if moving == anchor:
                raise CampaignError("a step cannot be moved relative to itself")
            reordered = [step for step in snapshot.steps if step != moving]
            position = reordered.index(anchor) + int(after is not None)
            reordered.insert(position, moving)
            if tuple(reordered) == snapshot.steps:
                return False
            history = self._execution_history(session)
            seen: set[str] = set()
            for new_position, step in enumerate(reordered, 1):
                missing = set(step.prerequisites) - seen
                if missing:
                    raise CampaignError(
                        f"{step.item.item_id} must follow prerequisite {sorted(missing)[0]}"
                    )
                seen.add(member_key(step.item))
                if new_position == step.position:
                    continue
                if (
                    step.complete
                    or step.item.item_id == snapshot.blocker_item_id
                    or self._has_history(step.item, history)
                    or any(
                        self._started(existing)
                        for existing in self._existing_rows(session, step.item)
                    )
                ):
                    raise CampaignError(
                        f"{step.item.item_id} already started or is held at the frontier"
                    )
            changed: list[tuple[CampaignStepRow, int]] = []
            for new_position, step in enumerate(reordered, 1):
                if new_position == step.position:
                    continue
                step_row = session.get(CampaignStepRow, member_key(step.item))
                if step_row is None:
                    raise CampaignError(f"missing campaign member: {step.item.item_id}")
                changed.append((step_row, new_position))
                step_row.position = -step.position
            # Vacate all changed positive positions before taking their new
            # ones. The intermediate values stay inside this transaction.
            session.flush()
            for step_row, new_position in changed:
                step_row.position = new_position
            row.order_actor = actor
            row.order_changed_at = now
            # The previous frontier may already carry its trigger label.
            # Persist the new order as unprepared before the coordinator
            # parks source labels; a crash cannot release both frontiers.
            row.prepared = 0
            row.updated_at = now
            return True

    def allows(self, item: WorkItem) -> bool:
        """Whether normal discovery may dispatch this standalone/member item."""
        member = self.member(item)
        if member is None:
            return True
        campaign = self.get(member.campaign_id)
        if campaign is None:
            return False
        return (
            campaign.prepared
            and not campaign.held
            and campaign.blocker_reason is None
            and campaign.next_step is not None
            and campaign.next_step.position == member.position
        )

    def mark_prepared(self, campaign_id: str, *, now: float) -> bool:
        """Release initial admission only after every source member is parked."""
        with self._write() as session:
            row = session.get(CampaignRow, campaign_id)
            if row is None:
                raise CampaignError(f"unknown campaign: {campaign_id}")
            if row.prepared:
                return False
            row.prepared = 1
            row.updated_at = now
            return True

    def set_hold(
        self, campaign_id: str, held: bool, *, reason: str, actor: str, now: float
    ) -> bool:
        if not actor.strip() or not reason.strip():
            raise CampaignError("campaign hold/resume needs an actor and a reason")
        with self._write() as session:
            row = session.get(CampaignRow, campaign_id)
            if row is None:
                raise CampaignError(f"unknown campaign: {campaign_id}")
            if bool(row.held) == held:
                return False
            row.held = int(held)
            row.hold_reason = reason if held else None
            row.hold_actor = actor
            row.updated_at = now
            return True

    def set_blocker(
        self, campaign_id: str, item_id: str | None, reason: str | None, *, now: float
    ) -> bool:
        """Persist an automatic wait reason independently of a person's hold."""
        if (item_id is None) != (reason is None) or (reason is not None and not reason.strip()):
            raise CampaignError("a campaign blocker needs both item and reason")
        with self._write() as session:
            row = session.get(CampaignRow, campaign_id)
            if row is None:
                raise CampaignError(f"unknown campaign: {campaign_id}")
            snapshot = self._snapshot(session, row)
            if item_id is not None and (
                snapshot.next_step is None
                or normalize_item_id(item_id) != snapshot.next_step.item.item_id
            ):
                raise CampaignError("a blocker must describe the first incomplete step")
            if row.blocker_item_id == item_id and row.blocker_reason == reason:
                return False
            row.blocker_item_id = item_id
            row.blocker_reason = reason
            row.updated_at = now
            return True

    def record_success(
        self, campaign_id: str, item_id: str, evidence: DeliveryEvidence, now: float
    ) -> bool:
        """Checkpoint verified delivery once; later row edits cannot revoke it."""
        evidence = DeliveryEvidence.model_validate(evidence.model_dump())
        with self._write() as session:
            row = session.get(CampaignRow, campaign_id)
            if row is None:
                raise CampaignError(f"unknown campaign: {campaign_id}")
            snapshot = self._snapshot(session, row)
            step = self._step_for_id(snapshot, item_id)
            if step.evidence is not None:
                if step.evidence != evidence:
                    raise CampaignError(f"{item_id} already has different delivery evidence")
                return False
            if snapshot.next_step != step:
                raise CampaignError(f"{item_id} is waiting for its predecessor")
            if step.item.kind != evidence.kind:
                raise CampaignError(f"{item_id} has the wrong kind of delivery evidence")
            if step.item.kind == "code" and (
                evidence.base != step.expected_base
                or (evidence.repo or "").casefold() != (step.item.repo or "").casefold()
            ):
                raise CampaignError(
                    f"{item_id} delivery does not match its intended repository/base"
                )
            step_row = session.get(CampaignStepRow, member_key(step.item))
            if step_row is None:
                raise CampaignError(f"missing campaign member: {item_id}")
            step_row.evidence_json = evidence.model_dump_json()
            step_row.completed_at = now
            row.blocker_item_id = None
            row.blocker_reason = None
            row.updated_at = now
            return True
