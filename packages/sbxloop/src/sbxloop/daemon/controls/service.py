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
from collections.abc import Callable
from typing import Any, Literal, TypeVar

from sbxloop.config import ScheduleConfig
from sbxloop.daemon.controls.operations import OperationRunner, OperationSpec, OperationStore
from sbxloop.daemon.controls.principal import Capability, Principal
from sbxloop.daemon.controls.protocol import ControlLoop
from sbxloop.daemon.controls.results import (
    CancelOutcome,
    ControlError,
    GateOutcome,
    GrantRoundsOutcome,
    ItemOutcome,
    ItemsOutcome,
    LogTailOutcome,
    Outcome,
    PauseOutcome,
    QueueOutcome,
    ReleaseOutcome,
    RepoResumeOutcome,
    RestartOutcome,
    ReviewResumeOutcome,
    ScheduleListOutcome,
    ScheduleOutcome,
    StatusOutcome,
    StopOutcome,
)
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
        **request: Any,
    ) -> OperationSpec:
        return OperationSpec(
            action=action,
            target_kind=target_kind,
            target_key=target_key,
            principal=principal,
            request=request,
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

    # -- holds ----------------------------------------------------------------------

    def pause(self, principal: Principal, hold: str | None = None) -> PauseOutcome:
        require(principal, "daemon:manage")
        name = _hold(hold)

        def apply(_: str | None) -> PauseOutcome:
            try:
                holds = self.loop.pause(name, by=principal.attribution())
            except ValueError as exc:
                raise ControlError("invalid_argument", str(exc)) from exc
            return PauseOutcome(hold=name, holds=list(holds))

        return self._record(self._spec("daemon.pause", principal, "hold", name), apply)

    def release(
        self, principal: Principal, hold: str | None = None, *, everything: bool = False
    ) -> ReleaseOutcome:
        """Release one hold (the operator's when unnamed), or every hold."""
        require(principal, "daemon:manage")
        name = None if everything else _hold(hold)

        def apply(_: str | None) -> ReleaseOutcome:
            try:
                holds = self.loop.unpause(name, by=principal.attribution())
            except ValueError as exc:
                raise ControlError("invalid_argument", str(exc)) from exc
            return ReleaseOutcome(hold=name, holds=list(holds))

        spec = self._spec("daemon.release", principal, "hold", name or "*", everything=everything)
        return self._record(spec, apply)

    # -- runs -----------------------------------------------------------------------

    def resume_review(self, principal: Principal, target: str) -> ReviewResumeOutcome:
        require(principal, "runs:control")

        def apply(_: str | None) -> ReviewResumeOutcome:
            try:
                message = self.loop.resume_review(target, principal.attribution())
            except ValueError as exc:
                raise ControlError("not_eligible", _message(exc)) from exc
            return ReviewResumeOutcome(target=target, message=message)

        return self._record(self._spec("run.review_resume", principal, "target", target), apply)

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

    def grant_rounds(self, principal: Principal, run_id: str, rounds: int) -> GrantRoundsOutcome:
        require(principal, "budgets:grant")
        if rounds < 1:
            raise ControlError("invalid_argument", f"rounds must be at least 1, not {rounds}")

        def apply(_: str | None) -> GrantRoundsOutcome:
            try:
                item = self.loop.grant_rounds(run_id, rounds, principal.attribution())
            except (KeyError, ValueError) as exc:
                raise ControlError(_code_for(exc), _message(exc)) from exc
            return GrantRoundsOutcome(run_id=run_id, rounds=rounds, item_id=item.item_id)

        spec = self._spec("run.grant_rounds", principal, "run", run_id, rounds=rounds)
        return self._record(spec, apply)

    # -- gates ----------------------------------------------------------------------

    def approve_gate(self, principal: Principal, target: str) -> GateOutcome:
        """Approve a parked merge, or release a held workload result."""
        require(principal, "gates:approve")

        def apply(_: str | None) -> GateOutcome:
            try:
                message = self.loop.approve_merge(target, by=principal.attribution())
            except (KeyError, ValueError) as exc:
                raise ControlError(_code_for(exc), _message(exc)) from exc
            return GateOutcome(target=target, message=message)

        return self._record(self._spec("gate.approve", principal, "target", target), apply)

    # -- items ----------------------------------------------------------------------

    def abandon(self, principal: Principal, item_id: str, reason: str | None) -> ItemOutcome:
        require(principal, "runs:control")
        item_id = normalize_item_id(item_id)

        def apply(_: str | None) -> ItemOutcome:
            try:
                item = self.loop.abandon_item(item_id, reason)
            except (KeyError, ValueError) as exc:
                raise ControlError(_code_for(exc), _message(exc)) from exc
            return ItemOutcome(verb="abandon", item=item)

        spec = self._spec("item.abandon", principal, "item", item_id, reason=reason)
        return self._record(spec, apply)

    def retry(self, principal: Principal, item_id: str) -> ItemOutcome:
        require(principal, "runs:control")
        item_id = normalize_item_id(item_id)

        def apply(_: str | None) -> ItemOutcome:
            try:
                item = self.loop.retry_item(item_id, principal.attribution())
            except (KeyError, ValueError) as exc:
                raise ControlError(_code_for(exc), _message(exc)) from exc
            return ItemOutcome(verb="retry", item=item)

        return self._record(self._spec("item.retry", principal, "item", item_id), apply)

    def requeue(self, principal: Principal, item_id: str) -> ItemOutcome:
        require(principal, "runs:control")
        item_id = normalize_item_id(item_id)

        def apply(_: str | None) -> ItemOutcome:
            try:
                item = self.loop.requeue_item(item_id)
            except (KeyError, ValueError) as exc:
                raise ControlError(_code_for(exc), _message(exc)) from exc
            return ItemOutcome(verb="requeue", item=item)

        return self._record(self._spec("item.requeue", principal, "item", item_id), apply)

    # -- daemon ---------------------------------------------------------------------

    def resume_repo(self, principal: Principal, repo: str) -> RepoResumeOutcome:
        require(principal, "daemon:manage")

        def apply(_: str | None) -> RepoResumeOutcome:
            try:
                health = self.loop.resume_repo(repo, principal.attribution())
            except (KeyError, ValueError) as exc:
                raise ControlError(_code_for(exc), _message(exc)) from exc
            return RepoResumeOutcome(repo=str(health.get("repo", repo)), health=dict(health))

        return self._record(self._spec("repo.resume", principal, "repo", repo), apply)

    def add_schedule(
        self, principal: Principal, spec: ScheduleConfig, *, source: str
    ) -> ScheduleOutcome:
        require(principal, "daemon:manage")

        def apply(_: str | None) -> ScheduleOutcome:
            try:
                message = self.loop.add_schedule(spec, principal.attribution(), source=source)
            except ValueError as exc:
                raise ControlError("invalid_argument", str(exc)) from exc
            return ScheduleOutcome(verb="add", name=spec.name, message=message)

        record = self._spec(
            "schedule.add", principal, "schedule", spec.name, source=source, **spec.model_dump()
        )
        return self._record(record, apply)

    def schedule_control(
        self, principal: Principal, verb: Literal["pause", "resume", "remove"], name: str
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

        return self._record(self._spec(f"schedule.{verb}", principal, "schedule", name), apply)

    def stop(self, principal: Principal) -> StopOutcome:
        """Graceful stop: the effect runs when the caller fires ``after``."""
        require(principal, "daemon:manage")
        return self._record(
            self._spec("daemon.stop", principal, "daemon", "daemon"),
            lambda _: StopOutcome(after=self.loop.request_stop),
        )

    def restart(self, principal: Principal, *, now: bool = False) -> RestartOutcome:
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

        return self._record(
            self._spec("daemon.restart", principal, "daemon", "daemon", now=now),
            lambda _: RestartOutcome(supervisor=supervisor, now=now, after=after),
        )


def _code_for(exc: BaseException) -> Any:
    """``KeyError`` names a target the daemon does not know; ``ValueError``
    a target that is not in a state the action applies to."""
    return "unknown_target" if isinstance(exc, KeyError) else "not_eligible"
