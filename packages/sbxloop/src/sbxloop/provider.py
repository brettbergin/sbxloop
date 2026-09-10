"""Durable inference recovery, independent of task repair and run kind.

Backoff is scheduled, never slept inside an agent call: a parked run keeps
its checkpoint and sandboxes, and cancellation/status need no provider.
The inference credential is currently one fixed source per backend per
home; repository GitHub tokens and model choices do not change its scope.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from sbxloop.db import begin_immediate
from sbxloop.db.engine_models import ProviderHoldRow, ProviderJobRow
from sbxloop.errors import SbxloopError
from sbxloop_worker.protocol import JobRequest, JobResult, ProviderFailure, Usage

if TYPE_CHECKING:
    from sbxloop.engine.store import StateStore
    from sbxloop.events import EventBus

MAX_THROTTLE_RETRIES = 3
BACKOFF_BASE_S = 30.0
BACKOFF_MAX_S = 300.0


@dataclass(frozen=True)
class ProviderHold:
    failure: ProviderFailure
    next_at: float | None
    attempts: int
    generation: int = 0

    def blocked(self, now: float) -> bool:
        return self.next_at is None or now < self.next_at

    def summary(self) -> str:
        recovery = (
            f"next eligible {datetime.fromtimestamp(self.next_at, UTC).isoformat()}"
            if self.next_at is not None
            else (
                f"provider reset {datetime.fromtimestamp(self.failure.reset_at, UTC).isoformat()}; "
                "explicit operator recovery required"
                if self.failure.reset_at is not None
                else "reset unknown; explicit operator recovery required"
            )
        )
        return f"{self.failure.backend}: {self.failure.reason}; {recovery}"


class ProviderHeldError(SbxloopError):
    """Control flow, deliberately outside WorkerError and task repair."""

    def __init__(self, hold: ProviderHold) -> None:
        super().__init__(hold.summary())
        self.hold = hold


class ProviderRecovery:
    def __init__(
        self,
        store: StateStore,
        backend: str,
        *,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.store, self.backend, self.clock, self.jitter = store, backend, clock, jitter
        self.scope = f"{backend}:default"

    @contextmanager
    def _write(self) -> Iterator[Session]:
        # Take SQLite's write lock before reading counters: the concierge
        # and an engine may be separate connections or processes.
        with (
            self.store._lock,
            begin_immediate(self.store._engine) as connection,
            Session(connection) as session,
        ):
            yield session
            session.flush()

    def hold(self) -> ProviderHold | None:
        with self.store._read() as session:
            row = session.get(ProviderHoldRow, self.scope)
            if row is None or not row.active:
                return None
            return ProviderHold(
                ProviderFailure.model_validate_json(row.failure_json),
                row.next_at,
                row.attempts,
                row.generation,
            )

    def check(self) -> None:
        hold = self.hold()
        if hold is not None and hold.blocked(self.clock()):
            raise ProviderHeldError(hold)

    def release(self, *, expected_generation: int | None = None) -> None:
        """An explicit operator recovery, with no model/credential change."""
        with self._write() as session:
            statement = update(ProviderHoldRow).where(ProviderHoldRow.scope == self.scope)
            if expected_generation is not None:
                statement = statement.where(ProviderHoldRow.generation == expected_generation)
            session.execute(
                statement.values(
                    active=0,
                    attempts=0,
                    updated_at=self.clock(),
                    generation=ProviderHoldRow.generation + 1,
                ),
            )

    def pending(self, run_id: str) -> bool:
        with self.store._read() as session:
            return (
                session.scalar(
                    select(ProviderJobRow.run_id)
                    .where(
                        ProviderJobRow.run_id == run_id,
                        ProviderJobRow.pending == 1,
                        ProviderJobRow.scope == self.scope,
                    )
                    .limit(1)
                )
                is not None
            )

    @staticmethod
    def job_key(job: JobRequest) -> str:
        # Job IDs and SDK session IDs change on restart. The request's
        # semantics identify the interrupted call, including its task.
        content = [
            job.recovery_key if job.recovery_key is not None else job.prompt,
            job.system_message,
            job.model,
            job.expect,
            job.permission_mode,
        ]
        return hashlib.sha256(json.dumps(content).encode()).hexdigest()

    def checkpoint(self, run_id: str, key: str) -> JobResult | None:
        with self.store._read() as session:
            row = session.get(ProviderJobRow, (run_id, key))
            return JobResult.model_validate_json(row.result_json) if row and row.pending else None

    def pin_model(self, job: JobRequest) -> JobRequest:
        """Model edits take effect after recovery, never inside an interrupted call.

        Only the model may differ: the existing semantic fingerprint must
        match with the recorded selection substituted. Unknown legacy
        selections still fail closed through the normal checkpoint check.
        """
        with self.store._read() as session:
            rows = session.scalars(
                select(ProviderJobRow).where(
                    ProviderJobRow.run_id == job.run_id,
                    ProviderJobRow.scope == self.scope,
                    ProviderJobRow.pending == 1,
                )
            ).all()
            for row in rows:
                if row.requested_model is None:
                    continue
                candidate = job.model_copy(update={"model": row.requested_model})
                if self.job_key(candidate) == row.job_key:
                    return candidate
        return job

    def park_recovery(self, reason: str) -> ProviderHeldError:
        failure = ProviderFailure(backend=self.backend, category="recovery", reason=reason)
        with self._write() as session:
            row = session.get(ProviderHoldRow, self.scope)
            generation = row.generation + 1 if row else 1
            session.merge(
                ProviderHoldRow(
                    scope=self.scope,
                    failure_json=failure.model_dump_json(),
                    attempts=0,
                    generation=generation,
                    next_at=None,
                    active=1,
                    updated_at=self.clock(),
                )
            )
        return ProviderHeldError(ProviderHold(failure, None, 0, generation))

    def record(self, job: JobRequest, result: JobResult) -> ProviderHold:
        assert result.error is not None and result.error.provider is not None
        failure = result.error.provider
        now = self.clock()
        with self._write() as session:
            row = session.get(ProviderHoldRow, self.scope)
            attempts = (row.attempts if row is not None and row.active else 0) + 1
            generation = row.generation + 1 if row else 1
            next_at = None
            if failure.category in ("throttle", "unavailable") and attempts <= MAX_THROTTLE_RETRIES:
                delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (attempts - 1))
                next_at = max(
                    now + delay + self.jitter() * delay / 4,
                    failure.retry_at or 0,
                    failure.reset_at or 0,
                )
            elif (
                failure.category == "quota"
                and failure.reset_at is not None
                and failure.reset_at > now
            ):
                next_at = failure.reset_at
            # An already standing hard hold cannot be shortened by another
            # in-flight request racing with it.
            if row is not None and row.active and (row.next_at is None or row.next_at > now):
                next_at = (
                    None if row.next_at is None or next_at is None else max(row.next_at, next_at)
                )
            session.merge(
                ProviderHoldRow(
                    scope=self.scope,
                    failure_json=failure.model_dump_json(),
                    attempts=attempts,
                    generation=generation,
                    next_at=next_at,
                    active=1,
                    updated_at=now,
                )
            )
            session.merge(
                ProviderJobRow(
                    run_id=job.run_id,
                    job_key=self.job_key(job),
                    scope=self.scope,
                    result_json=result.model_dump_json(),
                    pending=1,
                    requested_model=job.model,
                )
            )
        return ProviderHold(failure, next_at, attempts, generation)

    def submit(
        self,
        job: JobRequest,
        submit: Callable[[JobRequest], JobResult],
        bus: EventBus,
    ) -> JobResult:
        prior_hold = self.hold()
        self.check()
        job = self.pin_model(job)
        key = self.job_key(job)
        previous = self.checkpoint(job.run_id, key)
        if previous is None and self.pending(job.run_id):
            raise self.park_recovery(
                "The interrupted request changed; inspect the checkpoint before continuing"
            )
        request = job
        if previous is not None:
            failure = previous.error.provider if previous.error else None
            if failure and failure.partial_progress and not previous.session_id:
                raise self.park_recovery(
                    "Partial work has no resumable session; inspect the preserved checkpoint"
                )
            if previous.session_id:
                request = job.model_copy(
                    update={
                        "resume_session_id": previous.session_id,
                        "require_resume": bool(failure and failure.partial_progress),
                        "prompt": "Continue the interrupted request from its last completed step. "
                        "Retain completed work and do not repeat tool actions already performed. "
                        "Return the output in the format originally requested.",
                    }
                )
        result = submit(request)
        if previous is not None:
            result = result.model_copy(
                update={
                    "usage": (previous.usage or Usage()).merged(result.usage or Usage()),
                    "turns": (previous.turns or 0) + (result.turns or 0),
                }
            )
            if result.error is not None and result.error.provider is not None:
                prior_failure = previous.error.provider if previous.error else None
                result = result.model_copy(
                    update={
                        "session_id": result.session_id or previous.session_id,
                        "output_text": "\n".join(
                            filter(None, [previous.output_text, result.output_text])
                        ),
                        "error": result.error.model_copy(
                            update={
                                "provider": result.error.provider.model_copy(
                                    update={
                                        "partial_progress": result.error.provider.partial_progress
                                        or bool(prior_failure and prior_failure.partial_progress),
                                    }
                                ),
                            }
                        ),
                    }
                )
        if result.error is not None and result.error.provider is not None:
            hold = self.record(job, result)
            bus.emit(
                "provider.held",
                job.run_id,
                job_id=job.job_id,
                backend=self.backend,
                reason=hold.failure.reason,
                category=hold.failure.category,
                next_at=hold.next_at,
                message=hold.summary(),
                session_id=result.session_id,
            )
            raise ProviderHeldError(hold)
        if previous is not None:
            with self._write() as session:
                session.execute(
                    update(ProviderJobRow)
                    .where(
                        ProviderJobRow.run_id == job.run_id,
                        ProviderJobRow.job_key == key,
                    )
                    .values(pending=0, result_json=result.model_dump_json())
                )
            bus.emit("provider.recovered", job.run_id, backend=self.backend, job_id=job.job_id)
        # Only success closes a cooldown; a non-provider failure is not
        # evidence that the credential recovered.
        if result.status == "ok" and prior_hold is not None:
            self.release(expected_generation=prior_hold.generation)
        return result
