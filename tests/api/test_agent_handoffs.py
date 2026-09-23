"""Agent-originated handoffs are durable, scoped and bounded, not parsed prose."""

import io
import json
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sbxloop import telemetry
from sbxloop.api.collaboration import CollaborationError
from sbxloop.api.context import _visible_agent_reply
from sbxloop.daemon.concierge import ConciergeReply
from sbxloop.errors import ToolRejectedError
from sbxloop.log import configure_logging
from tests.api.conftest import build
from tests.api.test_collaboration import FakeConcierge, bearer, register
from tests.api.test_collaboration_controls import Blocking
from tests.api.test_collaboration_recovery import settled
from tests.unit.test_daemon_concierge import make


class HandoffConcierge(FakeConcierge):
    def submit_turn(self, text: str, **kwargs: Any) -> Any:
        role = kwargs["agent_role"]
        if not self.calls:
            first = kwargs["handoff"]("critic", "Check the proposed plan")
            assert kwargs["handoff"]("critic", "Check the proposed plan") == first
        elif role == "critic":
            kwargs["handoff"]("builder", "Explain how you would address the concern")
        return super().submit_turn(text, **kwargs)


class RevisionLoopConcierge(FakeConcierge):
    """Agents choose a review return path; the transport only runs the queue."""

    def submit_turn(self, text: str, **kwargs: Any) -> Any:
        index = len(self.calls)
        role = kwargs["agent_role"]
        if index == 0:
            assert role == "concierge"
            kwargs["handoff"]("planner", "Draft the requested artifact.")
        elif index == 1:
            assert role == "planner"
            kwargs["handoff"]("critic", "Review this draft and identify material gaps.")
        elif index == 2:
            assert role == "critic"
            kwargs["handoff"]("planner", "Revise the draft to address these findings.")
        elif index == 3:
            assert role == "planner"
            kwargs["handoff"]("concierge", "Give the person this revised final artifact.")
        else:
            assert index == 4 and role == "concierge"
        return super().submit_turn(text, **kwargs)


def test_tool_handoffs_run_with_peer_context_and_inherited_read_only(api: Any) -> None:
    concierge = HandoffConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@planner assess this idea"},
    ).json()
    done = settled(api.client, headers, channel, accepted["turn"]["id"])
    assert done["status"] == "completed", done
    assert [c["agent_role"] for c in concierge.calls] == ["planner", "critic", "builder"]
    assert [c["read_only"] for c in concierge.calls] == [False, True, True]
    assert "reply from planner" in concierge.calls[1]["history"]
    assert "Check the proposed plan" in concierge.calls[1]["text"]
    assert "Completed result from @planner:\nreply from planner" in concierge.calls[1]["text"]
    assert "Completed result from @critic:\nreply from critic" in concierge.calls[2]["text"]
    assert "@planner assess this idea" in concierge.calls[1]["text"]
    assert [p["requested_by"] for p in done["participants"]] == [None, "planner", "critic"]
    assert all(p["status"] == "completed" for p in done["participants"])
    assert len({c["message_id"] for c in concierge.calls}) == 3
    assert done["targets"] == ["planner"]
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    assert len([m for m in messages if m["kind"] == "agent_handoff"]) == 2


def test_agents_can_choose_a_bounded_review_revision_and_synthesis_loop(api: Any) -> None:
    concierge = RevisionLoopConcierge()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@concierge prepare and review a release checklist"},
    ).json()
    done = settled(api.client, headers, channel, accepted["turn"]["id"])

    assert done["status"] == "completed", done
    assert [call["agent_role"] for call in concierge.calls] == [
        "concierge",
        "planner",
        "critic",
        "planner",
        "concierge",
    ]
    assert [call["read_only"] for call in concierge.calls] == [
        False,
        False,
        True,
        True,
        True,
    ]
    assert "Completed result from @critic:\nreply from critic" in concierge.calls[3]["text"]
    assert "Completed result from @planner:\nreply from planner" in concierge.calls[4]["text"]
    assert all(participant["status"] == "completed" for participant in done["participants"])


