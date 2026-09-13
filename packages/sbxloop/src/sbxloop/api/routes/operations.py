"""Operations: what was asked, by whom, and what became of it."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from sbxloop.api.auth.deps import Authenticated, get_ctx, require
from sbxloop.api.context import PAGE_DEFAULT, PAGE_MAX, ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import OperationOut
from sbxloop.api.pagination import Page, decode_cursor, encode_cursor
from sbxloop.daemon.controls.operations import Operation, OperationStore

router = APIRouter(prefix="/v1", tags=["operations"])

STATES = ("accepted", "running", "reconciling", "succeeded", "failed", "expired")


def _store(ctx: ApiContext) -> OperationStore:
    store = getattr(ctx.loop, "operations", None)
    if not isinstance(store, OperationStore):
        raise Problem(503, "daemon_not_ready", "the daemon keeps no operation record")
    return store


@router.get("/operations", response_model=Page[OperationOut])
async def list_operations(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("audit:read")),  # noqa: B008
    state: Annotated[list[str] | None, Query()] = None,
    target_kind: Annotated[str | None, Query()] = None,
    target_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
    cursor: Annotated[str | None, Query()] = None,
) -> Page[OperationOut]:
    """Recent operations, newest first, filterable by state and target."""
    states = [s for s in state or [] if s in STATES]
    if state and len(states) != len(state):
        raise Problem(422, "invalid_request", f"state must be one of {', '.join(STATES)}")
    if (target_kind is None) != (target_id is None):
        raise Problem(422, "invalid_request", "target_kind and target_id go together")
    filters: dict[str, Any] = {"state": sorted(states), "target": [target_kind, target_id]}
    after: tuple[float, str] | None = None
    if cursor is not None:
        key = decode_cursor(cursor, filters)
        try:
            after = (float(key["a"]), str(key["i"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise Problem(400, "invalid_cursor", "the cursor is malformed") from exc
    target = (target_kind, target_id) if target_kind and target_id else None
    store = _store(ctx)
    rows: list[Operation] = await ctx.call(
        lambda: store.page(states=states or None, target=target, after=after, limit=limit + 1)
    )
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_cursor({"a": rows[-1].accepted_at, "i": rows[-1].id}, filters)
        if more and rows
        else None
    )
    return Page(
        data=[OperationOut.from_operation(op) for op in rows],
        next_cursor=next_cursor,
        has_more=more,
    )


@router.get("/operations/{operation_id}", response_model=OperationOut)
async def get_operation(
    operation_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("audit:read")),  # noqa: B008
) -> OperationOut:
    store = _store(ctx)
    op = await ctx.call(store.get, operation_id)
    if op is None:
        raise Problem(404, "not_found", "no such operation")
    return OperationOut.from_operation(op)
