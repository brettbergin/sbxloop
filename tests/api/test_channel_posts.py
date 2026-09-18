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

from concurrent.futures import Future
from pathlib import Path
from typing import Any

from sqlalchemy import insert, update

from sbxloop.agents.posts import ArtifactRef, ChannelPost, ChannelPoster, RunPostLedger
from sbxloop.config import Config
from sbxloop.daemon.concierge import ConciergeReply
from sbxloop.daemon.model import WorkItem
from sbxloop.db.api_models import ArtifactRow
from sbxloop.db.collaboration_models import ChannelRow, MessageRow
from sbxloop.ghids import chat_item_id, issue_item_id
from sbxloop_worker.protocol import Event
from tests.api.test_channel_access import _channel, _people
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled
from tests.unit.test_daemon_loop import Harness

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


def test_a_post_hangs_on_the_turn_that_asked_for_the_work(api: Any) -> None:
    """A channel runs several turns at once, so the newest turn when a post
    lands is routinely an unrelated question. A post that names the message
    it answers is grouped under the turn that asked for the work."""
    api.ctx.concierge = FakeConcierge()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    asked = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "Give me a list of items to make bread"},
    ).json()["turn"]
    assert api.ctx.turns.wait_idle(timeout=5)
    item = WorkItem(
        item_id=chat_item_id(asked["input_message_id"]),
        source_key=asked["input_message_id"],
        title="Bread list",
        body="Give me a list of items to make bread",
        kind="workload",
        channel_id=channel,
    )
    api.harness.dstore.upsert_new(item, api.clock())
    api.harness.clock.t += 10
    newest = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "Unrelated question"},
    ).json()["turn"]
    assert api.ctx.turns.wait_idle(timeout=5)
    assert newest["id"] != asked["id"]

    def _post(dedupe: str, reply_to: str | None) -> None:
        api.ctx.poster.post(
            ChannelPost(
                channel_id=channel,
                author_agent="planner",
                kind="plan",
                text="Split the ask into 5 tasks",
                run_id="r1",
                item_id=item.item_id,
                dedupe_key=dedupe,
                reply_to_message_id=reply_to,
            )
        )

    _post("r1:plan", asked["input_message_id"])
    # A post that names no message still finds the turn that asked for its
    # work through the item, and never the channel's newest turn.
    _post("r1:plan:unnamed", None)

    posted = _messages(api, headers, channel)
    assert [m["work"]["turn_id"] for m in posted] == [asked["id"], asked["id"]]


def test_the_poster_knows_what_a_run_already_posted(api: Any) -> None:
    """A resumed run counts the posts it made before towards its cap, so
    the poster answers which keys a run has posted under."""
    _headers, channel, item = _channel_with_work(api)
    poster = api.ctx.poster
    assert isinstance(poster, RunPostLedger)
    assert poster.run_post_keys("r1") == frozenset()

    for run_id, key in (("r1", "r1:plan"), ("r1", "r1:progress:t1"), ("r2", "r2:plan")):
        poster.post(
            ChannelPost(
                channel_id=channel,
                author_agent="planner",
                kind="plan",
                text="Split the ask into 2 tasks",
                run_id=run_id,
                item_id=item.item_id,
                dedupe_key=key,
            )
        )

    assert poster.run_post_keys("r1") == frozenset({"r1:plan", "r1:progress:t1"})


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
    # Building the listener's context over a daemon is what gives that
    # daemon a poster: the API server does nothing else to arrange it.
    assert api.loop.poster is api.ctx.poster
    assert isinstance(api.loop.poster, ChannelPoster)


def test_a_daemon_with_no_listener_has_no_poster(tmp_path: Path) -> None:
    # A run there reports through its events alone.
    config = Config.model_validate({"home": str(tmp_path / "state"), "github": {"repo": "o/r"}})
    assert Harness(tmp_path, config).loop.poster is None


def test_run_progress_is_advertised(api: Any) -> None:
    features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "collaboration.run_progress" in features


