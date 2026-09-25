"""Human sign-in policy and provider-bound, revocable application sessions."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
from sqlalchemy import delete, update

from sbxloop.api.auth import oidc
from sbxloop.api.auth.store import ApiAuthStore
from sbxloop.api.auth.tokens import mint_access
from sbxloop.config import Config
from sbxloop.db.api_models import OidcSessionRow
from sbxloop.db.collaboration_models import LocalUserRow
from tests.api.conftest import Api, build
from tests.api.test_auth_oidc import (
    CLIENT_ID,
    ISSUER,
    KID,
    LOCAL,
    OIDC,
    SECRET,
    FakeIdP,
    _exchange,
    _me,
    _register_local,
    _sign_in,
)
from tests.unit.test_daemon_loop import Clock

LOGOUT = "/v1/auth/oidc/backchannel-logout"
EVENT = "http://schemas.openid.net/event/backchannel-logout"


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> FakeIdP:
    fake = FakeIdP(clock=Clock())
    monkeypatch.setattr(oidc, "http_request", fake)
    monkeypatch.setenv("SBXLOOP_OIDC_CLIENT_SECRET", SECRET)
    return fake


@pytest.fixture
def served(tmp_path: Path, idp: FakeIdP) -> Iterator[Api]:
    built = build(tmp_path, oidc={**OIDC, "session_max_age_s": 600})
    idp.clock = built.clock
    with built.client:
        yield built
    built.ctx.close()


def _refresh(api: Api, tokens: dict[str, Any]) -> Any:
    return api.client.post(
        "/v1/auth/token",
        json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
    )


def _logout_token(api: Api, idp: FakeIdP, **claims: Any) -> str:
    payload = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "iat": int(api.clock()),
        "exp": int(api.clock()) + 300,
        "jti": "logout-1",
        "sub": "alice",
        "events": {EVENT: {}},
        **claims,
    }
    return jwt.encode(payload, idp.key, algorithm="RS256", headers={"kid": KID})


def _logout(api: Api, token: str) -> Any:
    return api.client.post(LOGOUT, data={"logout_token": token})


def test_sso_only_closes_every_human_password_grant_and_existing_session(
    served: Api,
    idp: FakeIdP,
) -> None:
    local = _register_local(served)
    machine = served.token()
    served.ctx.api.local_auth_enabled = False
    assert served.client.get("/v1/auth/providers").json()["local"] is False
    assert served.client.post("/v1/auth/local/register", json=LOCAL).status_code == 403
    assert (
        served.client.post(
            "/v1/auth/local/login",
            json={"username": LOCAL["username"], "password": LOCAL["password"]},
        ).status_code
        == 403
    )
    assert (
        served.client.post(
            "/v1/auth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": local["client_id"],
                "client_secret": LOCAL["password"],
            },
        ).status_code
        == 403
    )
    assert _me(served, local).status_code == 401
    assert _refresh(served, local).status_code == 401
    assert _refresh(served, machine).status_code == 200
    signed_in = _sign_in(served, idp, "alice")
    assert _me(served, signed_in).status_code == 200
    assert _refresh(served, signed_in).status_code == 200


def test_provider_outage_never_reenables_local_auth(served: Api, idp: FakeIdP) -> None:
    served.ctx.api.local_auth_enabled = False
    idp.reachable = False
    assert served.client.get("/v1/auth/providers").json() == {
        "local": False,
        "oidc": None,
        "policy_version": 1,
        "assistant_name": "Angie",
        "oidc_session_max_age_s": 600,
    }


def test_local_sign_in_remains_the_compatibility_default() -> None:
    assert Config.model_validate({}).api.local_auth_enabled is True


def test_oidc_session_expires_absolutely_across_rotation(served: Api, idp: FakeIdP) -> None:
    tokens = _sign_in(served, idp, "alice")
    served.clock.t += 500
    rotated = _refresh(served, tokens)
    assert rotated.status_code == 200
    assert rotated.json()["expires_in"] <= 100
    assert rotated.json()["refresh_expires_in"] <= 100
    served.clock.t += 101
    assert _me(served, rotated.json()).status_code == 401
    assert _refresh(served, rotated.json()).status_code == 401


def test_provider_logout_revokes_access_and_rotated_refresh_only_for_matching_subject(
    served: Api,
    idp: FakeIdP,
) -> None:
    alice = _sign_in(served, idp, "alice", sid="alice-session")
    bob = _sign_in(served, idp, "bob", sid="bob-session")
    rotated = _refresh(served, alice).json()
    response = _logout(served, _logout_token(served, idp))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert _me(served, alice).status_code == 401
    assert _me(served, rotated).status_code == 401
    assert _refresh(served, rotated).status_code == 401
    assert _me(served, bob).status_code == 200
    assert _refresh(served, bob).status_code == 200


def test_sid_logout_matches_both_identifiers_and_does_not_logout_new_sessions_on_replay(
    served: Api,
    idp: FakeIdP,
) -> None:
    first = _sign_in(served, idp, "alice", sid="first")
    second = _sign_in(served, idp, "alice", sid="second")
    assert _logout(served, _logout_token(served, idp, sid="first", sub="bob")).status_code == 200
    assert _me(served, first).status_code == 200
    token = _logout_token(served, idp, jti="logout-2", sid="first")
    assert _logout(served, token).status_code == 200
    assert _me(served, first).status_code == 401
    assert _me(served, second).status_code == 200
    served.clock.t += 1
    again = _sign_in(served, idp, "alice", sid="first")
    assert _logout(served, token).status_code == 200
    assert _me(served, again).status_code == 200


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://attacker.example"},
        {"aud": "other-client"},
        {"iat": 1},
        {"iat": 2_000_000},
        {"iat": float("nan")},
        {"exp": 1},
        {"events": {}},
        {"events": {EVENT: False}},
        {"nonce": "forbidden"},
        {"sub": None},
        {"sid": 123},
        {"jti": ""},
    ],
)
def test_invalid_signed_logout_never_changes_sessions(
    served: Api,
    idp: FakeIdP,
    changes: dict[str, Any],
) -> None:
    tokens = _sign_in(served, idp, "alice")
    assert _logout(served, _logout_token(served, idp, **changes)).status_code == 400
    assert _me(served, tokens).status_code == 200
    assert _refresh(served, tokens).status_code == 200


def test_unsigned_logout_is_rejected(served: Api, idp: FakeIdP) -> None:
    tokens = _sign_in(served, idp, "alice")
    unsigned = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "alice",
            "aud": CLIENT_ID,
            "iat": served.clock(),
            "events": {EVENT: {}},
            "jti": "x",
        },
        key="",
        algorithm="none",
    )
    assert _logout(served, unsigned).status_code == 400
    assert _me(served, tokens).status_code == 200


def test_authentik_logout_without_exp_is_bounded_by_iat(served: Api, idp: FakeIdP) -> None:
    tokens = _sign_in(served, idp, "alice", sid="session")
    claims = jwt.decode(_logout_token(served, idp), options={"verify_signature": False})
    del claims["exp"]
    del claims["sub"]
    claims["sid"] = "session"
    token = jwt.encode(claims, idp.key, algorithm="RS256", headers={"kid": KID})
    assert _logout(served, token).status_code == 200
    assert _me(served, tokens).status_code == 401


def test_disabled_user_cannot_refresh_even_with_active_client(served: Api, idp: FakeIdP) -> None:
    tokens = _sign_in(served, idp, "alice")
    with served.ctx.collaboration.dstore.transaction() as session:
        session.execute(update(LocalUserRow).values(active=0))
    assert _refresh(served, tokens).status_code == 401


def test_sso_only_rejects_legacy_human_access_without_session_provenance(
    served: Api,
    idp: FakeIdP,
) -> None:
    signed_in = _sign_in(served, idp, "alice")
    client = served.auth.get_client(signed_in["client_id"])
    assert client is not None
    token, _ = mint_access(
        served.keys,
        client_id=client.id,
        capabilities=client.capabilities,
        ttl_s=900,
        now=served.clock(),
    )
    served.ctx.api.local_auth_enabled = False
    assert _me(served, {"access_token": token}).status_code == 401


@pytest.mark.parametrize("remove_record", [False, True])
def test_refresh_cannot_escape_session_revocation_or_pruning_during_rotation(
    served: Api,
    idp: FakeIdP,
    monkeypatch: pytest.MonkeyPatch,
    remove_record: bool,
) -> None:
    tokens = _sign_in(served, idp, "alice")
    rotate = served.auth.rotate_refresh

    def rotate_then_invalidate(*args: Any, **kwargs: Any) -> Any:
        result = rotate(*args, **kwargs)
        with served.auth.sessions.transaction() as session:
            session.execute(
                delete(OidcSessionRow)
                if remove_record
                else update(OidcSessionRow).values(revoked_at=served.clock())
            )
        return result

    monkeypatch.setattr(served.auth, "rotate_refresh", rotate_then_invalidate)
    response = _refresh(served, tokens)
    assert response.status_code == 401
    assert "access_token" not in response.json()
    assert _me(served, tokens).status_code == 401


def test_logout_replay_deduplication_survives_store_recreation(served: Api, idp: FakeIdP) -> None:
    original = _sign_in(served, idp, "alice")
    event = _logout_token(served, idp)
    assert _logout(served, event).status_code == 200
    assert _me(served, original).status_code == 401
    served.ctx.auth = ApiAuthStore(served.auth.sessions)
    served.clock.t += 1
    latest = _sign_in(served, idp, "alice")
    assert _logout(served, event).status_code == 200
    assert _me(served, latest).status_code == 200


def test_id_token_cannot_be_reused_as_a_logout_event(served: Api, idp: FakeIdP) -> None:
    tokens = _sign_in(served, idp, "alice")
    assert _logout(served, idp.id_token()).status_code == 400
    assert _me(served, tokens).status_code == 200


def test_logout_signed_by_another_key_is_refused(served: Api, idp: FakeIdP) -> None:
    tokens = _sign_in(served, idp, "alice")
    other = FakeIdP(clock=served.clock)
    assert _logout(served, _logout_token(served, other)).status_code == 400
    assert _me(served, tokens).status_code == 200


def test_logout_with_multiple_audiences_including_this_client_is_valid(
    served: Api, idp: FakeIdP
) -> None:
    tokens = _sign_in(served, idp, "alice")
    assert _logout(served, _logout_token(served, idp, aud=[CLIENT_ID, "other"])).status_code == 200
    assert _me(served, tokens).status_code == 401


def test_logout_between_id_token_validation_and_session_insert_refuses_pending_login(
    served: Api,
    idp: FakeIdP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = served.ctx.oidc
    assert provider is not None
    exchange = provider.exchange

    def exchange_then_logout(**kwargs: Any) -> Any:
        identity = exchange(**kwargs)
        logout = provider.validate_logout_token(_logout_token(served, idp, sid="ended"))
        served.auth.provider_logout(
            issuer=logout.issuer,
            subject=logout.subject,
            provider_sid=logout.sid,
            jti=logout.jti,
            issued_at=logout.issued_at,
            now=served.clock(),
            replay_until=logout.replay_until,
        )
        return identity

    monkeypatch.setattr(provider, "exchange", exchange_then_logout)
    idp.identity("alice", sid="ended")
    response = _exchange(served)
    assert response.status_code == 401
    assert response.json()["code"] == "session_revoked"
    assert "access_token" not in response.json()


def test_logout_can_revoke_refresh_family_after_access_expiry(served: Api, idp: FakeIdP) -> None:
    served.ctx.api.access_token_ttl_s = 60
    original = _sign_in(served, idp, "alice", sid="ended")
    independent = _sign_in(served, idp, "alice", sid="keep")
    served.clock.t += 100
    assert _me(served, original).status_code == 401
    rotated = _refresh(served, original).json()
    response = served.client.post(
        "/v1/auth/revoke",
        headers={"Authorization": f"Bearer {original['access_token']}"},
        json={"refresh_token": original["refresh_token"]},
    )
    assert response.status_code == 204
    assert _me(served, rotated).status_code == 401
    assert _refresh(served, rotated).status_code == 401
    assert _refresh(served, independent).status_code == 200
    assert (
        served.client.post("/v1/auth/revoke", json={"refresh_token": "unknown-token"}).status_code
        == 204
    )
