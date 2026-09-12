"""Durable operations: every mutating control leaves a record before it acts.

A reply that got lost and a command that never ran look the same to a
client; so do a command the daemon claimed and a command it finished. The
record separates them. An :class:`Operation` is accepted (durable
admission), claimed by a daemon generation, and finished with the typed
outcome or the refusal — each transition written with its audit event in
one transaction, and the effect never acknowledged before the row is.

:class:`OperationStore` owns the rows; :class:`OperationRunner` drives one
command through the sequence; :func:`reconcile_operations` runs at recovery
and settles what a dead generation left, from domain evidence, never from
a timeout. The bus is not the transaction coordinator: an event published
to it proves nothing about durability, so nothing here relies on it.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from sbxloop.daemon.controls.principal import Principal
from sbxloop.daemon.controls.results import ControlError, Outcome
from sbxloop.db.api_models import ApiEventRow, OperationRow
from sbxloop.ids import _token
from sbxloop.log import get_logger

if TYPE_CHECKING:
    from sbxloop.daemon.store import DaemonStore

log = get_logger(__name__)

OperationState = Literal["accepted", "running", "reconciling", "succeeded", "failed", "expired"]
TERMINAL_OPERATION_STATES: frozenset[str] = frozenset({"succeeded", "failed", "expired"})

#: The effect each action promises. Success means exactly this, and no
#: more: a cancelled run's source report, a released gate's merge, the
#: process exit after a stop are separate outcomes with their own events.
EFFECTS: dict[str, str] = {
    "daemon.pause": "the hold stands and nothing new is claimed",
    "daemon.release": "the hold is released",
    "run.cancel": "the run reaches the cancelled state",
    "run.cancel_provider": "the parked run is settled as cancelled",
    "run.resume": "the run is admitted to the queue and resumes at the next tick",
    "run.review_resume": "the review wait is re-armed",
    "run.grant_rounds": "the grant is recorded and the item re-admitted",
    "gate.approve": "the approval is recorded and the gate release committed",
    "item.abandon": "the item is settled as abandoned and the source owed its report",
    "item.retry": "the item is re-queued with attempts reset",
    "item.requeue": "the item is unpinned and re-queued",
    "repo.resume": "the repository is polled again from the next tick",
    "schedule.add": "the schedule exists and fires from the next tick",
    "schedule.remove": "the schedule is gone",
    "schedule.pause": "the schedule's ticks are swallowed",
    "schedule.resume": "the schedule fires again",
    "daemon.stop": "the graceful stop is committed and signalled",
    "daemon.restart": "the restart is committed and signalled",
}


class Operation(BaseModel):
    """One accepted command, as the record describes it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    action: str
    target_kind: str
    target_key: str
    state: OperationState
    effect: str
    actor: dict[str, Any]
    request: dict[str, Any]
    accepted_at: float
    expires_at: float | None = None
    claimed_at: float | None = None
    finished_at: float | None = None
    claimed_generation: str | None = None
    expected_revision: int | None = None
    idempotency_scope: str | None = None
    idempotency_key: str | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_detail: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_OPERATION_STATES


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """What a surface asks to have recorded before the effect."""

    action: str
    target_kind: str
    target_key: str
    principal: Principal
    request: dict[str, Any] = field(default_factory=dict)
    #: ``(scope, key)``: a retry with the same pair and the same request
    #: returns the same operation; a different request is a conflict.
    idempotency: tuple[str, str] | None = None
    expected_revision: int | None = None
    #: Seconds after acceptance an unclaimed command expires rather than
    #: applying stale intent; ``None`` for a command claimed at once.
    ttl_s: float | None = None
    #: The effect completes after the call returns (a cancel honoured at
    #: the run's next boundary): the runner leaves the row ``running`` and
    #: the daemon finishes it when the effect is observed.
    deferred: bool = False


class IdempotencyConflict(Exception):
    """Same idempotency key, different request."""

    def __init__(self, existing: Operation) -> None:
        super().__init__(f"idempotency key already used by {existing.id} with a different request")
        self.existing = existing


class OperationReplay(Exception):
    """Same idempotency key, same request: the caller gets the existing
    operation instead of a second effect."""

    def __init__(self, existing: Operation) -> None:
        super().__init__(f"replay of {existing.id}")
        self.existing = existing


def new_operation_id() -> str:
    return "op_" + _token(16)


