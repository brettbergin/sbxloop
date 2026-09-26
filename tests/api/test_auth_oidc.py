"""Sign-in through an OpenID Connect provider, redeemed by the daemon.

The browser runs Authorization Code + PKCE and hands the code to
``POST /v1/auth/oidc/token``; the daemon, as the confidential client,
redeems it, validates the ID token, provisions the user on first sign-in
and answers with its own token pair. ``GET /v1/auth/providers`` tells a
signed-out client what it may offer. The provider here is a stub behind the
module's HTTP seam, signing with a local RSA key; only the redirect tests
open loopback servers, to drive the real transport.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError
from sqlalchemy import select, update

from sbxloop.api.auth import oidc
from sbxloop.config import ApiOidcConfig, Config, load_config
from sbxloop.daemon.controls.principal import ALL_CAPABILITIES, ROLE_CAPABILITIES
from sbxloop.db.api_models import ApiEventRow, ClientRow
from sbxloop.db.collaboration_models import LocalUserRow
from tests.api.conftest import Api, build

ISSUER = "https://idp.example.test/application/o/angie/"
CLIENT_ID = "angie"
SECRET = "oidc-client-secret-value-7f3a9c-0b1d2e3f4a5b6c7d"  # nosec B105 - test fixture
REDIRECT = "https://angie.example.test/auth/callback"
NONCE = "nonce-0123456789abcdef"
VERIFIER = "v" * 64
KID = "key-1"

OIDC: dict[str, Any] = {
    "enabled": True,
    "issuer": ISSUER,
    "client_id": CLIENT_ID,
    "redirect_uris": [REDIRECT],
}


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**public, "kid": kid, "use": "sig", "alg": "RS256"}


@dataclass
class FakeIdP:
    """An OpenID provider answering discovery, JWKS and the token endpoint."""

    clock: Any
    key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    discovery_issuer: str = ISSUER
    reachable: bool = True
    jwks_reachable: bool = True
    token_status: int = 200
    auth_methods: list[str] = field(default_factory=lambda: ["client_secret_basic"])
    requests: list[dict[str, Any]] = field(default_factory=list)
    #: What the next ID token says; set per test.
    claims: dict[str, Any] = field(default_factory=dict)
    header: dict[str, Any] = field(default_factory=lambda: {"kid": KID})
    #: Replaces the signed token outright when set.
    raw_id_token: str | None = None

    def identity(self, sub: str, **claims: Any) -> None:
        now = int(self.clock())
        self.claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": sub,
            "iat": now,
            "exp": now + 300,
            "nonce": NONCE,
            "email": f"{sub}@example.test",
            "email_verified": True,
            "preferred_username": sub,
            "name": f"Person {sub}",
            "groups": [],
            **claims,
        }

    def id_token(self) -> str:
        if self.raw_id_token is not None:
            return self.raw_id_token
        return jwt.encode(self.claims, self.key, algorithm="RS256", headers=self.header)

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        if not self.reachable:
            raise OSError("connection refused")
        base = ISSUER.rstrip("/")
        if url == f"{base}/.well-known/openid-configuration":
            document = {
                "issuer": self.discovery_issuer,
                "authorization_endpoint": "https://idp.example.test/application/o/authorize/",
                "token_endpoint": "https://idp.example.test/application/o/token/",
                "jwks_uri": f"{base}/jwks/",
                "end_session_endpoint": f"{base}/end-session/",
                "token_endpoint_auth_methods_supported": self.auth_methods,
                "id_token_signing_alg_values_supported": ["RS256"],
            }
            return 200, json.dumps(document).encode()
        if url == f"{base}/jwks/":
            if not self.jwks_reachable:
                raise OSError("connection refused")
            return 200, json.dumps({"keys": [_jwk(self.key, KID)]}).encode()
        if url == "https://idp.example.test/application/o/token/":
            if self.token_status != 200:
                return self.token_status, b'{"error": "invalid_grant"}'
            return 200, json.dumps(
                {"access_token": "idp-access", "token_type": "Bearer", "id_token": self.id_token()}
            ).encode()
        return 404, b"{}"

    def token_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["url"].endswith("/token/")]


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> FakeIdP:
    from tests.unit.test_daemon_loop import Clock

    fake = FakeIdP(clock=Clock())
    monkeypatch.setattr(oidc, "http_request", fake)
    monkeypatch.setenv("SBXLOOP_OIDC_CLIENT_SECRET", SECRET)
    return fake


def _serve(tmp_path: Path, idp: FakeIdP, **oidc_overrides: Any) -> Iterator[Api]:
    built = build(tmp_path, oidc={**OIDC, **oidc_overrides})
    # The stub signs with the API's own clock, so expiry is a matter of moving it.
    idp.clock = built.clock
    with built.client:
        yield built
    built.ctx.close()


@pytest.fixture
def served(tmp_path: Path, idp: FakeIdP) -> Iterator[Api]:
    yield from _serve(tmp_path, idp)


def _exchange(api: Api, **overrides: Any) -> Any:
    body = {
        "provider": "authentik",
        "code": "auth-code-1",
        "code_verifier": VERIFIER,
        "redirect_uri": REDIRECT,
        "nonce": NONCE,
        **overrides,
    }
    return api.client.post("/v1/auth/oidc/token", json=body)


def _sign_in(api: Api, idp: FakeIdP, sub: str, **claims: Any) -> dict[str, Any]:
    idp.identity(sub, **claims)
    response = _exchange(api)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _me(api: Api, tokens: dict[str, Any]) -> Any:
    return api.client.get(
        "/v1/users/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )


def _member(api: Api, tokens: dict[str, Any]) -> Any:
    member = api.ctx.collaboration.member_for_client(tokens["client_id"])
    assert member is not None
    return member


def _capabilities(api: Api, client_id: str) -> set[str]:
    with api.ctx.collaboration.dstore.read() as session:
        row = session.get(ClientRow, client_id)
        assert row is not None
        return set(json.loads(row.capabilities_json))


# -- the providers route -----------------------------------------------------------


def test_providers_offer_only_local_login_when_oidc_is_off(api: Api) -> None:
    response = api.client.get("/v1/auth/providers")

    assert response.status_code == 200
    assert response.json() == {
        "local": True,
        "oidc": None,
        "policy_version": 1,
        "oidc_session_max_age_s": None,
    }
    features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "auth.oidc" not in features


def test_providers_describe_the_configured_provider_without_a_token(
    served: Api, idp: FakeIdP
) -> None:
    response = served.client.get("/v1/auth/providers")

    assert response.status_code == 200
    assert response.json() == {
        "local": True,
        "policy_version": 1,
        "oidc_session_max_age_s": 28800,
        "oidc": {
            "id": "authentik",
            "label": "Authentik",
            "authorize_url": "https://idp.example.test/application/o/authorize/",
            "client_id": CLIENT_ID,
            "scopes": ["openid", "email", "profile"],
            "end_session_url": ISSUER + "end-session/",
            "native_redirect_uris": [],
        },
    }
    features = served.client.get("/v1/capabilities", headers=served.bearer()).json()["features"]
    assert "auth.oidc" in features
    assert "auth.oidc.native" not in features


def test_providers_offer_no_oidc_when_discovery_fails(served: Api, idp: FakeIdP) -> None:
    idp.reachable = False

    response = served.client.get("/v1/auth/providers")

    assert response.status_code == 200
    assert response.json() == {
        "local": True,
        "oidc": None,
        "policy_version": 1,
        "oidc_session_max_age_s": 28800,
    }


def test_a_failed_discovery_is_not_retried_on_every_request(served: Api, idp: FakeIdP) -> None:
    idp.reachable = False
    assert served.client.get("/v1/auth/providers").json()["oidc"] is None
    idp.reachable = True

    assert served.client.get("/v1/auth/providers").json()["oidc"] is None
    fetched = [r for r in idp.requests if r["url"].endswith("openid-configuration")]
    assert len(fetched) == 1

    served.clock.t += 31
    assert served.client.get("/v1/auth/providers").json()["oidc"] is not None


def test_a_discovery_document_for_another_issuer_is_not_trusted(served: Api, idp: FakeIdP) -> None:
    idp.discovery_issuer = "https://evil.example.test/"

    assert served.client.get("/v1/auth/providers").json()["oidc"] is None
    idp.identity("alice")
    refused = _exchange(served)
    assert refused.status_code == 503
    assert refused.json()["code"] == "oidc_unavailable"


# -- a successful exchange ---------------------------------------------------------


def test_the_first_person_to_sign_in_owns_the_workspace(served: Api, idp: FakeIdP) -> None:
    tokens = _sign_in(served, idp, "alice")

    assert tokens["token_type"] == "Bearer"
    assert tokens["refresh_token"].startswith("rt_")
    me = _me(served, tokens)
    assert me.status_code == 200, me.text
    assert me.json()["username"] == "alice"
    assert me.json()["email"] == "alice@example.test"
    assert me.json()["full_name"] == "Person alice"
    member = _member(served, tokens)
    assert member.role == "owner"
    assert member.user.auth_source == "oidc"
    assert _capabilities(served, tokens["client_id"]) == set(ALL_CAPABILITIES)

    # The daemon redeemed the code as a confidential client (basic auth).
    (request,) = idp.token_requests()
    form = urllib.parse.parse_qs((request["body"] or b"").decode())
    assert form == {
        "grant_type": ["authorization_code"],
        "code": ["auth-code-1"],
        "redirect_uri": [REDIRECT],
        "code_verifier": [VERIFIER],
    }
    expected = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
    assert request["headers"]["Authorization"] == f"Basic {expected}"

    # The refresh token rotates like any other.
    refreshed = served.client.post(
        "/v1/auth/token",
        json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
    )
    assert refreshed.status_code == 200, refreshed.text


def test_client_secret_post_is_used_only_when_basic_is_not_offered(
    served: Api, idp: FakeIdP
) -> None:
    idp.auth_methods = ["client_secret_post"]

    _sign_in(served, idp, "alice")

    (request,) = idp.token_requests()
    form = urllib.parse.parse_qs((request["body"] or b"").decode())
    assert form["client_id"] == [CLIENT_ID]
    assert form["client_secret"] == [SECRET]
    assert "Authorization" not in request["headers"]


def test_later_people_join_as_members_and_return_to_the_same_account(
    served: Api, idp: FakeIdP
) -> None:
    owner = _sign_in(served, idp, "alice")
    first = _sign_in(served, idp, "bob")
    again = _sign_in(served, idp, "bob", name="Robert", email="robert@example.test")

    bob = _member(served, first)
    assert bob.role == "member"
    assert _capabilities(served, first["client_id"]) == set(ROLE_CAPABILITIES["member"])
    assert again["client_id"] == first["client_id"]
    assert _me(served, again).json()["full_name"] == "Robert"
    assert _me(served, again).json()["email"] == "robert@example.test"
    assert {m.user.username for m in served.ctx.collaboration.list_members()} == {"alice", "bob"}
    assert _member(served, owner).role == "owner"


def test_a_taken_username_gets_a_numeric_suffix(served: Api, idp: FakeIdP) -> None:
    _sign_in(served, idp, "alice")
    other = _sign_in(served, idp, "alice-2nd", preferred_username="alice")

    assert _me(served, other).json()["username"] == "alice2"


def test_the_sign_in_is_audited_without_tokens_or_secrets(
    served: Api, idp: FakeIdP, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    tokens = _sign_in(served, idp, "alice")
    idp.identity("mallory", nonce="wrong")
    _exchange(served, code="auth-code-2")
    idp.token_status = 400
    _exchange(served, code="auth-code-3")

    with served.ctx.collaboration.dstore.read() as session:
        rows = session.scalars(select(ApiEventRow)).all()
        types = [row.type for row in rows]
        stored = " ".join(f"{row.actor_json} {row.data_json}" for row in rows)
    user_id = _member(served, tokens).user.id
    assert "collaboration.user.created" in types
    assert "auth.oidc.login" in types
    login = next(row for row in rows if row.type == "auth.oidc.login")
    assert json.loads(login.data_json) == {"user_id": user_id}
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (SECRET, "auth-code-1", "auth-code-2", "idp-access", NONCE, idp.id_token()):
        assert secret not in stored
        assert secret not in logged


# -- refused exchanges -------------------------------------------------------------


def _refused(api: Api, status: int, code: str, **overrides: Any) -> dict[str, Any]:
    response = _exchange(api, **overrides)
    assert response.status_code == status, response.text
    body = dict(response.json())
    assert body["code"] == code
    return body


def _no_users(api: Api) -> None:
    with api.ctx.collaboration.dstore.read() as session:
        assert session.scalars(select(LocalUserRow)).all() == []


def test_a_nonce_mismatch_is_refused(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice", nonce="another-nonce")
    body = _refused(served, 401, "oidc_exchange_failed")
    assert "nonce" not in body["detail"]
    _no_users(served)


def test_an_id_token_without_a_nonce_is_refused(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    del idp.claims["nonce"]
    _refused(served, 401, "oidc_exchange_failed")
    _no_users(served)


def test_an_id_token_from_another_issuer_is_refused(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice", iss="https://evil.example.test/")
    _refused(served, 401, "oidc_exchange_failed")
    _no_users(served)


def test_an_id_token_for_another_audience_is_refused(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice", aud="someone-else")
    _refused(served, 401, "oidc_exchange_failed")
    _no_users(served)


def test_an_expired_id_token_is_refused(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    served.clock.t += 300 + 61  # past exp and the 60 s leeway
    _refused(served, 401, "oidc_exchange_failed")
    _no_users(served)


def test_an_id_token_within_the_leeway_is_accepted(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    served.clock.t += 300 + 30
    assert _exchange(served).status_code == 200


def test_an_id_token_issued_in_the_future_is_refused(served: Api, idp: FakeIdP) -> None:
    now = int(served.clock())
    idp.identity("alice", iat=now + 600, exp=now + 900)
    _refused(served, 401, "oidc_exchange_failed")


def test_a_token_signed_by_another_key_is_refused(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    idp.raw_id_token = jwt.encode(idp.claims, impostor, algorithm="RS256", headers={"kid": KID})
    _refused(served, 401, "oidc_exchange_failed")
    _no_users(served)


def test_an_unsigned_token_is_refused(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    idp.raw_id_token = jwt.encode(idp.claims, None, algorithm="none")
    _refused(served, 401, "oidc_exchange_failed")
    _no_users(served)


def test_a_symmetric_token_keyed_with_the_client_secret_is_refused(
    served: Api, idp: FakeIdP
) -> None:
    idp.identity("alice")
    idp.raw_id_token = jwt.encode(idp.claims, SECRET, algorithm="HS256", headers={"kid": KID})
    _refused(served, 401, "oidc_exchange_failed")
    _no_users(served)


def test_a_token_without_a_subject_is_refused(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    del idp.claims["sub"]
    _refused(served, 401, "oidc_exchange_failed")
    _no_users(served)


def test_a_provider_error_is_refused_generically(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    idp.token_status = 400
    body = _refused(served, 401, "oidc_exchange_failed")
    assert "invalid_grant" not in body["detail"]


def test_an_unreachable_provider_is_reported_unavailable(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    idp.reachable = False
    _refused(served, 503, "oidc_unavailable")


def test_an_unreachable_key_set_is_reported_unavailable(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    idp.jwks_reachable = False
    _refused(served, 503, "oidc_unavailable")


def test_a_redirect_uri_outside_the_allowlist_is_refused_before_the_provider_is_called(
    served: Api, idp: FakeIdP
) -> None:
    idp.identity("alice")
    _refused(served, 400, "oidc_invalid_request", redirect_uri=REDIRECT + "/evil")
    _refused(served, 400, "oidc_invalid_request", redirect_uri=REDIRECT.upper())
    assert idp.token_requests() == []


# -- native-app redirect URIs (RFC 8252 section 7.1) --------------------------------

NATIVE = "com.example.app:/oauth2/callback"


@pytest.fixture
def native(tmp_path: Path, idp: FakeIdP) -> Iterator[Api]:
    yield from _serve(tmp_path, idp, native_redirect_uris=[NATIVE])


def test_a_configured_native_redirect_is_redeemed_with_the_provider(
    native: Api, idp: FakeIdP
) -> None:
    idp.identity("alice")

    response = _exchange(native, redirect_uri=NATIVE)

    assert response.status_code == 200, response.text
    (request,) = idp.token_requests()
    form = urllib.parse.parse_qs((request["body"] or b"").decode())
    assert form["redirect_uri"] == [NATIVE]


def test_the_web_redirect_still_works_beside_a_native_one(native: Api, idp: FakeIdP) -> None:
    assert _sign_in(native, idp, "alice")["token_type"] == "Bearer"


def test_a_native_redirect_that_is_not_configured_is_refused(native: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    for uri in (
        "com.example.other:/oauth2/callback",
        NATIVE + "/evil",
        "COM.EXAMPLE.APP:/oauth2/callback",
        "com.example.app://oauth2/callback",
    ):
        _refused(native, 400, "oidc_invalid_request", redirect_uri=uri)
    assert idp.token_requests() == []


def test_a_native_redirect_is_refused_when_none_is_configured(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    _refused(served, 400, "oidc_invalid_request", redirect_uri=NATIVE)
    assert idp.token_requests() == []


def test_providers_advertise_the_native_redirects_and_the_feature(native: Api) -> None:
    oidc_out = native.client.get("/v1/auth/providers").json()["oidc"]
    assert oidc_out["native_redirect_uris"] == [NATIVE]
    features = native.client.get("/v1/capabilities", headers=native.bearer()).json()["features"]
    assert "auth.oidc" in features
    assert "auth.oidc.native" in features


def test_native_redirects_are_not_advertised_while_oidc_is_off(tmp_path: Path) -> None:
    built = build(tmp_path, oidc={**OIDC, "enabled": False, "native_redirect_uris": [NATIVE]})
    with built.client:
        assert built.client.get("/v1/auth/providers").json()["oidc"] is None
        features = built.client.get("/v1/capabilities", headers=built.bearer()).json()["features"]
    built.ctx.close()
    assert "auth.oidc.native" not in features


def test_an_unknown_provider_is_refused(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    _refused(served, 400, "oidc_invalid_request", provider="google")
    assert idp.token_requests() == []


def test_the_exchange_is_refused_when_oidc_is_off(api: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    _refused(api, 400, "oidc_invalid_request")
    assert idp.requests == []


def test_repeated_failures_are_rate_limited(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice", nonce="wrong")
    for _ in range(10):
        assert _exchange(served).status_code == 401
    limited = _exchange(served)
    assert limited.status_code == 429
    assert limited.json()["code"] == "too_many_attempts"


# -- groups and roles --------------------------------------------------------------


@pytest.fixture
def grouped(tmp_path: Path, idp: FakeIdP) -> Iterator[Api]:
    yield from _serve(
        tmp_path,
        idp,
        owner_groups=["sbx-owners"],
        admin_groups=["sbx-admins"],
        allowed_groups=["sbx-owners", "sbx-admins", "sbx-users"],
    )


def test_people_outside_the_allowed_groups_are_refused(grouped: Api, idp: FakeIdP) -> None:
    idp.identity("eve", groups=["everyone"])
    _refused(grouped, 403, "oidc_not_allowed")
    idp.identity("eve")
    del idp.claims["groups"]
    _refused(grouped, 403, "oidc_not_allowed")
    _no_users(grouped)


def test_groups_decide_the_role_and_it_follows_them(grouped: Api, idp: FakeIdP) -> None:
    _sign_in(grouped, idp, "alice", groups=["sbx-owners"])
    bob = _sign_in(grouped, idp, "bob", groups=["sbx-admins", "sbx-users"])
    carol = _sign_in(grouped, idp, "carol", groups=["sbx-owners"])
    dave = _sign_in(grouped, idp, "dave", groups="sbx-users")

    assert _member(grouped, bob).role == "admin"
    assert _capabilities(grouped, bob["client_id"]) == set(ROLE_CAPABILITIES["admin"])
    assert _member(grouped, carol).role == "owner"
    assert _member(grouped, dave).role == "member"

    _sign_in(grouped, idp, "bob", groups=["sbx-users"])
    assert _member(grouped, bob).role == "member"
    assert _capabilities(grouped, bob["client_id"]) == set(ROLE_CAPABILITIES["member"])
    _sign_in(grouped, idp, "carol", groups=["sbx-users"])
    assert _member(grouped, carol).role == "member"


def test_the_last_owner_is_never_demoted_by_groups(grouped: Api, idp: FakeIdP) -> None:
    # The first person owns the workspace whatever their groups say.
    alice = _sign_in(grouped, idp, "alice", groups=["sbx-users"])
    assert _member(grouped, alice).role == "owner"

    again = _sign_in(grouped, idp, "alice", groups=["sbx-users"])

    assert _member(grouped, again).role == "owner"
    assert _capabilities(grouped, again["client_id"]) == set(ALL_CAPABILITIES)


def test_roles_are_left_alone_when_no_role_groups_are_configured(served: Api, idp: FakeIdP) -> None:
    _sign_in(served, idp, "alice")
    bob = _sign_in(served, idp, "bob")
    served.ctx.collaboration.set_role(_member(served, bob).user.id, "admin")

    _sign_in(served, idp, "bob", groups=["anything"])

    assert _member(served, bob).role == "admin"


def test_a_concurrent_first_sign_in_is_answered_with_a_retryable_conflict(
    served: Api, idp: FakeIdP, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    from sbxloop.api.collaboration import CollaborationStore

    def collide(self: Any, **_: Any) -> Any:
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(CollaborationStore, "_sign_in_external", collide)
    idp.identity("alice")
    _refused(served, 409, "oidc_account_conflict")


def test_a_removed_member_is_refused(served: Api, idp: FakeIdP) -> None:
    _sign_in(served, idp, "alice")
    bob = _sign_in(served, idp, "bob")
    assert served.ctx.collaboration.remove_member(_member(served, bob).user.id)

    idp.identity("bob")
    _refused(served, 403, "oidc_account_disabled")


def test_an_inactive_user_is_refused(served: Api, idp: FakeIdP) -> None:
    _sign_in(served, idp, "alice")
    bob = _sign_in(served, idp, "bob")
    with served.ctx.collaboration.dstore.transaction() as session:
        session.execute(
            update(LocalUserRow).where(LocalUserRow.client_id == bob["client_id"]).values(active=0)
        )

    idp.identity("bob")
    _refused(served, 403, "oidc_account_disabled")


def test_an_unknown_person_is_refused_without_auto_provisioning(
    tmp_path: Path, idp: FakeIdP
) -> None:
    for api in _serve(tmp_path, idp, auto_provision=False):
        idp.identity("alice")
        _refused(api, 403, "oidc_not_provisioned")
        _no_users(api)


# -- linking a local account -------------------------------------------------------

LOCAL = {
    "email": "alice@example.test",
    "username": "alice-local",
    "password": "correct horse battery staple",
}


@pytest.fixture
def linking(tmp_path: Path, idp: FakeIdP) -> Iterator[Api]:
    yield from _serve(tmp_path, idp, link_verified_email=True)


def _register_local(api: Api) -> dict[str, Any]:
    registered = api.client.post("/v1/auth/local/register", json=LOCAL)
    assert registered.status_code == 201, registered.text
    return dict(registered.json())


def _users(api: Api) -> dict[str, LocalUserRow]:
    with api.ctx.collaboration.dstore.read() as session:
        rows = session.scalars(select(LocalUserRow)).all()
        session.expunge_all()
        return {row.client_id: row for row in rows}


def _is_separate_account(api: Api, tokens: dict[str, Any], local: dict[str, Any]) -> None:
    """The sign-in got its own member account; the local one is untouched."""
    assert tokens["client_id"] != local["client_id"]
    users = _users(api)
    assert users[local["client_id"]].oidc_subject is None
    assert users[local["client_id"]].email == LOCAL["email"]
    # The clashing address stays with its holder; the new account gets one
    # that can never receive mail.
    assert users[tokens["client_id"]].email.endswith("@users.invalid")
    assert _member(api, tokens).role == "member"
    assert _member(api, {"client_id": local["client_id"]}).role == "owner"


def test_a_local_account_is_linked_by_a_verified_email(linking: Api, idp: FakeIdP) -> None:
    registered = _register_local(linking)

    tokens = _sign_in(linking, idp, "alice", email="Alice@Example.test")

    assert tokens["client_id"] == registered["client_id"]
    with linking.ctx.collaboration.dstore.read() as session:
        row = session.scalars(select(LocalUserRow)).one()
        assert (row.oidc_issuer, row.oidc_subject) == (ISSUER, "alice")
        assert row.auth_source == "local"
    # The password still works, and the next sign-in finds the same account.
    login = linking.client.post(
        "/v1/auth/local/login", json={"username": "alice-local", "password": LOCAL["password"]}
    )
    assert login.status_code == 200
    assert _sign_in(linking, idp, "alice")["client_id"] == tokens["client_id"]
    assert _member(linking, tokens).role == "owner"


def test_a_verified_email_links_nothing_unless_linking_is_enabled(
    served: Api, idp: FakeIdP
) -> None:
    local = _register_local(served)

    tokens = _sign_in(served, idp, "alice")

    _is_separate_account(served, tokens, local)
    assert _sign_in(served, idp, "alice")["client_id"] == tokens["client_id"]
    with served.ctx.collaboration.dstore.read() as session:
        types = set(session.scalars(select(ApiEventRow.type)).all())
    assert "auth.oidc.linked" not in types


def test_linking_is_off_by_default() -> None:
    assert ApiOidcConfig.model_validate(OIDC).link_verified_email is False


@pytest.mark.parametrize("verified", [False, None])
def test_an_unverified_email_gets_a_new_account_instead_of_a_link(
    linking: Api, idp: FakeIdP, verified: bool | None
) -> None:
    local = _register_local(linking)

    idp.identity("alice", email_verified=verified)
    if verified is None:
        del idp.claims["email_verified"]
    response = _exchange(linking)
    assert response.status_code == 200, response.text
    tokens = dict(response.json())

    _is_separate_account(linking, tokens, local)
    # The same person returns to that account, not to the local one.
    idp.identity("alice", email_verified=False)
    assert _exchange(linking).json()["client_id"] == tokens["client_id"]


def test_an_email_already_linked_to_another_identity_gets_a_new_account(
    linking: Api, idp: FakeIdP
) -> None:
    local = _register_local(linking)
    _sign_in(linking, idp, "alice")  # links the local account

    other = _sign_in(linking, idp, "mallory", email=LOCAL["email"])

    assert other["client_id"] != local["client_id"]
    users = _users(linking)
    assert users[local["client_id"]].oidc_subject == "alice"
    assert users[other["client_id"]].email.endswith("@users.invalid")
    assert _member(linking, other).role == "member"


def _invite(api: Api, role: str, email: str | None) -> str:
    """The raw token of an invite the owner issued, addressed to ``email`` or open."""
    store = api.ctx.collaboration
    owner = next(m.user.id for m in store.list_members() if m.role == "owner")
    _, raw = store.create_invite(role, email, created_by=owner, ttl_s=3600, now=api.clock())
    return raw


def _register_invited(api: Api, name: str, email: str, invite_token: str) -> dict[str, Any]:
    body = {
        "email": email,
        "username": name,
        "password": f"{name} has a long password",
        "invite_token": invite_token,
    }
    registered = api.client.post("/v1/auth/local/register", json=body)
    assert registered.status_code == 201, registered.text
    return dict(registered.json())


def _change_email(api: Api, tokens: dict[str, Any], email: str) -> None:
    response = api.client.patch(
        "/v1/users/me",
        json={"email": email},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert response.status_code == 200, response.text


def test_a_member_who_took_a_colleagues_email_is_not_linked_or_promoted(
    tmp_path: Path, idp: FakeIdP
) -> None:
    for api in _serve(tmp_path, idp, link_verified_email=True, admin_groups=["admins"]):
        _register_local(api)  # the owner
        mallory = _register_invited(
            api, "mallory", "mallory@example.test", _invite(api, "member", "mallory@example.test")
        )
        _change_email(api, mallory, "newadmin@example.test")

        # The new admin's first sign-in: the provider has verified the address.
        tokens = _sign_in(api, idp, "newadmin", groups=["admins"])
        again = _sign_in(api, idp, "newadmin", groups=["admins"])

        assert tokens["client_id"] != mallory["client_id"]
        assert again["client_id"] == tokens["client_id"]
        assert _users(api)[mallory["client_id"]].oidc_subject is None
        assert _member(api, mallory).role == "member"
        assert _member(api, tokens).role == "admin"


def test_an_address_chosen_at_an_open_invite_is_not_linkable(linking: Api, idp: FakeIdP) -> None:
    _register_local(linking)
    bob = _register_invited(linking, "bob", "bob@example.test", _invite(linking, "member", None))

    tokens = _sign_in(linking, idp, "bob")

    assert tokens["client_id"] != bob["client_id"]
    assert _users(linking)[bob["client_id"]].oidc_subject is None


def test_an_address_an_invite_was_sent_to_is_linkable(linking: Api, idp: FakeIdP) -> None:
    _register_local(linking)
    bob = _register_invited(
        linking, "bob", "Bob@Example.test", _invite(linking, "member", "bob@example.test")
    )

    tokens = _sign_in(linking, idp, "bob")

    assert tokens["client_id"] == bob["client_id"]
    assert _users(linking)[bob["client_id"]].oidc_subject == "bob"


def test_an_email_changed_by_its_holder_is_not_linkable(linking: Api, idp: FakeIdP) -> None:
    registered = _register_local(linking)
    _change_email(linking, registered, "alice@elsewhere.test")

    # The changed address is not trusted for a link...
    stranger = _sign_in(linking, idp, "stranger", email="alice@elsewhere.test")
    assert stranger["client_id"] != registered["client_id"]
    # ...and the address the account was registered with is held by nobody
    # now, so a sign-in with it gets an account of its own too.
    other = _sign_in(linking, idp, "other", email=LOCAL["email"])
    assert other["client_id"] != registered["client_id"]


def test_the_linking_sign_in_never_changes_the_role(tmp_path: Path, idp: FakeIdP) -> None:
    for api in _serve(tmp_path, idp, link_verified_email=True, admin_groups=["admins"]):
        _register_local(api)  # the owner
        bob = _register_invited(
            api, "bob", "bob@example.test", _invite(api, "member", "bob@example.test")
        )

        tokens = _sign_in(api, idp, "bob", groups=["admins"])

        assert tokens["client_id"] == bob["client_id"]
        assert _member(api, bob).role == "member"
        # The next sign-in follows the groups like any other.
        _sign_in(api, idp, "bob", groups=["admins"])
        assert _member(api, bob).role == "admin"


def test_an_unverified_new_email_still_provisions(served: Api, idp: FakeIdP) -> None:
    tokens = _sign_in(served, idp, "alice", email_verified=False)
    me = _me(served, tokens)
    assert me.status_code == 200, me.text
    # The claim is not trusted: the account gets an address that can never
    # receive mail, not the one the provider did not check.
    assert me.json()["email"].endswith("@users.invalid")


def test_an_unverified_email_never_replaces_the_stored_one(served: Api, idp: FakeIdP) -> None:
    tokens = _sign_in(served, idp, "alice")
    assert _me(served, tokens).json()["email"] == "alice@example.test"

    _sign_in(served, idp, "alice", email="alice@elsewhere.test", email_verified=False)
    assert _me(served, tokens).json()["email"] == "alice@example.test"

    # A verified change is still followed.
    _sign_in(served, idp, "alice", email="alice@elsewhere.test")
    assert _me(served, tokens).json()["email"] == "alice@elsewhere.test"


def test_an_unverified_claim_does_not_take_an_address_from_its_owner(
    served: Api, idp: FakeIdP
) -> None:
    _register_local(served)
    mallory = _sign_in(served, idp, "mallory", email="victim@example.test", email_verified=False)

    # The real person can still register with the address they were invited by.
    victim = _register_invited(
        served, "victim", "victim@example.test", _invite(served, "member", "victim@example.test")
    )

    assert _me(served, victim).json()["email"] == "victim@example.test"
    assert _users(served)[mallory["client_id"]].email.endswith("@users.invalid")


def test_a_person_without_an_email_still_provisions(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    del idp.claims["email"]
    response = _exchange(served)
    assert response.status_code == 200, response.text
    other = _sign_in(served, idp, "bob", email=None)
    assert _me(served, other).status_code == 200


# -- configuration -----------------------------------------------------------------


def test_existing_configuration_without_the_section_still_loads() -> None:
    config = Config.model_validate({"api": {"enabled": True}})
    assert config.api.oidc.enabled is False
    assert config.api.oidc.client_secret_env == "SBXLOOP_OIDC_CLIENT_SECRET"
    assert config.api.oidc.scopes == ["openid", "email", "profile"]
    assert config.api.oidc.algorithms == ["RS256", "ES256"]


def test_the_audience_defaults_to_the_client_id() -> None:
    assert ApiOidcConfig.model_validate(OIDC).expected_audience == CLIENT_ID
    custom = ApiOidcConfig.model_validate({**OIDC, "audience": "api://sbx"})
    assert custom.expected_audience == "api://sbx"


@pytest.mark.parametrize(
    "overrides",
    [
        {"issuer": None},
        {"client_id": ""},
        {"redirect_uris": []},
        {"issuer": "http://idp.example.test/"},
        {"redirect_uris": ["http://angie.example.test/auth/callback"]},
        {"redirect_uris": ["not a url"]},
        {"algorithms": ["HS256"]},
        {"algorithms": ["none"]},
        {"algorithms": []},
        {"scopes": ["email"]},
        {"default_role": "owner"},
        {"client_secret_env": "not a name"},
        {"client_secret_env": "SBXLOOP_OTHER_SECRET"},
    ],
)
def test_an_enabled_provider_must_be_complete_and_safe(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ApiOidcConfig.model_validate({**OIDC, **overrides})


@pytest.mark.parametrize(
    "uri",
    [
        "com.example.app:/oauth2/callback",
        "com.example.app://oauth2/callback",
        "net.example.my-app+beta:/cb",
        "  com.example.app:/oauth2/callback  ",
    ],
)
def test_a_reverse_dns_private_use_scheme_is_a_native_redirect(uri: str) -> None:
    config = ApiOidcConfig.model_validate({**OIDC, "native_redirect_uris": [uri]})
    assert config.native_redirect_uris == [uri.strip()]
    assert config.redirect_uris == [REDIRECT]


def test_native_redirects_default_to_none() -> None:
    assert ApiOidcConfig.model_validate(OIDC).native_redirect_uris == []


@pytest.mark.parametrize(
    ("uri", "reason"),
    [
        ("app:/cb", "reverse-DNS"),
        ("myapp://callback", "reverse-DNS"),
        ("https://angie.example.test/auth/callback", "private-use"),
        ("http://localhost:3000/cb", "private-use"),
        ("com.example.app:", "path"),
        ("com.example.app:/cb#frag", "fragment"),
        ("com.example.app:/c b", "whitespace"),
        ("com.example.app://user:pw@host/cb", "credential"),
        ("/oauth2/callback", "private-use"),
        ("1com.example:/cb", "private-use"),
        ("", "private-use"),
    ],
)
def test_a_malformed_native_redirect_is_refused_at_load(uri: str, reason: str) -> None:
    with pytest.raises(ValidationError) as caught:
        ApiOidcConfig.model_validate({**OIDC, "native_redirect_uris": [uri]})
    message = str(caught.value)
    assert "api.oidc.native_redirect_uris" in message
    assert reason in message


def test_native_redirects_do_not_relax_the_web_redirect_rules() -> None:
    with pytest.raises(ValidationError):
        ApiOidcConfig.model_validate(
            {**OIDC, "redirect_uris": ["com.example.app:/oauth2/callback"]}
        )


def test_plain_http_is_allowed_only_for_localhost() -> None:
    config = ApiOidcConfig.model_validate(
        {
            **OIDC,
            "issuer": "http://localhost:9000/application/o/angie/",
            "redirect_uris": ["http://localhost:3000/auth/callback", "http://127.0.0.1:3000/cb"],
        }
    )
    assert config.issuer == "http://localhost:9000/application/o/angie/"


def test_a_disabled_provider_needs_nothing() -> None:
    assert ApiOidcConfig.model_validate({"enabled": False}).issuer is None


def test_the_secret_variable_is_not_read_as_a_setting(tmp_path: Path) -> None:
    config = load_config(tmp_path, env={"SBXLOOP_OIDC_CLIENT_SECRET": SECRET})
    assert SECRET not in config.model_dump_json()


def test_discovery_is_cached(served: Api, idp: FakeIdP) -> None:
    served.client.get("/v1/auth/providers")
    _sign_in(served, idp, "alice")
    _sign_in(served, idp, "bob")
    discovery = [r for r in idp.requests if r["url"].endswith("openid-configuration")]
    jwks = [r for r in idp.requests if r["url"].endswith("/jwks/")]
    assert len(discovery) == 1
    assert len(jwks) == 1
    assert all(r["headers"].get("Authorization") is None for r in discovery + jwks)


# -- the provider transport never follows a redirect --------------------------------


class _Recorder:
    """A loopback HTTP server that answers each path from ``routes`` and
    records every request it receives."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], bytes]]) -> None:
        import http.server
        import threading

        self.routes = routes
        self.seen: list[dict[str, Any]] = []
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                recorder.seen.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": self.rfile.read(length) if length else b"",
                    }
                )
                status, headers, body = recorder.routes.get(self.path, (404, {}, b"{}"))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _answer
            do_POST = _answer

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _Recorder:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_the_transport_returns_a_redirect_instead_of_following_it(
    no_proxy: None, status: int
) -> None:
    with _Recorder({}) as elsewhere:
        target = f"{elsewhere.base}/capture"
        with _Recorder({"/token": (status, {"Location": target}, b"")}) as provider:
            got, _ = oidc._urllib_request(
                "POST",
                f"{provider.base}/token",
                headers={"Authorization": "Basic c2VjcmV0", "Content-Type": "text/plain"},
                body=b"client_secret=" + SECRET.encode(),
                timeout=5.0,
            )
    assert got == status
    assert elsewhere.seen == []


