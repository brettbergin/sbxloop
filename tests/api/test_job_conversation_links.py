"""Operations lists only link to conversations the reader may open."""

from tests.api.conftest import Api
from tests.api.test_channel_access import _channel, _invite
from tests.api.test_collaboration import bearer, register
from tests.unit.test_daemon_loop import gh_item


def test_item_list_and_detail_link_to_a_visible_conversation(api: Api) -> None:
    owner = register(api)
    guest = _invite(api, "member", "guest")
    channel = _channel(api, bearer(owner), "workspace")
    item = gh_item("1", channel_id=channel)
    api.harness.dstore.upsert_new(item, api.clock())

    listed = api.client.get("/v1/items", headers=bearer(guest))
    assert listed.status_code == 200, listed.text
    (row,) = listed.json()["data"]
    assert row["channel_id"] == channel
    detail = api.client.get(f"/v1/items/{row['id']}", headers=bearer(guest))
    assert detail.status_code == 200, detail.text
    assert detail.json()["channel_id"] == channel


def test_a_private_or_deleted_conversation_is_never_linked_for_a_nonmember(api: Api) -> None:
    owner = register(api)
    guest = _invite(api, "member", "guest")
    channel = _channel(api, bearer(owner))
    item = gh_item("1", channel_id=channel)
    api.harness.dstore.upsert_new(item, api.clock())

    (row,) = api.client.get("/v1/items", headers=bearer(guest)).json()["data"]
    assert row["channel_id"] is None
    assert (
        api.client.get(f"/v1/items/{row['id']}", headers=bearer(guest)).json()["channel_id"] is None
    )
    own = api.client.get("/v1/items", headers=bearer(owner)).json()["data"][0]
    assert own["channel_id"] == channel

    removed = api.client.delete(f"/v1/channels/{channel}", headers=bearer(owner))
    assert removed.status_code == 204, removed.text
    assert (
        api.client.get("/v1/items", headers=bearer(owner)).json()["data"][0]["channel_id"] is None
    )
