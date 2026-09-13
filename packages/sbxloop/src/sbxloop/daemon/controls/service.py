"""The typed control service every surface shares.

One method per operator verb. Each takes the :class:`Principal` first,
checks the capability the verb needs, hands the loop the principal's
attribution as the ``by`` it always took, and returns a typed outcome.
Refusals the loop raises as ``ValueError`` / ``KeyError`` come back as
:class:`ControlError` with the loop's sentence intact, so the prose edge
renders exactly what it rendered before this layer existed.

No sentence is composed here except for refusals this layer introduces
(a principal without the capability). The loop's own prose replies —
``resume_review``, ``approve_merge``, the schedule verbs — ride inside
the outcome as ``message`` until the loop grows structured results of
its own; a JSON surface exposes the structured fields and may show the
message, but never parses it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import Any, Literal, TypeVar

from sbxloop.config import ScheduleConfig
from sbxloop.daemon.controls.intake import (
    AdmitRequest,
    IssueAdmission,
    admit_issue,
    build_item,
    target_key,
    upsert,
)
from sbxloop.daemon.controls.operations import OperationRunner, OperationSpec, OperationStore
from sbxloop.daemon.controls.principal import Capability, Principal
from sbxloop.daemon.controls.protocol import ControlLoop
from sbxloop.daemon.controls.results import (
    AdmitOutcome,
    CancelOutcome,
    ControlError,
    GateOutcome,
    GrantRoundsOutcome,
    ItemOutcome,
    ItemsOutcome,
    LogRecordsOutcome,
    LogTailOutcome,
    Outcome,
    PauseOutcome,
    QueueOutcome,
    ReleaseOutcome,
    RepoResumeOutcome,
    RestartOutcome,
    ResumeOutcome,
    ReviewResumeOutcome,
    ScheduleListOutcome,
    ScheduleOutcome,
    StatusOutcome,
    SteerOutcome,
    StopOutcome,
)
from sbxloop.daemon.controls.steering import SteeringStore
from sbxloop.daemon.holds import OPERATOR_HOLD, hold_name
from sbxloop.ghids import normalize_item_id


def _message(exc: BaseException) -> str:
    """The loop's sentence, as the prose edge always showed it."""
    return str(exc.args[0]) if exc.args else str(exc)


def _hold(hold: str | None) -> str:
    """The operator's hold when unnamed; a named one validated here so a
    bad name never reaches the loop (which validates again, harmlessly)."""
    try:
        return hold_name(hold or OPERATOR_HOLD)
    except ValueError as exc:
        raise ControlError("invalid_argument", str(exc)) from exc


def require(principal: Principal, capability: Capability) -> None:
    if not principal.can(capability):
        raise ControlError(
            "forbidden",
            f"{principal.id} (via {principal.via}) lacks {capability}",
            capability=capability,
        )


OutcomeT = TypeVar("OutcomeT", bound=Outcome)


