"""Steering and stopping from a chat turn (plan S-A11), through the turn path.

A mention of an agent working a live run in the channel is direction for
that run: the turn records `steered_run_id` and the agent acknowledges it,
rather than answering from scratch. A mention with no live run is an
ordinary turn. `/stop` and `@agent stop` cancel through the control
service, under the capabilities of the person who typed them, so a
workspace member who may not cancel a run through the API may not cancel it
from chat either. Only a person's own mention steers: a peer an agent hands
off to answers the request it was handed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from alembic import command

from sbxloop.agents.assignment import AgentAssignment, plan_assignment
from sbxloop.daemon.controls.results import CancelOutcome
from sbxloop.db import open_engine
from sbxloop.db.schema import _config
from sbxloop.engine.model import TaskSpec
from tests.api.conftest import Api
from tests.api.test_channel_access import _invite
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_recovery import settled
from tests.api.test_control import in_flight, run_public
from tests.unit.test_daemon_loop import gh_item


class RecordingEngine:
    """The engine of a run in flight, as far as steering reaches it."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str | None, str | None]] = []

    def post_user_message(
        self, text: str, *, task_id: str | None = None, agent_slug: str | None = None
    ) -> str:
        self.messages.append((text, task_id, agent_slug))
        return f"msg-{len(self.messages)}"


class LiveHandle:
    """A run in flight in ``channel_id`` with ``assignment_json``."""

    def __init__(self, run_id: str, channel_id: str, assignment_json: str) -> None:
        self.run_id = run_id
        self.engine = RecordingEngine()

        class Item:
            pass

        self.item = Item()
        self.item.channel_id = channel_id  # type: ignore[attr-defined]
        self.item.assignment_json = assignment_json  # type: ignore[attr-defined]
        self.item.kind = "code"  # type: ignore[attr-defined]
        self.item.item_id = f"api:{run_id}"  # type: ignore[attr-defined]


def _planned(api: Api, channel_id: str) -> AgentAssignment:
    """The assignment a code run in ``channel_id`` is admitted with, the
    way the loop's `_assign` plans it: roles and agents, no tasks."""
    return plan_assignment(
        api.loop.agents, kind="code", lead=None, requested={}, channel_id=channel_id
    )


def _live(api: Api, channel_id: str, assignment: AgentAssignment, run_id: str = "r1") -> LiveHandle:
    handle = LiveHandle(run_id, channel_id, assignment.to_json())
    api.loop._runs[run_id] = handle
    api.harness.store.create_run(run_id, "an outcome", kind="code")
    return handle


def _cancels(api: Api) -> list[str]:
    cancelled: list[str] = []

    def cancel(run_id: str, **_: Any) -> CancelOutcome:
        cancelled.append(run_id)
        return CancelOutcome(mode="current", target=run_id, message="stopping")

    api.loop.cancel_run = cancel
    return cancelled


def _turn(api: Api, headers: dict[str, str], channel: str, content: str) -> dict[str, Any]:
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns", headers=headers, json={"content": content}
    )
    assert accepted.status_code == 202, accepted.text
    return settled(api.client, headers, channel, accepted.json()["turn"]["id"])


def _replies(api: Api, headers: dict[str, str], channel: str) -> list[dict[str, Any]]:
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    return [m for m in messages if m["role"] == "assistant"]


def _channel(api: Api, headers: dict[str, str], visibility: str | None = None) -> str:
    channel = str(api.client.post("/v1/channels", json={}, headers=headers).json()["id"])
    if visibility is not None:
        changed = api.client.patch(
            f"/v1/channels/{channel}", json={"visibility": visibility}, headers=headers
        )
        assert changed.status_code == 200, changed.text
    return channel


