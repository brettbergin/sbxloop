"""The daemon's live state."""

from __future__ import annotations

from typing import Any

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

    def read() -> tuple[dict[str, Any], int | None]:
        outcome = service.status(auth.principal)
        # The snapshot's high-water mark: everything the engine has written
        # is projected first, so a client subscribing from it sees no gap.
        ctx.chronology.project(ctx.clock())
        return outcome.status, ctx.chronology.watermark()

    status, watermark = await ctx.call(read)
    view = Status.from_status(status, now=ctx.clock())
    return view.model_copy(update={"watermark": watermark})