def _live_provider(provider: _Recorder, elsewhere: _Recorder, **routes: Any) -> None:
    issuer = f"{provider.base}/o/"
    moved = (302, {"Location": f"{elsewhere.base}/capture"}, b"")
    document = {
        "issuer": issuer,
        "authorization_endpoint": f"{provider.base}/authorize",
        "token_endpoint": f"{provider.base}/token",
        "jwks_uri": f"{provider.base}/jwks",
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
    }
    provider.routes.update(
        {
            "/o/.well-known/openid-configuration": (200, {}, json.dumps(document).encode()),
            "/jwks": (200, {}, json.dumps({"keys": []}).encode()),
            "/token": (200, {}, b"{}"),
        }
    )
    for path, redirected in routes.items():
        if redirected:
            provider.routes[path] = moved


def _live_config(provider: _Recorder) -> ApiOidcConfig:
    return ApiOidcConfig.model_validate(
        {
            **OIDC,
            "issuer": f"{provider.base}/o/",
            "redirect_uris": ["http://localhost:3000/auth/callback"],
            "request_timeout_s": 5.0,
        }
    )


def test_a_redirected_discovery_is_reported_unavailable(no_proxy: None) -> None:
    with _Recorder({}) as elsewhere, _Recorder({}) as provider:
        _live_provider(provider, elsewhere)
        provider.routes["/o/.well-known/openid-configuration"] = (
            302,
            {"Location": f"{elsewhere.base}/capture"},
            b"",
        )
        live = oidc.OidcProvider(_live_config(provider), clock=lambda: 0.0, env={})
        with pytest.raises(oidc.OidcUnavailable) as caught:
            live.discovery()
    assert caught.value.status == 503
    assert caught.value.code == "oidc_unavailable"
    assert elsewhere.seen == []


