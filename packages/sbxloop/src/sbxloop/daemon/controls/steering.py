"""Steering instructions as records (#1038).

A chat message in a run's thread reaches the agent and is answered in the
thread; a remote client has no thread. So an instruction it submits is a
row first — who, for which run at which revision, the text, what it
cites, a deadline — and its fate is written on the same row: ``delivered``
when the engine took it (with the engine's message id), ``handled`` when
the agent's ``chat.reply`` lands (the chronology projection settles it in
the same transaction as the event), ``failed`` when the daemon refused it,
``undelivered`` when the run ended without answering. Conflicting
directions are not arbitrated here: every instruction is handed to the
agent in the order it arrived and answered in turn, so the later one is
heard last, and the record shows what each one did.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select, text, update

from sbxloop.daemon.controls.principal import Principal
from sbxloop.daemon.store import DaemonStore
from sbxloop.db.api_models import SteeringRow
from sbxloop.ids import _token

SteeringStatus = Literal["accepted", "delivered", "handled", "failed", "undelivered"]
#: Statuses a row cannot leave.
SETTLED: frozenset[str] = frozenset({"handled", "failed", "undelivered"})


@dataclass(frozen=True, slots=True)
class Steering:
    id: str
    run_id: str
    message_id: str | None
    principal: dict[str, Any]
    text: str
    source_refs: list[str]
    expected_revision: int | None
    submitted_at: float
    deadline_at: float | None
    status: str
    delivered_at: float | None = None
    handled_at: float | None = None
    reply: str | None = None
    action: str | None = None
    error: str | None = None
    operation_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def new_steering_id() -> str:
    return "str_" + _token(16)


def _row(row: SteeringRow) -> Steering:
    return Steering(
        id=str(row.id),
        run_id=str(row.run_id),
        message_id=row.message_id,
        principal=json.loads(row.principal_json),
        text=str(row.text),
        source_refs=list(json.loads(row.source_refs_json or "[]")),
        expected_revision=row.expected_revision,
        submitted_at=float(row.submitted_at),
        deadline_at=row.deadline_at,
        status=str(row.status),
        delivered_at=row.delivered_at,
        handled_at=row.handled_at,
        reply=row.reply,
        action=row.action,
        error=row.error,
        operation_id=row.operation_id,
    )


class SteeringStore:
    def __init__(self, dstore: DaemonStore) -> None:
        self.dstore = dstore

    def create(
        self,
        *,
        run_id: str,
        text: str,
        principal: Principal,
        source_refs: Sequence[str],
        expected_revision: int | None,
        now: float,
        deadline_at: float | None,
        operation_id: str | None,
    ) -> Steering:
        steering_id = new_steering_id()
        with self.dstore.transaction() as session:
            session.add(
                SteeringRow(
                    id=steering_id,
                    run_id=run_id,
                    message_id=None,
                    principal_json=json.dumps(principal.audit(), default=str),
                    text=text,
                    source_refs_json=json.dumps(list(source_refs)),
                    expected_revision=expected_revision,
                    submitted_at=now,
                    deadline_at=deadline_at,
                    status="accepted",
                    operation_id=operation_id,
                )
            )
        found = self.get(steering_id)
        assert found is not None  # nosec B101 - just inserted
        return found

    def delivered(self, steering_id: str, message_id: str, now: float) -> None:
        with self.dstore.transaction() as session:
            session.execute(
                update(SteeringRow)
                .where(SteeringRow.id == steering_id)
                .values(status="delivered", message_id=message_id, delivered_at=now)
            )

    def failed(self, steering_id: str, error: str, now: float) -> None:
        with self.dstore.transaction() as session:
            session.execute(
                update(SteeringRow)
                .where(SteeringRow.id == steering_id)
                .values(status="failed", error=error[:2000], handled_at=now)
            )

    @staticmethod
    def settle_reply(
        session: Any,
        *,
        message_id: str,
        now: float,
        reply: str | None,
        action: str | None,
        error: str | None,
    ) -> None:
        """The agent's answer to a delivered instruction, written inside
        the caller's transaction (the chronology's, when it projects the
        ``chat.reply`` that carries it)."""
        session.execute(
            update(SteeringRow)
            .where(SteeringRow.message_id == message_id, SteeringRow.status == "delivered")
            .values(
                status="failed" if error else "handled",
                handled_at=now,
                reply=reply,
                action=action,
                error=error,
            )
        )

    def undelivered_for_run(self, run_id: str, now: float) -> int:
        """The run ended: whatever it never answered is ``undelivered``."""
        with self.dstore.transaction() as session:
            result = session.execute(
                update(SteeringRow)
                .where(
                    SteeringRow.run_id == run_id,
                    SteeringRow.status.in_(("accepted", "delivered")),
                )
                .values(status="undelivered", handled_at=now)
            )
            return int(getattr(result, "rowcount", 0) or 0)

    def settle_orphans(self, current_run_id: str | None, now: float) -> int:
        """Rows still waiting on a run that is not in flight (a restart):
        nothing will answer them."""
        with self.dstore.transaction() as session:
            stmt = (
                update(SteeringRow)
                .where(SteeringRow.status.in_(("accepted", "delivered")))
                .values(status="undelivered", handled_at=now)
            )
            if current_run_id is not None:
                stmt = stmt.where(SteeringRow.run_id != current_run_id)
            result = session.execute(stmt)
            return int(getattr(result, "rowcount", 0) or 0)

    def get(self, steering_id: str) -> Steering | None:
        with self.dstore.read() as session:
            row = session.get(SteeringRow, steering_id)
            return None if row is None else _row(row)

    def for_operation(self, operation_id: str) -> Steering | None:
        with self.dstore.read() as session:
            row = session.scalars(
                select(SteeringRow).where(SteeringRow.operation_id == operation_id)
            ).first()
            return None if row is None else _row(row)

    def for_run(self, run_id: str) -> list[Steering]:
        """The run's instructions in the order they were submitted (the
        row order breaks a tie on the clock)."""
        with self.dstore.read() as session:
            rows = session.scalars(
                select(SteeringRow)
                .where(SteeringRow.run_id == run_id)
                .order_by(SteeringRow.submitted_at.asc(), text("rowid ASC"))
            )
            return [_row(row) for row in rows]
