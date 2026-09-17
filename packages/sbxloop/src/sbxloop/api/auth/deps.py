"""FastAPI dependencies: the principal behind a bearer token, the
capability a route needs, and a daemon that is ready to act."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Request
from sqlalchemy.exc import SQLAlchemyError

from sbxloop.api.auth.store import Client
from sbxloop.api.auth.tokens import AccessClaims, TokenError, verify_access
from sbxloop.api.collaboration import Member
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.daemon.controls.principal import Capability, Principal, Role
from sbxloop.log import get_logger

log = get_logger(__name__)


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
    #: The workspace member the client belongs to. ``None`` for a plain API
    #: client with no local user (or a user no longer in the workspace);
    #: such a client keeps the reach its capabilities give it.
    member: Member | None = None


#: A member's last-seen time is written at most this often.
LAST_SEEN_INTERVAL_S = 60.0


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


def _touch_last_seen(ctx: ApiContext, user_id: str, now: float) -> None:
    """Best effort: a busy, locked or full database never fails the request
    (or a stream's access re-check) over a last-seen time."""
    try:
        ctx.collaboration.touch_last_seen(user_id, now, LAST_SEEN_INTERVAL_S)
    except (SQLAlchemyError, sqlite3.Error) as exc:
        log.debug("auth.last_seen_skipped", user_id=user_id, error=str(exc))


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
    member = ctx.collaboration.member_for_client(client.id)
    if member is not None:
        if not member.user.active:
            # A deactivated person's tokens stop working at once, whatever
            # they were minted with.
            raise Problem(
                401,
                "user_inactive",
                "the user behind this client is deactivated",
                headers={"WWW-Authenticate": 'Bearer realm="sbxloop"'},
            )
        seen = member.user.last_seen_at
        if seen is None or now - seen >= LAST_SEEN_INTERVAL_S:
            _touch_last_seen(ctx, member.user.id, now)
    return Authenticated(
        principal=principal, claims=claims, client=client, token=token, member=member
    )


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


def member_of(auth: Authenticated) -> Member:
    """The calling member, or the problem a client without an active local
    profile has always been refused with."""
    member = auth.member
    if member is None or not member.user.active:
        raise Problem(403, "local_profile_required", "this client is not the local Angie user")
    return member


async def current_member(auth: Authenticated = Depends(current)) -> Member:  # noqa: B008
    """The workspace member behind the request; 403 for anyone else."""
    return member_of(auth)


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


#: How much a role may do, lowest first: each role can do what the ones
#: before it can.
ROLE_RANK: dict[Role, int] = {"member": 0, "admin": 1, "owner": 2}


def role_of(auth: Authenticated) -> Role | None:
    """The workspace role the caller acts with: its member's role, ``owner``
    for a plain API client holding ``daemon:manage`` (an operator), and
    ``None`` for any other plain client."""
    if auth.member is not None:
        return auth.member.role if auth.member.user.active else None
    if auth.principal.can("daemon:manage"):
        return "owner"
    return None


def require_role(
    minimum: Role,
) -> Callable[..., Coroutine[Any, Any, Authenticated]]:
    """A dependency admitting callers whose workspace role is at least
    ``minimum`` (see :func:`role_of`); anyone else is 403
    ``forbidden_role``."""

    async def dependency(auth: Authenticated = Depends(current)) -> Authenticated:  # noqa: B008
        role = role_of(auth)
        if role is None or ROLE_RANK[role] < ROLE_RANK[minimum]:
            raise Problem(
                403,
                "forbidden_role",
                f"this action needs the {minimum} role or higher",
                role=minimum,
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
