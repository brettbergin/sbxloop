"""A run's chronicle: its events, told in its channel by the agents working it.

The plan comes from the planner, each finished task from the agent that did
it, the verdict from the critic, a steering reply from the agent that was
asked, and the delivery and any notice from the lead. The commentary is
bounded: progress is coalesced to one post per interval and the whole run is
capped, but what ends a run is always said. A replayed or resumed run says
each moment once.

Expected values come from the events these tests publish and the knobs they
set, never from the code under test.
"""

from __future__ import annotations

from typing import Any

from sbxloop.agents.assignment import AgentAssignment, AgentBinding
from sbxloop.agents.chronicle import RunChronicle
from sbxloop.agents.posts import ArtifactRef, ChannelPost
from sbxloop.config import Config
from sbxloop.daemon.model import WorkItem
from sbxloop.events import HostEventTypes
from sbxloop_worker.protocol import Event, EventTypes

RUN = "r1"
CHANNEL = "chn_1"
REPORT = ArtifactRef(
    id="art_1",
    run_id=RUN,
    relpath="bread_items.md",
    media_type="text/markdown",
    size=42,
)


class FakePoster:
    """Records what a run posted, and dedupes as the real one does."""

    def __init__(self) -> None:
        self.posts: list[ChannelPost] = []
        self.by_key: dict[str, str] = {}

    def post(self, post: ChannelPost) -> str | None:
        known = self.by_key.get(post.dedupe_key)
        if known is not None:
            return known
        message_id = f"msg_{len(self.posts)}"
        self.by_key[post.dedupe_key] = message_id
        self.posts.append(post)
        return message_id

    def channel_for_item(self, item_id: str) -> str | None:
        return CHANNEL

    def artifacts_for_run(self, run_id: str) -> tuple[ArtifactRef, ...]:
        return (REPORT,)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _binding(slug: str, role: str) -> AgentBinding:
    return AgentBinding(
        slug=slug,
        name=slug.title(),
        role=role,  # type: ignore[arg-type]
        model=None,
        persona="",
        memory_block="",
        tools=None,
        credentials=(),
        revision=1,
    )


def _assignment() -> AgentAssignment:
    """A named team, so every post has an agent to credit."""
    people = {
        "chef": "lead",
        "baker": "planner",
        "kneader": "builder",
        "taster": "critic",
        "runner": "operator",
    }
    return AgentAssignment(
        lead="chef",
        roles={"planner": "baker", "builder": "kneader", "critic": "taster", "operator": "runner"},
        agents={slug: _binding(slug, role) for slug, role in people.items()},
        channel_id=CHANNEL,
    )


def _item(channel_id: str | None = CHANNEL) -> WorkItem:
    return WorkItem(
        item_id="chat:msg_1",
        source_key="msg_1",
        title="Bread list",
        body="Give me a list of items to make bread",
        kind="code",
        channel_id=channel_id,
    )


def _config(**agent_team: Any) -> Config:
    return Config.model_validate({"agent_team": agent_team} if agent_team else {})


def _chronicle(clock: Clock, *, config: Config | None = None) -> tuple[RunChronicle, FakePoster]:
    poster = FakePoster()
    return (
        RunChronicle(
            poster,
            _assignment(),
            _item(),
            config or _config(),
            clock,
            artifacts=poster,
        ),
        poster,
    )


def _tasks(n: int) -> Event:
    return Event.now(
        HostEventTypes.RUN_TASKS,
        RUN,
        tasks=[{"id": f"t{i}", "title": f"Step {i}", "state": "pending"} for i in range(1, n + 1)],
    )


def _task_end(index: int, *, agent: str | None = None) -> Event:
    data: dict[str, Any] = {"task_id": f"t{index}", "title": f"Step {index}", "state": "done"}
    if agent is not None:
        data["agent_slug"] = agent
    return Event.now(HostEventTypes.TASK_END, RUN, **data)


def _kinds(poster: FakePoster) -> list[tuple[str, str]]:
    return [(p.kind, p.author_agent) for p in poster.posts]


def test_each_event_becomes_a_post_by_the_agent_that_did_the_work() -> None:
    clock = Clock()
    chronicle, poster = _chronicle(clock)

    chronicle.on_event(_tasks(5))
    chronicle.on_event(_task_end(1))
    chronicle.on_event(
        Event.now(
            HostEventTypes.REVIEW_VERDICT,
            RUN,
            round=1,
            verdict="request_changes",
            findings=2,
            blocking=1,
        )
    )
    chronicle.on_event(
        Event.now(HostEventTypes.CHAT_REPLY, RUN, message_id="stm_1", reply="On it.")
    )
    chronicle.on_event(
        Event.now(HostEventTypes.RUN_MERGED, RUN, pr=9, url="https://example.test/pr/9")
    )

    assert _kinds(poster) == [
        ("plan", "baker"),
        ("progress", "kneader"),
        ("review", "taster"),
        ("reply", "baker"),
        ("delivery", "chef"),
    ]
    assert poster.posts[0].text == "Split the ask into 5 tasks"
    assert poster.posts[1].text == "Finished task 1 of 5: Step 1"
    assert poster.posts[2].text == "2 findings, 1 blocking"
    assert poster.posts[3].text == "On it."
    assert "https://example.test/pr/9" in poster.posts[4].text


def test_a_notice_names_what_stopped_the_run() -> None:
    clock = Clock()
    chronicle, poster = _chronicle(clock)

    chronicle.on_event(Event.now(HostEventTypes.RUN_BLOCKED, RUN, pr=9, why="the base moved"))
    chronicle.on_event(
        Event.now(HostEventTypes.RUN_END, RUN, state="blocked", reason="the base moved")
    )

    assert _kinds(poster) == [("notice", "chef")]
    assert "the base moved" in poster.posts[0].text


