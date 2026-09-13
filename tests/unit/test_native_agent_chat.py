"""Chat role model routing and tool boundaries use the actual worker contract."""

from pathlib import Path

from tests.unit.test_daemon_concierge import make


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
