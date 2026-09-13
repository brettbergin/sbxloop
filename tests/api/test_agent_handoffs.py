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
from sbxloop.daemon.concierge import ConciergeReply
from sbxloop.errors import ToolRejectedError
from sbxloop.log import configure_logging
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
    assert "@planner assess this idea" in concierge.calls[1]["text"]
    assert [p["requested_by"] for p in done["participants"]] == [None, "planner", "critic"]
    assert all(p["status"] == "completed" for p in done["participants"])
    assert len({c["message_id"] for c in concierge.calls}) == 3
    assert done["targets"] == ["planner"]
    messages = api.client.get(f"/v1/channels/{channel}/messages", headers=headers).json()
    assert len([m for m in messages if m["kind"] == "agent_handoff"]) == 2


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
                    ("handoff_agent", {"agent_slug": agent, "message": message}),
                    ("handoff_agent", {"agent_slug": "critic", "message": "Review this plan"}),
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
                    ("handoff_agent", {"agent_slug": "critic", "message": "Review this plan"})
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
    with pytest.raises(CollaborationError, match="depth"):
        ask(4, "operator")
    store.cancel_turn(user, channel, turn, api.clock())
    with pytest.raises(CollaborationError):
        ask(4, "builder")
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
        api.ctx.turn_executor.submit(lambda: None).result(timeout=5)
        done = settled(api.client, headers, channel, turn["id"])
        assert done["status"] == "cancelled"
        assert [p["status"] for p in done["participants"]] == ["completed", "cancelled"]
        assert len(concierge.calls) == 1
    finally:
        if not concierge.first.done():
            concierge.first.set_result(ConciergeReply("released"))


def test_prose_mentions_alone_do_not_dispatch_a_peer(api: Any) -> None:
    concierge = Blocking()
    api.ctx.concierge = concierge
    headers = bearer(register(api))
    channel = api.client.post("/v1/channels", json={}, headers=headers).json()["id"]
    route = f"/v1/channels/{channel}/turns"
    turn = api.client.post(route, headers=headers, json={"content": "@planner help"}).json()["turn"]
    concierge.first.set_result(ConciergeReply("@critic should review this"))
    done = settled(api.client, headers, channel, turn["id"])
    assert done["status"] == "completed"
    assert len(done["participants"]) == len(concierge.calls) == 1
