"""Reported usage: one run's, and a bounded window's — telemetry, never
an invoice."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from sbxloop.api.auth.deps import Authenticated, get_ctx, require
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import RunUsage, UsagePool, UsageWindow, rfc3339
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
    90 days wide)."""
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
        raise Problem(422, "invalid_request", "a usage window is at most 90 days wide")

    def read() -> UsageWindow:
        views = Views(ctx)
        return window_usage(
            views.store, since=start, until=end, now=now, runs=views.store.list_runs
        )

    return await ctx.call(read)


@router.get("/usage/pool", response_model=UsagePool)
async def get_usage_pool(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> UsagePool:
    """Today's workspace budget pool: runs against `[daemon]
    max_runs_per_day` and reported input plus output tokens, from runs and
    chat turns, against `[daemon] daily_token_budget`."""

    def read() -> UsagePool:
        figures = ctx.loop.usage_pool.snapshot(ctx.clock())
        return UsagePool(
            day_start=rfc3339(figures["day_start"]) or "",
            resets_at=rfc3339(figures["resets_at"]) or "",
            runs_today=figures["runs_today"],
            max_runs_per_day=figures["max_runs_per_day"],
            tokens_today=figures["tokens_today"],
            daily_token_budget=figures["daily_token_budget"],
            runs_tokens_today=figures["runs_tokens_today"],
            turns_tokens_today=figures["turns_tokens_today"],
        )

    return await ctx.call(read)
