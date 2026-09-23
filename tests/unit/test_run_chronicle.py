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

    def __init__(self, *, files: bool = True) -> None:
        self.posts: list[ChannelPost] = []
        self.by_key: dict[str, str] = {}
        self.files = files

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
        return (REPORT,) if self.files else ()

    def run_post_keys(self, run_id: str) -> frozenset[str]:
        return frozenset(p.dedupe_key for p in self.posts if p.run_id == run_id)


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


def _item(channel_id: str | None = CHANNEL, *, kind: str = "code") -> WorkItem:
    return WorkItem(
        item_id="chat:msg_1",
        source_key="msg_1",
        title="Bread list",
        body="Give me a list of items to make bread",
        kind=kind,  # type: ignore[arg-type]
        channel_id=channel_id,
    )


def _config(**agent_team: Any) -> Config:
    return Config.model_validate({"agent_team": agent_team} if agent_team else {})


def _chronicle(
    clock: Clock,
    *,
    config: Config | None = None,
    kind: str = "code",
    files: bool = True,
) -> tuple[RunChronicle, FakePoster]:
    poster = FakePoster(files=files)
    return (
        RunChronicle(
            poster,
            _assignment(),
            _item(kind=kind),
            config or _config(),
            clock,
            artifacts=poster,
        ),
        poster,
    )


def _tasks(n: int, *, done: int = 0) -> Event:
    """The roster a run announces, with the first ``done`` tasks settled,
    which is what a resume re-announces."""
    return Event.now(
        HostEventTypes.RUN_TASKS,
        RUN,
        tasks=[
            {"id": f"t{i}", "title": f"Step {i}", "state": "done" if i <= done else "pending"}
            for i in range(1, n + 1)
        ],
    )


def _task_end(index: int, *, agent: str | None = None, state: str = "done") -> Event:
    data: dict[str, Any] = {"task_id": f"t{index}", "title": f"Step {index}", "state": state}
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


def test_a_reply_stamped_with_a_slug_not_on_the_run_is_told_in_the_steering_voice() -> None:
    """A reply names the agent that answered it only when that agent is on
    the run. A stamp the team does not have (a steer can name any agent) is
    not an author: the post is credited to the run's own steering agent."""
    clock = Clock()
    chronicle, poster = _chronicle(clock)

    chronicle.on_event(
        Event.now(
            HostEventTypes.CHAT_REPLY,
            RUN,
            message_id="stm_9",
            reply="Approved, ship it.",
            action="continue",
            agent_slug="reviewer-bot",
        )
    )

    assert _kinds(poster) == [("reply", "baker")]
    assert poster.posts[0].text == "Approved, ship it."


def test_a_task_stamped_with_a_slug_not_on_the_run_is_credited_to_its_role() -> None:
    clock = Clock()
    chronicle, poster = _chronicle(clock)

    chronicle.on_event(_tasks(2))
    chronicle.on_event(_task_end(1, agent="reviewer-bot"))

    assert _kinds(poster)[1] == ("progress", "kneader")


def test_a_task_that_failed_or_was_skipped_is_not_announced_as_finished() -> None:
    """A task ends in whatever state it reached. Only work that finished
    counts towards the run's progress, and only a failure is worth its own
    line: a skipped task did nothing, and the run's end says why."""
    clock = Clock()
    chronicle, poster = _chronicle(clock, config=_config(progress_interval_s=0))

    chronicle.on_event(_tasks(3))
    clock.t += 1
    chronicle.on_event(_task_end(1, state="failed"))
    clock.t += 1
    chronicle.on_event(_task_end(2, state="skipped"))
    clock.t += 1
    chronicle.on_event(_task_end(3))

    assert [p.text for p in poster.posts if p.kind == "progress"] == [
        "Task failed: Step 1",
        "Finished task 1 of 3: Step 3",
    ]


def test_a_resumed_run_counts_the_tasks_it_had_already_finished() -> None:
    """A resume re-announces the roster with each task's persisted state,
    and the chronicle it re-attaches to picks up the count there."""
    clock = Clock()
    chronicle, poster = _chronicle(clock, config=_config(progress_interval_s=0))

    chronicle.on_event(_tasks(5, done=2))
    clock.t += 1
    chronicle.on_event(_task_end(3))

    assert [p.text for p in poster.posts if p.kind == "progress"] == [
        "Finished task 3 of 5: Step 3"
    ]


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


def test_the_delivery_is_the_answer_the_chat_sink_carried() -> None:
    """A run publishes to every sink its tasks named, and only the chat
    sink carries the answer the requester asked for. One delivery post per
    run, and it is that answer - never the line about where a file landed."""
    clock = Clock()
    chronicle, poster = _chronicle(clock)

    chronicle.on_event(
        Event.now(
            HostEventTypes.RUN_PUBLISHED,
            RUN,
            sink="artifact",
            tasks=["t1"],
            message="1 file delivered to runs/r1/artifacts",
        )
    )
    chronicle.on_event(
        Event.now(
            HostEventTypes.RUN_PUBLISHED,
            RUN,
            sink="chat",
            tasks=["t1"],
            message="Here is your bread list: flour, water, salt, yeast.",
        )
    )
    chronicle.on_event(Event.now(HostEventTypes.RUN_END, RUN, state="published"))

    delivery = [p for p in poster.posts if p.kind == "delivery"]
    assert [p.text for p in delivery] == ["Here is your bread list: flour, water, salt, yeast."]
    assert delivery[0].artifacts == (REPORT,)