class TestMentionSteering:
    def test_a_mention_of_an_agent_on_a_live_run_steers_it(self, api: Api) -> None:
        concierge = FakeConcierge()
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        channel = _channel(api, headers)
        handle = _live(api, channel, _planned(api, channel))

        done = _turn(api, headers, channel, "@builder also add the flag")

        assert done["status"] == "completed", done
        assert done["steered_run_id"] == "r1"
        assert handle.engine.messages == [("@builder also add the flag", None, "builder")]
        # The agent acknowledged the direction; it did not answer afresh.
        assert concierge.calls == []
        (reply,) = _replies(api, headers, channel)
        assert reply["agent_slug"] == "builder"
        assert "Taken as direction for run `r1`" in reply["content"]

    def test_a_mention_with_no_live_run_is_an_ordinary_turn(self, api: Api) -> None:
        concierge = FakeConcierge()
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        channel = _channel(api, headers)

        done = _turn(api, headers, channel, "@builder how would you do it?")

        assert done["status"] == "completed", done
        assert done.get("steered_run_id") is None
        assert [c["agent_role"] for c in concierge.calls] == ["builder"]
        (reply,) = _replies(api, headers, channel)
        assert reply["content"] == "reply from builder"

    def test_the_mention_steers_the_one_task_the_engine_gave_that_agent(self, api: Api) -> None:
        """The admission snapshot has no tasks; the engine records who does
        each task after planning. A mention finds the task there."""
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _channel(api, headers)
        handle = _live(api, channel, _planned(api, channel))
        store = api.harness.store
        store.save_tasks("r1", [TaskSpec(id="t1", title="t1"), TaskSpec(id="t2", title="t2")])
        store.set_task_assignees("r1", {"t1": "builder", "t2": "builder"})
        done_task = next(t for t in store.get_tasks("r1") if t.spec.id == "t2")
        done_task.state = "done"
        store.update_task("r1", done_task)

        done = _turn(api, headers, channel, "@builder use the other library")

        assert done["steered_run_id"] == "r1"
        assert handle.engine.messages == [("@builder use the other library", "t1", "builder")]

    def test_a_peer_handed_off_to_answers_the_request_instead_of_steering(self, api: Api) -> None:
        """The person mentioned the planner; the planner handed off to the
        builder, which is working a run here. The builder answers the
        planner's request; the person's words are not sent to the run."""

        class Handoff(FakeConcierge):
            def submit_turn(self, text: str, **kwargs: Any) -> Any:
                if kwargs["agent_role"] == "planner":
                    kwargs["handoff"]("builder", "Say how long the build takes")
                return super().submit_turn(text, **kwargs)

        concierge = Handoff()
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        channel = _channel(api, headers)
        planned = _planned(api, channel)
        only_builder = AgentAssignment(
            lead=planned.lead,
            roles={"builder": "builder"},
            agents={"builder": planned.agents["builder"]},
            channel_id=channel,
        )
        handle = _live(api, channel, only_builder)

        done = _turn(api, headers, channel, "@planner plan the release")

        assert done["status"] == "completed", done
        assert done.get("steered_run_id") is None
        assert handle.engine.messages == []
        assert [c["agent_role"] for c in concierge.calls] == ["planner", "builder"]
        assert "Say how long the build takes" in concierge.calls[1]["text"]

    def test_an_agent_mentioning_an_agent_on_a_live_run_does_not_steer_it(self, api: Api) -> None:
        """An agent's reply that mentions the builder starts a follow-up
        turn by the builder (#1212). That turn speaks as the agent, not the
        person, so it answers as a peer and never reaches the run."""
        from tests.api.test_agent_mentions import ScriptedConcierge

        concierge = ScriptedConcierge({"planner": "Plan ready. @builder how long will it take?"})
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        channel = _channel(api, headers)
        planned = _planned(api, channel)
        only_builder = AgentAssignment(
            lead=planned.lead,
            roles={"builder": "builder"},
            agents={"builder": planned.agents["builder"]},
            channel_id=channel,
        )
        handle = _live(api, channel, only_builder)

        _turn(api, headers, channel, "@planner plan the release")
        assert api.ctx.turns.wait_idle(timeout=10), "the channel never went quiet"

        turns = api.client.get(f"/v1/channels/{channel}/turns", headers=headers).json()
        follow_up = [t for t in turns if t["trigger"] == "mention"]
        assert [t["targets"] for t in follow_up] == [["builder"]]
        assert all(t.get("steered_run_id") is None for t in turns)
        assert handle.engine.messages == []
        assert [c["session_key"].rsplit(":", 1)[-1] for c in concierge.calls] == [
            "planner",
            "builder",
        ]