def fingerprint(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _row(row: OperationRow) -> Operation:
    return Operation(
        id=str(row.id),
        action=str(row.action),
        target_kind=str(row.target_kind),
        target_key=str(row.target_key),
        state=row.state,  # type: ignore[arg-type]
        effect=str(row.effect),
        actor=json.loads(row.actor_json),
        request=json.loads(row.request_json or "{}"),
        accepted_at=float(row.accepted_at),
        expires_at=None if row.expires_at is None else float(row.expires_at),
        claimed_at=None if row.claimed_at is None else float(row.claimed_at),
        finished_at=None if row.finished_at is None else float(row.finished_at),
        claimed_generation=None if row.claimed_generation is None else str(row.claimed_generation),
        expected_revision=None if row.expected_revision is None else int(row.expected_revision),
        idempotency_scope=None if row.idempotency_scope is None else str(row.idempotency_scope),
        idempotency_key=None if row.idempotency_key is None else str(row.idempotency_key),
        result=None if row.result_json is None else json.loads(row.result_json),
        error_code=None if row.error_code is None else str(row.error_code),
        error_detail=None if row.error_detail is None else str(row.error_detail),
    )


def _target_refs(kind: str, key: str) -> tuple[str | None, str | None]:
    """The run and item an event row indexes by, from the target."""
    return (key if kind == "run" else None), (key if kind == "item" else None)


class OperationStore:
    """The ``api_operations`` and ``api_events`` rows, written together.

    Every method that changes an operation writes its audit event in the
    same transaction, under the daemon store's lock, so a reader never sees
    a transition without its event or an event without its transition.
    """

    def __init__(self, dstore: DaemonStore) -> None:
        self.dstore = dstore

    # -- writes --------------------------------------------------------------------

    def accept(self, spec: OperationSpec, now: float) -> tuple[Operation, bool]:
        """Durably admit a command. Returns the operation and whether it
        was created now (``False``: an idempotent replay of an existing
        one). Raises :class:`IdempotencyConflict` for a reused key with a
        different request."""
        scope, key = spec.idempotency or (None, None)
        digest = fingerprint(spec.request) if spec.idempotency else None
        with self.dstore.transaction() as session:
            if scope is not None:
                existing = session.scalars(
                    select(OperationRow).where(
                        OperationRow.idempotency_scope == scope,
                        OperationRow.idempotency_key == key,
                    )
                ).first()
                if existing is not None:
                    if existing.fingerprint != digest:
                        raise IdempotencyConflict(_row(existing))
                    return _row(existing), False
            op_id = new_operation_id()
            session.execute(
                insert(OperationRow).values(
                    id=op_id,
                    action=spec.action,
                    target_kind=spec.target_kind,
                    target_key=spec.target_key,
                    state="accepted",
                    effect=EFFECTS.get(spec.action, ""),
                    actor_json=json.dumps(spec.principal.audit(), default=str),
                    idempotency_scope=scope,
                    idempotency_key=key,
                    fingerprint=digest,
                    expected_revision=spec.expected_revision,
                    request_json=json.dumps(spec.request, sort_keys=True, default=str),
                    accepted_at=now,
                    expires_at=None if spec.ttl_s is None else now + spec.ttl_s,
                )
            )
            self._event(
                session,
                "operation.accepted",
                now,
                op_id,
                spec.target_kind,
                spec.target_key,
                spec.principal,
                {"action": spec.action, "state": "accepted"},
            )
            row = session.get(OperationRow, op_id)
            assert row is not None  # nosec B101 - just inserted under the lock
            return _row(row), True

    def claim(self, op_id: str, generation: str | None, now: float) -> None:
        with self.dstore.transaction() as session:
            session.execute(
                update(OperationRow)
                .where(OperationRow.id == op_id, OperationRow.state == "accepted")
                .values(state="running", claimed_at=now, claimed_generation=generation)
            )

    def finish(
        self,
        op_id: str,
        now: float,
        *,
        state: Literal["succeeded", "failed", "expired", "reconciling"],
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> Operation | None:
        """Record the terminal (or reconciling) state. A no-op on a row
        that is already terminal: the first verdict stands."""
        with self.dstore.transaction() as session:
            row = session.get(OperationRow, op_id)
            if row is None or row.state in TERMINAL_OPERATION_STATES:
                return None if row is None else _row(row)
            row.state = state
            row.finished_at = now if state in TERMINAL_OPERATION_STATES else None
            row.result_json = None if result is None else json.dumps(result, default=str)
            row.error_code = error_code
            row.error_detail = error_detail
            self._event(
                session,
                "operation.finished"
                if state in TERMINAL_OPERATION_STATES
                else "operation.reconciling",
                now,
                op_id,
                str(row.target_kind),
                str(row.target_key),
                None,
                {
                    "action": str(row.action),
                    "state": state,
                    "error_code": error_code,
                    "error_detail": error_detail,
                },
            )
            session.flush()
            return _row(row)

    # -- reads ---------------------------------------------------------------------

    def get(self, op_id: str) -> Operation | None:
        with self.dstore.read() as session:
            row = session.get(OperationRow, op_id)
            return None if row is None else _row(row)

    def recent(
        self,
        *,
        states: Sequence[str] | None = None,
        target: tuple[str, str] | None = None,
        limit: int = 100,
    ) -> list[Operation]:
        """Newest first, bounded."""
        stmt = select(OperationRow).order_by(OperationRow.accepted_at.desc()).limit(limit)
        if states:
            stmt = stmt.where(OperationRow.state.in_(list(states)))
        if target is not None:
            stmt = stmt.where(
                OperationRow.target_kind == target[0], OperationRow.target_key == target[1]
            )
        with self.dstore.read() as session:
            return [_row(row) for row in session.scalars(stmt)]

    def pending(self) -> list[Operation]:
        """Every operation not yet settled, oldest first."""
        stmt = (
            select(OperationRow)
            .where(OperationRow.state.in_(["accepted", "running", "reconciling"]))
            .order_by(OperationRow.accepted_at.asc())
        )
        with self.dstore.read() as session:
            return [_row(row) for row in session.scalars(stmt)]

    def events(self, *, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """The public chronology after a cursor, oldest first."""
        stmt = (
            select(ApiEventRow)
            .where(ApiEventRow.seq > after_seq)
            .order_by(ApiEventRow.seq.asc())
            .limit(limit)
        )
        with self.dstore.read() as session:
            return [
                {
                    "seq": int(row.seq),
                    "recorded_at": float(row.recorded_at),
                    "occurred_at": float(row.occurred_at),
                    "type": str(row.type),
                    "run_id": row.run_id,
                    "item_id": row.item_id,
                    "operation_id": row.operation_id,
                    "actor": None if row.actor_json is None else json.loads(row.actor_json),
                    "source_seq": row.source_seq,
                    "data": {} if row.data_json is None else json.loads(row.data_json),
                    "schema_version": int(row.schema_version),
                }
                for row in session.scalars(stmt)
            ]

    @staticmethod
    def _event(
        session: Session,
        type_: str,
        now: float,
        op_id: str,
        target_kind: str,
        target_key: str,
        principal: Principal | None,
        data: dict[str, Any],
    ) -> None:
        run_id, item_id = _target_refs(target_kind, target_key)
        session.execute(
            insert(ApiEventRow).values(
                recorded_at=now,
                occurred_at=now,
                type=type_,
                run_id=run_id,
                item_id=item_id,
                operation_id=op_id,
                actor_json=None if principal is None else json.dumps(principal.audit()),
                source_seq=None,
                data_json=json.dumps(data, default=str),
            )
        )


class OperationRunner:
    """Drive one command: accept, claim, apply, finish.

    The four ``after_*`` seams are no-ops a fault-injection test replaces
    to crash the process between steps; each boundary has a defined
    outcome under :func:`reconcile_operations`.
    """

    def __init__(
        self,
        store: OperationStore,
        *,
        generation: Callable[[], str | None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.generation = generation
        self.clock = clock
        self.after_accept: Callable[[Operation], None] = lambda op: None
        self.after_claim: Callable[[Operation], None] = lambda op: None
        self.after_effect: Callable[[Operation], None] = lambda op: None
        self.after_commit: Callable[[Operation], None] = lambda op: None

    def run_sync(self, spec: OperationSpec, fn: Callable[[str], Outcome]) -> Outcome:
        """Record ``spec``, then apply ``fn`` (handed the operation id, for
        an effect that completes later) in the caller's thread.

        Raises :class:`OperationReplay` when the idempotency pair names an
        operation that already exists with the same request, and
        :class:`IdempotencyConflict` when the request differs. A
        :class:`ControlError` from ``fn`` finishes the operation ``failed``
        with its code and is re-raised; any other exception finishes it
        ``failed`` with ``code="crashed"`` and is re-raised too.
        """
        op, created = self.store.accept(spec, self.clock())
        if not created:
            raise OperationReplay(op)
        self.after_accept(op)
        self.store.claim(op.id, self.generation(), self.clock())
        self.after_claim(op)
        try:
            outcome = fn(op.id)
        except ControlError as exc:
            self.store.finish(
                op.id, self.clock(), state="failed", error_code=exc.code, error_detail=exc.message
            )
            # The refusal names its record too: the surface answers with
            # the sentence and the id a client can look up.
            exc.detail.setdefault("operation_id", op.id)
            raise
        except Exception as exc:
            self.store.finish(
                op.id,
                self.clock(),
                state="failed",
                error_code="crashed",
                error_detail=f"{type(exc).__name__}: {exc}"[:2000],
            )
            raise
        self.after_effect(op)
        after = getattr(outcome, "after", None)
        if callable(after):
            # The effect runs once the reply is on its way (a stop must not
            # tear a chat bridge down under its own answer): the row stays
            # running until then, and finishes when the effect has run.
            def finish_after() -> None:
                after()
                self.store.finish(
                    op.id, self.clock(), state="succeeded", result=outcome.model_dump(mode="json")
                )

            return outcome.model_copy(update={"operation_id": op.id, "after": finish_after})
        if spec.deferred:
            return outcome.model_copy(update={"operation_id": op.id})
        self.store.finish(
            op.id, self.clock(), state="succeeded", result=outcome.model_dump(mode="json")
        )
        self.after_commit(op)
        return outcome.model_copy(update={"operation_id": op.id})


def reconcile_operations(loop: Any, *, generation: str, now: float) -> list[Operation]:
    """Settle what a previous generation left unfinished, from evidence.

    An ``accepted`` row was never claimed: the process died before it
    could act, or the command sat past its deadline — ``expired`` either
    way, so stale intent is never applied at boot. A ``running`` row
    claimed by another generation is judged per action from what the
    domain shows: a run's state, a gate's state, an item's state. Where
    the evidence does not decide, the row is ``reconciling`` with the
    reason, for an operator; it is never guessed ``succeeded``. Returns
    the operations touched.
    """
    store: OperationStore = loop.operations
    touched: list[Operation] = []
    for op in store.pending():
        if op.state == "reconciling":
            continue
        if op.state == "accepted":
            why = (
                "past its deadline before it was claimed"
                if op.expires_at is not None and now > op.expires_at
                else "the daemon restarted before the command was claimed"
            )
            done = store.finish(op.id, now, state="expired", error_detail=why)
        elif op.claimed_generation == generation:
            continue
        else:
            state, code, detail = _judge(loop, op)
            done = store.finish(op.id, now, state=state, error_code=code, error_detail=detail)
        if done is not None:
            touched.append(done)
            log.info(
                "operation.reconciled",
                operation=op.id,
                action=op.action,
                state=done.state,
                detail=done.error_detail,
            )
    return touched


def _judge(
    loop: Any, op: Operation
) -> tuple[Literal["succeeded", "failed", "reconciling"], str | None, str | None]:
    """The verdict on a claimed operation from what the domain shows."""
    from sbxloop.engine.model import TERMINAL_RUN_STATES
    from sbxloop.errors import SbxloopError

    if op.action in ("run.cancel", "run.cancel_provider"):
        try:
            record = loop.store.get_run(op.target_key)
        except SbxloopError:
            return "failed", "unknown_target", "no such run"
        if record.state == "cancelled":
            return "succeeded", None, None
        if record.state in TERMINAL_RUN_STATES:
            return "failed", "target_already_terminal", f"run is {record.state}"
        return "failed", "interrupted_before_effect", f"run is {record.state}; cancel was lost"
    if op.action == "gate.approve":
        gate = loop.dstore.merge_gate_for(op.target_key)
        if gate is None:
            return "failed", "unknown_target", "no gate for the target"
        if gate.state in ("merged", "released"):
            return "succeeded", None, None
        if gate.state == "approving":
            return "reconciling", None, "the gate is still being completed"
        return "failed", "not_eligible", f"gate is {gate.state}"
    if op.action == "run.resume":
        item_id = loop.dstore.item_for_run(op.target_key)
        item = loop.dstore.get(item_id) if item_id else None
        if (
            item is not None
            and item.run_id == op.target_key
            and item.state in ("queued", "running", "done")
        ):
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", "the run was not admitted"
    if op.action in ("item.abandon", "item.retry", "item.requeue"):
        item = loop.dstore.get(op.target_key)
        if item is None:
            return "failed", "unknown_target", "no such item"
        expected = {"item.abandon": "failed", "item.retry": "queued", "item.requeue": "queued"}
        if item.state == expected[op.action]:
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", f"item is {item.state}"
    if op.action in ("daemon.stop", "daemon.restart"):
        # The process exited and a new generation is answering: that is
        # exactly the effect these promise.
        return "succeeded", None, None
    if op.action in ("daemon.pause", "daemon.release"):
        holds = set(loop.holds)
        held = op.target_key in holds
        if (op.action == "daemon.pause") == held:
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", "the hold did not survive the restart"
    return "reconciling", None, "the effect could not be established from the record"
