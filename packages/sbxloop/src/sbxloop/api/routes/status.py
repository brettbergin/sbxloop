"""The daemon's live state."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from sbxloop.api.auth.deps import Authenticated, get_ctx, require
from sbxloop.api.context import ApiContext
from sbxloop.api.models import Status

router = APIRouter(prefix="/v1", tags=["status"])


@router.get("/status", response_model=Status)
async def status(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> Status:
    """Live current/claiming state, queue counts, holds, breaker, stopping,
    version and the time it was observed. Live, not a database snapshot:
    an unavailable daemon answers 503 rather than a stale picture."""
    service = ctx.service()
    outcome = await ctx.call(service.status, auth.principal)
    return Status.from_status(outcome.status, now=ctx.clock())
