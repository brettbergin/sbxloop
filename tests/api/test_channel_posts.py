"""What a run says in the channel that asked for it.

A run posts under the name of the agent doing the work: an ``agent_update``
message authored by that agent, carrying the post's kind and the files it
delivered. Every post names a dedupe key, so a replayed or resumed run says
a thing once. A silenced channel keeps its deliveries and drops the running
commentary. The channel a post goes to is the one the item was admitted
with, whether or not any message there mentions it.

Expected values come from the posts these tests write and the items they
admit, never from the code under test.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import update

from sbxloop.agents.posts import ArtifactRef, ChannelPost, ChannelPoster
from sbxloop.daemon.model import WorkItem
from sbxloop.db.collaboration_models import ChannelRow
from sbxloop.ghids import chat_item_id
from tests.api.test_collaboration import FakeConcierge, bearer, register

REPORT = ArtifactRef(
    id="art_bread",
    run_id="run_r1",
    relpath="bread_items.md",
    media_type="text/markdown",
    size=42,
)


def _channel_with_work(api: Any) -> tuple[dict[str, str], str, WorkItem]:
    """A channel that asked for one workload, and the item it asked for."""
    api.ctx.concierge = FakeConcierge()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "Give me a list of items to make bread"},
    ).json()
    assert api.ctx.turns.wait_idle(timeout=5)
    key = accepted["turn"]["input_message_id"]
    item = WorkItem(
        item_id=chat_item_id(key),
        source_key=key,
        title="Bread list",
        body="Give me a list of items to make bread",
        kind="workload",
        channel_id=channel,
    )
    api.harness.dstore.upsert_new(item, api.clock())
    return headers, channel, item


def _messages(api: Any, headers: dict[str, str], channel: str) -> list[dict[str, Any]]:
    response = api.client.get(f"/v1/channels/{channel}/messages", headers=headers)
    assert response.status_code == 200, response.text
    return [m for m in response.json() if m["kind"] == "agent_update"]


def _silence(api: Any, channel: str, until: float) -> None:
    with api.harness.dstore.transaction() as session:
        session.execute(
            update(ChannelRow).where(ChannelRow.id == channel).values(silenced_until=until)
        )


def test_a_run_post_is_an_agent_update_message_by_its_agent(api: Any) -> None:
    headers, channel, item = _channel_with_work(api)

    message_id = api.ctx.poster.post(
        ChannelPost(
            channel_id=channel,
            author_agent="planner",
            kind="plan",
            text="Split the ask into 5 tasks",
            run_id="r1",
            item_id=item.item_id,
            dedupe_key="r1:plan",
            artifacts=(REPORT,),
        )
    )

    assert message_id is not None
    posted = _messages(api, headers, channel)
    assert len(posted) == 1
    assert posted[0]["id"] == message_id
    assert posted[0]["content"] == "Split the ask into 5 tasks"
    assert posted[0]["role"] == "assistant"
    assert posted[0]["post_kind"] == "plan"
    assert posted[0]["agent_slug"] == "planner"
    # An agent author reads back with the name its registry entry carries.
    planner = api.ctx.agents.get("planner")
    assert planner is not None
    assert posted[0]["author"] == {
        "kind": "agent",
        "id": "planner",
        "display_name": planner.spec.name,
    }
    assert [a["relpath"] for a in posted[0]["work"]["artifacts"]] == ["bread_items.md"]
    assert posted[0]["work"]["run_id"] == "run_r1"


def test_the_same_dedupe_key_posts_once(api: Any) -> None:
    headers, channel, item = _channel_with_work(api)
    post = ChannelPost(
        channel_id=channel,
        author_agent="builder",
        kind="progress",
        text="Finished task 2 of 5: knead the dough",
        run_id="r1",
        item_id=item.item_id,
        dedupe_key="r1:progress:2",
    )

    first = api.ctx.poster.post(post)
    again = api.ctx.poster.post(post)

    assert first is not None
    assert again == first
    assert [m["id"] for m in _messages(api, headers, channel)] == [first]


def test_a_silenced_channel_drops_progress_and_keeps_delivery(api: Any) -> None:
    headers, channel, item = _channel_with_work(api)
    _silence(api, channel, api.clock() + 600)

    def _post(kind: str, text: str) -> str | None:
        return api.ctx.poster.post(
            ChannelPost(
                channel_id=channel,
                author_agent="angie",
                kind=kind,  # type: ignore[arg-type]
                text=text,
                run_id="r1",
                item_id=item.item_id,
                dedupe_key=f"r1:{kind}",
            )
        )

    assert _post("progress", "Finished task 2 of 5: knead the dough") is None
    delivery = _post("delivery", "Here is the bread list")

    assert delivery is not None
    posted = _messages(api, headers, channel)
    assert [(m["post_kind"], m["content"]) for m in posted] == [
        ("delivery", "Here is the bread list")
    ]


def test_the_channel_of_an_item_is_the_one_it_was_admitted_with(api: Any) -> None:
    _headers, channel, item = _channel_with_work(api)
    detached = WorkItem(
        item_id="api:detached",
        source_key="api:detached",
        title="Detached",
        body="Bake without being asked in a message",
        kind="workload",
        channel_id=channel,
    )
    api.harness.dstore.upsert_new(detached, api.clock())

    assert api.ctx.poster.channel_for_item(item.item_id) == channel
    assert api.ctx.poster.channel_for_item(detached.item_id) == channel
    assert api.ctx.poster.channel_for_item("api:missing") is None


def test_a_post_into_a_channel_that_is_gone_is_dropped(api: Any) -> None:
    headers, channel, item = _channel_with_work(api)
    assert api.client.delete(f"/v1/channels/{channel}", headers=headers).status_code == 204

    assert (
        api.ctx.poster.post(
            ChannelPost(
                channel_id=channel,
                author_agent="angie",
                kind="delivery",
                text="Here is the bread list",
                run_id="r1",
                item_id=item.item_id,
                dedupe_key="r1:delivery",
            )
        )
        is None
    )


def test_the_daemon_loop_takes_the_poster_the_listener_supplies(api: Any) -> None:
    # A daemon with no API listener has none, and a run reports through
    # its events alone.
    assert api.loop.poster is None

    api.loop.poster = api.ctx.poster

    assert isinstance(api.loop.poster, ChannelPoster)


def test_run_progress_is_advertised(api: Any) -> None:
    features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "collaboration.run_progress" in features
