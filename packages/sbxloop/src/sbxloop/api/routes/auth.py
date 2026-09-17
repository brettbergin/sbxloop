"""Token minting and revocation.

``POST /v1/auth/token`` takes client credentials or a refresh token and
answers with a short-lived access token and a rotated refresh token.
Failures are rate-limited per client id and per source address, apart
from every other limit. ``POST /v1/auth/revoke`` ends the access token it
was made with, and a refresh token family when one is named.

``GET /v1/auth/providers`` tells a signed-out client which sign-ins it may
offer, and ``POST /v1/auth/oidc/token`` redeems an OpenID Connect
authorization code for the same token pair a local login returns.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from sbxloop.api.auth.deps import Authenticated, current, get_ctx
from sbxloop.api.auth.oidc import OidcError, OidcProvider, role_for_groups
from sbxloop.api.auth.store import AuthError, Client
from sbxloop.api.auth.tokens import mint_access, scope_from
from sbxloop.api.collaboration import CollaborationError
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import (
    AuthProviders,
    OidcProviderOut,
    OidcTokenRequest,
    RevokeRequest,
    TokenRequest,
    TokenResponse,
)
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


def _providers(ctx: ApiContext) -> AuthProviders:
    provider = ctx.oidc
    oidc: OidcProviderOut | None = None
    if provider is not None:
        try:
            discovery = provider.discovery()
        except OidcError as exc:
            log.warning("api.oidc_discovery_failed", reason=exc.reason)
        else:
            settings = provider.config
            oidc = OidcProviderOut(
                id=settings.id,
                label=settings.label,
                authorize_url=discovery.authorization_endpoint,
                client_id=settings.client_id,
                scopes=list(settings.scopes),
                end_session_url=discovery.end_session_endpoint,
            )
    return AuthProviders(local=True, oidc=oidc)


@router.get("/providers", response_model=AuthProviders)
async def providers(ctx: ApiContext = Depends(get_ctx)) -> AuthProviders:  # noqa: B008
    """The sign-ins a signed-out client may offer. Needs no token. ``oidc``
    is null unless a provider is configured and its discovery answered."""
    return await ctx.call(_providers, ctx)


#: Account refusals from the store, answered as 403 with the store's code.
_ACCOUNT_REFUSALS = frozenset(
    {"oidc_account_disabled", "oidc_not_provisioned", "oidc_email_conflict"}
)


def _oidc_sign_in(
    ctx: ApiContext, provider: OidcProvider, body: OidcTokenRequest, keys: list[str]
) -> TokenResponse:
    settings = provider.config
    try:
        identity = provider.exchange(
            code=body.code,
            code_verifier=body.code_verifier,
            redirect_uri=body.redirect_uri,
            nonce=body.nonce,
        )
    except OidcError as exc:
        if exc.status != 503:
            _failed(ctx, keys)
        log.warning("api.oidc_exchange_failed", reason=exc.reason)
        raise Problem(exc.status, exc.code, exc.message) from exc
    try:
        user = ctx.collaboration.sign_in_external(
            issuer=identity.issuer,
            subject=identity.subject,
            username=identity.username,
            email=identity.email,
            email_verified=identity.email_verified,
            full_name=identity.name,
            role_from_groups=role_for_groups(settings, identity.groups),
            default_role=settings.default_role,
            auto_provision=settings.auto_provision,
            now=ctx.clock(),
        )
    except CollaborationError as exc:
        if exc.code == "oidc_account_conflict":
            raise Problem(409, exc.code, exc.message) from exc
        if exc.code not in _ACCOUNT_REFUSALS:
            raise
        log.warning("api.oidc_account_refused", reason=exc.code)
        raise Problem(403, exc.code, exc.message) from exc
    client = ctx.auth.get_client(user.client_id)
    if client is None or not client.active:
        log.warning("api.oidc_account_refused", reason="client_revoked", user_id=user.id)
        raise Problem(403, "oidc_account_disabled", "this account is disabled")
    for key in keys:
        ctx.limiter.reset(key)
    log.info("api.oidc_login", user_id=user.id)
    return grant_tokens(ctx, client, family_id=None)


@router.post("/oidc/token", response_model=TokenResponse)
async def oidc_token(
    body: OidcTokenRequest,
    request: Request,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
) -> TokenResponse:
    """Redeem an OpenID Connect authorization code (Authorization Code +
    PKCE, run by the browser) for a token pair. The daemon authenticates to
    the provider as the confidential client, validates the ID token, and
    creates the account on a first sign-in. Needs no token."""
    keys = [f"addr:{_address(request)}"]
    _limited(ctx, keys)
    provider = ctx.oidc
    if (
        provider is None
        or body.provider != provider.config.id
        or body.redirect_uri not in provider.config.redirect_uris
    ):
        _failed(ctx, keys)
        raise Problem(
            400,
            "oidc_invalid_request",
            "unknown sign-in provider or redirect_uri not allowed",
        )
    tokens = await ctx.call(_oidc_sign_in, ctx, provider, body, keys)
    ctx.hub.notify()
    return tokens


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
