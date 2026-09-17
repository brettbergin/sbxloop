"""Steering one task, and one agent, by mentioning it (plan S-A11).

Until now every instruction went into one mailbox and was answered by
whichever task lane reached a phase boundary first, in the run's own
steering voice. With several lanes in flight that meant "steer the builder
working on t2" could be answered by the lane working on t1, and the reply
never came from the agent the person named.

The contract here: a message addressed to a task waits in that task's own
mailbox and is answered by that lane, however many are running; a message
naming an agent is answered in that agent's persona and its `chat.reply`
says so; a message addressed to neither behaves exactly as it always did; a
task that ends with messages still waiting hands them back rather than
swallowing them; and a mention in a channel routes to the one live run that
agent is working, or to nothing at all when there is no single answer.
"""

from __future__ import annotations

import queue
from pathlib import Path
from typing import Any

import pytest

from sbxloop.agents.assignment import AgentAssignment, AgentBinding
from sbxloop.daemon.controls.principal import Principal
from sbxloop.daemon.controls.results import CancelOutcome, ControlError
from sbxloop.engine.engine import ChatMessage, LoopEngine
from sbxloop.engine.model import SteerVerdict, TaskSpec
from sbxloop.engine.phases import PhaseSpend
from sbxloop.events import HostEventTypes
from tests.conftest import FakeSbx
from tests.unit.test_engine import Harness


def binding(slug: str, role: str) -> AgentBinding:
    return AgentBinding(
        slug=slug,
        name=slug.title(),
        role=role,  # type: ignore[arg-type]
        model=None,
        persona=f"You are {slug}.",
        memory_block="",
        tools=None,
        credentials=(),
        revision=1,
    )


