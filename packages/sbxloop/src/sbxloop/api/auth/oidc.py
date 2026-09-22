"""Sign-in through an OpenID Connect provider.

The browser client runs Authorization Code + PKCE against the provider and
hands the code to the daemon, which redeems it as the confidential client
(its secret never reaches the browser), validates the ID token and reads
who signed in. Discovery and the key set are fetched from the configured
issuer, cached, and refreshed on their own schedule; every call here
blocks, so the routes run it on the API executor.

Nothing in this module logs or raises a token, a code, a claim or the
client secret: failures carry a fixed reason code, and the route answers
with a generic sentence.
"""

from __future__ import annotations

import base64
import hmac
import json
import math
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import jwt

from sbxloop.config import ApiOidcConfig
from sbxloop.daemon.controls.principal import Role

#: Largest provider response read; discovery, a key set and a token
#: response are all far smaller.
MAX_RESPONSE_BYTES = 1 << 20
#: An ID token naming a key the cached set lacks refetches the set, but no
#: more often than this.
JWKS_REFETCH_MIN_S = 10.0
#: After a failed discovery, requests are answered from the failure this
#: long: an unreachable provider does not tie up a thread per request.
DISCOVERY_RETRY_S = 30.0
LOGOUT_MAX_AGE_S = 300
LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"


class HttpRequest(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]: ...


class OidcError(Exception):
    """A refused sign-in. ``reason`` is for the log; ``code`` for the client."""

    status = 401
    code = "oidc_exchange_failed"
    message = "the sign-in could not be completed; start it again"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OidcUnavailable(OidcError):
    status = 503
    code = "oidc_unavailable"
    message = "the identity provider cannot be reached; try again later"


class OidcNotAllowed(OidcError):
    status = 403
    code = "oidc_not_allowed"
    message = "this account is not allowed to sign in here"


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the answer is raised as the 3xx it is.

    Following one would replay the request, the client's credentials
    included, to a URL the configured provider did not name; a redirect
    is a provider error instead."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


#: Every provider call goes through this opener, which follows no redirect.
_OPENER = urllib.request.build_opener(_RefuseRedirects)


def _urllib_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, bytes]:
    """One HTTP exchange; a non-2xx answer, a redirect included, is
    returned, not raised or followed."""
    if urllib.parse.urlsplit(url).scheme not in ("https", "http"):
        raise OSError("unsupported URL scheme")
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(  # nosec B310 - http(s) only, checked above
            request, timeout=timeout
        ) as response:
            return int(response.status), bytes(response.read(MAX_RESPONSE_BYTES + 1))
    except urllib.error.HTTPError as exc:
        with exc:
            return int(exc.code), bytes(exc.read(MAX_RESPONSE_BYTES + 1))


#: The transport every provider call goes through; tests replace it.
http_request: HttpRequest = _urllib_request


@dataclass(frozen=True, slots=True)
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None
    token_auth_methods: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Identity:
    """Who the provider says signed in."""

    issuer: str
    subject: str
    username: str | None
    email: str | None
    email_verified: bool
    name: str | None
    groups: tuple[str, ...]
    sid: str | None = None
    issued_at: float = 0


@dataclass(frozen=True, slots=True)
class Logout:
    issuer: str
    subject: str | None
    sid: str | None
    jti: str
    issued_at: float
    replay_until: float


@dataclass(frozen=True, slots=True)
class _Key:
    alg: str | None
    jwk: jwt.PyJWK


def role_for_groups(config: ApiOidcConfig, groups: tuple[str, ...]) -> Role | None:
    """The role the provider's groups grant, or ``None`` when no role groups
    are configured (the workspace's own role assignments then stand)."""
    if not config.maps_roles:
        return None
    if set(groups) & set(config.owner_groups):
        return "owner"
    if set(groups) & set(config.admin_groups):
        return "admin"
    return config.default_role