def test_handoff_work_product_is_visible_and_reaches_the_peer(api: Any, tmp_path: Path) -> None:
    checklist = (
        "1. Run the release checks.\n"
        "2. Verify the staged deployment.\n"
        "3. Confirm the rollback path."
    )
    concierge, client, _, _, _ = make(
        tmp_path / "concierge",
        [
            {
                "calls": [
                    (
                        "handoff_agent",
                        {
                            "agent_slug": "critic",
                            "message": "Review the checklist for missing risks.",
                            "work_product": checklist,
                        },
                    )
                ],
                "text": "The critic is queued and will reply shortly.",
            },
            {"text": "The checklist also needs an owner for rollback."},
        ],
    )
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    try:
        accepted = api.client.post(
            f"/v1/channels/{channel}/turns",
            headers=headers,
            json={"content": "@planner draft a checklist and ask a peer to review it"},
        ).json()
        done = settled(api.client, headers, channel, accepted["turn"]["id"])
        assert done["status"] == "completed", done
        messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
        planner = next(
            message
            for message in messages
            if message["kind"] == "agent_result" and message.get("agent_slug") == "planner"
        )
        handoff = next(message for message in messages if message["kind"] == "agent_handoff")
        assert planner["content"].startswith(checklist)
        assert "queued and will reply shortly" in planner["content"]
        assert checklist not in handoff["content"]
        assert "Review the checklist for missing risks." in handoff["content"]
        assert checklist in client.jobs[1].prompt
    finally:
        concierge.close()


def test_visible_reply_does_not_repeat_a_reformatted_work_product() -> None:
    artifact = (
        "## Angie Release Checklist\n"
        "- [ ] Verify the intended version is running.\n"
        "- [ ] Exercise one canary workload from intake through publication.\n"
        "- [ ] Confirm the rollback trigger and responsible owner."
    )
    reply = (
        "Here is the completed checklist.\n\n"
        "## Angie Release Checklist\n"
        "- Verify the intended version is running.\n"
        "- Exercise one canary workload from intake through publication.\n"
        "- Confirm the rollback trigger and responsible owner.\n\n"
        "The critic is reviewing it now."
    )

    assert _visible_agent_reply(reply, (artifact,)) == reply


@pytest.fixture
def handoff_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[io.StringIO, list[dict[str, Any]]]]:
    """Exercise the real logging processor without sending a remote report."""
    stream = io.StringIO()
    reports: list[dict[str, Any]] = []
    monkeypatch.setattr(telemetry, "_client", SimpleNamespace(capture_event=reports.append))
    configure_logging("DEBUG", fmt="json", stream=stream)
    try:
        yield stream, reports
    finally:
        configure_logging("DEBUG")


@pytest.mark.parametrize(
    ("agent", "message", "reason"),
    [
        ("planner", "Review my own plan", "Address a different agent."),
        ("unknown", "Review this plan", "Choose a native sbxloop agent."),
        ("critic", " ", "Provide a message of 1 to 4000 characters."),
        (42, "Review this plan", "An agent slug and message are required."),
        ("critic", None, "An agent slug and message are required."),
    ],
)
def test_rejected_handoff_returns_feedback_without_reporting_a_crash(
    api: Any,
    tmp_path: Path,
    handoff_diagnostics: tuple[io.StringIO, list[dict[str, Any]]],
    agent: Any,
    message: Any,
    reason: str,
) -> None:
    concierge, client, _, _, _ = make(
        tmp_path / "concierge",
        [
            {
                "calls": [
                    (
                        "handoff_agent",
                        {
                            "agent_slug": agent,
                            "message": message,
                            "work_product": "Draft plan",
                        },
                    ),
                    (
                        "handoff_agent",
                        {
                            "agent_slug": "critic",
                            "message": "Review this plan",
                            "work_product": "Draft plan",
                        },
                    ),
                ],
                "text": "I asked the critic to review the plan.",
            },
            {"text": "The plan is ready."},
        ],
    )
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    try:
        accepted = api.client.post(
            f"/v1/channels/{channel}/turns",
            headers=headers,
            json={"content": "@planner assess this idea"},
        ).json()
        done = settled(api.client, headers, channel, accepted["turn"]["id"])
        assert done["status"] == "completed", done
        assert [response.ok for response in client.responses] == [False, True]
        assert reason in client.responses[0].error
        assert reason in client.responses[0].text
        assert [participant["agent_slug"] for participant in done["participants"]] == [
            "planner",
            "critic",
        ]
        assert all(participant["status"] == "completed" for participant in done["participants"])
        messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
        assert len([message for message in messages if message["kind"] == "agent_handoff"]) == 1
        stream, reports = handoff_diagnostics
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        refusals = [record for record in records if record["event"] == "concierge.tool_rejected"]
        assert len(refusals) == 1
        assert refusals[0]["tool"] == "handoff_agent"
        assert reason in json.dumps(refusals[0])
        assert "exception" not in refusals[0]
        assert reports == []
    finally:
        concierge.close()


