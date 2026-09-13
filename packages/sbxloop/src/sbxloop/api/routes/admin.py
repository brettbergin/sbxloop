"""Daemon administration: holds, the graceful stop and the supervised
restart, a suspended repository's polling, and the schedules (#1040)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response

from sbxloop.api import admin
from sbxloop.api.auth.deps import Authenticated, get_ctx, ready_daemon, require
from sbxloop.api.commands import idempotency
from sbxloop.api.context import ApiContext
from sbxloop.api.models import (
    DaemonCommandResult,
    Hold,
    HoldRequest,
    HoldResult,
    RepositoryResult,
    RestartRequest,
    Schedule,
    ScheduleCreate,
    ScheduleResult,
)
from sbxloop.api.pagination import Page
from sbxloop.api.projections import holds_view, schedule_by_name, schedules_view

router = APIRouter(prefix="/v1", tags=["admin"])


# -- holds -------------------------------------------------------------------------


@router.get("/daemon/holds", response_model=Page[Hold])
async def list_holds(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> Page[Hold]:
    """Every hold standing, with whose it is and why: while any stands
    nothing new is claimed; the run in flight is not touched."""
    return Page(data=await ctx.call(holds_view, ctx.loop))


@router.post("/daemon/holds", response_model=HoldResult, status_code=201)
async def take_hold(
    body: HoldRequest,
    request: Request,
    response: Response,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("daemon:manage")),  # noqa: B008
) -> HoldResult:
    """Take a named hold attributed to this client. It persists across a
    restart and is never released by a disconnect: release it, or let an
    operator override it. ``201`` for a new hold, ``200`` for one already
    standing under that name."""
    pair = idempotency(request, auth.principal, "/v1/daemon/holds", required=False)
    result = await admin.take_hold(ctx, auth, body, pair)
    if not result.created:
        response.status_code = 200
    return result


@router.delete("/daemon/holds/{name}", response_model=HoldResult)
async def release_hold(
    name: str,
    request: Request,
    force: Annotated[bool, Query()] = False,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("daemon:manage")),  # noqa: B008
) -> HoldResult:
    """Release one hold. Another principal's is refused as
    ``409 hold_owned`` unless ``force=true`` says this is an override,
    which the operation records as such."""
    pair = idempotency(request, auth.principal, f"/v1/daemon/holds/{name}", required=False)
    return await admin.release_hold(ctx, auth, name, force=force, pair=pair)


# -- stop and restart ---------------------------------------------------------------


@router.post("/daemon/stop", response_model=DaemonCommandResult, status_code=202)
async def stop_daemon(
    request: Request,
    background: BackgroundTasks,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("daemon:manage")),  # noqa: B008
) -> DaemonCommandResult:
    """Ask the daemon to stop gracefully: nothing new is claimed, the run
    and landing in flight finish, then the process exits — and this
    listener with it. ``202`` is durable acceptance, not the exit: a
    stream hears ``daemon.stop_requested``, then ``closing``; a poll of the
    operation may find the connection refused. Under a service manager
    that restarts the daemon this is a restart, and every hold standing
    now still stands when it is back."""
    pair = idempotency(request, auth.principal, "/v1/daemon/stop", required=False)
    result, after = await admin.lifecycle(ctx, auth, "stop", None, pair)
    if after is not None:
        background.add_task(after)
    return result


@router.post("/daemon/restart", response_model=DaemonCommandResult, status_code=202)
async def restart_daemon(
    request: Request,
    background: BackgroundTasks,
    body: RestartRequest | None = None,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("daemon:manage")),  # noqa: B008
) -> DaemonCommandResult:
    """A stop the supervisor undoes: refused as ``409 unsupervised`` when
    nothing would start the daemon again. ``now`` cancels the run in
    flight first (it is resumable). The reply carries the generation that
    accepted the request; the daemon is back when ``/health/ready``
    reports a new one."""
    pair = idempotency(request, auth.principal, "/v1/daemon/restart", required=False)
    result, after = await admin.lifecycle(ctx, auth, "restart", body, pair)
    if after is not None:
        background.add_task(after)
    return result


# -- repositories --------------------------------------------------------------------


@router.post("/repositories/{repository_id}/resume", response_model=RepositoryResult)
async def resume_repository(
    repository_id: str,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("daemon:manage")),  # noqa: B008
) -> RepositoryResult:
    """Poll a suspended or backing-off repository again now; one that is
    polling normally is refused by name."""
    pair = idempotency(
        request, auth.principal, f"/v1/repositories/{repository_id}/resume", required=False
    )
    return await admin.resume_repository(ctx, auth, repository_id, pair)


# -- schedules -------------------------------------------------------------------------


@router.get("/schedules", response_model=Page[Schedule])
async def list_schedules(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> Page[Schedule]:
    """Every schedule with its cadence, last due, next due and who paused it."""
    return Page(data=await ctx.call(schedules_view, ctx.loop))


@router.get("/schedules/{name}", response_model=Schedule)
async def get_schedule(
    name: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> Schedule:
    return await ctx.call(schedule_by_name, ctx.loop, name)


@router.post("/schedules", response_model=ScheduleResult, status_code=201)
async def create_schedule(
    body: ScheduleCreate,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("daemon:manage")),  # noqa: B008
) -> ScheduleResult:
    """Create a schedule under the rules the concierge's tool applies: a
    declared profile, a free name, exactly one cadence. Live from the
    next tick."""
    pair = idempotency(request, auth.principal, "/v1/schedules", required=False)
    return await admin.create_schedule(ctx, auth, body, pair)


@router.post("/schedules/{name}/pause", response_model=ScheduleResult)
async def pause_schedule(
    name: str,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("daemon:manage")),  # noqa: B008
) -> ScheduleResult:
    """Park the schedule: its ticks are swallowed until it is resumed."""
    pair = idempotency(request, auth.principal, f"/v1/schedules/{name}/pause", required=False)
    return await admin.schedule_command(ctx, auth, "pause", name, pair)


@router.post("/schedules/{name}/resume", response_model=ScheduleResult)
async def resume_schedule(
    name: str,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("daemon:manage")),  # noqa: B008
) -> ScheduleResult:
    """Release a parked schedule; a tick that passed while paused is not made up."""
    pair = idempotency(request, auth.principal, f"/v1/schedules/{name}/resume", required=False)
    return await admin.schedule_command(ctx, auth, "resume", name, pair)


@router.delete("/schedules/{name}", response_model=ScheduleResult)
async def remove_schedule(
    name: str,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("daemon:manage")),  # noqa: B008
) -> ScheduleResult:
    """Delete the schedule and its state; a tick already queued or running is untouched."""
    pair = idempotency(request, auth.principal, f"/v1/schedules/{name}", required=False)
    return await admin.schedule_command(ctx, auth, "remove", name, pair)
