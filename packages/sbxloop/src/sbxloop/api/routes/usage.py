"""Reported usage: one run's, and a bounded window's — telemetry, never
an invoice."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from sbxloop.api.auth.deps import Authenticated, get_ctx, require
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import RunUsage, UsageWindow
from sbxloop.api.projections import Views
from sbxloop.api.usage import WINDOW_MAX_S, parse_when, run_usage, window_usage
from sbxloop.daemon.loop import day_window

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/runs/{run_id}/usage", response_model=RunUsage)
async def get_run_usage(
    run_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> RunUsage:
    """Tokens and turns the run's backend reported, by persona and by
    phase and model; unknown values stay unknown."""

    def read() -> RunUsage:
        views = Views(ctx)
        record = views.run_by_public_id(run_id)
        return run_usage(views.store, record)

    return await ctx.call(read)


@router.get("/usage", response_model=UsageWindow)
async def get_usage(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    since: Annotated[str | None, Query()] = None,
    until: Annotated[str | None, Query()] = None,
) -> UsageWindow:
    """Reported usage across the runs touched in a window (RFC 3339 or
    epoch bounds; the daemon's current calendar day when omitted; at most
    31 days wide)."""
    now = ctx.clock()
    day_start, day_end = day_window(now, ctx.config.daemon.run_cap_timezone)
    try:
        start = parse_when(since, default=day_start)
        end = parse_when(until, default=day_end if since is None else now)
    except ValueError as exc:
        raise Problem(422, "invalid_request", str(exc)) from exc
    if end <= start:
        raise Problem(422, "invalid_request", "until must be after since")
    if end - start > WINDOW_MAX_S:
        raise Problem(422, "invalid_request", "a usage window is at most 31 days wide")

    def read() -> UsageWindow:
        views = Views(ctx)
        return window_usage(
            views.store, since=start, until=end, now=now, runs=views.store.list_runs
        )

    return await ctx.call(read)