def _string(value: Any) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _groups(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _json_object(body: bytes) -> dict[str, Any] | None:
    if len(body) > MAX_RESPONSE_BYTES:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _time(value: Any) -> float | None:
    """A NumericDate claim as a float; None when it is not one."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) else None
    return None


class OidcProvider:
    """One configured provider: discovery, keys and the code exchange."""

    def __init__(
        self,
        config: ApiOidcConfig,
        *,
        clock: Callable[[], float],
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.clock = clock
        self._env = env
        self._lock = threading.Lock()
        self._discovery: tuple[float, Discovery] | None = None
        self._discovery_failed_at: float | None = None
        self._jwks: tuple[float, tuple[_Key, ...]] | None = None

    # -- provider documents --------------------------------------------------------

    def _get(self, url: str, what: str) -> dict[str, Any]:
        try:
            status, body = http_request(
                "GET",
                url,
                headers={"Accept": "application/json"},
                body=None,
                timeout=self.config.request_timeout_s,
            )
        except (OSError, ValueError) as exc:
            raise OidcUnavailable(f"{what}_unreachable") from exc
        if status != 200:
            raise OidcUnavailable(f"{what}_status_{status}")
        data = _json_object(body)
        if data is None:
            raise OidcUnavailable(f"{what}_malformed")
        return data

    def discovery(self) -> Discovery:
        """The provider's metadata, cached for ``discovery_cache_s``."""
        with self._lock:
            now = self.clock()
            cached = self._discovery
            if cached is not None and now - cached[0] < self.config.discovery_cache_s:
                return cached[1]
            failed = self._discovery_failed_at
            if failed is not None and now - failed < DISCOVERY_RETRY_S:
                raise OidcUnavailable("discovery_backoff")
            try:
                found = self._fetch_discovery()
            except OidcUnavailable:
                self._discovery_failed_at = now
                raise
            self._discovery_failed_at = None
            self._discovery = (now, found)
            return found

    def _fetch_discovery(self) -> Discovery:
        issuer = self.config.issuer or ""
        data = self._get(issuer.rstrip("/") + "/.well-known/openid-configuration", "discovery")
        # The document is trusted only for the issuer it was asked about.
        if data.get("issuer") != issuer:
            raise OidcUnavailable("discovery_issuer_mismatch")
        # Plain http only for an issuer that is itself plain http (localhost):
        # the token endpoint is sent the client secret.
        schemes = ("https://",) if issuer.startswith("https://") else ("https://", "http://")
        endpoints: dict[str, str] = {}
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = data.get(key)
            if not isinstance(value, str) or not value.startswith(schemes):
                raise OidcUnavailable(f"discovery_missing_{key}")
            endpoints[key] = value
        end_session = data.get("end_session_endpoint")
        methods = data.get("token_endpoint_auth_methods_supported")
        return Discovery(
            issuer=issuer,
            authorization_endpoint=endpoints["authorization_endpoint"],
            token_endpoint=endpoints["token_endpoint"],
            jwks_uri=endpoints["jwks_uri"],
            end_session_endpoint=end_session if isinstance(end_session, str) else None,
            # The specification's default when the provider says nothing.
            token_auth_methods=(
                tuple(m for m in methods if isinstance(m, str))
                if isinstance(methods, list)
                else ("client_secret_basic",)
            ),
        )

    def _key_set(self, jwks_uri: str, *, refresh: bool) -> tuple[_Key, ...]:
        with self._lock:
            now = self.clock()
            cached = self._jwks
            if cached is not None:
                age = now - cached[0]
                if age < self.config.jwks_cache_s and not (refresh and age >= JWKS_REFETCH_MIN_S):
                    return cached[1]
            data = self._get(jwks_uri, "jwks")
            raw_keys = data.get("keys")
            if not isinstance(raw_keys, list):
                raise OidcUnavailable("jwks_malformed")
            keys: list[_Key] = []
            for raw in raw_keys:
                if not isinstance(raw, dict) or raw.get("use", "sig") != "sig":
                    continue
                try:
                    keys.append(_Key(alg=raw.get("alg"), jwk=jwt.PyJWK(raw)))
                except jwt.PyJWTError:
                    continue  # a key type this install cannot use
            self._jwks = (now, tuple(keys))
            return self._jwks[1]

    # -- the exchange --------------------------------------------------------------

    def _secret(self) -> str:
        env = os.environ if self._env is None else self._env
        secret = env.get(self.config.client_secret_env, "")
        if not secret:
            raise OidcUnavailable("client_secret_missing")
        return secret

    def _redeem(self, discovery: Discovery, code: str, redirect_uri: str, verifier: str) -> str:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        client_id, secret = self.config.client_id, self._secret()
        methods = discovery.token_auth_methods
        if "client_secret_basic" in methods or "client_secret_post" not in methods:
            # RFC 6749 section 2.3.1: each part form-encoded before joining.
            pair = f"{urllib.parse.quote(client_id, safe='')}:{urllib.parse.quote(secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        else:
            form["client_id"] = client_id
            form["client_secret"] = secret
        try:
            status, raw = http_request(
                "POST",
                discovery.token_endpoint,
                headers=headers,
                body=urllib.parse.urlencode(form).encode(),
                timeout=self.config.request_timeout_s,
            )
        except (OSError, ValueError) as exc:
            raise OidcUnavailable("token_endpoint_unreachable") from exc
        if status >= 500:
            raise OidcUnavailable(f"token_endpoint_status_{status}")
        if status != 200:
            raise OidcError(f"token_endpoint_status_{status}")
        data = _json_object(raw)
        id_token = None if data is None else data.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OidcError("token_response_without_id_token")
        return id_token

    def _signing_key(self, discovery: Discovery, kid: Any, algorithm: str) -> jwt.PyJWK:
        for refresh in (False, True):
            candidates = [
                key
                for key in self._key_set(discovery.jwks_uri, refresh=refresh)
                if (kid is None or key.jwk.key_id == kid) and key.alg in (None, algorithm)
            ]
            if len(candidates) == 1:
                return candidates[0].jwk
            if len(candidates) > 1:
                raise OidcError("id_token_key_ambiguous")
        raise OidcError("id_token_key_unknown")

    def validate_id_token(self, discovery: Discovery, token: str, nonce: str) -> dict[str, Any]:
        """The ID token's claims once its signature, issuer, audience, times
        and nonce all check out."""
        config = self.config
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OidcError("id_token_malformed") from exc
        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or algorithm not in config.algorithms:
            raise OidcError("id_token_algorithm_refused")
        key = self._signing_key(discovery, header.get("kid"), algorithm)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=[algorithm],
                audience=config.expected_audience,
                issuer=discovery.issuer,
                options={
                    # Times are checked below, against the daemon's clock.
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "require": ["iss", "aud", "sub", "exp", "iat"],
                },
            )
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise OidcError("id_token_invalid") from exc
        now, leeway = self.clock(), config.leeway_s
        exp, iat, nbf = _time(claims.get("exp")), _time(claims.get("iat")), _time(claims.get("nbf"))
        if exp is None or iat is None or (nbf is None and claims.get("nbf") is not None):
            raise OidcError("id_token_times_malformed")
        if exp + leeway <= now:
            raise OidcError("id_token_expired")
        if iat - leeway > now:
            raise OidcError("id_token_issued_in_future")
        if nbf is not None and nbf - leeway > now:
            raise OidcError("id_token_not_yet_valid")
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        azp = claims.get("azp")
        if (azp is not None or len(audiences) > 1) and azp != config.client_id:
            raise OidcError("id_token_authorized_party")
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(
            token_nonce.encode(), nonce.encode()
        ):
            raise OidcError("id_token_nonce_mismatch")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise OidcError("id_token_without_subject")
        return claims

    def validate_logout_token(self, token: str) -> Logout:
        """Verify an OIDC Back-Channel Logout 1.0 token before touching any session.

        The original standard permits no exp (as used by Authentik); a five
        minute iat window always bounds acceptance. When exp exists it is
        checked as well. Replay ids remain durable for the entire window.
        """
        discovery = self.discovery()
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OidcError("logout_token_malformed") from exc
        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or algorithm not in self.config.algorithms:
            raise OidcError("logout_token_algorithm_refused")
        if header.get("typ") not in (None, "JWT", "logout+jwt"):
            raise OidcError("logout_token_type_refused")
        key = self._signing_key(discovery, header.get("kid"), algorithm)
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],
                audience=self.config.client_id,
                issuer=discovery.issuer,
                options={
                    "require": ["iss", "aud", "iat", "jti", "events"],
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except (jwt.PyJWTError, ValueError, TypeError) as exc:
            raise OidcError("logout_token_invalid") from exc
        now, leeway = self.clock(), self.config.leeway_s
        issued = _time(claims.get("iat"))
        if issued is None or issued > now + leeway or issued + LOGOUT_MAX_AGE_S + leeway <= now:
            raise OidcError("logout_token_issued_at")
        for field, earliest in (("exp", False), ("nbf", True)):
            if field not in claims:
                continue
            value = _time(claims[field])
            if value is None or (value - leeway > now if earliest else value + leeway <= now):
                raise OidcError("logout_token_time_invalid")
        if "nonce" in claims:
            raise OidcError("logout_token_nonce_forbidden")
        events = claims["events"]
        if not isinstance(events, dict) or not isinstance(events.get(LOGOUT_EVENT), dict):
            raise OidcError("logout_token_event_invalid")
        for field in ("sub", "sid", "jti"):
            if field in claims and (
                not isinstance(claims[field], str) or not claims[field].strip()
            ):
                raise OidcError("logout_token_identifier_invalid")
        subject, sid = claims.get("sub"), claims.get("sid")
        if subject is None and sid is None:
            raise OidcError("logout_token_without_session")
        # An explicit authorized party must be this client; multiple audiences
        # are allowed by the logout standard and do not require azp.
        if "azp" in claims and claims["azp"] != self.config.client_id:
            raise OidcError("logout_token_authorized_party")
        return Logout(
            discovery.issuer,
            subject,
            sid,
            claims["jti"],
            issued,
            issued + LOGOUT_MAX_AGE_S + leeway,
        )

    def exchange(self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> Identity:
        """Redeem ``code`` and return who signed in; raises :class:`OidcError`."""
        config = self.config
        if not nonce:
            raise OidcError("nonce_missing")
        discovery = self.discovery()
        token = self._redeem(discovery, code, redirect_uri, code_verifier)
        claims = self.validate_id_token(discovery, token, nonce)
        groups = _groups(claims.get(config.groups_claim))
        if config.allowed_groups and not set(groups) & set(config.allowed_groups):
            raise OidcNotAllowed("not_in_allowed_groups")
        sid = claims.get("sid")
        if sid is not None and (not isinstance(sid, str) or not sid.strip()):
            raise OidcError("id_token_session_invalid")
        return Identity(
            issuer=discovery.issuer,
            subject=str(claims["sub"]),
            username=_string(claims.get(config.username_claim)),
            email=_string(claims.get(config.email_claim)),
            email_verified=claims.get("email_verified") is True,
            name=_string(claims.get(config.name_claim)),
            groups=groups,
            sid=sid,
            issued_at=float(claims["iat"]),
        )


__all__ = [
    "Discovery",
    "HttpRequest",
    "Identity",
    "OidcError",
    "OidcNotAllowed",
    "OidcProvider",
    "OidcUnavailable",
    "http_request",
    "role_for_groups",
]