class ControlService:
    """Typed controls over a :class:`~sbxloop.daemon.controls.protocol.ControlLoop`.

    A loop with an operation store (the real daemon) has every mutating
    verb recorded as a durable operation before it acts; ``operation_ids``
    collects the ids this instance recorded, newest last, for the surface
    that answers. A loop without one (a test double) is driven directly.
    """

    def __init__(self, loop: ControlLoop) -> None:
        self.loop = loop
        self.operation_ids: list[str] = []
        operations = getattr(loop, "operations", None)
        self.runner: OperationRunner | None = (
            OperationRunner(
                operations,
                generation=lambda: getattr(loop, "generation", None),
                clock=getattr(loop, "clock", time.time),
            )
            if isinstance(operations, OperationStore)
            else None
        )

    def _record(self, spec: OperationSpec, fn: Callable[[str | None], OutcomeT]) -> OutcomeT:
        """Run ``fn`` under a durable operation when the loop keeps them;
        ``fn`` receives the operation id (``None`` without a store)."""
        if self.runner is None:
            return fn(None)
        try:
            outcome = self.runner.run_sync(spec, fn)
        except ControlError as exc:
            recorded = exc.detail.get("operation_id")
            if isinstance(recorded, str):
                self.operation_ids.append(recorded)
            raise
        if outcome.operation_id is not None:
            self.operation_ids.append(outcome.operation_id)
        return outcome  # type: ignore[return-value]

    def _spec(
        self,
        action: str,
        principal: Principal,
        target_kind: str,
        target_key: str,
        *,
        idempotency: tuple[str, str] | None = None,
        expected_revision: int | None = None,
        **request: Any,
    ) -> OperationSpec:
        return OperationSpec(
            action=action,
            target_kind=target_kind,
            target_key=target_key,
            principal=principal,
            request=request,
            idempotency=idempotency,
            expected_revision=expected_revision,
        )

    # -- reads ----------------------------------------------------------------------

    def status(self, principal: Principal) -> StatusOutcome:
        require(principal, "runs:read")
        return StatusOutcome(status=self.loop.status())

    def queue(self, principal: Principal) -> QueueOutcome:
        require(principal, "runs:read")
        return QueueOutcome(items=list(self.loop.dstore.queued()))

    def items(self, principal: Principal) -> ItemsOutcome:
        require(principal, "runs:read")
        return ItemsOutcome(items=list(self.loop.dstore.items()))

    def schedules(self, principal: Principal) -> ScheduleListOutcome:
        require(principal, "runs:read")
        return ScheduleListOutcome(rows=list(self.loop.schedules()))

    def log_tail(
        self,
        principal: Principal,
        *,
        tail: int,
        level: str | None,
        grep: str | None,
        max_chars: int | None,
    ) -> LogTailOutcome:
        require(principal, "diagnostics:read")
        # Lazily: the log tail renderer lives with the prose edge.
        from sbxloop.daemon.control import format_log_tail

        text = format_log_tail(tail=tail, level=level, grep=grep, max_chars=max_chars)
        if text.startswith("unknown log level"):
            raise ControlError("invalid_argument", text)
        return LogTailOutcome(text=text)

    def log_records(
        self, principal: Principal, *, tail: int, level: str | None, grep: str | None
    ) -> LogRecordsOutcome:
        """The same ring buffer as ``log_tail``, as records rather than a
        rendered block: for a surface that shapes its own lines."""
        require(principal, "diagnostics:read")
        from sbxloop.daemon.control import LOG_LEVELS, LOG_TAIL_MAX
        from sbxloop.log import log_buffer

        tail = max(1, min(LOG_TAIL_MAX, tail))
        level = (level or "").strip().upper() or None
        buffer = log_buffer()
        try:
            records = buffer.tail(tail, level=level, grep=grep or None)
        except ValueError as exc:
            raise ControlError(
                "invalid_argument",
                f"unknown log level {level!r} — use one of {', '.join(LOG_LEVELS)}",
            ) from exc
        return LogRecordsOutcome(
            records=[
                {
                    "timestamp": r.timestamp,
                    "level": r.level,
                    "logger": r.logger,
                    "message": r.line,
                }
                for r in records
            ],
            buffer_size=len(buffer),
        )

    # -- holds ----------------------------------------------------------------------

    def pause(
        self,
        principal: Principal,
        hold: str | None = None,
        *,
        reason: str = "",
        idempotency: tuple[str, str] | None = None,
    ) -> PauseOutcome:
        require(principal, "daemon:manage")
        name = _hold(hold)

        def apply(op_id: str | None) -> PauseOutcome:
            before = set(self.loop.holds)
            extra: dict[str, Any] = {}
            if self.runner is not None:
                # A loop that keeps holds durably records whose it is.
                extra = {
                    "via": principal.via,
                    "operation_id": op_id,
                    "reason": reason,
                    "owner_id": principal.id,
                }
            try:
                holds = self.loop.pause(name, by=principal.attribution(), **extra)
            except ValueError as exc:
                raise ControlError("invalid_argument", str(exc)) from exc
            return PauseOutcome(
                hold=name, holds=list(holds), fresh=name not in before, reason=reason
            )

        # The hold's name rides the request: two holds taken under one
        # idempotency key on the one route are a conflict, not a replay.
        spec = self._spec(
            "daemon.pause",
            principal,
            "hold",
            name,
            idempotency=idempotency,
            hold=name,
            reason=reason,
        )
        return self._record(spec, apply)

    def release(
        self,
        principal: Principal,
        hold: str | None = None,
        *,
        everything: bool = False,
        only_own: bool = False,
        idempotency: tuple[str, str] | None = None,
    ) -> ReleaseOutcome:
        """Release one hold (the operator's when unnamed), or every hold.
        ``only_own`` refuses a hold another principal took — the remote
        contract's default, where releasing one person's hold must never
        release someone else's; the prose surfaces keep the operator's
        override."""
        require(principal, "daemon:manage")
        name = None if everything else _hold(hold)

        def apply(_: str | None) -> ReleaseOutcome:
            if only_own and name is not None:
                self._check_hold_owner(name, principal)
            try:
                holds = self.loop.unpause(name, by=principal.attribution())
            except ValueError as exc:
                raise ControlError("invalid_argument", str(exc)) from exc
            return ReleaseOutcome(hold=name, holds=list(holds))

        spec = self._spec(
            "daemon.release",
            principal,
            "hold",
            name or "*",
            idempotency=idempotency,
            everything=everything,
        )
        return self._record(spec, apply)

    def _check_hold_owner(self, name: str, principal: Principal) -> None:
        """A hold with a recorded owner is released by that owner; another
        principal must say it is overriding (``only_own=False``)."""
        for hold in self.loop.dstore.holds():
            if hold.name != name:
                continue
            if hold.owner_id and hold.owner_id != principal.id:
                raise ControlError(
                    "hold_owned",
                    f"hold {name!r} belongs to {hold.owner_display or hold.owner_id}"
                    + (f" (via {hold.via})" if hold.via else "")
                    + "; release it with force to override",
                    hold=name,
                    owner=hold.owner_display or hold.owner_id,
                    via=hold.via,
                )
            return

    # -- runs -----------------------------------------------------------------------

    def resume_review(
        self,
        principal: Principal,
        target: str,
        *,
        idempotency: tuple[str, str] | None = None,
    ) -> ReviewResumeOutcome:
        require(principal, "runs:control")

        def apply(_: str | None) -> ReviewResumeOutcome:
            try:
                message = self.loop.resume_review(target, principal.attribution())
            except ValueError as exc:
                raise ControlError("not_eligible", _message(exc)) from exc
            return ReviewResumeOutcome(target=target, message=message)

        spec = self._spec("run.review_resume", principal, "target", target, idempotency=idempotency)
        return self._record(spec, apply)

    def steer(
        self,
        principal: Principal,
        run_id: str,
        text: str,
        *,
        source_refs: Sequence[str] = (),
        expected_revision: int | None = None,
        deadline_s: float | None = None,
        idempotency: tuple[str, str] | None = None,
    ) -> SteerOutcome:
        """Submit explicit direction to the run in flight (#1038): a record
        first, then the hand-over; the record says what became of it."""
        require(principal, "runs:steer")
        text = text.strip()
        if not text:
            raise ControlError("invalid_argument", "an instruction needs some text")
        store = SteeringStore(self.loop.dstore)

        def apply(op_id: str | None) -> SteerOutcome:
            # The record is written under the operation (a replay of the
            # same key finds the operation, never a second record) and
            # carries the operation id, so the reconciler can find it.
            now = self.loop.clock()
            record = store.create(
                run_id=run_id,
                text=text,
                principal=principal,
                source_refs=list(source_refs),
                expected_revision=expected_revision,
                now=now,
                deadline_at=None if deadline_s is None else now + deadline_s,
                operation_id=op_id,
            )
            try:
                message_id = self.loop.steer_run(
                    run_id,
                    text,
                    by=principal.attribution(),
                    expected_revision=expected_revision,
                )
            except ControlError as exc:
                store.failed(record.id, exc.message, self.loop.clock())
                exc.detail.setdefault("steering_id", record.id)
                raise
            store.delivered(record.id, message_id, self.loop.clock())
            return SteerOutcome(steering_id=record.id, run_id=run_id, message_id=message_id)

        spec = self._spec(
            "run.steer",
            principal,
            "run",
            run_id,
            idempotency=idempotency,
            expected_revision=expected_revision,
            text=text,
            source_refs=list(source_refs),
        )
        return self._record(spec, apply)

    def cancel_current(self, principal: Principal, *, retry: bool = False) -> CancelOutcome:
        """Cancel the run in flight. Refused when nothing is running."""
        require(principal, "runs:control")
        # The record names the run in flight when the loop can say which;
        # the loop's own lock decides whether there is one at all.
        current = self.loop.status().get("current") or {}
        run_id = str(current.get("run_id") or "") or None

        def apply(op_id: str | None) -> CancelOutcome:
            kwargs: dict[str, Any] = {"retry": retry}
            if op_id is not None:
                kwargs["operation_id"] = op_id
            if not self.loop.cancel_current(principal.attribution(), **kwargs):
                raise ControlError("not_eligible", "nothing is running.")
            return CancelOutcome(mode="current", retry=retry, target=run_id)

        spec = OperationSpec(
            action="run.cancel",
            target_kind="run",
            target_key=run_id or "current",
            principal=principal,
            request={"retry": retry},
            # Honoured at the run's next boundary; the loop finishes the
            # record when the run settles.
            deferred=True,
        )
        return self._record(spec, apply)

    def cancel_provider(self, principal: Principal, target: str) -> CancelOutcome:
        """Cancel a run parked on a provider outage."""
        require(principal, "runs:control")

        def apply(_: str | None) -> CancelOutcome:
            try:
                message = self.loop.cancel_provider(target, principal.attribution())
            except ValueError as exc:
                raise ControlError("not_eligible", str(exc)) from exc
            return CancelOutcome(mode="provider", target=target, message=message)

        return self._record(self._spec("run.cancel_provider", principal, "target", target), apply)

    def cancel_run(
        self,
        principal: Principal,
        run_id: str,
        *,
        retry: bool = False,
        expected_revision: int | None = None,
        idempotency: tuple[str, str] | None = None,
    ) -> CancelOutcome:
        """Cancel one run by identity, whatever state the daemon holds it
        in; ``expected_revision`` refuses a cancel meant for an earlier
        state of the run."""
        require(principal, "runs:control")

        def apply(op_id: str | None) -> CancelOutcome:
            return self.loop.cancel_run(
                run_id,
                by=principal.attribution(),
                retry=retry,
                expected_revision=expected_revision,
                operation_id=op_id,
            )

        spec = OperationSpec(
            action="run.cancel",
            target_kind="run",
            target_key=run_id,
            principal=principal,
            request={"retry": retry},
            idempotency=idempotency,
            expected_revision=expected_revision,
            deferred=True,
        )
        outcome = self._record(spec, apply)
        if outcome.mode != "current" and outcome.operation_id is not None and self.runner:
            # Settled here and now, not at a run boundary: the record is
            # finished at once rather than by the settle step.
            self.runner.store.finish(
                outcome.operation_id,
                self.runner.clock(),
                state="succeeded",
                result=outcome.model_dump(mode="json"),
            )
        return outcome

    def resume_run(
        self,
        principal: Principal,
        run_id: str,
        *,
        expected_revision: int | None = None,
        idempotency: tuple[str, str] | None = None,
    ) -> ResumeOutcome:
        """Admit a persisted run to the daemon's queue for resume."""
        require(principal, "runs:control")

        def apply(_: str | None) -> ResumeOutcome:
            return self.loop.resume_run(
                run_id, by=principal.attribution(), expected_revision=expected_revision
            )

        spec = OperationSpec(
            action="run.resume",
            target_kind="run",
            target_key=run_id,
            principal=principal,
            request={},
            idempotency=idempotency,
            expected_revision=expected_revision,
        )
        return self._record(spec, apply)

    def grant_rounds(
        self,
        principal: Principal,
        run_id: str,
        rounds: int,
        *,
        expected_revision: int | None = None,
        idempotency: tuple[str, str] | None = None,
    ) -> GrantRoundsOutcome:
        require(principal, "budgets:grant")
        if rounds < 1:
            raise ControlError("invalid_argument", f"rounds must be at least 1, not {rounds}")

        def apply(_: str | None) -> GrantRoundsOutcome:
            self._check_run_revision(run_id, expected_revision)
            try:
                item = self.loop.grant_rounds(run_id, rounds, principal.attribution())
            except (KeyError, ValueError) as exc:
                raise ControlError(_code_for(exc), _message(exc)) from exc
            return GrantRoundsOutcome(run_id=run_id, rounds=rounds, item_id=item.item_id)

        spec = self._spec(
            "run.grant_rounds",
            principal,
            "run",
            run_id,
            idempotency=idempotency,
            expected_revision=expected_revision,
            rounds=rounds,
        )
        return self._record(spec, apply)

    def _check_run_revision(self, run_id: str, expected: int | None) -> None:
        """``stale_revision`` when the run row moved past what the caller
        acted on; checked just before the loop's own refusals."""
        if expected is None:
            return
        store = getattr(self.loop, "store", None)
        if store is None:
            return
        try:
            current = int(store.get_run(run_id).revision)
        except Exception as exc:
            raise ControlError("unknown_target", f"unknown run {run_id}") from exc
        if current != expected:
            raise ControlError(
                "stale_revision",
                f"run {run_id} is at revision {current}, not {expected}",
                revision=current,
            )

    # -- gates ----------------------------------------------------------------------

    def approve_gate(
        self,
        principal: Principal,
        target: str,
        *,
        expected_revision: int | None = None,
        idempotency: tuple[str, str] | None = None,
    ) -> GateOutcome:
        """Approve a parked merge, or release a held workload result. With
        ``expected_revision`` the approval binds to that revision of the
        gate (#1038): the loop's swap requires it."""
        require(principal, "gates:approve")

        def apply(_: str | None) -> GateOutcome:
            # The keyword only when there is one: the prose edge's doubles
            # answer the two-argument form.
            bound: dict[str, Any] = (
                {} if expected_revision is None else {"expected_revision": expected_revision}
            )
            try:
                message = self.loop.approve_merge(target, by=principal.attribution(), **bound)
            except (KeyError, ValueError) as exc:
                raise ControlError(_code_for(exc), _message(exc)) from exc
            return GateOutcome(target=target, message=message)

        spec = self._spec(
            "gate.approve",
            principal,
            "target",
            target,
            idempotency=idempotency,
            expected_revision=expected_revision,
        )
        return self._record(spec, apply)

    # -- items ----------------------------------------------------------------------

    def admit(
        self,
        principal: Principal,
        request: AdmitRequest,
        *,
        idempotency: tuple[str, str] | None = None,
    ) -> AdmitOutcome:
        """Admit work through its source's rules (#1036): an existing
        issue, an inline workload, or a registered recipe. Recorded as
        one ``item.admit`` operation against the item it queues, so a
        replay under the same idempotency pair names the same row."""
        require(principal, "items:create")
        key = target_key(request)
        loop: Any = self.loop

        def apply(_: str | None) -> AdmitOutcome:
            if isinstance(request, IssueAdmission):
                item = admit_issue(loop, request)
            else:
                item = build_item(loop.config, request, item_id=key, requested_by=None)
            stored, fresh = upsert(loop, item, by=principal.attribution())
            return AdmitOutcome(item=stored, fresh=fresh)

        spec = self._spec(
            "item.admit",
            principal,
            "item",
            key,
            idempotency=idempotency,
            **_request_fields(request),
        )
        return self._record(spec, apply)

    def _check_item_revision(self, item_id: str, expected: int | None) -> None:
        """``stale_revision`` when the item has moved past what the caller
        acted on. Checked just before the transition rather than inside
        it: the store's item transitions are conditional on state, so a
        row that moved between the check and the write is refused by the
        state it is actually in."""
        if expected is None:
            return
        item = self.loop.dstore.get(item_id)
        if item is None:
            raise ControlError("unknown_target", f"unknown item {item_id}")
        if item.revision != expected:
            raise ControlError(
                "stale_revision",
                f"{item_id} is at revision {item.revision}, not {expected}",
                revision=item.revision,
            )

    def abandon(
        self,
        principal: Principal,
        item_id: str,
        reason: str | None,
        *,
        expected_revision: int | None = None,
        idempotency: tuple[str, str] | None = None,
    ) -> ItemOutcome:
        require(principal, "runs:control")
        item_id = normalize_item_id(item_id)

        def apply(_: str | None) -> ItemOutcome:
            self._check_item_revision(item_id, expected_revision)
            try:
                item = self.loop.abandon_item(item_id, reason)
            except (KeyError, ValueError) as exc:
                raise ControlError(_code_for(exc), _message(exc)) from exc
            return ItemOutcome(verb="abandon", item=item)

        spec = self._spec(
            "item.abandon",
            principal,
            "item",
            item_id,
            idempotency=idempotency,
            expected_revision=expected_revision,
            reason=reason,
        )
        return self._record(spec, apply)

    def retry(
        self,
        principal: Principal,
        item_id: str,
        *,
        expected_revision: int | None = None,
        idempotency: tuple[str, str] | None = None,
    ) -> ItemOutcome:
        require(principal, "runs:control")
        item_id = normalize_item_id(item_id)

        def apply(_: str | None) -> ItemOutcome:
            self._check_item_revision(item_id, expected_revision)
            try:
                item = self.loop.retry_item(item_id, principal.attribution())
            except (KeyError, ValueError) as exc:
                raise ControlError(_code_for(exc), _message(exc)) from exc
            return ItemOutcome(verb="retry", item=item)

        spec = self._spec(
            "item.retry",
            principal,
            "item",
            item_id,
            idempotency=idempotency,
            expected_revision=expected_revision,
        )
        return self._record(spec, apply)

    def requeue(
        self,
        principal: Principal,
        item_id: str,
        *,
        expected_revision: int | None = None,
        idempotency: tuple[str, str] | None = None,
    ) -> ItemOutcome:
        require(principal, "runs:control")
        item_id = normalize_item_id(item_id)

        def apply(_: str | None) -> ItemOutcome:
            self._check_item_revision(item_id, expected_revision)
            try:
                item = self.loop.requeue_item(item_id)
            except (KeyError, ValueError) as exc:
                raise ControlError(_code_for(exc), _message(exc)) from exc
            return ItemOutcome(verb="requeue", item=item)

        spec = self._spec(
            "item.requeue",
            principal,
            "item",
            item_id,
            idempotency=idempotency,
            expected_revision=expected_revision,
        )
        return self._record(spec, apply)

    # -- daemon ---------------------------------------------------------------------

    def resume_repo(
        self, principal: Principal, repo: str, *, idempotency: tuple[str, str] | None = None
    ) -> RepoResumeOutcome:
        require(principal, "daemon:manage")

        def apply(_: str | None) -> RepoResumeOutcome:
            try:
                health = self.loop.resume_repo(repo, principal.attribution())
            except (KeyError, ValueError) as exc:
                raise ControlError(_code_for(exc), _message(exc)) from exc
            return RepoResumeOutcome(repo=str(health.get("repo", repo)), health=dict(health))

        spec = self._spec("repo.resume", principal, "repo", repo, idempotency=idempotency)
        return self._record(spec, apply)

    def add_schedule(
        self,
        principal: Principal,
        spec: ScheduleConfig,
        *,
        source: str,
        idempotency: tuple[str, str] | None = None,
    ) -> ScheduleOutcome:
        require(principal, "daemon:manage")

        def apply(_: str | None) -> ScheduleOutcome:
            try:
                message = self.loop.add_schedule(spec, principal.attribution(), source=source)
            except ValueError as exc:
                raise ControlError("invalid_argument", str(exc)) from exc
            return ScheduleOutcome(verb="add", name=spec.name, message=message)

        record = self._spec(
            "schedule.add",
            principal,
            "schedule",
            spec.name,
            idempotency=idempotency,
            source=source,
            **spec.model_dump(),
        )
        return self._record(record, apply)

    def schedule_control(
        self,
        principal: Principal,
        verb: Literal["pause", "resume", "remove"],
        name: str,
        *,
        idempotency: tuple[str, str] | None = None,
    ) -> ScheduleOutcome:
        """``pause`` / ``resume`` / ``remove`` one schedule."""
        require(principal, "daemon:manage")

        def apply(_: str | None) -> ScheduleOutcome:
            try:
                if verb == "pause":
                    message = self.loop.pause_schedule(name, principal.attribution())
                elif verb == "resume":
                    message = self.loop.resume_schedule(name, principal.attribution())
                else:
                    message = self.loop.remove_schedule(name, principal.attribution())
            except ValueError as exc:
                raise ControlError("unknown_target", _message(exc)) from exc
            return ScheduleOutcome(verb=verb, name=name, message=message)

        spec = self._spec(f"schedule.{verb}", principal, "schedule", name, idempotency=idempotency)
        return self._record(spec, apply)

    def stop(
        self, principal: Principal, *, idempotency: tuple[str, str] | None = None
    ) -> StopOutcome:
        """Graceful stop: the effect runs when the caller fires ``after``."""
        require(principal, "daemon:manage")
        return self._record(
            self._spec("daemon.stop", principal, "daemon", "daemon", idempotency=idempotency),
            lambda _: StopOutcome(after=self.loop.request_stop),
        )

    def restart(
        self,
        principal: Principal,
        *,
        now: bool = False,
        idempotency: tuple[str, str] | None = None,
    ) -> RestartOutcome:
        """Courtesy exit under a supervisor that starts the daemon again;
        refused by name when nothing would."""
        require(principal, "daemon:manage")
        # Lazily: the loop imports nothing from here, and this module must
        # not pull the whole loop in for one sentence.
        from sbxloop.daemon.loop import UNSUPERVISED_REFUSAL

        supervisor = self.loop.supervisor()
        if supervisor is None:
            raise ControlError("unsupervised", UNSUPERVISED_REFUSAL)
        who = principal.attribution() or "operator"
        loop = self.loop

        def after() -> None:
            loop.request_restart(by=who, reason="operator restart", now=now)

        spec = self._spec(
            "daemon.restart", principal, "daemon", "daemon", idempotency=idempotency, now=now
        )
        return self._record(
            spec, lambda _: RestartOutcome(supervisor=supervisor, now=now, after=after)
        )


def _request_fields(request: AdmitRequest) -> dict[str, Any]:
    """The request as the operation records it: its fields, and which form
    it took — enough to fingerprint a replay and to show a reader what was
    asked, never a secret."""
    fields = asdict(request)
    fields.pop("key", None)
    return {"form": type(request).__name__.removesuffix("Admission").lower(), **fields}


def _code_for(exc: BaseException) -> Any:
    """``KeyError`` names a target the daemon does not know; ``ValueError``
    a target that is not in a state the action applies to."""
    return "unknown_target" if isinstance(exc, KeyError) else "not_eligible"