class RecordingPhases:
    """A stand-in for the phase runner: every steer it is asked for, with
    the task and the agent it was asked as."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.guidance: list[str] = []

    def steer(
        self,
        message: str,
        *,
        tasks: Any,
        task: Any,
        stage: str | None = None,
        binding: AgentBinding | None = None,
    ) -> SteerVerdict:
        self.calls.append(
            (
                message,
                None if task is None else task.spec.id,
                None if binding is None else binding.slug,
            )
        )
        return SteerVerdict(reply="noted", action="continue", guidance="")

    def drain_spend(self) -> PhaseSpend:
        return PhaseSpend(usage=None, turns=None)

    def add_guidance(self, guidance: str) -> None:
        self.guidance.append(guidance)


def engine_with_tasks(
    harness: Harness, *task_ids: str, max_parallel_tasks: int = 2
) -> tuple[LoopEngine, str, list[Any]]:
    """An engine holding a run whose board is ``task_ids``, all in flight."""
    engine = harness.engine(budgets={"max_parallel_tasks": max_parallel_tasks})
    run_id = "run-steer"
    engine.store.create_run(run_id, "an outcome")
    engine.store.save_tasks(run_id, [TaskSpec(id=tid, title=tid) for tid in task_ids])
    tasks = engine.store.get_tasks(run_id)
    for task in tasks:
        task.state = "executing"
        engine.store.update_task(run_id, task)
    return engine, run_id, engine.store.get_tasks(run_id)


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


class TestTargetedMailbox:
    def test_a_targeted_message_is_answered_by_the_task_it_names(self, harness: Harness) -> None:
        """Two lanes in flight; the message names t2, so t2's lane answers
        it with t2 as the current task — not whichever lane got there
        first, and not as run-level direction."""
        engine, run_id, tasks = engine_with_tasks(harness, "t1", "t2")
        t1, t2 = tasks
        engine.post_user_message("also add the flag", task_id="t2")

        wrong = RecordingPhases()
        engine._process_task_chat(run_id, wrong, t1)
        assert wrong.calls == []

        right = RecordingPhases()
        engine._process_task_chat(run_id, right, t2)
        assert right.calls == [("also add the flag", "t2", None)]

    def test_an_untargeted_message_is_unchanged(self, harness: Harness) -> None:
        """No target, two lanes: the shared mailbox, and `_steer_target`'s
        refusal to guess a lane, exactly as before."""
        engine, run_id, tasks = engine_with_tasks(harness, "t1", "t2")
        t1, _ = tasks
        engine.post_user_message("how is it going?")
        lane = RecordingPhases()
        engine._process_task_chat(run_id, lane, t1)
        assert lane.calls == []
        engine._process_chat(run_id, lane, engine._steer_target(t1))
        assert lane.calls == [("how is it going?", None, None)]

    def test_one_lane_still_answers_the_shared_mailbox_alone(self, harness: Harness) -> None:
        """With one task the shared mailbox still names it, so the existing
        steer_task path is untouched."""
        engine, run_id, tasks = engine_with_tasks(harness, "t1", max_parallel_tasks=1)
        (t1,) = tasks
        engine.post_user_message("do it differently")
        lane = RecordingPhases()
        engine._process_chat(run_id, lane, engine._steer_target(t1))
        assert lane.calls == [("do it differently", "t1", None)]

    def test_a_finished_task_hands_its_messages_back(self, harness: Harness) -> None:
        """A message addressed to a task that ended must still be answered,
        as run-level direction, rather than sitting in a dead mailbox."""
        engine, run_id, tasks = engine_with_tasks(harness, "t1", "t2")
        _, t2 = tasks
        engine.post_user_message("also add the flag", task_id="t2")
        engine._release_task_chat(t2)
        lane = RecordingPhases()
        engine._process_task_chat(run_id, lane, t2)
        assert lane.calls == []
        engine._process_chat(run_id, lane, None)
        assert lane.calls == [("also add the flag", None, None)]

    def test_a_message_for_a_task_that_is_not_running_is_still_answered(
        self, harness: Harness
    ) -> None:
        """A target no lane will ever drain must not swallow the message:
        the run-level drain sweeps it back and answers it there."""
        engine, run_id, _ = engine_with_tasks(harness, "t1", "t2")
        engine.post_user_message("for a task that never ran", task_id="nope")
        lane = RecordingPhases()
        engine._process_chat(run_id, lane, None)
        assert lane.calls == [("for a task that never ran", None, None)]

    def test_the_reply_event_names_the_task_and_the_agent(self, harness: Harness) -> None:
        engine, run_id, tasks = engine_with_tasks(harness, "t1", "t2")
        _, t2 = tasks
        engine._assignment = AgentAssignment(
            lead="angie",
            roles={"builder": "scout"},
            agents={"scout": binding("scout", "builder")},
            tasks={"t2": "scout"},
        )
        engine.post_user_message("use the other library", task_id="t2", agent_slug="scout")
        lane = RecordingPhases()
        engine._process_task_chat(run_id, lane, t2)
        # The steer was asked as the mentioned agent.
        assert lane.calls == [("use the other library", "t2", "scout")]
        replies = [e for e in harness.events if e.type == HostEventTypes.CHAT_REPLY]
        assert [(e.data.get("task_id"), e.data.get("agent_slug")) for e in replies] == [
            ("t2", "scout")
        ]

    def test_an_untargeted_reply_event_gains_no_fields(self, harness: Harness) -> None:
        """A run nobody steered by name emits the events it always did."""
        engine, run_id, tasks = engine_with_tasks(harness, "t1", max_parallel_tasks=1)
        (t1,) = tasks
        engine.post_user_message("status?")
        engine._process_chat(run_id, RecordingPhases(), engine._steer_target(t1))
        for event in harness.events:
            if event.type in {HostEventTypes.CHAT_REPLY, HostEventTypes.CHAT_MESSAGE}:
                assert "task_id" not in event.data
                assert "agent_slug" not in event.data

    def test_a_forged_agent_slug_falls_back_to_the_runs_own_voice(self, harness: Harness) -> None:
        """A slug the run's assignment does not have buys no persona."""
        engine, run_id, tasks = engine_with_tasks(harness, "t1", max_parallel_tasks=1)
        (t1,) = tasks
        engine.post_user_message("do it", agent_slug="not-on-this-run")
        lane = RecordingPhases()
        engine._process_chat(run_id, lane, engine._steer_target(t1))
        assert lane.calls == [("do it", "t1", None)]


class TestTheMessageItself:
    def test_a_chat_message_carries_its_target(self) -> None:
        plain = ChatMessage("m1", "text")
        assert (plain.task_id, plain.agent_slug) == (None, None)
        aimed = ChatMessage("m2", "text", task_id="t2", agent_slug="scout")
        assert (aimed.task_id, aimed.agent_slug) == ("t2", "scout")

    def test_a_targeted_message_goes_to_its_own_mailbox(self, harness: Harness) -> None:
        engine, _, _ = engine_with_tasks(harness, "t1", "t2")
        engine.post_user_message("for t2", task_id="t2")
        with pytest.raises(queue.Empty):
            engine._chat_queue.get_nowait()
        assert engine._task_chat_queues["t2"].get_nowait().text == "for t2"


