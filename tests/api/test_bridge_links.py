"""Linking a bridge surface to a channel, and linking a person's identity.

A channel's links say which external surfaces mirror it. Managing them takes
managing the channel, and a channel a member cannot see has no links to show.
A person proves who they are on a bridge with a short-lived code they type
there, which is what maps an inbound message to their account.
"""

from __future__ import annotations

from typing import Any

from tests.api.test_channel_access import _channel, _people, _user_id
from tests.api.test_collaboration import FakeConcierge, bearer, register


def _link(api: Any, headers: dict[str, str], channel_id: str, **body: Any) -> dict[str, Any]:
    created = api.client.post(
        f"/v1/channels/{channel_id}/links",
        json={"backend": "discord", "surface_id": "C1", **body},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


def test_the_bridges_route_says_which_backends_are_configured(api: Any) -> None:
    owner = bearer(register(api))
    listed = api.client.get("/v1/bridges", headers=owner)
    assert listed.status_code == 200, listed.text
    rows = {row["backend"]: row for row in listed.json()["data"]}
    assert set(rows) == {"discord", "slack", "mattermost"}
    assert rows["discord"]["label"] == "Discord"
    # Nothing is configured in the test daemon's config.
    assert all(row["configured"] is False for row in rows.values())


def test_capabilities_advertise_the_bridges_feature(api: Any) -> None:
    owner = bearer(register(api))
    features = api.client.get("/v1/capabilities", headers=owner).json()["features"]
    assert "collaboration.bridges" in features


def test_links_are_created_listed_and_deleted(api: Any) -> None:
    owner = bearer(register(api))
    channel_id = _channel(api, owner)
    link = _link(api, owner, channel_id, thread_id="T9", allow_guests=True)
    assert link["backend"] == "discord"
    assert link["surface_id"] == "C1"
    assert link["thread_id"] == "T9"
    assert link["allow_guests"] is True
    assert link["active"] is True
    assert link["created_by"] == _user_id(api, owner)

    listed = api.client.get(f"/v1/channels/{channel_id}/links", headers=owner)
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["data"]] == [link["id"]]

    removed = api.client.delete(f"/v1/channels/{channel_id}/links/{link['id']}", headers=owner)
    assert removed.status_code == 204, removed.text
    assert api.client.get(f"/v1/channels/{channel_id}/links", headers=owner).json()["data"] == []


def test_one_surface_carries_one_link(api: Any) -> None:
    owner = bearer(register(api))
    first = _channel(api, owner)
    second = _channel(api, owner)
    _link(api, owner, first)
    clash = api.client.post(
        f"/v1/channels/{second}/links",
        json={"backend": "discord", "surface_id": "C1"},
        headers=owner,
    )
    assert clash.status_code == 409, clash.text
    assert clash.json()["code"] == "link_exists"


def test_managing_links_takes_managing_the_channel(api: Any) -> None:
    api.ctx.concierge = FakeConcierge()
    owner, guest, _admin = _people(api)
    channel_id = _channel(api, owner, "workspace")
    refused = api.client.post(
        f"/v1/channels/{channel_id}/links",
        json={"backend": "discord", "surface_id": "C1"},
        headers=guest,
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "channel_forbidden"
    assert api.client.get(f"/v1/channels/{channel_id}/links", headers=guest).status_code == 403


def test_a_channel_a_member_cannot_see_has_no_links(api: Any) -> None:
    api.ctx.concierge = FakeConcierge()
    owner, guest, _admin = _people(api)
    channel_id = _channel(api, owner)
    hidden = api.client.get(f"/v1/channels/{channel_id}/links", headers=guest)
    assert hidden.status_code == 404, hidden.text
    assert hidden.json()["code"] == "channel_not_found"


def test_a_link_code_maps_an_external_author_to_the_account(api: Any) -> None:
    owner = bearer(register(api))
    issued = api.client.post("/v1/users/me/identities/link-code", headers=owner)
    assert issued.status_code == 201, issued.text
    code = issued.json()["code"]
    assert code and issued.json()["expires_at"]

    assert api.client.get("/v1/users/me/identities", headers=owner).json()["data"] == []
    identity = api.ctx.collaboration.redeem_link_code(
        code, backend="discord", external_user_id="U1", display_name="Casey", now=api.clock()
    )
    assert identity is not None
    assert identity.user_id == _user_id(api, owner)

    listed = api.client.get("/v1/users/me/identities", headers=owner).json()["data"]
    assert [(row["backend"], row["external_user_id"]) for row in listed] == [("discord", "U1")]
    assert listed[0]["display_name"] == "Casey"

    removed = api.client.delete("/v1/users/me/identities/discord", headers=owner)
    assert removed.status_code == 204, removed.text
    assert api.client.get("/v1/users/me/identities", headers=owner).json()["data"] == []


def test_a_link_code_is_spent_once(api: Any) -> None:
    owner = bearer(register(api))
    code = api.client.post("/v1/users/me/identities/link-code", headers=owner).json()["code"]
    assert (
        api.ctx.collaboration.redeem_link_code(
            code, backend="discord", external_user_id="U1", display_name=None, now=api.clock()
        )
        is not None
    )
    assert (
        api.ctx.collaboration.redeem_link_code(
            code, backend="discord", external_user_id="U2", display_name=None, now=api.clock()
        )
        is None
    )


def test_a_message_carries_its_origin(api: Any) -> None:
    api.ctx.concierge = FakeConcierge()
    owner = bearer(register(api))
    channel_id = _channel(api, owner)
    posted = api.client.post(
        f"/v1/channels/{channel_id}/turns", json={"content": "hello"}, headers=owner
    )
    assert posted.status_code == 202, posted.text
    # A message typed in Angie has no external origin.
    assert posted.json()["message"]["origin"] is None
