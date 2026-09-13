"""Chat role model routing and tool boundaries use the actual worker contract."""

from pathlib import Path

from tests.unit.test_daemon_concierge import make


def test_worker_can_request_a_peer_only_inside_an_authorized_turn(tmp_path: Path) -> None:
    requests: list[tuple[str, str]] = []
    args = {"agent_slug": "critic", "message": "Review this plan"}
    concierge, client, _, _, _ = make(
        tmp_path,
        [
            {"calls": [("handoff_agent", args)], "text": "Peer requested"},
            {"calls": [("handoff_agent", args)], "text": "Ordinary reply"},
            {"calls": [("sbx_control", {"text": "pause"})], "text": "Read-only advice"},
        ],
    )

    def handoff(agent: str, message: str) -> str:
        requests.append((agent, message))
        return "Queued peer"

    try:
        concierge.submit_turn("plan", author="owner", agent_role="planner", handoff=handoff).result(
            timeout=10
        )
        concierge.submit_turn("hello", author="owner", allow_actions=False).result(timeout=10)
        concierge.submit_turn(
            "advise", author="owner", agent_role="builder", read_only=True
        ).result(timeout=10)
        assert requests == [("critic", "Review this plan")]
        assert "handoff_agent" in {t.name for t in client.jobs[0].host_tools}
        assert "handoff_agent" not in {t.name for t in client.jobs[1].host_tools}
        assert [r.ok for r in client.responses] == [True, False, False]
        assert "sbx_control" not in {t.name for t in client.jobs[2].host_tools}
        assert not client.jobs[2].mcp_servers
    finally:
        concierge.close()


def test_role_models_are_scoped_to_turn_and_critic_cannot_mutate(tmp_path: Path) -> None:
    concierge, client, _, _, _ = make(
        tmp_path,
        [
            {"text": "build advice"},
            {"calls": [("sbx_control", {"text": "pause"})], "text": "review"},
            {"text": "hello"},
        ],
        config={
            "agent": {"models": {"build": "builder-model", "review": "critic-model"}},
            "concierge": {"model": "chat-model"},
        },
    )
    try:
        for role in ("builder", "critic", "concierge"):
            concierge.submit_turn(
                "assess", author="owner", agent_role=role, session_key=role
            ).result(timeout=10)
        assert [j.model for j in client.jobs] == ["builder-model", "critic-model", "chat-model"]
        critic = client.jobs[1]
        assert "run_detail" in {t.name for t in critic.host_tools}
        assert "sbx_control" not in {t.name for t in critic.host_tools}
        assert not critic.mcp_servers
        assert not client.responses[0].ok
        assert "sbx_control" in {t.name for t in client.jobs[2].host_tools}
    finally:
        concierge.close()