class FakeHandle:
    def __init__(self, run_id: str, channel_id: str | None, assignment_json: str | None) -> None:
        self.run_id = run_id

        class Item:
            pass

        self.item = Item()
        self.item.channel_id = channel_id  # type: ignore[attr-defined]
        self.item.assignment_json = assignment_json  # type: ignore[attr-defined]
        self.item.kind = "code"  # type: ignore[attr-defined]
        self.item.item_id = f"api:{run_id}"  # type: ignore[attr-defined]


ROLE_OF = {"scout": "builder", "critic": "critic"}


def assignment_json(tasks: dict[str, str], agents: tuple[str, ...] = ("scout", "critic")) -> str:
    return AgentAssignment(
        lead="angie",
        roles={ROLE_OF[slug]: slug for slug in agents},
        agents={slug: binding(slug, ROLE_OF[slug]) for slug in agents},
        tasks=tasks,
    ).to_json()


class TestRouteMention:
    """`route_mention` decides which run an `@agent` in a channel is about."""

    def _loop(self, tmp_path: Path) -> Any:
        from tests.unit.test_daemon_loop import Harness as LoopHarness

        return LoopHarness(tmp_path).loop

    def test_no_live_run_is_not_a_steer(self, tmp_path: Path) -> None:
        loop = self._loop(tmp_path)
        assert loop.route_mention("ch1", "scout", "hi", Principal.trusted("me", "chat")) is None

    def test_a_run_in_another_channel_is_not_a_steer(self, tmp_path: Path) -> None:
        loop = self._loop(tmp_path)
        loop._runs["r1"] = FakeHandle("r1", "other", assignment_json({"t1": "scout"}))
        assert loop.live_runs_for_agent("ch1", "scout") == []

    def test_a_run_the_agent_is_not_on_is_not_a_steer(self, tmp_path: Path) -> None:
        loop = self._loop(tmp_path)
        loop._runs["r1"] = FakeHandle("r1", "ch1", assignment_json({"t1": "scout"}))
        assert loop.live_runs_for_agent("ch1", "nobody") == []

    def test_the_one_live_run_is_the_target(self, tmp_path: Path) -> None:
        loop = self._loop(tmp_path)
        loop._runs["r1"] = FakeHandle("r1", "ch1", assignment_json({"t1": "scout"}))
        (target,) = loop.live_runs_for_agent("ch1", "scout")
        assert target.run_id == "r1"

    def test_two_live_runs_in_one_channel_are_ambiguous(self, tmp_path: Path) -> None:
        loop = self._loop(tmp_path)
        loop._runs["r1"] = FakeHandle("r1", "ch1", assignment_json({"t1": "scout"}))
        loop._runs["r2"] = FakeHandle("r2", "ch1", assignment_json({"t9": "scout"}))
        assert len(loop.live_runs_for_agent("ch1", "scout")) == 2
        assert loop.route_mention("ch1", "scout", "hi", Principal.trusted("me", "chat")) is None

    def test_one_task_in_flight_is_steered_by_name(self, tmp_path: Path) -> None:
        loop = self._loop(tmp_path)
        loop._runs["r1"] = FakeHandle("r1", "ch1", assignment_json({"t1": "scout"}))
        loop.store.create_run("r1", "an outcome")
        loop.store.save_tasks("r1", [TaskSpec(id="t1", title="t1")])
        (target,) = loop.live_runs_for_agent("ch1", "scout")
        assert target.task_ids == ("t1",)

    def test_two_tasks_in_flight_fall_back_to_the_run(self, tmp_path: Path) -> None:
        """No single task means no task target: the instruction is run-level
        direction rather than a guess at which lane was meant."""
        loop = self._loop(tmp_path)
        loop._runs["r1"] = FakeHandle("r1", "ch1", assignment_json({"t1": "scout", "t2": "scout"}))
        loop.store.create_run("r1", "an outcome")
        loop.store.save_tasks("r1", [TaskSpec(id="t1", title="t1"), TaskSpec(id="t2", title="t2")])
        (target,) = loop.live_runs_for_agent("ch1", "scout")
        assert sorted(target.task_ids) == ["t1", "t2"]

    def test_a_finished_task_is_not_in_flight(self, tmp_path: Path) -> None:
        loop = self._loop(tmp_path)
        loop._runs["r1"] = FakeHandle("r1", "ch1", assignment_json({"t1": "scout"}))
        loop.store.create_run("r1", "an outcome")
        loop.store.save_tasks("r1", [TaskSpec(id="t1", title="t1")])
        (task,) = loop.store.get_tasks("r1")
        task.state = "done"
        loop.store.update_task("r1", task)
        (target,) = loop.live_runs_for_agent("ch1", "scout")
        assert target.task_ids == ()

    def test_steering_a_run_that_is_not_really_live_is_refused_by_name(
        self, tmp_path: Path
    ) -> None:
        """The handle says the run is in flight but the engine is not this
        loop's: the refusal is the control service's, not a crash."""
        loop = self._loop(tmp_path)
        loop._runs["r1"] = FakeHandle("r1", "ch1", assignment_json({}))
        with pytest.raises((ControlError, AttributeError)):
            loop.route_mention("ch1", "scout", "hi", Principal.trusted("me", "chat"))


