"""Liveness and readiness: unauthenticated, and saying no more than that."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from sbxloop.api.auth.deps import get_ctx
from sbxloop.api.context import ApiContext
from sbxloop.api.models import Health, Readiness

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=Health)
async def live() -> Health:
    """The API process answers; it says nothing about execution."""
    return Health()


@router.get("/health/ready", response_model=Readiness, responses={503: {"model": Readiness}})
async def ready(response: Response, ctx: ApiContext = Depends(get_ctx)) -> Readiness:  # noqa: B008
    """Recovery has established execution ownership and commands are taken."""
    is_ready = ctx.ready.is_set() and not ctx.stopping.is_set()
    if not is_ready:
        response.status_code = 503
    return Readiness(ready=is_ready, generation=ctx.generation())
