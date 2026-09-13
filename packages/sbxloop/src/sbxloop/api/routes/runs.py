"""Runs: history with bounded filters, one run's summary, its task graph."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from sbxloop.api.auth.deps import Authenticated, get_ctx, require
from sbxloop.api.context import PAGE_DEFAULT, PAGE_MAX, ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import Run, Task
from sbxloop.api.pagination import Page, decode_cursor, encode_cursor
from sbxloop.api.projections import Views
from sbxloop.engine.model import RunRecord, RunState

router = APIRouter(prefix="/v1", tags=["runs"])

RUN_STATES: tuple[str, ...] = RunState.__args__  # type: ignore[attr-defined]
RUN_KINDS: tuple[str, ...] = ("code", "workload", "tool")


@router.get("/runs", response_model=Page[Run])
async def list_runs(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    state: Annotated[list[str] | None, Query()] = None,
    kind: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
    cursor: Annotated[str | None, Query()] = None,
) -> Page[Run]:
    """Runs touched most recently first, filterable by state and kind."""
    states = [s for s in state or [] if s in RUN_STATES]
    if state and len(states) != len(state):
        raise Problem(422, "invalid_request", f"state must be one of {', '.join(RUN_STATES)}")
    kinds = [k for k in kind or [] if k in RUN_KINDS]
    if kind and len(kinds) != len(kind):
        raise Problem(422, "invalid_request", f"kind must be one of {', '.join(RUN_KINDS)}")
    filters: dict[str, Any] = {"state": sorted(states), "kind": sorted(kinds)}
    after: tuple[float, str] | None = None
    if cursor is not None:
        key = decode_cursor(cursor, filters)
        try:
            after = (float(key["u"]), str(key["r"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise Problem(400, "invalid_cursor", "the cursor is malformed") from exc

    def read() -> tuple[list[Run], list[RunRecord], bool]:
        views = Views(ctx)
        rows: list[RunRecord] = views.store.page_runs(
            states=states or None, kinds=kinds or None, after=after, limit=limit + 1
        )
        more = len(rows) > limit
        rows = rows[:limit]
        return views.runs(rows), rows, more

    data, rows, more = await ctx.call(read)
    next_cursor = (
        encode_cursor({"u": rows[-1].updated_at, "r": rows[-1].run_id}, filters)
        if more and rows
        else None
    )
    return Page(data=data, next_cursor=next_cursor, has_more=more)


@router.get("/runs/{run_id}", response_model=Run)
async def get_run(
    run_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> Run:
    """The run's summary, stage, result, gate and available actions."""

    def read() -> Run:
        views = Views(ctx)
        return views.run(views.run_by_public_id(run_id))

    return await ctx.call(read)


@router.get("/runs/{run_id}/tasks", response_model=list[Task])
async def get_tasks(
    run_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> list[Task]:
    """The task graph with each task's progress and verification summary."""

    def read() -> list[Task]:
        views = Views(ctx)
        record = views.run_by_public_id(run_id)
        return views.tasks(record.run_id)

    return await ctx.call(read)