def test_the_steering_request_accepts_a_task_and_an_agent() -> None:
    from sbxloop.api.models import SteerRequest

    plain = SteerRequest(text="do it")
    assert (plain.task_id, plain.agent_slug) == (None, None)
    aimed = SteerRequest(text="do it", task_id="t2", agent_slug="scout")
    assert (aimed.task_id, aimed.agent_slug) == ("t2", "scout")


class TestStopFromChat:
    """Stopping stays explicit: a word that means stop, never an inference
    from a message that merely sounds urgent."""

    def test_the_stop_words_are_recognised(self) -> None:
        from sbxloop.daemon.controls.steering import stop_command

        assert stop_command("/stop", None) == "channel"
        assert stop_command("  /CANCEL  ", None) == "channel"
        assert stop_command("@scout stop", "scout") == "agent"
        assert stop_command("@scout  STOP", "scout") == "agent"

    def test_anything_else_is_not_a_stop(self) -> None:
        from sbxloop.daemon.controls.steering import stop_command

        # A mention of the word, not the command.
        assert stop_command("@scout stop using that library", "scout") is None
        assert stop_command("please stop", None) is None
        assert stop_command("/stop the presses", None) is None
        # The command has to name the agent whose turn this is.
        assert stop_command("@critic stop", "scout") is None
        assert stop_command("", None) is None

    def test_a_channel_stop_cancels_every_run_in_that_channel(self, tmp_path: Path) -> None:
        from tests.unit.test_daemon_loop import Harness as LoopHarness

        loop = LoopHarness(tmp_path).loop
        cancelled: list[str] = []

        def cancel(run_id: str, **kw: Any) -> CancelOutcome:
            cancelled.append(run_id)
            return CancelOutcome(mode="current", target=run_id, message="stopping")

        loop.cancel_run = cancel  # type: ignore[assignment]
        loop._runs["r1"] = FakeHandle("r1", "ch1", assignment_json({"t1": "scout"}))
        loop._runs["r2"] = FakeHandle("r2", "ch1", assignment_json({"t1": "critic"}))
        loop._runs["r3"] = FakeHandle("r3", "other", assignment_json({"t1": "scout"}))
        stopped = loop.stop_channel("ch1", Principal.trusted("me", "chat"))
        assert sorted(stopped) == ["r1", "r2"]
        assert sorted(cancelled) == ["r1", "r2"]

    def test_an_agent_stop_cancels_only_that_agents_runs(self, tmp_path: Path) -> None:
        from tests.unit.test_daemon_loop import Harness as LoopHarness

        loop = LoopHarness(tmp_path).loop
        cancelled: list[str] = []

        def cancel(run_id: str, **kw: Any) -> CancelOutcome:
            cancelled.append(run_id)
            return CancelOutcome(mode="current", target=run_id, message="stopping")

        loop.cancel_run = cancel  # type: ignore[assignment]
        loop._runs["r1"] = FakeHandle("r1", "ch1", assignment_json({"t1": "scout"}))
        loop._runs["r2"] = FakeHandle(
            "r2", "ch1", assignment_json({"t1": "critic"}, agents=("critic",))
        )
        stopped = loop.stop_channel("ch1", Principal.trusted("me", "chat"), agent_slug="scout")
        assert stopped == ["r1"]
        assert cancelled == ["r1"]
