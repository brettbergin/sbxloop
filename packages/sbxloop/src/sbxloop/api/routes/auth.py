"""Token minting and revocation.

``POST /v1/auth/token`` takes client credentials or a refresh token and
answers with a short-lived access token and a rotated refresh token.
Failures are rate-limited per client id and per source address, apart
from every other limit. ``POST /v1/auth/revoke`` ends the access token it
was made with, and a refresh token family when one is named.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from sbxloop.api.auth.deps import Authenticated, current, get_ctx
from sbxloop.api.auth.store import AuthError, Client
from sbxloop.api.auth.tokens import mint_access, scope_from
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import RevokeRequest, TokenRequest, TokenResponse
from sbxloop.log import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _limited(ctx: ApiContext, keys: list[str]) -> None:
    now = ctx.clock()
    for key in keys:
        wait = ctx.limiter.retry_after(key, now)
        if wait is not None:
            raise Problem(
                429,
                "too_many_attempts",
                "too many failed authentication attempts; try again later",
                headers={"Retry-After": str(int(wait) + 1)},
            )


def _failed(ctx: ApiContext, keys: list[str]) -> None:
    now = ctx.clock()
    for key in keys:
        ctx.limiter.record_failure(key, now)


def grant_tokens(ctx: ApiContext, client: Client, *, family_id: str | None) -> TokenResponse:
    now = ctx.clock()
    api = ctx.api
    access, claims = mint_access(
        ctx.keys,
        client_id=client.id,
        capabilities=client.capabilities,
        ttl_s=api.access_token_ttl_s,
        now=now,
    )
    refresh = ctx.auth.issue_refresh(
        client.id, family_id=family_id, now=now, ttl_s=api.refresh_token_ttl_s
    )
    return TokenResponse(
        access_token=access,
        expires_in=api.access_token_ttl_s,
        refresh_token=refresh,
        refresh_expires_in=api.refresh_token_ttl_s,
        scope=scope_from(claims.scope),
        client_id=client.id,
    )


def _client_credentials(ctx: ApiContext, body: TokenRequest, address: str) -> TokenResponse:
    if not body.client_id or not body.client_secret:
        raise Problem(422, "invalid_request", "client_id and client_secret are required")
    keys = [f"client:{body.client_id}", f"addr:{address}"]
    _limited(ctx, keys)
    try:
        client = ctx.auth.authenticate(body.client_id, body.client_secret, ctx.clock())
    except AuthError as exc:
        _failed(ctx, keys)
        log.warning("api.auth_failed", client=body.client_id, reason=exc.code)
        raise Problem(401, exc.code, exc.message) from exc
    for key in keys:
        ctx.limiter.reset(key)
    return grant_tokens(ctx, client, family_id=None)


def _refresh(ctx: ApiContext, body: TokenRequest, address: str) -> TokenResponse:
    if not body.refresh_token:
        raise Problem(422, "invalid_request", "refresh_token is required")
    keys = [f"addr:{address}"]
    _limited(ctx, keys)
    api = ctx.api
    try:
        client, fresh = ctx.auth.rotate_refresh(
            body.refresh_token, now=ctx.clock(), ttl_s=api.refresh_token_ttl_s
        )
    except AuthError as exc:
        _failed(ctx, keys)
        log.warning("api.refresh_failed", reason=exc.code)
        raise Problem(401, exc.code, exc.message) from exc
    now = ctx.clock()
    access, claims = mint_access(
        ctx.keys,
        client_id=client.id,
        capabilities=client.capabilities,
        ttl_s=api.access_token_ttl_s,
        now=now,
    )
    return TokenResponse(
        access_token=access,
        expires_in=api.access_token_ttl_s,
        refresh_token=fresh,
        refresh_expires_in=api.refresh_token_ttl_s,
        scope=scope_from(claims.scope),
        client_id=client.id,
    )


@router.post("/token", response_model=TokenResponse)
async def token(
    body: TokenRequest,
    request: Request,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
) -> TokenResponse:
    """Exchange client credentials, or a refresh token, for a token pair.
    A refresh token is single-use: presenting one twice revokes its
    family, and the client authenticates with its secret again."""
    address = _address(request)
    if body.grant_type == "client_credentials":
        return await ctx.call(_client_credentials, ctx, body, address)
    return await ctx.call(_refresh, ctx, body, address)


@router.post("/revoke", status_code=204)
async def revoke(
    body: RevokeRequest | None = None,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(current),  # noqa: B008
) -> None:
    """End this access token now (it would otherwise run to its expiry),
    and a refresh token's whole family when one is named."""
    now = ctx.clock()
    claims = auth.claims

    def apply() -> None:
        ctx.auth.revoke_access(claims.jti, claims.client_id, claims.expires_at, now)
        if body is not None and body.refresh_token:
            ctx.auth.revoke_refresh(body.refresh_token, now)

    await ctx.call(apply)