def _turn(api: Any, headers: dict[str, str], channel: str, content: str) -> dict[str, Any]:
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns", headers=headers, json={"content": content}
    ).json()
    assert api.ctx.turns.wait_idle(timeout=5)
    return dict(accepted["turn"])


def test_a_snapshot_the_channel_cannot_show_still_leaves_it_readable(api: Any) -> None:
    headers, channel, item = _channel_with_work(api)

    message_id = api.ctx.poster.post(
        ChannelPost(
            channel_id=channel,
            author_agent="builder",
            kind="progress",
            text="Kneading",
            run_id="r1",
            item_id=item.item_id,
            dedupe_key="r1:progress:1",
            work={"phase": "build", "percent": 40},
        )
    )

    assert message_id is not None
    posted = _messages(api, headers, channel)
    assert [(m["id"], m["content"]) for m in posted] == [(message_id, "Kneading")]
    # What the platform itself knows about the work, in place of a shape no
    # client could read: the channel stays readable either way.
    assert posted[0]["work"]["item_id"] is not None
    assert "phase" not in posted[0]["work"]


def test_a_stored_snapshot_this_build_cannot_read_hides_only_itself(api: Any) -> None:
    headers, channel, item = _channel_with_work(api)
    message_id = api.ctx.poster.post(
        ChannelPost(
            channel_id=channel,
            author_agent="builder",
            kind="progress",
            text="Kneading",
            run_id="r1",
            item_id=item.item_id,
            dedupe_key="r1:progress:1",
        )
    )
    with api.harness.dstore.transaction() as session:
        session.execute(
            update(MessageRow)
            .where(MessageRow.id == message_id)
            .values(work_json='{"from": "a build that knew a field this one does not"}')
        )

    posted = _messages(api, headers, channel)
    assert [(m["id"], m["content"], m["work"]) for m in posted] == [(message_id, "Kneading", None)]


def test_a_reply_to_another_channels_message_does_not_borrow_its_turn(api: Any) -> None:
    headers, channel, item = _channel_with_work(api)
    other = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    elsewhere = _turn(api, headers, other, "Something else entirely")

    message_id = api.ctx.poster.post(
        ChannelPost(
            channel_id=channel,
            author_agent="planner",
            kind="reply",
            text="Answering",
            run_id="r1",
            item_id=item.item_id,
            dedupe_key="r1:reply",
            reply_to_message_id=elsewhere["input_message_id"],
        )
    )

    assert message_id is not None
    posted = _messages(api, headers, channel)
    assert [m["turn_id"] for m in posted] != [elsewhere["id"]]


def test_a_post_belongs_to_the_turn_that_asked_for_its_item(api: Any) -> None:
    headers, channel, item = _channel_with_work(api)
    asking = api.client.get(f"/v1/channels/{channel}/turns", headers=headers).json()[0]["id"]
    later = _turn(api, headers, channel, "Unrelated, while the run is out")
    assert later["id"] != asking

    api.ctx.poster.post(
        ChannelPost(
            channel_id=channel,
            author_agent="builder",
            kind="progress",
            text="Kneading",
            run_id="r1",
            item_id=item.item_id,
            dedupe_key="r1:progress:1",
        )
    )

    assert [m["turn_id"] for m in _messages(api, headers, channel)] == [asking]


def test_a_post_keeps_its_files_in_a_channel_with_no_turn(api: Any) -> None:
    api.ctx.concierge = FakeConcierge()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    item = WorkItem(
        item_id="api:unasked",
        source_key="api:unasked",
        title="Bread list",
        body="Bake without being asked in a message",
        kind="workload",
        channel_id=channel,
    )
    api.harness.dstore.upsert_new(item, api.clock())

    api.ctx.poster.post(
        ChannelPost(
            channel_id=channel,
            author_agent="angie",
            kind="delivery",
            text="Here is the bread list",
            run_id="r1",
            item_id=item.item_id,
            dedupe_key="r1:delivery",
            artifacts=(REPORT,),
        )
    )

    posted = _messages(api, headers, channel)
    assert len(posted) == 1
    work = posted[0]["work"]
    assert work is not None
    assert work["turn_id"] is None
    assert [a["relpath"] for a in work["artifacts"]] == ["bread_items.md"]