def test_a_redirected_key_set_is_reported_unavailable(no_proxy: None) -> None:
    with _Recorder({}) as elsewhere, _Recorder({}) as provider:
        _live_provider(provider, elsewhere, **{"/jwks": True})
        live = oidc.OidcProvider(_live_config(provider), clock=lambda: 0.0, env={})
        with pytest.raises(oidc.OidcUnavailable) as caught:
            live._key_set(live.discovery().jwks_uri, refresh=False)
    assert caught.value.code == "oidc_unavailable"
    assert elsewhere.seen == []


def test_a_redirected_token_endpoint_fails_without_replaying_the_secret(no_proxy: None) -> None:
    with _Recorder({}) as elsewhere, _Recorder({}) as provider:
        _live_provider(provider, elsewhere, **{"/token": True})
        env = {"SBXLOOP_OIDC_CLIENT_SECRET": SECRET}
        live = oidc.OidcProvider(_live_config(provider), clock=lambda: 0.0, env=env)
        with pytest.raises(oidc.OidcError) as caught:
            live.exchange(
                code="auth-code-1",
                code_verifier=VERIFIER,
                redirect_uri="http://localhost:3000/auth/callback",
                nonce=NONCE,
            )
    assert caught.value.status == 401
    assert caught.value.code == "oidc_exchange_failed"
    assert [r["path"] for r in provider.seen if r["method"] == "POST"] == ["/token"]
    assert elsewhere.seen == []


def test_a_redirect_from_the_provider_is_refused_through_the_api(served: Api, idp: FakeIdP) -> None:
    idp.identity("alice")
    idp.token_status = 302
    _refused(served, 401, "oidc_exchange_failed")
    assert len(idp.token_requests()) == 1


# -- the operator documentation ------------------------------------------------------


def _doc_section(path: Path, heading: str) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = lines.index(heading)
    level = len(heading.split(" ", 1)[0])
    fenced = False
    for end in range(start + 1, len(lines)):
        line = lines[end]
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#") and len(line.split(" ", 1)[0]) <= level:
            return "\n".join(lines[start:end])
    return "\n".join(lines[start:])


@pytest.mark.parametrize(
    ("doc", "heading"),
    [
        ("user-guide.md", "#### Sign in with an OIDC provider (Authentik)"),
        ("api.md", "### Sign-in through an OpenID Connect provider"),
    ],
)
def test_the_oidc_docs_speak_to_operators_only(doc: str, heading: str) -> None:
    root = Path(__file__).resolve().parents[2]
    section = _doc_section(root / "docs" / doc, heading).lower()
    for note in ("field-unverified", "unverified:", "todo", "fixme", "note to", "the lead"):
        assert note not in section, note
    assert "example.com" in section