def test_a_task_names_its_own_agent_over_its_role() -> None:
    clock = Clock()
    chronicle, poster = _chronicle(clock)

    chronicle.on_event(_tasks(2))
    chronicle.on_event(_task_end(1, agent="runner"))

    assert _kinds(poster)[1] == ("progress", "runner")


def test_progress_is_coalesced_to_one_post_per_interval() -> None:
    clock = Clock()
    chronicle, poster = _chronicle(clock, config=_config(progress_interval_s=60))

    chronicle.on_event(_tasks(4))
    chronicle.on_event(_task_end(1))
    clock.t += 30
    chronicle.on_event(_task_end(2))
    clock.t += 30
    chronicle.on_event(_task_end(3))

    progress = [p for p in poster.posts if p.kind == "progress"]
    assert [p.text for p in progress] == [
        "Finished task 1 of 4: Step 1",
        "Finished task 3 of 4: Step 3",
    ]


def test_the_cap_stops_the_commentary_and_never_the_delivery() -> None:
    clock = Clock()
    chronicle, poster = _chronicle(
        clock, config=_config(max_posts_per_run=2, progress_interval_s=0)
    )

    chronicle.on_event(_tasks(4))
    for index in range(1, 5):
        clock.t += 1
        chronicle.on_event(_task_end(index))
    chronicle.on_event(
        Event.now(HostEventTypes.RUN_MERGED, RUN, pr=9, url="https://example.test/pr/9")
    )

    assert _kinds(poster) == [("plan", "baker"), ("progress", "kneader"), ("delivery", "chef")]


def test_a_replayed_run_posts_each_moment_once() -> None:
    clock = Clock()
    chronicle, poster = _chronicle(clock, config=_config(progress_interval_s=0))
    events = [
        _tasks(2),
        _task_end(1),
        Event.now(HostEventTypes.RUN_MERGED, RUN, pr=9, url="https://example.test/pr/9"),
    ]
    for event in events:
        clock.t += 1
        chronicle.on_event(event)

    # A resume re-announces the roster and re-plays what settled; the same
    # keys come back, so nothing is said twice.
    resumed = RunChronicle(
        poster, _assignment(), _item(), _config(progress_interval_s=0), clock, artifacts=poster
    )
    for event in events:
        clock.t += 1
        resumed.on_event(event)

    assert len(poster.posts) == 3
    assert sorted({p.dedupe_key for p in poster.posts}) == [
        f"{RUN}:delivery",
        f"{RUN}:plan",
        f"{RUN}:progress:t1",
    ]


def test_a_delivery_carries_the_runs_files() -> None:
    clock = Clock()
    chronicle, poster = _chronicle(clock)

    chronicle.on_event(Event.now(HostEventTypes.RUN_PUBLISHED, RUN, sink="chat", tasks=["t1"]))

    (delivery,) = [p for p in poster.posts if p.kind == "delivery"]
    assert delivery.artifacts == (REPORT,)
    assert delivery.run_id == RUN
    assert delivery.channel_id == CHANNEL


def test_chronicle_off_posts_nothing() -> None:
    clock = Clock()
    chronicle, poster = _chronicle(clock, config=_config(chronicle="off"))

    chronicle.on_event(_tasks(3))
    chronicle.on_event(_task_end(1))
    chronicle.on_event(
        Event.now(HostEventTypes.RUN_MERGED, RUN, pr=9, url="https://example.test/pr/9")
    )

    assert poster.posts == []


def test_chronicle_quiet_keeps_only_what_ends_the_run() -> None:
    clock = Clock()
    chronicle, poster = _chronicle(clock, config=_config(chronicle="quiet"))

    chronicle.on_event(_tasks(3))
    chronicle.on_event(_task_end(1))
    chronicle.on_event(
        Event.now(HostEventTypes.RUN_MERGED, RUN, pr=9, url="https://example.test/pr/9")
    )

    assert _kinds(poster) == [("delivery", "chef")]


def test_an_event_nobody_is_waiting_on_posts_nothing() -> None:
    clock = Clock()
    chronicle, poster = _chronicle(clock)

    chronicle.on_event(_tasks(3))
    chronicle.on_event(Event.now(EventTypes.AGENT_TOOL_START, RUN, tool="bash"))

    assert len(poster.posts) == 1


def test_a_run_with_no_channel_has_no_chronicle() -> None:
    clock = Clock()
    poster = FakePoster()

    assert (
        RunChronicle.for_item(
            poster, _assignment(), _item(None), _config(), clock, artifacts=poster
        )
        is None
    )


def test_a_dispatched_run_tells_its_channel_what_it_did(tmp_path: Any) -> None:
    """The daemon attaches the chronicle to the run it launches."""
    from tests.unit.test_daemon_loop import Harness

    harness = Harness(tmp_path)
    poster = FakePoster()
    harness.loop.poster = poster
    item = WorkItem(
        item_id="chat:msg_2",
        source_key="msg_2",
        title="Bread list",
        body="Give me a list of items to make bread",
        kind="workload",
        channel_id=CHANNEL,
    )

    def runner(_item: Any, _cfg: Any, run_id: str, bus: Any, _resume: bool) -> Any:
        bus.emit(HostEventTypes.RUN_TASKS, run_id, tasks=[{"id": "t1", "title": "Bake"}])
        bus.emit(HostEventTypes.RUN_END, run_id, state="completed")
        return harness.runner(_item, _cfg, run_id, bus, _resume)

    harness.loop._runner = runner
    harness.source.items = [item]
    harness.outcomes = ["completed"]
    harness.clock.t += 10
    harness.loop.tick()

    assert [p.kind for p in poster.posts] == ["plan", "delivery"]