class TestStopFromChat:
    def test_a_channel_stop_cancels_the_channels_runs(self, api: Api) -> None:
        concierge = FakeConcierge()
        api.ctx.concierge = concierge
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _live(api, channel, _planned(api, channel))
        cancelled = _cancels(api)

        done = _turn(api, headers, channel, "/stop")

        assert done["status"] == "completed", done
        assert cancelled == ["r1"]
        assert concierge.calls == []
        (reply,) = _replies(api, headers, channel)
        assert "Stopping `r1`" in reply["content"]

    def test_an_agent_stop_cancels_that_agents_runs(self, api: Api) -> None:
        api.ctx.concierge = FakeConcierge()
        headers = bearer(register(api))
        channel = _channel(api, headers)
        _live(api, channel, _planned(api, channel))
        cancelled = _cancels(api)

        _turn(api, headers, channel, "@builder stop")

        assert cancelled == ["r1"]

    def test_a_member_may_not_stop_runs_from_chat(self, api: Api) -> None:
        """`runs:control` stays with admins: the API refuses a member's
        cancel, and so does chat."""
        concierge = FakeConcierge()
        api.ctx.concierge = concierge
        owner = bearer(register(api))
        guest = bearer(_invite(api, "member", "guest"))
        channel = _channel(api, owner, visibility="workspace")
        _live(api, channel, _planned(api, channel))
        cancelled = _cancels(api)

        done = _turn(api, guest, channel, "/stop")

        assert done["status"] == "completed", done
        assert cancelled == []
        (reply,) = _replies(api, guest, channel)
        assert "do not have permission to stop runs" in reply["content"]

    def test_a_member_may_still_steer_by_mention(self, api: Api) -> None:
        """`runs:steer` is a member's, so a member's mention steers, and the
        record names the person, not a display name."""
        api.ctx.concierge = FakeConcierge()
        owner = bearer(register(api))
        guest = bearer(_invite(api, "member", "guest"))
        channel = _channel(api, owner, visibility="workspace")
        handle = _live(api, channel, _planned(api, channel))

        done = _turn(api, guest, channel, "@builder also add the flag")

        assert done["steered_run_id"] == "r1"
        assert handle.engine.messages == [("@builder also add the flag", None, "builder")]
        guest_id = api.client.get("/v1/users/me", headers=guest).json()["id"]
        listed = api.client.get(f"/v1/runs/{run_public('r1')}/steering", headers=owner)
        assert listed.status_code == 200, listed.text
        (record,) = listed.json()["data"]
        assert record["actor"]["id"] == guest_id


def test_the_steering_route_passes_the_task_and_agent_to_the_run(api: Api) -> None:
    thread, run_id, release = in_flight(api, gh_item("1"))
    try:
        response = api.client.post(
            f"/v1/runs/{run_public(run_id)}/steering",
            json={"text": "use the other library", "task_id": "t2", "agent_slug": "builder"},
            headers=api.bearer(),
        )
        assert response.status_code == 202, response.text
        handle = api.loop._current
        assert handle is not None
        queued = handle.engine._task_chat_queues["t2"].get_nowait()
        assert (queued.text, queued.task_id, queued.agent_slug) == (
            "use the other library",
            "t2",
            "builder",
        )
    finally:
        release.set()
        thread.join(10)


def test_revision_0031_adds_the_column_and_is_safe_to_run_twice(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    engine = open_engine(path)
    try:
        with engine.begin() as conn:
            command.upgrade(_config(conn), "0031")
        with engine.begin() as conn:
            command.downgrade(_config(conn), "0030")
        with engine.begin() as conn:
            command.upgrade(_config(conn), "0031")
        with engine.begin() as conn:
            command.stamp(_config(conn), "0030")
        with engine.begin() as conn:
            # The column is already there: the guard keeps the rerun a no-op.
            command.upgrade(_config(conn), "0031")
    finally:
        engine.dispose()
    conn2 = sqlite3.connect(path)
    try:
        columns = {row[1] for row in conn2.execute("PRAGMA table_info(collaboration_turns)")}
    finally:
        conn2.close()
    assert "steered_run_id" in columns
