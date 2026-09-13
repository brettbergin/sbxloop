"""What this installation offers: the contract version, features, limits."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from sbxloop import __version__
from sbxloop.api.auth.deps import Authenticated, current, get_ctx
from sbxloop.api.auth.ratelimit import FailureLimiter
from sbxloop.api.context import PAGE_DEFAULT, PAGE_MAX, ApiContext
from sbxloop.api.models import Capabilities, Limits, Me, Retention, rfc3339
from sbxloop.daemon.controls.principal import CAPABILITIES

router = APIRouter(prefix="/v1", tags=["meta"])

#: What this release serves; a later stage appends to it.
FEATURES: tuple[str, ...] = (
    "status",
    "operations",
    "auth.client_credentials",
    "auth.refresh",
    "items",
    "queue",
    "runs",
    "intake.issue",
    "intake.workload",
    "intake.tool",
    "events",
    "events.stream",
    "ws",
    "runs.control",
    "steering",
    "gates",
    "artifacts",
    "usage",
    "diagnostics.logs",
    "diagnostics.configuration",
    "daemon.holds",
    "daemon.lifecycle",
    "repositories.resume",
    "schedules",
    "auth.local_user",
    "collaboration.channels",
    "collaboration.turns",
    "collaboration.agents",
    "collaboration.teams",
    "collaboration.preferences",
    "collaboration.workflows",
    "collaboration.connections.read",
)


@router.get("/capabilities", response_model=Capabilities)
async def capabilities(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(current),  # noqa: B008
) -> Capabilities:
    api = ctx.api
    limiter = FailureLimiter()
    return Capabilities(
        server_version=__version__,
        features=list(FEATURES),
        capabilities=list(CAPABILITIES),
        limits=Limits(
            page_default=PAGE_DEFAULT,
            page_max=PAGE_MAX,
            max_body_bytes=api.max_body_bytes,
            max_stream_clients=api.max_stream_clients,
            auth_failures_per_minute=limiter.limit,
            auth_lockout_s=int(limiter.lockout_s),
        ),
        retention=Retention(
            replay_s=api.replay_retention_s,
            idempotency_s=api.idempotency_retention_s,
            operation_deadline_s=api.operation_deadline_s,
        ),
    )


@router.get("/me", response_model=Me)
async def me(auth: Authenticated = Depends(current)) -> Me:  # noqa: B008
    """The authenticated client and the capabilities its token carries now."""
    return Me(
        client_id=auth.client.id,
        name=auth.client.name,
        capabilities=[cap for cap in CAPABILITIES if cap in auth.principal.capabilities],
        token_expires_at=rfc3339(auth.claims.expires_at) or "",
    )
