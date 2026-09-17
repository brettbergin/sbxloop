"""Sign-in through an OpenID Connect provider, redeemed by the daemon.

The browser runs Authorization Code + PKCE and hands the code to
``POST /v1/auth/oidc/token``; the daemon, as the confidential client,
redeems it, validates the ID token, provisions the user on first sign-in
and answers with its own token pair. ``GET /v1/auth/providers`` tells a
signed-out client what it may offer. The provider here is a stub behind the
module's HTTP seam, signing with a local RSA key: no network is used.
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
    assert response.json() == {"local": True, "oidc": None}
    features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "auth.oidc" not in features


def test_providers_describe_the_configured_provider_without_a_token(
    served: Api, idp: FakeIdP
) -> None:
    response = served.client.get("/v1/auth/providers")

    assert response.status_code == 200
    assert response.json() == {
        "local": True,
        "oidc": {
            "id": "authentik",
            "label": "Authentik",
            "authorize_url": "https://idp.example.test/application/o/authorize/",
            "client_id": CLIENT_ID,
            "scopes": ["openid", "email", "profile"],
            "end_session_url": ISSUER + "end-session/",
        },
    }
    features = served.client.get("/v1/capabilities", headers=served.bearer()).json()["features"]
    assert "auth.oidc" in features


def test_providers_offer_no_oidc_when_discovery_fails(served: Api, idp: FakeIdP) -> None:
    idp.reachable = False

    response = served.client.get("/v1/auth/providers")

    assert response.status_code == 200
    assert response.json() == {"local": True, "oidc": None}


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


def test_an_unverified_new_email_still_provisions(served: Api, idp: FakeIdP) -> None:
    tokens = _sign_in(served, idp, "alice", email_verified=False)
    assert _me(served, tokens).json()["email"] == "alice@example.test"


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
