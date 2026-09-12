"""Running a recorded command from a route: the idempotency pair, the
replay and the conflict, as the spike's command contract has them.

Every mutating route builds an idempotency scope from the workspace, the
principal, the method and the canonical route, takes the client's
``Idempotency-Key``, and calls the service inside :func:`run_command`. A
replay of the same key and payload answers with the operation that
already exists (or the refusal it recorded); a different payload under
the same key is ``409 idempotency_conflict``.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Request

from sbxloop.api.context import ApiContext
from sbxloop.api.errors import CONTROL_STATUS, Problem
from sbxloop.daemon.controls.operations import IdempotencyConflict, Operation, OperationReplay
from sbxloop.daemon.controls.principal import Principal
from sbxloop.daemon.controls.results import Outcome

KEY_HEADER = "Idempotency-Key"
KEY_MAX = 200


def idempotency(
    request: Request, principal: Principal, route: str, *, required: bool
) -> tuple[str, str] | None:
    """The ``(scope, key)`` pair for this request, ``None`` when the client
    sent no key and the route does not insist on one."""
    key = request.headers.get(KEY_HEADER, "").strip()
    if not key:
        if required:
            raise Problem(
                422,
                "idempotency_key_required",
                f"a {KEY_HEADER} header is required to admit work",
            )
        return None
    if len(key) > KEY_MAX:
        raise Problem(422, "invalid_request", f"{KEY_HEADER} is limited to {KEY_MAX} characters")
    scope = f"{principal.workspace_id}:{principal.id}:{request.method}:{route}"
    return scope, key


class Replayed(Exception):
    """The command was a replay: the existing operation, for the route to
    answer from."""

    def __init__(self, operation: Operation) -> None:
        super().__init__(operation.id)
        self.operation = operation


async def run_command[T: Outcome](ctx: ApiContext, fn: Callable[[], T]) -> T:
    """Run ``fn`` on the executor, turning the operation store's replay
    and conflict into what the route answers."""
    try:
        return await ctx.call(fn)
    except OperationReplay as exc:
        raise Replayed(exc.existing) from exc
    except IdempotencyConflict as exc:
        raise Problem(
            409,
            "idempotency_conflict",
            "the idempotency key was already used with a different request",
            operation_id=exc.existing.id,
        ) from exc


def replayed_problem(operation: Operation) -> Problem | None:
    """What a replayed operation that did not succeed answers: the refusal
    it recorded, or a conflict while the first attempt is still running."""
    if operation.state == "succeeded":
        return None
    if operation.state in ("failed", "expired"):
        code = operation.error_code or "failed"
        return Problem(
            CONTROL_STATUS.get(code, 409),
            code,
            operation.error_detail or "the earlier attempt failed",
            operation_id=operation.id,
        )
    return Problem(
        409,
        "already_in_progress",
        "the same request is still being applied",
        operation_id=operation.id,
    )