def test_a_run_with_no_chat_sink_names_what_it_delivered_at_its_end() -> None:
    clock = Clock()
    chronicle, poster = _chronicle(clock, files=False)

    chronicle.on_event(
        Event.now(
            HostEventTypes.RUN_PUBLISHED,
            RUN,
            sink="issue",
            tasks=["t1"],
            message="result filed as https://example.test/issues/4",
        )
    )
    assert poster.posts == []

    chronicle.on_event(Event.now(HostEventTypes.RUN_END, RUN, state="published"))

    assert [(p.kind, p.text) for p in poster.posts] == [
        ("delivery", "result filed as https://example.test/issues/4")
    ]


def test_a_post_names_the_message_that_asked_for_the_work() -> None:
    """A channel can have several turns in flight, so a post that does not
    name the message it answers lands on an unrelated one. The item's
    source key is that message for a run a chat message asked for."""
    clock = Clock()
    chronicle, poster = _chronicle(clock, kind="workload")

    chronicle.on_event(_tasks(2))
    chronicle.on_event(_task_end(1))
    chronicle.on_event(Event.now(HostEventTypes.RUN_END, RUN, state="completed"))

    assert len(poster.posts) == 3
    assert {p.reply_to_message_id for p in poster.posts} == {"msg_1"}

    # A code run comes from an issue, whose source key names no message.
    code, code_poster = _chronicle(clock)
    code.on_event(_tasks(2))
    assert [p.reply_to_message_id for p in code_poster.posts] == [None]


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


def _gh_item(**overrides: Any) -> WorkItem:
    from tests.unit.test_daemon_loop import gh_item

    return gh_item(channel_id=CHANNEL, **overrides)


def _one_attempt(tmp_path: Any, **agent_team: Any) -> Config:
    """A daemon that gives an item one attempt, so a failed run stays
    pinned to its item for an operator to resume."""
    return Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "github": {"repo": "o/r"},
            "daemon": {"max_attempts_per_item": 1},
            "agent_team": agent_team,
        }
    )


def _fail_then_resume(harness: Any, segments: int) -> None:
    """Dispatch one item whose run fails, then resume it ``segments - 1``
    times, each resumed segment failing again."""
    harness.source.items = [_gh_item()]
    harness.outcomes = ["failed"]
    harness.loop.tick()
    item = harness.dstore.get("gh:issue:1")
    assert item is not None and item.run_id is not None
    harness.source.items = []
    for _ in range(segments - 1):
        harness.loop.resume_run(item.run_id, by="brett")
        harness.outcomes = ["failed"]
        harness.clock.t += 10
        harness.loop.tick()
    assert [resume for _run, resume in harness.runs] == [False] + [True] * (segments - 1)


def test_a_run_that_stops_again_after_a_resume_says_so_again(tmp_path: Any) -> None:
    """An operator may resume a failed run. When the resumed run fails
    again the channel hears it again, rather than keeping the first
    failure's reason as the run's last word."""
    from tests.unit.test_daemon_loop import Harness

    harness = Harness(tmp_path, _one_attempt(tmp_path))
    poster = FakePoster()
    harness.loop.poster = poster
    reasons = iter(["the tests broke", "the tests broke again"])

    def runner(item: Any, cfg: Any, run_id: str, bus: Any, resume: bool) -> Any:
        reason = next(reasons)
        bus.emit(HostEventTypes.RUN_END, run_id, state="failed", reason=reason)
        # The same segment reporting its end twice still says it once.
        bus.emit(HostEventTypes.RUN_END, run_id, state="failed", reason=reason)
        return harness.runner(item, cfg, run_id, bus, resume)

    harness.loop._runner = runner
    _fail_then_resume(harness, 2)

    assert [p.text for p in poster.posts if p.kind == "notice"] == [
        "Run ended: failed. the tests broke",
        "Run ended: failed. the tests broke again",
    ]


def test_a_resumed_run_keeps_its_count_and_its_cap(tmp_path: Any) -> None:
    """``max_posts_per_run`` bounds the run, not each segment of it: a
    resumed run picks up the posts it already made, and its next finished
    task is numbered after the ones the run had already done."""
    from tests.unit.test_daemon_loop import Harness

    harness = Harness(tmp_path, _one_attempt(tmp_path, max_posts_per_run=4, progress_interval_s=0))
    poster = FakePoster()
    harness.loop.poster = poster
    segments = iter(
        [
            # The plan, one finished task, and the failure: three posts.
            (0, 1),
            # Task 2 finishes; the cap leaves room for one more post.
            (1, 2),
            # The run has said all it may; task 3 goes unannounced.
            (2, 3),
        ]
    )

    def runner(item: Any, cfg: Any, run_id: str, bus: Any, resume: bool) -> Any:
        done, finishing = next(segments)
        bus.emit(
            HostEventTypes.RUN_TASKS,
            run_id,
            tasks=[
                {"id": f"t{i}", "title": f"Step {i}", "state": "done" if i <= done else "pending"}
                for i in range(1, 6)
            ],
        )
        bus.emit(
            HostEventTypes.TASK_END,
            run_id,
            task_id=f"t{finishing}",
            title=f"Step {finishing}",
            state="done",
        )
        bus.emit(HostEventTypes.RUN_END, run_id, state="failed", reason=f"segment {finishing}")
        return harness.runner(item, cfg, run_id, bus, resume)

    harness.loop._runner = runner
    _fail_then_resume(harness, 3)

    assert [p.text for p in poster.posts if p.kind in ("plan", "progress")] == [
        "Split the ask into 5 tasks",
        "Finished task 1 of 5: Step 1",
        "Finished task 2 of 5: Step 2",
    ]