def test_a_posts_event_names_its_run_publicly(api: Any) -> None:
    _headers, channel, item = _channel_with_work(api)

    api.ctx.poster.post(
        ChannelPost(
            channel_id=channel,
            author_agent="planner",
            kind="plan",
            text="Split the ask into 5 tasks",
            run_id="r1",
            item_id=item.item_id,
            dedupe_key="r1:plan",
        )
    )

    events = api.client.get(
        "/v1/events", params={"type_prefix": "collaboration.message.created"}, headers=api.bearer()
    ).json()["data"]
    posts = [e for e in events if e["data"].get("post_kind")]
    assert [e["data"]["run_id"] for e in posts] == ["run_r1"]


def test_a_channel_that_cannot_be_written_to_does_not_fail_the_run(
    api: Any, monkeypatch: Any
) -> None:
    _headers, channel, item = _channel_with_work(api)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the store is having a day")

    monkeypatch.setattr(type(api.ctx.collaboration), "post_agent_update", _boom)
    monkeypatch.setattr("sbxloop.api.channel_posts.channel_for_item", _boom)

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
    assert api.ctx.poster.channel_for_item(item.item_id) is None


class _CodeConcierge:
    """A turn that files an issue, as the concierge's code intent does."""

    def submit_turn(self, text: str, **kwargs: Any) -> Future[ConciergeReply]:
        kwargs["on_code_work"]("owner/repo", 12, "Add a feature")
        future: Future[ConciergeReply] = Future()
        future.set_result(ConciergeReply("Queued the requested issue."))
        return future


def test_a_code_runs_post_hangs_on_the_turn_that_filed_its_issue(api: Any) -> None:
    api.ctx.concierge = _CodeConcierge()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "Add a feature", "intent": "code"},
    ).json()
    settled(api.client, headers, channel, accepted["turn"]["id"])
    filed = accepted["turn"]["id"]
    item = WorkItem(
        item_id=issue_item_id(12, "owner/repo"),
        source_key="12",
        repo="owner/repo",
        title="Add a feature",
        kind="code",
    )
    api.harness.dstore.upsert_new(item, api.clock())
    api.ctx.concierge = FakeConcierge()
    assert _turn(api, headers, channel, "Unrelated, while the run is out")["id"] != filed

    assert api.ctx.poster.channel_for_item(item.item_id) == channel
    api.ctx.poster.post(
        ChannelPost(
            channel_id=channel,
            author_agent="builder",
            kind="progress",
            text="Opened the pull request",
            run_id="r1",
            item_id=item.item_id,
            dedupe_key="r1:progress:1",
        )
    )

    assert [m["turn_id"] for m in _messages(api, headers, channel)] == [filed]


def test_a_post_of_a_kind_this_build_does_not_know_is_dropped(api: Any) -> None:
    headers, channel, item = _channel_with_work(api)

    def _post(kind: Any, text: str) -> str | None:
        return api.ctx.poster.post(
            ChannelPost(
                channel_id=channel,
                author_agent="builder",
                kind=kind,
                text=text,
                run_id="r1",
                item_id=item.item_id,
                dedupe_key="r1:progress:1",
            )
        )

    assert _post("musing", "Thinking about flour") is None
    assert _messages(api, headers, channel) == []
    # The dropped post held no claim on its key: a known kind under it posts.
    kept = _post("progress", "Kneading")
    assert kept is not None
    assert [(m["id"], m["post_kind"]) for m in _messages(api, headers, channel)] == [
        (kept, "progress")
    ]


def test_a_stored_post_kind_this_build_cannot_read_hides_only_itself(api: Any) -> None:
    headers, channel, item = _channel_with_work(api)
    message_id = api.ctx.poster.post(
        ChannelPost(
            channel_id=channel,
            author_agent="builder",
            kind="progress",
            text="Kneading",
            run_id="r1",
            item_id=item.item_id,
            dedupe_key="r1:progress:1",
        )
    )
    with api.harness.dstore.transaction() as session:
        session.execute(
            update(MessageRow)
            .where(MessageRow.id == message_id)
            .values(post_kind="a kind a later build added")
        )

    posted = _messages(api, headers, channel)
    assert [(m["id"], m["content"], m["post_kind"]) for m in posted] == [
        (message_id, "Kneading", None)
    ]


