"""Access tokens: short-lived Ed25519-signed JWTs.

Claims are the standard ones plus ``scope`` (the capabilities, space
separated) and ``workspace``. The algorithm list is exactly ``EdDSA``: a
token claiming another is refused before its signature is looked at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt

from sbxloop.api.auth.keys import SigningKeys
from sbxloop.daemon.controls.principal import CAPABILITIES, WORKSPACE_ID, Capability
from sbxloop.ids import _token

ISSUER = "sbxloop"
AUDIENCE = "sbxloop-api"
ALGORITHM = "EdDSA"
#: Clock skew tolerated on ``exp``/``iat``: the issuer and verifier are one
#: host, so this only covers a proxy that buffered the request.
LEEWAY_S = 30


class TokenError(Exception):
    """A token that cannot be accepted; ``code`` is the problem code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AccessClaims:
    client_id: str
    jti: str
    scope: frozenset[Capability]
    issued_at: float
    expires_at: float
    kid: str
    workspace_id: str


def scope_from(capabilities: frozenset[Capability]) -> str:
    return " ".join(cap for cap in CAPABILITIES if cap in capabilities)


def parse_scope(text: str) -> frozenset[Capability]:
    present = set(text.split())
    return frozenset(cap for cap in CAPABILITIES if cap in present)


def mint_access(
    keys: SigningKeys,
    *,
    client_id: str,
    capabilities: frozenset[Capability],
    ttl_s: int,
    now: float,
) -> tuple[str, AccessClaims]:
    if keys.current.private is None:
        raise TokenError("no_signing_key", "the current signing key has no private half")
    claims = AccessClaims(
        client_id=client_id,
        jti=_token(16),
        scope=capabilities,
        issued_at=now,
        expires_at=now + ttl_s,
        kid=keys.current.kid,
        workspace_id=WORKSPACE_ID,
    )
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": client_id,
        "iat": int(now),
        "exp": int(claims.expires_at),
        "jti": claims.jti,
        "scope": scope_from(capabilities),
        "workspace": WORKSPACE_ID,
    }
    token = jwt.encode(
        payload, keys.current.private, algorithm=ALGORITHM, headers={"kid": keys.current.kid}
    )
    return token, claims


def verify_access(keys: SigningKeys, token: str, *, now: float) -> AccessClaims:
    """The claims of a token this installation minted and that has not
    expired; :class:`TokenError` names why otherwise."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise TokenError("invalid_token", "not a token this installation minted") from exc
    if header.get("alg") != ALGORITHM:
        raise TokenError("invalid_token", "the token's algorithm is not accepted")
    key = keys.verifier(header.get("kid"))
    if key is None:
        raise TokenError("invalid_token", "the token was signed by a key this installation lacks")
    try:
        payload = jwt.decode(
            token,
            key.public,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            # Expiry is judged below against the caller's clock — the
            # daemon's, which a test may hold still — not the wall clock.
            options={
                "require": ["exp", "iat", "sub", "jti"],
                "verify_exp": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise TokenError("invalid_token", "the access token is not valid") from exc
    try:
        expires_at, issued_at = float(payload["exp"]), float(payload["iat"])
    except (TypeError, ValueError) as exc:
        raise TokenError("invalid_token", "the access token is not valid") from exc
    if expires_at + LEEWAY_S < now:
        raise TokenError("token_expired", "the access token has expired")
    if issued_at - LEEWAY_S > now:
        raise TokenError("invalid_token", "the access token is from the future")
    return AccessClaims(
        client_id=str(payload["sub"]),
        jti=str(payload["jti"]),
        scope=parse_scope(str(payload.get("scope", ""))),
        issued_at=float(payload["iat"]),
        expires_at=float(payload["exp"]),
        kid=str(header.get("kid")),
        workspace_id=str(payload.get("workspace", WORKSPACE_ID)),
    )
