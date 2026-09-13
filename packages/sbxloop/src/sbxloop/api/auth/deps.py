"""FastAPI dependencies: the principal behind a bearer token, the
capability a route needs, and a daemon that is ready to act."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Request

from sbxloop.api.auth.store import Client
from sbxloop.api.auth.tokens import AccessClaims, TokenError, verify_access
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.daemon.controls.principal import Capability, Principal


def get_ctx(request: Request) -> ApiContext:
    ctx = request.app.state.ctx
    assert isinstance(ctx, ApiContext)  # nosec B101 - wired by create_app
    return ctx


@dataclass(frozen=True, slots=True)
class Authenticated:
    principal: Principal
    claims: AccessClaims
    client: Client
    #: The bearer token as presented: a long-lived stream re-verifies it
    #: on a schedule and closes when it no longer stands.
    token: str = ""


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Problem(
            401,
            "unauthenticated",
            "a bearer access token is required",
            headers={"WWW-Authenticate": 'Bearer realm="sbxloop"'},
        )
    return token.strip()


def _resolve(ctx: ApiContext, token: str) -> Authenticated:
    now = ctx.clock()
    try:
        claims = verify_access(ctx.keys, token, now=now)
    except TokenError as exc:
        raise Problem(
            401, exc.code, exc.message, headers={"WWW-Authenticate": 'Bearer realm="sbxloop"'}
        ) from exc
    if ctx.auth.is_revoked(claims.jti):
        raise Problem(401, "token_revoked", "the access token was revoked")
    client = ctx.auth.get_client(claims.client_id)
    if client is None or not client.active:
        raise Problem(401, "client_revoked", "the client was revoked")
    # The token's scope, narrowed by what the client still holds: a grant
    # taken away since the token was minted is gone at once.
    capabilities = claims.scope & client.capabilities
    principal = Principal(
        kind="client",
        id=client.id,
        display=client.name,
        via="api",
        capabilities=capabilities,
        workspace_id=claims.workspace_id,
    )
    return Authenticated(principal=principal, claims=claims, client=client, token=token)


def resolve_token(ctx: ApiContext, token: str) -> Authenticated:
    """The principal behind a raw bearer token — for a transport that
    carries it outside the request headers (a WebSocket's first frame, a
    stream's periodic re-check). Raises the same problems ``current`` does."""
    return _resolve(ctx, token)


async def current(request: Request, ctx: ApiContext = Depends(get_ctx)) -> Authenticated:  # noqa: B008
    token = _bearer(request)
    auth = await ctx.call(_resolve, ctx, token)
    request.state.principal = auth.principal
    return auth


def require(
    capability: Capability,
) -> Callable[..., Coroutine[Any, Any, Authenticated]]:
    async def dependency(auth: Authenticated = Depends(current)) -> Authenticated:  # noqa: B008
        if not auth.principal.can(capability):
            raise Problem(
                403,
                "forbidden",
                f"{auth.principal.id} lacks {capability}",
                capability=capability,
            )
        return auth

    return dependency


async def ready_daemon(ctx: ApiContext = Depends(get_ctx)) -> ApiContext:  # noqa: B008
    """A mutation needs the daemon past recovery: until then the command
    would land on state recovery is still settling."""
    if ctx.stopping.is_set():
        raise Problem(503, "daemon_stopping", "the daemon is shutting down")
    if not ctx.ready.is_set():
        raise Problem(
            503,
            "daemon_not_ready",
            "the daemon is recovering; commands are refused until it is ready",
            headers={"Retry-After": "5"},
        )
    return ctx