def test_unexpected_handoff_failure_keeps_its_traceback_and_report(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handoff_diagnostics: tuple[io.StringIO, list[dict[str, Any]]],
) -> None:
    concierge, client, _, _, _ = make(
        tmp_path / "concierge",
        [
            {
                "calls": [
                    (
                        "handoff_agent",
                        {
                            "agent_slug": "critic",
                            "message": "Review this plan",
                            "work_product": "Draft plan",
                        },
                    )
                ],
                "text": "The peer request failed.",
            }
        ],
    )

    def fail_handoff(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("handoff storage unavailable")

    monkeypatch.setattr(api.ctx.collaboration, "queue_handoff", fail_handoff)
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    try:
        accepted = api.client.post(
            f"/v1/channels/{channel}/turns",
            headers=headers,
            json={"content": "@planner assess this idea"},
        ).json()
        done = settled(api.client, headers, channel, accepted["turn"]["id"])
        assert done["status"] == "completed", done
        assert len(client.responses) == 1
        assert not client.responses[0].ok
        assert "RuntimeError: handoff storage unavailable" in client.responses[0].error
        assert len(done["participants"]) == 1
        stream, reports = handoff_diagnostics
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        failures = [record for record in records if record["event"] == "concierge.tool_failed"]
        assert len(failures) == 1
        local_error = failures[0]["exception"][0]
        assert local_error["exc_type"] == "RuntimeError"
        assert local_error["exc_value"] == "handoff storage unavailable"
        assert local_error["frames"]
        assert len(reports) == 1
        assert reports[0]["message"]["message"] == "concierge.tool_failed"
        error = reports[0]["exception"]["values"][0]
        assert error["type"] == "RuntimeError"
        assert error["stacktrace"]["frames"][-1]["function"] == "fail_handoff"
    finally:
        concierge.close()


def setup_turn(api: Any, targets: tuple[str, ...] = ("planner",)) -> tuple[Any, str, str, str]:
    headers = bearer(register(api))
    user = api.client.get("/v1/users/me", headers=headers).json()["id"]
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    store = api.ctx.collaboration
    turn, _, _ = store.accept_turn(
        user,
        channel,
        content="original",
        targets=targets,
        client_turn_id="root",
        client_message_id=None,
        actor=None,
        now=api.clock(),
        intent="delegate",
    )
    store.start_turn(turn.id, api.clock())
    store.participant_started(turn.id, 0, api.clock())
    return store, user, channel, turn.id


def test_handoff_limits_scope_cancellation_and_restart(api: Any) -> None:
    store, user, channel, turn = setup_turn(api)

    def ask(index: int, role: str, text: str = "inspect") -> str:
        return store.queue_handoff(user, channel, turn, index, role, text, api.clock())

    with pytest.raises(CollaborationError):
        store.queue_handoff(user, "other", turn, 0, "critic", "inspect", api.clock())
    with pytest.raises(CollaborationError):
        ask(0, "unknown")
    with pytest.raises(CollaborationError):
        ask(0, "planner")
    first = ask(0, "critic")
    assert ask(0, "critic") == first
    ask(0, "builder")
    with pytest.raises(CollaborationError, match="two"):
        ask(0, "operator")
    store.append_reply(
        turn, agent_slug="planner", content="plan", now=api.clock(), participant_index=0
    )
    store.participant_started(turn, 1, api.clock())
    ask(1, "planner")
    store.participant_started(turn, 3, api.clock())
    ask(3, "critic")
    store.participant_started(turn, 4, api.clock())
    ask(4, "operator")
    store.participant_started(turn, 5, api.clock())
    with pytest.raises(CollaborationError, match="depth"):
        ask(5, "builder")
    store.cancel_turn(user, channel, turn, api.clock())
    with pytest.raises(CollaborationError):
        ask(5, "builder")
    assert store.recover_turns(api.clock()) == []
    assert store.get_turn(user, channel, turn).status == "cancelled"


def test_restart_does_not_mistake_root_reply_for_finished_handoffs(api: Any) -> None:
    store, user, channel, turn = setup_turn(api)
    store.queue_handoff(user, channel, turn, 0, "critic", "review", api.clock())
    store.append_reply(
        turn, agent_slug="planner", content="plan", now=api.clock(), participant_index=0
    )
    assert store.recover_turns(api.clock()) == []
    assert store.get_turn(user, channel, turn).status == "failed"


def test_six_handoffs_is_a_shared_budget_across_a_team(api: Any) -> None:
    store, user, channel, turn = setup_turn(api, ("planner", "operator", "concierge"))
    for index in range(3):
        store.participant_started(turn, index, api.clock())
        for target in ("builder", "critic"):
            store.queue_handoff(user, channel, turn, index, target, "inspect", api.clock())
    with pytest.raises(CollaborationError, match="six"):
        store.queue_handoff(user, channel, turn, 2, "planner", "another", api.clock())
    assert len(store.get_turn(user, channel, turn).participants) == 9


def test_completed_handoffs_are_not_replayed_on_recovery(api: Any) -> None:
    store, user, channel, turn = setup_turn(api)
    store.queue_handoff(user, channel, turn, 0, "critic", "review", api.clock())
    for index, target in enumerate(("planner", "critic")):
        store.participant_started(turn, index, api.clock())
        store.append_reply(
            turn, agent_slug=target, content="done", now=api.clock(), participant_index=index
        )
    assert store.recover_turns(api.clock()) == []
    assert store.get_turn(user, channel, turn).status == "completed"


def test_conversation_has_no_handoff(api: Any) -> None:
    api.ctx.concierge = FakeConcierge()
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    response = api.client.post(
        f"/v1/channels/{channel}/turns", headers=headers, json={"content": "hello"}
    ).json()
    settled(api.client, headers, channel, response["turn"]["id"])
    assert api.ctx.concierge.calls[0]["handoff"] is None


def test_stop_skips_dynamically_queued_peers_and_prose_does_not_dispatch(api: Any) -> None:
    concierge = Blocking()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    route = f"/v1/channels/{channel}/turns"
    turn = api.client.post(route, headers=headers, json={"content": "@planner help"}).json()["turn"]
    try:
        deadline = time.monotonic() + 2
        while not concierge.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        concierge.calls[0]["handoff"]("critic", "Review this")
        assert api.client.post(f"{route}/{turn['id']}/cancel", headers=headers).status_code == 200
        with pytest.raises(ToolRejectedError, match="no longer running"):
            concierge.calls[0]["handoff"]("builder", "Too late")
        concierge.first.set_result(ConciergeReply("@operator could help too"))
        assert api.ctx.turns.wait_idle(timeout=5)
        done = settled(api.client, headers, channel, turn["id"])
        assert done["status"] == "cancelled"
        assert [p["status"] for p in done["participants"]] == ["completed", "cancelled"]
        assert len(concierge.calls) == 1
    finally:
        if not concierge.first.done():
            concierge.first.set_result(ConciergeReply("released"))


def test_prose_mentions_alone_do_not_dispatch_a_peer_into_this_turn(api: Any) -> None:
    """`handoff_agent` is the only thing that grows a turn a peer. A prose
    mention in a reply leaves this turn's participants exactly as they
    were; now that agents address each other it starts a turn of its own
    instead (tests/api/test_agent_mentions.py), which is not this one."""
    concierge = Blocking()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    route = f"/v1/channels/{channel}/turns"
    turn = api.client.post(route, headers=headers, json={"content": "@planner help"}).json()["turn"]
    concierge.first.set_result(ConciergeReply("@critic should review this"))
    done = settled(api.client, headers, channel, turn["id"])
    assert done["status"] == "completed"
    assert [p["agent_slug"] for p in done["participants"]] == ["planner"]
    assert concierge.calls[0]["session_key"] == f"{channel}:planner"
    # Whatever the mention started belongs to another turn, never this one.
    assert api.ctx.turns.wait_idle(timeout=5)
    again = api.client.get(f"{route}/{turn['id']}", headers=headers).json()
    assert [p["agent_slug"] for p in again["participants"]] == ["planner"]


class WorkHandoffConcierge(FakeConcierge):
    """Each participant, in order, hands the person's ask to the next peer
    in ``chain``; the transport records what every participant was offered."""

    def __init__(self, chain: tuple[str, ...]) -> None:
        super().__init__()
        self.chain = chain

    def submit_turn(self, text: str, **kwargs: Any) -> Any:
        index = len(self.calls)
        if index < len(self.chain):
            kwargs["handoff"](self.chain[index], f"Carry on with step {index + 1}")
        return super().submit_turn(text, **kwargs)


SCOUT_SPEC: dict[str, Any] = {
    "slug": "scout",
    "name": "Scout",
    "instructions": "Gather the facts first.",
    "roles": ["planner"],
    "can_start": ["workload"],
}


def _start_run(call: dict[str, Any], ask: str) -> str:
    """Invoke the ``start_run`` a participant was offered; the refusal text
    when it was refused, else the text the agent reads."""
    (tool,) = [tool for tool in call["agent_tools"] if tool.spec.name == "start_run"]
    try:
        return str(tool.impl({"kind": "workload", "ask": ask}))
    except ToolRejectedError as exc:
        return f"refused: {exc}"


def test_a_handoff_peer_starts_work_one_hop_deeper_than_the_agent_that_handed_off(
    tmp_path: Path,
) -> None:
    """Agent-started work counts its chain depth through handoffs: a person's
    turn is depth 0, so the agent the person asked starts work at depth 1,
    and a peer it hands off to sits at depth 1 and would start at depth 2.
    With ``max_chain_depth = 1`` the peer's start is what the knob refuses."""
    built = build(
        tmp_path,
        config={
            "agent_team": {"max_chain_depth": 1},
            "workloads": [{"name": "research", "sinks": ["chat"]}],
        },
    )
    with built.client:
        concierge = WorkHandoffConcierge(("helper",))
        built.ctx.concierge = concierge
        headers = bearer(register(built))
        for spec in (SCOUT_SPEC, {**SCOUT_SPEC, "slug": "helper", "name": "Helper"}):
            created = built.client.post("/v1/agents", json=spec, headers=headers)
            assert created.status_code == 201, created.text
        channel = built.client.post("/v1/channels", json={}, headers=headers).json()["id"]
        accepted = built.client.post(
            f"/v1/channels/{channel}/turns",
            headers=headers,
            json={"content": "@scout research the options", "intent": "delegate"},
        ).json()
        done = settled(built.client, headers, channel, accepted["turn"]["id"])
        assert done["status"] == "completed", done
        assert [c["session_key"] for c in concierge.calls] == [
            f"{channel}:scout",
            f"{channel}:helper",
        ]
        assert [p["requested_by"] for p in done["participants"]] == [None, "scout"]
        # The agent the person asked is at depth 0: the chain-depth knob
        # does not refuse its start.
        assert "max_chain_depth" not in _start_run(concierge.calls[0], "compare the options")
        # Its peer is one hop deeper, which is the knob's limit.
        refusal = _start_run(concierge.calls[1], "compare them again")
        assert refusal.startswith("refused: ")
        assert "1 agent-started hop deep" in refusal
        assert "max_chain_depth is 1" in refusal
    built.ctx.close()


def test_a_handoff_from_an_agent_that_may_start_work_keeps_every_peer_guarded(api: Any) -> None:
    """A handoff never grants a peer more starting power than the agent that
    handed off had. An agent whose starts answer to its ``can_start``
    guardrails is never offered the concierge's unguarded start tools, so
    the peers it hands off to (a built-in, and through it Angie) are not
    offered them either; the peers are still not demoted to read-only. A
    handoff from an agent that declares no ``can_start`` is unchanged."""
    concierge = WorkHandoffConcierge(("planner", "concierge"))
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    assert api.client.post("/v1/agents", json=SCOUT_SPEC, headers=headers).status_code == 201
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@scout file and label an issue for the options", "intent": "delegate"},
    ).json()
    done = settled(api.client, headers, channel, accepted["turn"]["id"])
    assert done["status"] == "completed", done
    assert [p["agent_slug"] for p in done["participants"]] == ["scout", "planner", "concierge"]
    assert [c["guarded_start"] for c in concierge.calls] == [False, True, True]
    assert [c["read_only"] for c in concierge.calls] == [False, False, False]
    assert [c["start_work"] for c in concierge.calls] == [True, True, True]

    concierge = WorkHandoffConcierge(("concierge",))
    api.ctx.concierge = concierge
    accepted = api.client.post(
        f"/v1/channels/{channel}/turns",
        headers=headers,
        json={"content": "@planner file and label an issue for the options", "intent": "delegate"},
    ).json()
    done = settled(api.client, headers, channel, accepted["turn"]["id"])
    assert done["status"] == "completed", done
    assert [p["agent_slug"] for p in done["participants"]] == ["planner", "concierge"]
    assert [c["guarded_start"] for c in concierge.calls] == [False, False]
