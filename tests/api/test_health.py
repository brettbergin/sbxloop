"""Liveness says the process answers; readiness says the daemon is past
recovery — and until it is, a command is refused, not queued."""

from __future__ import annotations

from pathlib import Path

from tests.api.conftest import build


def test_live_answers_before_ready(tmp_path: Path) -> None:
    api = build(tmp_path, ready=False)
    with api.client as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json() == {"ready": False, "generation": None, "projection_lag": None}
        api.loop.recover()
        api.ctx.ready.set()
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["ready"] and ready.json()["generation"] == api.loop.generation
    api.ctx.close()


def test_health_needs_no_token_and_says_nothing_more(tmp_path: Path) -> None:
    api = build(tmp_path)
    with api.client as client:
        body = client.get("/health/ready").json()
        assert set(body) == {"ready", "generation", "projection_lag"}
        # No version, no paths, no queue: detail needs a token.
        assert "version" not in body
    api.ctx.close()


def test_stopping_flips_readiness(tmp_path: Path) -> None:
    api = build(tmp_path)
    with api.client as client:
        api.ctx.stopping.set()
        assert client.get("/health/ready").status_code == 503
        assert client.get("/health/live").status_code == 200
    api.ctx.close()


def test_every_response_carries_a_request_id(tmp_path: Path) -> None:
    api = build(tmp_path)
    with api.client as client:
        minted = client.get("/health/live").headers["X-Request-Id"]
        assert minted.startswith("req_")
        echoed = client.get("/health/live", headers={"X-Request-Id": "abc-123"})
        assert echoed.headers["X-Request-Id"] == "abc-123"
    api.ctx.close()


def test_every_response_forbids_content_sniffing(tmp_path: Path) -> None:
    """Every answer declares its own type and says so: a client never holds
    a response back to guess it, a problem and a refused body included."""
    api = build(tmp_path)
    with api.client as client:
        responses = [
            client.get("/health/live"),
            client.get("/v1/openapi.json"),
            client.get("/v1/capabilities"),
            client.post(
                "/v1/auth/token",
                content=b"{}",
                headers={"Content-Length": str(api.ctx.api.max_body_bytes + 1)},
            ),
        ]
        assert [r.status_code for r in responses] == [200, 200, 401, 413]
        for response in responses:
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers.get_list("X-Content-Type-Options") == ["nosniff"]
    api.ctx.close()
