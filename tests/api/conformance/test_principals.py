"""Two identities with different grants, and a grant taken away: every
mutation is refused by capability before its target is looked at, and a
revoked client is refused on its next request and dropped from its
stream."""

from __future__ import annotations

import json

import pytest

from sbxloop.api import ws as ws_module
from tests.api.conformance.conftest import Client, register

MUTATIONS = [
    ("post", "/v1/items", {"kind": "workload", "ask": "x"}, "items:create"),
    ("post", "/v1/runs/run_x/cancel", None, "runs:control"),
    ("post", "/v1/runs/run_x/resume", None, "runs:control"),
    ("post", "/v1/items/itm_x/abandon", None, "runs:control"),
    ("post", "/v1/runs/run_x/steering", {"text": "x"}, "runs:steer"),
    ("post", "/v1/runs/run_x/round-grants", {"rounds": 1}, "budgets:grant"),
    ("post", "/v1/gates/gate_x/approve", {"expected_revision": 1}, "gates:approve"),
    ("post", "/v1/daemon/holds", {"name": "h"}, "daemon:manage"),
    ("delete", "/v1/daemon/holds/h", None, "daemon:manage"),
    ("post", "/v1/daemon/stop", None, "daemon:manage"),
    ("post", "/v1/daemon/restart", None, "daemon:manage"),
    ("post", "/v1/schedules/s/pause", None, "daemon:manage"),
    ("post", "/v1/repositories/repo_x/resume", None, "daemon:manage"),
    ("get", "/v1/operations", None, "audit:read"),
    ("get", "/v1/logs", None, "diagnostics:read"),
    ("get", "/v1/configuration", None, "diagnostics:read"),
    ("get", "/v1/artifacts/art_x", None, "artifacts:read"),
]


class TestGrants:
    @pytest.mark.parametrize(("method", "path", "body", "capability"), MUTATIONS)
    def test_a_reader_is_refused_by_capability_before_the_target_is_looked_at(
        self,
        reader: Client,
        method: str,
        path: str,
        body: dict[str, object] | None,
        capability: str,
    ) -> None:
        if method == "get":
            response = reader.get(path)
        elif method == "delete":
            response = reader.delete(path)
        else:
            response = reader.post(path, body, key="k")
        assert response.status_code == 403, (path, response.text)
        problem = response.json()
        assert problem["code"] == "forbidden" and problem["capability"] == capability
        assert reader.client_id in problem["detail"]

    def test_what_a_reader_may_read_it_reads(self, reader: Client, operator: Client) -> None:
        operator.post("/v1/items", {"kind": "workload", "ask": "x"}, key=operator.fresh_key())
        for path in ("/v1/status", "/v1/items", "/v1/queue", "/v1/runs", "/v1/events"):
            assert reader.get(path).status_code == 200, path
        assert reader.get("/v1/me").json()["capabilities"] == ["runs:read"]

    def test_no_token_and_a_forged_token_are_refused_alike(self, reader: Client) -> None:
        api = reader.api
        assert api.client.get("/v1/status").status_code == 401
        forged = {"Authorization": "Bearer " + reader.token[:-3] + "xyz"}
        response = api.client.get("/v1/status", headers=forged)
        assert response.status_code == 401 and response.json()["code"] == "invalid_token"
        assert response.headers["WWW-Authenticate"].startswith("Bearer")


class TestRevocation:
    def test_a_revoked_client_is_refused_at_once_and_dropped_from_its_stream(
        self, operator: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = operator.api
        monkeypatch.setattr(ws_module, "ACCESS_RECHECK_S", 0.0)
        with api.client.websocket_connect("/v1/ws", headers=operator.headers) as ws:
            assert json.loads(ws.receive_text())["type"] == "hello"
            assert (
                operator.post(
                    "/v1/items", {"kind": "workload", "ask": "x"}, key=operator.fresh_key()
                ).status_code
                == 201
            )
            api.auth.revoke_client(operator.client_id, api.clock())
            refused = operator.post(
                "/v1/items", {"kind": "workload", "ask": "y"}, key=operator.fresh_key()
            )
            assert refused.status_code == 401 and refused.json()["code"] == "client_revoked"
            assert operator.get("/v1/status").status_code == 401
            ws.send_text(json.dumps({"type": "ping"}))
            while (frame := json.loads(ws.receive_text()))["type"] != "closing":
                pass
            assert frame == {"type": "closing", "reason": "client_revoked"}
        # The work already admitted stands: revocation is not abandonment.
        other = register(api, "other")
        assert len(other.get("/v1/items").json()["data"]) == 1

    def test_a_refresh_token_presented_twice_revokes_the_family(self, operator: Client) -> None:
        api = operator.api
        client, secret = api.auth.create_client(
            "rotating", frozenset({"runs:read"}), created_by="test", now=api.clock()
        )
        first = api.client.post(
            "/v1/auth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": client.id,
                "client_secret": secret,
            },
        ).json()
        second = api.client.post(
            "/v1/auth/token",
            json={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
        )
        assert second.status_code == 200
        reused = api.client.post(
            "/v1/auth/token",
            json={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
        )
        assert reused.status_code == 401 and reused.json()["code"] == "refresh_reuse_detected"
        # The rotated token is gone with the family; the secret still mints.
        again = api.client.post(
            "/v1/auth/token",
            json={"grant_type": "refresh_token", "refresh_token": second.json()["refresh_token"]},
        )
        assert again.status_code == 401
        assert (
            api.client.post(
                "/v1/auth/token",
                json={
                    "grant_type": "client_credentials",
                    "client_id": client.id,
                    "client_secret": secret,
                },
            ).status_code
            == 200
        )
