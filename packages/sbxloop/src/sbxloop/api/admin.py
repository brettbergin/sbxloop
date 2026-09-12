"""Daemon administration through the shared service: holds, the graceful
stop and the supervised restart, a suspended repository's polling, and
the schedules (#1040). The REST routes and the WebSocket's commands call
these; each is one recorded operation with the same idempotency the
other commands have.

A stop or a restart is *accepted*, durably, before its effect: the
service records the operation and hands back ``after``, which the
transport fires once its reply is on its way — so the client that asked
holds a record of acceptance whatever becomes of the connection, and the
process exit is a separate fact it observes through readiness.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from sbxloop.api.auth.deps import Authenticated
from sbxloop.api.commands import Replayed, _operation, replayed_problem, run_command
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import (
    CurrentRun,
    DaemonCommandResult,
    HoldRequest,
    HoldResult,
    OperationOut,
    RepositoryResult,
    RestartRequest,
    ScheduleCreate,
    ScheduleResult,
)
from sbxloop.api.projections import Views, holds_view, not_found, schedule_by_name
from sbxloop.config import ScheduleConfig
from sbxloop.daemon.controls.operations import Operation
from sbxloop.daemon.controls.results import Outcome

ScheduleVerb = Literal["pause", "resume", "remove"]
After = Callable[[], None]

#: What a schedule created through the API records as its provenance.
SCHEDULE_SOURCE = "api"


async def _apply[T: Outcome](ctx: ApiContext, fn: Callable[[], T]) -> tuple[T | None, Operation]:
    """Run the service call; on a replay, the existing operation with no
    fresh outcome (a refusal it recorded is raised as it was)."""
    try:
        outcome = await run_command(ctx, fn)
    except Replayed as replay:
        existing = replay.operation
        problem = replayed_problem(existing)
        if problem is not None and existing.state != "running":
            raise problem from replay
        return None, existing
    return outcome, _operation(ctx, outcome.operation_id)


# -- holds -------------------------------------------------------------------------


async def take_hold(
    ctx: ApiContext, auth: Authenticated, body: HoldRequest, pair: tuple[str, str] | None
) -> HoldResult:
    """Take a named hold attributed to the principal: nothing new is
    claimed while it stands; the run in flight is not touched."""
    principal = auth.principal
    service = ctx.service()
    outcome, operation = await _apply(
        ctx, lambda: service.pause(principal, body.name, reason=body.reason, idempotency=pair)
    )
    created = bool(getattr(outcome, "fresh", False)) if outcome is not None else False
    holds = await ctx.call(holds_view, ctx.loop)
    ctx.hub.notify()
    return HoldResult(
        hold=body.name,
        holds=holds,
        created=created,
        operation=OperationOut.from_operation(operation),
    )


async def release_hold(
    ctx: ApiContext,
    auth: Authenticated,
    name: str,
    *,
    force: bool,
    pair: tuple[str, str] | None,
) -> HoldResult:
    """Release one hold. Another principal's hold is refused
    (``409 hold_owned``) unless ``force`` says the caller is overriding:
    releasing one person's hold never releases someone else's by
    accident."""
    principal = auth.principal
    service = ctx.service()

    def apply() -> Outcome:
        if name not in ctx.loop.holds:
            raise not_found()
        return service.release(principal, name, only_own=not force, idempotency=pair)

    _outcome, operation = await _apply(ctx, apply)
    holds = await ctx.call(holds_view, ctx.loop)
    ctx.hub.notify()
    return HoldResult(hold=name, holds=holds, operation=OperationOut.from_operation(operation))


# -- stop and restart ---------------------------------------------------------------


async def lifecycle(
    ctx: ApiContext,
    auth: Authenticated,
    action: Literal["stop", "restart"],
    body: RestartRequest | None,
    pair: tuple[str, str] | None,
) -> tuple[DaemonCommandResult, After | None]:
    """Accept a graceful stop, or a restart under a supervisor that will
    start the daemon again (refused by name otherwise). The effect is
    ``after``: the transport fires it once the reply is on its way."""
    principal = auth.principal
    service = ctx.service()
    now = bool(body.now) if body is not None else False

    def apply() -> Outcome:
        if action == "stop":
            return service.stop(principal, idempotency=pair)
        return service.restart(principal, now=now, idempotency=pair)

    outcome, operation = await _apply(ctx, apply)
    after: After | None = getattr(outcome, "after", None) if outcome is not None else None
    status = await ctx.call(lambda: dict(ctx.loop.status()))
    current = status.get("current")
    result = DaemonCommandResult(
        action=action,
        generation=ctx.generation(),
        supervisor=getattr(outcome, "supervisor", None) if outcome is not None else None,
        now=now,
        current=(
            CurrentRun(
                item_id=str(current["item_id"]),
                run_id=str(current["run_id"]),
                title=str(current.get("title", "")),
                kind=str(current.get("kind", "code")),
                profile=current.get("profile"),
            )
            if current
            else None
        ),
        operation=OperationOut.from_operation(operation),
    )
    if after is not None:
        # The chronology says the exit is coming before it comes: a stream
        # that hears this reconnects to the next generation, not to a
        # listener that is gone.
        ctx.chronology.record(
            f"daemon.{action}_requested",
            ctx.clock(),
            operation_id=operation.id,
            actor={"kind": principal.kind, "id": principal.id, "via": principal.via},
            data={"now": now, "supervisor": result.supervisor, "generation": result.generation},
        )
        ctx.hub.notify()
    return result, after


# -- repositories --------------------------------------------------------------------


async def resume_repository(
    ctx: ApiContext, auth: Authenticated, public_id: str, pair: tuple[str, str] | None
) -> RepositoryResult:
    """Poll a suspended or backing-off repository again now."""
    principal = auth.principal
    service = ctx.service()

    def apply() -> Outcome:
        entry = Views(ctx).repository_by_public_id(public_id)
        return service.resume_repo(principal, entry.repo, idempotency=pair)

    _outcome, operation = await _apply(ctx, apply)

    def project() -> RepositoryResult:
        views = Views(ctx)
        entry = views.repository_by_public_id(public_id)
        repository = next(r for r in views.repositories() if r.repository == entry.repo)
        return RepositoryResult(
            repository=repository, operation=OperationOut.from_operation(operation)
        )

    result = await ctx.call(project)
    ctx.hub.notify()
    return result


# -- schedules -------------------------------------------------------------------------


async def schedule_command(
    ctx: ApiContext,
    auth: Authenticated,
    verb: ScheduleVerb,
    name: str,
    pair: tuple[str, str] | None,
) -> ScheduleResult:
    """Pause, resume or remove one schedule under its existing semantics."""
    principal = auth.principal
    service = ctx.service()

    def apply() -> Outcome:
        schedule_by_name(ctx.loop, name)
        return service.schedule_control(principal, verb, name, idempotency=pair)

    outcome, operation = await _apply(ctx, apply)
    message = str(getattr(outcome, "message", "")) if outcome is not None else ""
    if not message and operation.result:
        message = str(operation.result.get("message") or "")

    def project() -> ScheduleResult:
        schedule = None if verb == "remove" else schedule_by_name(ctx.loop, name)
        return ScheduleResult(
            schedule=schedule, message=message, operation=OperationOut.from_operation(operation)
        )

    result = await ctx.call(project)
    ctx.hub.notify()
    return result


async def create_schedule(
    ctx: ApiContext, auth: Authenticated, body: ScheduleCreate, pair: tuple[str, str] | None
) -> ScheduleResult:
    """Create a schedule: live from the next tick, under the same rules
    the concierge's tool and ``schedules add`` apply."""
    principal = auth.principal
    service = ctx.service()
    try:
        spec = ScheduleConfig.model_validate(body.model_dump())
    except ValueError as exc:
        raise Problem(422, "invalid_request", str(exc)) from exc

    def apply() -> Outcome:
        return service.add_schedule(principal, spec, source=SCHEDULE_SOURCE, idempotency=pair)

    outcome, operation = await _apply(ctx, apply)
    message = str(getattr(outcome, "message", "")) if outcome is not None else ""
    if not message and operation.result:
        message = str(operation.result.get("message") or "")

    def project() -> ScheduleResult:
        return ScheduleResult(
            schedule=schedule_by_name(ctx.loop, spec.name),
            message=message,
            operation=OperationOut.from_operation(operation),
        )

    result = await ctx.call(project)
    ctx.hub.notify()
    return result


