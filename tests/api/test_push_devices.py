"""The device registry behind push notifications (``/v1/users/me/devices``).

A person registers a mobile device's push token with sbxloop; sbxloop
enrolls it with the push relay, keeps the opaque handle the relay returns,
and from then on pushes content-free pings through that handle. What the
ping is about stays here: the device fetches it from
``/v1/users/me/notifications/{ref}``. These tests run against
:class:`~tests.fakes.fake_relay.FakeRelay` over HTTP.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from sbxloop.db.api_models import ApiEventRow
from tests.api.conftest import Api, build
from tests.fakes.fake_relay import FakeRelay

TOKEN_A = "a1" * 32
TOKEN_B = "b2" * 40
RELAY = "http://relay.test"
OWNER = {
    "email": "owner@example.test",
    "username": "owner",
    "password": "correct horse battery staple",
    "full_name": "Olive Owner",
}


def device(token: str = TOKEN_A, env: str = "sandbox", **extra: Any) -> dict[str, Any]:
    return {
        "platform": "ios",
        "token": token,
        "env": env,
        "server_ref": "home.server-1",
        "name": "Olive's phone",
        **extra,
    }


def headers(token: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {token['access_token']}"}


def register_owner(api: Api) -> dict[str, str]:
    response = api.client.post("/v1/auth/local/register", json=OWNER)
    assert response.status_code == 201, response.text
    return headers(response.json())


def register_member(api: Api, username: str = "bob", role: str = "member") -> dict[str, str]:
    owner = api.ctx.collaboration.user_by_username("owner")
    assert owner is not None
    _, raw = api.ctx.collaboration.create_invite(
        role, f"{username}@example.test", created_by=owner.id, ttl_s=3600, now=api.clock()
    )
    response = api.client.post(
        "/v1/auth/local/register",
        json={
            "email": f"{username}@example.test",
            "username": username,
            "password": "another long password",
            "full_name": username.title() + " Builder",
            "invite_token": raw,
        },
    )
    assert response.status_code == 201, response.text
    return headers(response.json())


def push_api(tmp_path: Path, relay: FakeRelay, **push: Any) -> Api:
    built = build(tmp_path, config={"push": {"enabled": True, "relay_url": RELAY, **push}})
    built.ctx.relay_transport = relay.transport
    return built


@pytest.fixture
def relay() -> FakeRelay:
    return FakeRelay()


@pytest.fixture
def api(tmp_path: Path, relay: FakeRelay) -> Iterator[Api]:
    built = push_api(tmp_path, relay)
    with built.client:
        yield built
    built.ctx.close()


def enrolls(relay: FakeRelay) -> list[dict[str, Any]]:
    return [body for route, body in relay.requests if route == "/v1/enroll"]


# -- registration ---------------------------------------------------------------------


def test_a_device_is_enrolled_listed_and_deleted(api: Api, relay: FakeRelay) -> None:
    me = register_owner(api)

    created = api.client.post("/v1/users/me/devices", json=device(), headers=me)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["id"].startswith("dev_")
    assert body["platform"] == "ios"
    assert body["env"] == "sandbox"
    assert body["server_ref"] == "home.server-1"
    assert body["name"] == "Olive's phone"
    assert body["token_suffix"] == TOKEN_A[-6:]
    assert body["prefs"] == {
        "mentions": True,
        "gates": True,
        "work": True,
        "failures": True,
        "per_channel": {},
    }
    assert body["last_push_at"] is None
    assert body["created_at"].endswith("Z") and body["updated_at"].endswith("Z")
    # Neither the token nor the relay's handle ever comes back.
    assert "token" not in body and "handle" not in body
    assert enrolls(relay) == [{"token": TOKEN_A, "env": "sandbox"}]

    listed = api.client.get("/v1/users/me/devices", headers=me)
    assert listed.status_code == 200
    assert listed.json() == {"items": [body]}

    assert api.client.delete(f"/v1/users/me/devices/{body['id']}", headers=me).status_code == 204
    assert api.client.get("/v1/users/me/devices", headers=me).json() == {"items": []}
    again = api.client.delete(f"/v1/users/me/devices/{body['id']}", headers=me)
    assert again.status_code == 404
    assert again.json()["code"] == "device_not_found"


def test_registering_the_same_token_again_updates_in_place(api: Api, relay: FakeRelay) -> None:
    me = register_owner(api)
    first = api.client.post("/v1/users/me/devices", json=device(), headers=me).json()

    prefs = {
        "mentions": True,
        "gates": False,
        "work": True,
        "failures": True,
        "per_channel": {"chn_quiet": "none", "chn_busy": "mentions"},
    }
    api.clock.t += 5
    same_env = api.client.post(
        "/v1/users/me/devices",
        json=device(prefs=prefs, name="Renamed", server_ref="home-2"),
        headers=me,
    )
    assert same_env.status_code == 200, same_env.text
    assert same_env.json()["id"] == first["id"]
    assert same_env.json()["prefs"] == prefs
    assert same_env.json()["name"] == "Renamed"
    assert same_env.json()["server_ref"] == "home-2"
    assert same_env.json()["updated_at"] != first["updated_at"]
    # Nothing about the enrollment changed, so the relay was not asked again.
    assert len(enrolls(relay)) == 1

    moved = api.client.post("/v1/users/me/devices", json=device(env="production"), headers=me)
    assert moved.status_code == 200
    assert moved.json()["id"] == first["id"]
    assert moved.json()["env"] == "production"
    assert enrolls(relay)[-1] == {"token": TOKEN_A, "env": "production"}
    # A request that leaves prefs out keeps the ones stored.
    assert moved.json()["prefs"] == prefs
    assert len(api.client.get("/v1/users/me/devices", headers=me).json()["items"]) == 1


def test_a_token_is_matched_whatever_its_case(api: Api) -> None:
    me = register_owner(api)
    first = api.client.post("/v1/users/me/devices", json=device(TOKEN_A), headers=me).json()
    upper = api.client.post("/v1/users/me/devices", json=device(TOKEN_A.upper()), headers=me)
    assert upper.status_code == 200 and upper.json()["id"] == first["id"]


def test_devices_belong_to_the_person_who_registered_them(api: Api, relay: FakeRelay) -> None:
    owner = register_owner(api)
    bob = register_member(api)
    mine = api.client.post("/v1/users/me/devices", json=device(), headers=owner).json()
    # The same physical token under another account is that account's own row.
    theirs = api.client.post("/v1/users/me/devices", json=device(), headers=bob)
    assert theirs.status_code == 201
    assert theirs.json()["id"] != mine["id"]

    assert [
        d["id"] for d in api.client.get("/v1/users/me/devices", headers=bob).json()["items"]
    ] == [theirs.json()["id"]]
    for response in (
        api.client.delete(f"/v1/users/me/devices/{mine['id']}", headers=bob),
        api.client.post(f"/v1/users/me/devices/{mine['id']}/test", headers=bob),
    ):
        assert response.status_code == 404
        assert response.json()["code"] == "device_not_found"
    assert len(api.client.get("/v1/users/me/devices", headers=owner).json()["items"]) == 1


def test_the_routes_need_a_signed_in_person(api: Api) -> None:
    assert api.client.get("/v1/users/me/devices").status_code == 401
    assert api.client.post("/v1/users/me/devices", json=device()).status_code == 401
    # A plain API client has no local profile to own a device.
    client = api.bearer()
    refused = api.client.get("/v1/users/me/devices", headers=client)
    assert refused.status_code == 403
    assert refused.json()["code"] == "local_profile_required"


def test_devices_per_person_are_capped(tmp_path: Path, relay: FakeRelay) -> None:
    api = push_api(tmp_path, relay, max_devices_per_user=2)
    with api.client:
        me = register_owner(api)
        for token in ("01" * 32, "02" * 32):
            assert (
                api.client.post("/v1/users/me/devices", json=device(token), headers=me).status_code
                == 201
            )
        refused = api.client.post("/v1/users/me/devices", json=device("03" * 32), headers=me)
        assert refused.status_code == 409
        assert refused.json()["code"] == "device_limit_reached"
        assert refused.json()["limit"] == 2
        # Refused before the relay was asked for anything.
        assert len(enrolls(relay)) == 2
        # Updating a device already registered is not a new one.
        again = api.client.post("/v1/users/me/devices", json=device("01" * 32), headers=me)
        assert again.status_code == 200
    api.ctx.close()


@pytest.mark.parametrize(
    "push",
    [
        {},
        {"enabled": True},
        {"enabled": False, "relay_url": RELAY},
    ],
)
def test_without_a_relay_registration_is_refused_by_name(
    tmp_path: Path, relay: FakeRelay, push: dict[str, Any]
) -> None:
    api = build(tmp_path, config={"push": push})
    api.ctx.relay_transport = relay.transport
    with api.client:
        me = register_owner(api)
        refused = api.client.post("/v1/users/me/devices", json=device(), headers=me)
        assert refused.status_code == 503
        body = refused.json()
        assert body["code"] == "push_disabled"
        assert "[push] enabled" in body["detail"] and "relay_url" in body["detail"]
        assert api.client.post("/v1/users/me/devices/dev_x/test", headers=me).status_code == 503
        # Reading what is stored still works; there is just nothing to push with.
        assert api.client.get("/v1/users/me/devices", headers=me).json() == {"items": []}
    assert relay.requests == []
    api.ctx.close()


@pytest.mark.parametrize(
    ("script", "code"),
    [
        (
            {"status": 400, "body": {"error": "invalid_request", "detail": "token"}},
            "push_relay_refused",
        ),
        ({"status": 503, "body": {"error": "unavailable"}}, "push_relay_unavailable"),
        ({"status": 200, "body": {"no_handle": True}}, "push_relay_unavailable"),
        (None, "push_relay_unavailable"),
    ],
)
def test_a_relay_that_refuses_or_is_unreachable_is_a_bad_gateway(
    api: Api, relay: FakeRelay, script: dict[str, Any] | None, code: str
) -> None:
    me = register_owner(api)
    if script is None:
        relay.unreachable()
    else:
        relay.fail_next(script["status"], script["body"])
    refused = api.client.post("/v1/users/me/devices", json=device(), headers=me)
    assert refused.status_code == 502, refused.text
    assert refused.json()["code"] == code
    assert api.client.get("/v1/users/me/devices", headers=me).json() == {"items": []}


@pytest.mark.parametrize(
    "change",
    [
        {"token": "not-hex" * 10},
        {"token": "ab" * 10},
        {"token": "ab" * 101},
        {"platform": "android"},
        {"env": "staging"},
        {"server_ref": "has space"},
        {"server_ref": ""},
        {"server_ref": "x" * 65},
        {"name": "n" * 81},
        {"prefs": {"per_channel": {"chn_1": "loud"}}},
        {"prefs": {"mentions": True, "surprise": True}},
    ],
)
def test_a_malformed_registration_is_refused(
    api: Api, relay: FakeRelay, change: dict[str, Any]
) -> None:
    me = register_owner(api)
    response = api.client.post("/v1/users/me/devices", json={**device(), **change}, headers=me)
    assert response.status_code == 422, response.text
    assert relay.requests == []


# -- the test push and the notification a device fetches ----------------------------


def test_a_test_push_reaches_only_that_device(api: Api, relay: FakeRelay) -> None:
    me = register_owner(api)
    one = api.client.post("/v1/users/me/devices", json=device(TOKEN_A), headers=me).json()
    api.client.post("/v1/users/me/devices", json=device(TOKEN_B), headers=me)

    queued = api.client.post(f"/v1/users/me/devices/{one['id']}/test", headers=me)
    assert queued.status_code == 202, queued.text
    ref = queued.json()["ref"]
    # Queued, not sent on the request path.
    assert relay.sent == []

    api.ctx.push.dispatcher.deliver_due()
    assert relay.pushes() == [{"srv": "home.server-1", "k": "test", "ref": ref, "thread": ""}]
    assert relay.sent[0].token == TOKEN_A

    fetched = api.client.get(f"/v1/users/me/notifications/{ref}", headers=me)
    assert fetched.status_code == 200
    body = fetched.json()
    assert body == {
        "ref": ref,
        "kind": "test",
        "channel_id": None,
        "turn_id": None,
        "title": "Test notification",
        "body": "Notifications from this server reach this device.",
        "created_at": body["created_at"],
    }
    listed = api.client.get("/v1/users/me/devices", headers=me).json()["items"]
    pushed = {d["id"]: d["last_push_at"] for d in listed}
    assert pushed[one["id"]] is not None
    assert [v for k, v in pushed.items() if k != one["id"]] == [None]


def test_a_notification_is_only_its_recipients(api: Api) -> None:
    owner = register_owner(api)
    bob = register_member(api)
    dev = api.client.post("/v1/users/me/devices", json=device(), headers=owner).json()
    ref = api.client.post(f"/v1/users/me/devices/{dev['id']}/test", headers=owner).json()["ref"]

    for who, path in (
        (bob, f"/v1/users/me/notifications/{ref}"),
        (owner, "/v1/users/me/notifications/ntf_doesnotexist"),
    ):
        missing = api.client.get(path, headers=who)
        assert missing.status_code == 404
        assert missing.json()["code"] == "notification_not_found"


# -- the capability, and what never leaves ------------------------------------------


@pytest.mark.parametrize(
    ("push", "offered"),
    [
        ({}, False),
        ({"enabled": True}, False),
        ({"relay_url": RELAY}, False),
        ({"enabled": True, "relay_url": RELAY}, True),
    ],
)
def test_the_feature_is_offered_only_with_a_relay(
    tmp_path: Path, push: dict[str, Any], offered: bool
) -> None:
    api = build(tmp_path, config={"push": push})
    with api.client:
        features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert ("push.apns_relay" in features) is offered
    api.ctx.close()


def test_no_token_or_handle_reaches_an_event_or_a_log(
    api: Api, relay: FakeRelay, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    me = register_owner(api)
    dev = api.client.post("/v1/users/me/devices", json=device(), headers=me).json()
    api.client.post("/v1/users/me/devices", json=device(env="production"), headers=me)
    api.client.post(f"/v1/users/me/devices/{dev['id']}/test", headers=me)
    relay.fail_next(502, {"error": "upstream", "reason": "InternalServerError"})
    api.ctx.push.dispatcher.deliver_due()
    relay.fail_next(400, {"error": "invalid_request"})
    api.clock.t += 60
    api.ctx.push.dispatcher.deliver_due()
    api.client.post(f"/v1/users/me/devices/{dev['id']}/test", headers=me)
    relay.unregistered.add(TOKEN_A)
    api.ctx.push.dispatcher.deliver_due()

    secrets = {
        TOKEN_A,
        TOKEN_A.upper(),
        FakeRelay.seal(TOKEN_A, "sandbox"),
        FakeRelay.seal(TOKEN_A, "production"),
    }
    with api.harness.dstore.read() as session:
        events = [
            json.dumps([row.type, row.data_json, row.actor_json])
            for row in session.scalars(select(ApiEventRow))
        ]
    for secret in secrets:
        assert all(secret not in event for event in events)
        assert secret not in caplog.text
        for record in caplog.records:
            assert secret not in json.dumps(record.__dict__, default=str)
    # The logs did say what happened, by device id.
    assert dev["id"] in caplog.text
