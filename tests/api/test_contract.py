"""The HTTP contract: problem+json everywhere, bounded bodies, CORS off
unless allowlisted, the listener on its own thread."""

from __future__ import annotations

import json
import signal
from pathlib import Path

import httpx
import pytest

from sbxloop.api.server import ApiServer
from tests.api.conftest import Api, build


class TestProblems:
    def test_validation_errors_are_problems_with_locations(self, api: Api) -> None:
        response = api.client.post("/v1/auth/token", json={"grant_type": "magic"})
        assert response.status_code == 422
        body = response.json()
        assert body["type"] == "urn:sbxloop:problem:invalid_request"
        assert body["title"] == "Unprocessable Content" and body["instance"] == "/v1/auth/token"
        assert body["errors"][0]["loc"] == ["body", "grant_type"]
        assert body["request_id"].startswith("req_")

    def test_unknown_routes_and_methods_are_problems_too(self, api: Api) -> None:
        assert api.client.get("/v1/nothing").json()["code"] == "not_found"
        wrong = api.client.delete("/health/live")
        assert wrong.status_code == 405 and wrong.json()["code"] == "method_not_allowed"

    def test_a_crash_is_a_500_that_names_the_request(self, api: Api) -> None:
        def explode() -> dict[str, object]:
            raise RuntimeError("boom")

        api.loop.status = explode  # type: ignore[method-assign]
        response = api.client.get("/v1/status", headers=api.bearer())
        assert response.status_code == 500
        body = response.json()
        assert body["code"] == "internal_error" and "boom" not in body["detail"]
        assert body["request_id"] == response.headers["X-Request-Id"]


class TestBodies:
    def test_oversized_bodies_are_refused_before_parsing(self, tmp_path: Path) -> None:
        api = build(tmp_path, max_body_bytes=1024)
        with api.client as client:
            big = {
                "grant_type": "client_credentials",
                "client_id": "x" * 2000,
                "client_secret": "y",
            }
            response = client.post("/v1/auth/token", json=big)
            assert response.status_code == 413
            assert response.json()["code"] == "body_too_large" and response.json()["limit"] == 1024
            chunked = client.post(
                "/v1/auth/token",
                content=iter([json.dumps({"grant_type": "client_credentials"}).encode()]),
                headers={"Transfer-Encoding": "chunked", "Content-Type": "application/json"},
            )
            assert chunked.status_code == 411
        api.ctx.close()


class TestCors:
    def test_cors_is_off_by_default(self, api: Api) -> None:
        response = api.client.options(
            "/v1/status",
            headers={"Origin": "https://app.example", "Access-Control-Request-Method": "GET"},
        )
        assert "access-control-allow-origin" not in response.headers

    def test_an_allowlisted_origin_is_answered(self, tmp_path: Path) -> None:
        api = build(tmp_path, cors_origins=["https://app.example"])
        with api.client as client:
            response = client.options(
                "/v1/status",
                headers={"Origin": "https://app.example", "Access-Control-Request-Method": "GET"},
            )
            assert response.headers["access-control-allow-origin"] == "https://app.example"
            other = client.options(
                "/v1/status",
                headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
            )
            assert "access-control-allow-origin" not in other.headers
        api.ctx.close()

    def test_a_wildcard_origin_is_refused_by_the_config(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="'\\*' is refused"):
            build(tmp_path, cors_origins=["*"])


class TestListenerThread:
    def test_the_listener_serves_on_its_thread_and_leaves_signals_alone(
        self, tmp_path: Path
    ) -> None:
        api = build(tmp_path)
        before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        from sbxloop.api.app import create_app

        # Port 0 is the kernel's "any free port": not an operator setting,
        # so the config refuses it and the test bypasses validation.
        ephemeral = api.ctx.config.api.model_copy(update={"port": 0})
        server = ApiServer(create_app(api.ctx), ephemeral, ctx=api.ctx)
        server.start()
        try:
            assert server.port != 0
            url = f"http://127.0.0.1:{server.port}"
            assert httpx.get(f"{url}/health/live").json() == {"status": "ok"}
            pair = api.token()
            live = httpx.get(
                f"{url}/v1/status", headers={"Authorization": f"Bearer {pair['access_token']}"}
            )
            assert live.status_code == 200 and live.json()["generation"] == api.loop.generation
            # uvicorn on a thread captures no signals: the daemon's stand.
            assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before
        finally:
            server.close()
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"http://127.0.0.1:{server.port}/health/live", timeout=1.0)
        assert api.ctx.stopping.is_set()