#: The administrative actions the WebSocket's command frame may name.
ADMIN_ACTIONS: dict[str, tuple[str, str]] = {
    "daemon.hold": ("daemon:manage", "/v1/daemon/holds"),
    "daemon.release": ("daemon:manage", "/v1/daemon/holds/{id}"),
    "daemon.stop": ("daemon:manage", "/v1/daemon/stop"),
    "daemon.restart": ("daemon:manage", "/v1/daemon/restart"),
    "repository.resume": ("daemon:manage", "/v1/repositories/{id}/resume"),
    "schedule.create": ("daemon:manage", "/v1/schedules"),
    "schedule.pause": ("daemon:manage", "/v1/schedules/{id}/pause"),
    "schedule.resume": ("daemon:manage", "/v1/schedules/{id}/resume"),
    "schedule.remove": ("daemon:manage", "/v1/schedules/{id}"),
}


async def run_admin(
    ctx: ApiContext,
    auth: Authenticated,
    *,
    action: str,
    target: str | None,
    params: dict[str, Any],
    pair: tuple[str, str] | None,
) -> tuple[dict[str, Any], After | None]:
    """An administrative command frame, routed to the function its REST
    route calls; the result as the REST body would be, and the effect a
    stop or restart defers until the reply is sent."""
    from pydantic import ValidationError

    try:
        if action == "daemon.hold":
            hold = await take_hold(ctx, auth, HoldRequest.model_validate(params), pair)
            return hold.model_dump(mode="json"), None
        if action in ("daemon.stop", "daemon.restart"):
            body = RestartRequest.model_validate(params) if action == "daemon.restart" else None
            kind: Literal["stop", "restart"] = "stop" if action == "daemon.stop" else "restart"
            result, after = await lifecycle(ctx, auth, kind, body, pair)
            return result.model_dump(mode="json"), after
        if action == "schedule.create":
            created = await create_schedule(ctx, auth, ScheduleCreate.model_validate(params), pair)
            return created.model_dump(mode="json"), None
        if not target:
            raise Problem(422, "invalid_request", f"{action} needs a target")
        if action == "daemon.release":
            released = await release_hold(
                ctx, auth, target, force=bool(params.get("force", False)), pair=pair
            )
            return released.model_dump(mode="json"), None
        if action == "repository.resume":
            resumed = await resume_repository(ctx, auth, target, pair)
            return resumed.model_dump(mode="json"), None
        verb: ScheduleVerb = action.removeprefix("schedule.")  # type: ignore[assignment]
        changed = await schedule_command(ctx, auth, verb, target, pair)
        return changed.model_dump(mode="json"), None
    except ValidationError as exc:
        raise Problem(
            422,
            "invalid_request",
            "the command's params are not valid",
            errors=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
        ) from exc