def _admitted_run(api: Any, channel_id: str, key: str) -> str:
    """Run a workload admitted with ``channel_id`` and asked for by no
    message in it; returns the run id."""
    item = WorkItem(
        item_id=chat_item_id(key),
        source_key=key,
        title="Weekly report",
        body="Write the weekly report",
        kind="workload",
        channel_id=channel_id,
    )
    api.harness.dstore.upsert_new(item, api.clock())
    api.harness.source.items = [item]
    api.harness.outcomes = ["completed"]
    api.clock.t += 10
    api.loop.tick()
    run_id = str(api.harness.runs[-1][0])
    api.harness.store.append_event(
        Event(ts=api.clock(), run_id=run_id, type="phase.start", data={"phase": "plan"})
    )
    return run_id


def _file(api: Any, run_id: str, artifact_id: str) -> None:
    """Catalogue one file of the run, already pruned, so reading it never
    touches the disk."""
    with api.harness.dstore.transaction() as session:
        session.execute(
            insert(ArtifactRow).values(
                id=artifact_id,
                run_id=run_id,
                relpath="report.md",
                size=9,
                sha256="0" * 64,
                media_type="text/markdown",
                origin="workspace",
                recorded_at=api.clock(),
                available=0,
                tombstoned_at=api.clock(),
            )
        )


def test_a_channels_members_read_the_files_of_its_runs(api: Any) -> None:
    owner, guest, _admin = _people(api)
    shared = _channel(api, owner, "workspace")
    private = _channel(api, owner)
    shared_run = _admitted_run(api, shared, "report:shared")
    _file(api, shared_run, "art_shared")
    private_run = _admitted_run(api, private, "report:private")
    _file(api, private_run, "art_private")

    def reads(headers: dict[str, str], run_id: str, artifact_id: str) -> list[int]:
        return [
            api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=headers).status_code,
            api.client.get(f"/v1/artifacts/{artifact_id}", headers=headers).status_code,
            api.client.get(f"/v1/artifacts/{artifact_id}/content", headers=headers).status_code,
        ]

    # The guest can open the workspace channel, so its run's files are theirs;
    # the bytes are gone (410), which is past the door, not at it.
    assert reads(guest, shared_run, "art_shared") == [200, 200, 410]
    listed = api.client.get(f"/v1/runs/run_{shared_run}/artifacts", headers=guest).json()
    assert [a["id"] for a in listed["data"]] == ["art_shared"]
    # A private channel they are not in: refused as they always were.
    assert reads(guest, private_run, "art_private") == [403, 403, 403]
    refused = api.client.get("/v1/artifacts/art_private", headers=guest).json()
    assert refused["capability"] == "artifacts:read"
    # An id nobody catalogued looks the same to them as one they may not see.
    assert api.client.get("/v1/artifacts/art_nope", headers=guest).status_code == 403
    for everyone in (owner, api.bearer()):
        assert reads(everyone, private_run, "art_private") == [200, 200, 410]


def test_a_channels_members_read_the_events_of_a_run_admitted_with_it(api: Any) -> None:
    owner, guest, _admin = _people(api)
    shared = _channel(api, owner, "workspace")
    private = _channel(api, owner)
    shared_run = _admitted_run(api, shared, "report:shared")
    private_run = _admitted_run(api, private, "report:private")

    def run_types(headers: dict[str, str], run_id: str) -> list[str]:
        page = api.client.get(f"/v1/runs/run_{run_id}/events", headers=headers)
        assert page.status_code == 200, page.text
        return [e["type"] for e in page.json()["data"]]

    assert "phase.start" in run_types(guest, shared_run)
    assert run_types(guest, private_run) == []
    assert "phase.start" in run_types(owner, private_run)
