"""Client credentials become a short-lived access token and a single-use
refresh token; every failure mode is named, none leaks a secret."""

from __future__ import annotations

from pathlib import Path

import jwt
import pytest

from sbxloop.api.auth.keys import load_or_create, rotate
from sbxloop.api.auth.store import check_secret, hash_secret, parse_capabilities
from sbxloop.api.auth.tokens import LEEWAY_S, TokenError, mint_access, verify_access
from sbxloop.daemon.controls.principal import ALL_CAPABILITIES
from tests.api.conftest import Api


class TestSecrets:
    def test_a_secret_is_stored_only_as_its_verifier(self, api: Api) -> None:
        client, secret = api.register("ci")
        rows = api.auth.list_clients()
        assert [c.id for c in rows] == [client.id]
        # Nowhere in the database does the secret appear.
        with api.auth.sessions.read() as session:
            from sqlalchemy import text

            stored = session.execute(text("SELECT secret_hash FROM api_clients")).scalar_one()
        assert secret not in stored and stored.startswith("scrypt$")
        assert check_secret(secret, stored) and not check_secret(secret + "x", stored)

    def test_hashing_is_salted(self) -> None:
        assert hash_secret("s") != hash_secret("s")
        assert not check_secret("s", "not-a-hash")

    def test_capabilities_are_validated_by_name(self) -> None:
        assert parse_capabilities(["runs:read", "runs:read"]) == frozenset({"runs:read"})
        with pytest.raises(ValueError, match="unknown capabilities: bogus"):
            parse_capabilities(["bogus", "runs:read"])


class TestTokenGrant:
    def test_client_credentials_mint_a_pair(self, api: Api) -> None:
        pair = api.token(frozenset({"runs:read", "audit:read"}))
        assert pair["token_type"] == "Bearer" and pair["expires_in"] == 900
        assert pair["scope"] == "runs:read audit:read"
        assert pair["refresh_token"].startswith("rt_") and pair["refresh_expires_in"] == 604800
        claims = jwt.decode(pair["access_token"], options={"verify_signature": False})
        assert claims["iss"] == "sbxloop" and claims["aud"] == "sbxloop-api"
        assert claims["sub"] == pair["client_id"] and claims["workspace"] == "local"
        header = jwt.get_unverified_header(pair["access_token"])
        assert header["alg"] == "EdDSA" and header["kid"] == api.keys.current.kid

    def test_a_wrong_secret_or_unknown_client_answer_alike(self, api: Api) -> None:
        client, _secret = api.register()
        wrong = api.client.post(
            "/v1/auth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": client.id,
                "client_secret": "no",
            },
        )
        unknown = api.client.post(
            "/v1/auth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": "cli_nope",
                "client_secret": "no",
            },
        )
        for response in (wrong, unknown):
            assert response.status_code == 401
            assert response.headers["content-type"].startswith("application/problem+json")
            assert response.json()["code"] == "invalid_client"
            assert "no" not in response.json()["detail"].split()

    def test_failures_are_rate_limited_apart_from_work(self, api: Api) -> None:
        client, secret = api.register()
        bad = {"grant_type": "client_credentials", "client_id": client.id, "client_secret": "x"}
        for _ in range(10):
            assert api.client.post("/v1/auth/token", json=bad).status_code == 401
        locked = api.client.post("/v1/auth/token", json=bad)
        assert locked.status_code == 429 and locked.headers["Retry-After"]
        assert locked.json()["code"] == "too_many_attempts"
        # The right secret is refused too while locked out …
        good = {**bad, "client_secret": secret}
        assert api.client.post("/v1/auth/token", json=good).status_code == 429
        # … and works once the lockout passes.
        api.clock.t += 61
        assert api.client.post("/v1/auth/token", json=good).status_code == 200

    def test_a_revoked_client_cannot_mint(self, api: Api) -> None:
        client, secret = api.register()
        api.auth.revoke_client(client.id, api.clock())
        response = api.client.post(
            "/v1/auth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": client.id,
                "client_secret": secret,
            },
        )
        assert response.status_code == 401


