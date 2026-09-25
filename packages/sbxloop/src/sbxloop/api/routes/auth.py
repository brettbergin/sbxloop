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

from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request, Response

from sbxloop.api.auth.deps import Authenticated, current, get_ctx
from sbxloop.api.auth.oidc import OidcError, OidcProvider, role_for_groups
from sbxloop.api.auth.store import AuthError, Client, OidcSession
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


def grant_tokens(
    ctx: ApiContext,
    client: Client,
    *,
    family_id: str | None,
    session: OidcSession | None = None,
) -> TokenResponse:
    now = ctx.clock()
    api = ctx.api
    access_ttl = api.access_token_ttl_s
    refresh_ttl = api.refresh_token_ttl_s
    if session is not None:
        remaining = max(1, int(session.expires_at - now))
        access_ttl = min(access_ttl, remaining)
        refresh_ttl = min(refresh_ttl, remaining)
        family_id = session.id
    access, claims = mint_access(
        ctx.keys,
        client_id=client.id,
        capabilities=client.capabilities,
        ttl_s=access_ttl,
        now=now,
        session_id=None if session is None else session.id,
    )
    refresh = ctx.auth.issue_refresh(client.id, family_id=family_id, now=now, ttl_s=refresh_ttl)
    return TokenResponse(
        access_token=access,
        expires_in=access_ttl,
        refresh_token=refresh,
        refresh_expires_in=refresh_ttl,
        scope=scope_from(claims.scope),
        client_id=client.id,
    )


def _client_credentials(ctx: ApiContext, body: TokenRequest, address: str) -> TokenResponse:
    if not body.client_id or not body.client_secret:
        raise Problem(422, "invalid_request", "client_id and client_secret are required")
    keys = [f"client:{body.client_id}", f"addr:{address}"]
    _limited(ctx, keys)
    if (
        not ctx.api.local_auth_enabled
        and ctx.collaboration.user_by_client(body.client_id) is not None
    ):
        raise Problem(403, "local_auth_disabled", "sign in through the identity provider")
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
            body.refresh_token,
            now=ctx.clock(),
            ttl_s=api.refresh_token_ttl_s,
            local_auth_enabled=api.local_auth_enabled,
            oidc_issuer=api.oidc.issuer if api.oidc.enabled else "",
        )
        session = ctx.auth.refresh_session(
            fresh,
            now=ctx.clock(),
            oidc_issuer=api.oidc.issuer if api.oidc.enabled else "",
        )
    except AuthError as exc:
        _failed(ctx, keys)
        log.warning("api.refresh_failed", reason=exc.code)
        raise Problem(401, exc.code, exc.message) from exc
    now = ctx.clock()
    access_ttl, refresh_ttl = api.access_token_ttl_s, api.refresh_token_ttl_s
    if session is not None:
        remaining = max(1, int(session.expires_at - now))
        access_ttl, refresh_ttl = min(access_ttl, remaining), min(refresh_ttl, remaining)
    access, claims = mint_access(
        ctx.keys,
        client_id=client.id,
        capabilities=client.capabilities,
        ttl_s=access_ttl,
        now=now,
        session_id=None if session is None else session.id,
    )
    return TokenResponse(
        access_token=access,
        expires_in=access_ttl,
        refresh_token=fresh,
        refresh_expires_in=refresh_ttl,
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
                native_redirect_uris=list(settings.native_redirect_uris),
            )
    return AuthProviders(
        local=ctx.api.local_auth_enabled,
        oidc=oidc,
        oidc_session_max_age_s=None if provider is None else provider.config.session_max_age_s,
    )


@router.get("/providers", response_model=AuthProviders)
async def providers(ctx: ApiContext = Depends(get_ctx)) -> AuthProviders:  # noqa: B008
    """The sign-ins a signed-out client may offer. Needs no token. ``oidc``
    is null unless a provider is configured and its discovery answered."""
    return await ctx.call(_providers, ctx)


#: Account refusals from the store, answered as 403 with the store's code.
_ACCOUNT_REFUSALS = frozenset({"oidc_account_disabled", "oidc_not_provisioned"})


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
            link_verified_email=settings.link_verified_email,
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
    try:
        session = ctx.auth.create_oidc_session(
            client.id,
            issuer=identity.issuer,
            subject=identity.subject,
            provider_sid=identity.sid,
            identity_issued_at=identity.issued_at,
            now=ctx.clock(),
            ttl_s=settings.session_max_age_s,
        )
    except AuthError as exc:
        raise Problem(401, exc.code, exc.message) from exc
    return grant_tokens(ctx, client, family_id=None, session=session)


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
        or not provider.config.allows_redirect(body.redirect_uri)
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


@router.post(
    "/oidc/backchannel-logout",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/x-www-form-urlencoded": {
                    "schema": {
                        "type": "object",
                        "required": ["logout_token"],
                        "properties": {"logout_token": {"type": "string", "maxLength": 16384}},
                    }
                }
            },
        }
    },
)
async def backchannel_logout(
    request: Request,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
) -> Response:
    """An authenticated, signed provider event; no browser or bearer session needed."""
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/x-www-form-urlencoded"
    ):
        raise Problem(400, "invalid_logout_token", "a signed logout token is required")
    try:
        form = parse_qs((await request.body()).decode("utf-8"), max_num_fields=32)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Problem(400, "invalid_logout_token", "a signed logout token is required") from exc
    values = form.get("logout_token", [])
    if len(values) != 1 or len(values[0]) > 16384:
        raise Problem(400, "invalid_logout_token", "a signed logout token is required")
    provider = ctx.oidc
    if provider is None:
        raise Problem(400, "invalid_logout_token", "the sign-in provider is not configured")

    def apply() -> None:
        try:
            logout = provider.validate_logout_token(values[0])
        except OidcError as exc:
            status = 503 if exc.status == 503 else 400
            raise Problem(
                status, "invalid_logout_token", "the logout token could not be verified"
            ) from exc
        ctx.auth.provider_logout(
            issuer=logout.issuer,
            jti=logout.jti,
            subject=logout.subject,
            provider_sid=logout.sid,
            issued_at=logout.issued_at,
            now=ctx.clock(),
            replay_until=max(logout.replay_until, ctx.clock() + provider.config.session_max_age_s),
        )

    await ctx.call(apply)
    ctx.hub.notify()
    return Response(status_code=200, headers={"Cache-Control": "no-store"})


@router.post("/revoke", status_code=204)
async def revoke(
    request: Request,
    body: RevokeRequest | None = None,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
) -> None:
    """End this access token now (it would otherwise run to its expiry),
    and a refresh token's whole family when one is named."""
    now = ctx.clock()
    refresh = None if body is None else body.refresh_token
    if refresh:
        # Possession authorizes ending only this token's family. A browser
        # must be able to log out after its access token has expired. Unknown
        # tokens receive the same empty response, as OAuth revocation requires.
        await ctx.call(ctx.auth.revoke_refresh, refresh, now)
    auth: Authenticated | None = None
    try:
        auth = await current(request, ctx)
    except Problem:
        if not refresh:
            raise

    def apply() -> None:
        if auth is not None:
            claims = auth.claims
            ctx.auth.revoke_access(claims.jti, claims.client_id, claims.expires_at, now)

    await ctx.call(apply)
    ctx.hub.notify()