class TestAccessTokens:
    def test_a_token_opens_the_door_its_scope_allows(self, api: Api) -> None:
        reader = api.bearer(frozenset({"runs:read"}))
        assert api.client.get("/v1/status", headers=reader).status_code == 200
        forbidden = api.client.get("/v1/operations", headers=reader)
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == "forbidden"
        assert forbidden.json()["capability"] == "audit:read"

    def test_no_token_is_401_with_a_challenge(self, api: Api) -> None:
        response = api.client.get("/v1/status")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith("Bearer")
        assert response.json()["code"] == "unauthenticated"
        assert api.client.get("/v1/status", headers={"Authorization": "Basic x"}).status_code == 401

    def test_expiry_is_judged_by_the_daemons_clock(self, api: Api) -> None:
        headers = api.bearer()
        assert api.client.get("/v1/status", headers=headers).status_code == 200
        api.clock.t += 900 + LEEWAY_S + 1
        expired = api.client.get("/v1/status", headers=headers)
        assert expired.status_code == 401 and expired.json()["code"] == "token_expired"

    def test_a_token_from_another_key_or_algorithm_is_refused(
        self, api: Api, tmp_path: Path
    ) -> None:
        other = load_or_create(type(api.ctx.config.paths)(tmp_path / "elsewhere"))
        token, _ = mint_access(
            other, client_id="cli_x", capabilities=ALL_CAPABILITIES, ttl_s=60, now=api.clock()
        )
        with pytest.raises(TokenError) as excinfo:
            verify_access(api.keys, token, now=api.clock())
        assert excinfo.value.code == "invalid_token"
        forged = jwt.encode(
            {"iss": "sbxloop", "aud": "sbxloop-api", "sub": "cli_x", "iat": 1, "exp": 2**31},
            "an-hmac-key-long-enough-for-sha256-so-pyjwt-does-not-warn",
            algorithm="HS256",
        )
        assert (
            api.client.get("/v1/status", headers={"Authorization": f"Bearer {forged}"}).status_code
            == 401
        )
        assert (
            api.client.get("/v1/status", headers={"Authorization": "Bearer nonsense"}).status_code
            == 401
        )

    def test_revoking_the_client_ends_live_tokens(self, api: Api) -> None:
        pair = api.token()
        headers = {"Authorization": f"Bearer {pair['access_token']}"}
        assert api.client.get("/v1/status", headers=headers).status_code == 200
        api.auth.revoke_client(pair["client_id"], api.clock())
        refused = api.client.get("/v1/status", headers=headers)
        assert refused.status_code == 401 and refused.json()["code"] == "client_revoked"

    def test_a_narrowed_grant_narrows_a_live_token(self, api: Api) -> None:
        """The token's scope is what the client still holds, not what it
        held when the token was minted."""
        pair = api.token(frozenset({"runs:read", "audit:read"}))
        headers = {"Authorization": f"Bearer {pair['access_token']}"}
        assert api.client.get("/v1/operations", headers=headers).status_code == 200
        with api.auth.sessions.transaction() as session:
            from sqlalchemy import text

            session.execute(text("UPDATE api_clients SET capabilities_json = '[\"runs:read\"]'"))
        assert api.client.get("/v1/operations", headers=headers).status_code == 403
        me = api.client.get("/v1/me", headers=headers).json()
        assert me["capabilities"] == ["runs:read"]

    def test_the_key_rotates_without_orphaning_live_tokens(self, api: Api) -> None:
        pair = api.token()
        old_kid = api.keys.current.kid
        rotated = rotate(api.ctx.config.paths)
        api.ctx.keys = rotated
        assert rotated.current.kid != old_kid and rotated.previous is not None
        assert rotated.previous.kid == old_kid
        headers = {"Authorization": f"Bearer {pair['access_token']}"}
        assert api.client.get("/v1/status", headers=headers).status_code == 200
        fresh = api.token()
        assert jwt.get_unverified_header(fresh["access_token"])["kid"] == rotated.current.kid
        # A second rotation forgets the first key: its tokens are gone.
        api.ctx.keys = rotate(api.ctx.config.paths)
        assert api.client.get("/v1/status", headers=headers).status_code == 401


class TestRefresh:
    def _refresh(self, api: Api, token: str) -> object:
        return api.client.post(
            "/v1/auth/token", json={"grant_type": "refresh_token", "refresh_token": token}
        )

    def test_rotation_issues_a_new_pair_and_retires_the_old(self, api: Api) -> None:
        first = api.token()
        second = self._refresh(api, first["refresh_token"])
        assert second.status_code == 200  # type: ignore[attr-defined]
        body = second.json()  # type: ignore[attr-defined]
        assert body["refresh_token"] != first["refresh_token"]
        assert body["client_id"] == first["client_id"]
        headers = {"Authorization": f"Bearer {body['access_token']}"}
        assert api.client.get("/v1/status", headers=headers).status_code == 200

    def test_reuse_revokes_the_whole_family(self, api: Api) -> None:
        first = api.token()
        second = self._refresh(api, first["refresh_token"]).json()  # type: ignore[attr-defined]
        replay = self._refresh(api, first["refresh_token"])
        assert replay.status_code == 401  # type: ignore[attr-defined]
        assert replay.json()["code"] == "refresh_reuse_detected"  # type: ignore[attr-defined]
        # The legitimate holder's newest token is dead too …
        dead = self._refresh(api, second["refresh_token"])
        assert dead.status_code == 401 and dead.json()["code"] == "invalid_grant"  # type: ignore[attr-defined]
        # … and a fresh client-credentials grant starts a new family.
        assert api.token()["refresh_token"]

    def test_an_expired_or_revoked_refresh_token_is_refused(self, api: Api) -> None:
        pair = api.token()
        api.clock.t += 604800 + 1
        assert self._refresh(api, pair["refresh_token"]).status_code == 401  # type: ignore[attr-defined]
        again = api.token()
        api.auth.revoke_refresh(again["refresh_token"], api.clock())
        assert self._refresh(api, again["refresh_token"]).status_code == 401  # type: ignore[attr-defined]
        assert self._refresh(api, "rt_unknown").json()["code"] == "invalid_grant"  # type: ignore[attr-defined]


class TestRevoke:
    def test_revoke_ends_the_access_token_and_a_named_family(self, api: Api) -> None:
        pair = api.token()
        headers = {"Authorization": f"Bearer {pair['access_token']}"}
        response = api.client.post(
            "/v1/auth/revoke", headers=headers, json={"refresh_token": pair["refresh_token"]}
        )
        assert response.status_code == 204
        refused = api.client.get("/v1/status", headers=headers)
        assert refused.status_code == 401 and refused.json()["code"] == "token_revoked"
        again = api.client.post(
            "/v1/auth/token",
            json={"grant_type": "refresh_token", "refresh_token": pair["refresh_token"]},
        )
        assert again.status_code == 401
        # The denylist is pruned once the token would have expired anyway.
        api.clock.t += 901
        assert api.auth.prune(api.clock()) >= 1
