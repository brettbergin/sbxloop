"""Holds and the daemon's own lifecycle: independent holds by two clients,
a release by someone else, holds across a restart, readiness while
recovery runs, and a graceful stop that is accepted before it acts."""

from __future__ import annotations

from pathlib import Path

from tests.api.conformance.conftest import Client, register
from tests.api.conftest import build
from tests.unit.test_daemon_loop import gh_item


class TestHolds:
    def test_two_clients_hold_independently_and_neither_releases_the_others(
        self, admin: Client
    ) -> None:
        api = admin.api
        deployer = register(api, "deployer", frozenset({"daemon:manage", "runs:read"}))
        assert (
            admin.post("/v1/daemon/holds", {"name": "ops", "reason": "incident"}).status_code == 201
        )
        assert deployer.post("/v1/daemon/holds", {"name": "deploy-7"}).status_code == 201
        holds = {h["name"]: h for h in admin.get("/v1/daemon/holds").json()["data"]}
        assert holds["ops"]["owner"] == "admin" and holds["deploy-7"]["owner"] == "deployer"
        # Nothing new is claimed while either stands.
        api.harness.source = type(api.harness.source)()  # a plain fake source
        api.loop.source = api.harness.source
        api.harness.source.items = [gh_item("1")]
        assert api.loop.tick().idle_kind == "paused"
        # The deployer releases its own; the admin's stands until the admin does.
        refused = deployer.delete("/v1/daemon/holds/ops")
        assert refused.status_code == 409 and refused.json()["code"] == "hold_owned"
        assert deployer.delete("/v1/daemon/holds/deploy-7").json()["holds"][0]["name"] == "ops"
        assert api.loop.tick().idle_kind == "paused"
        assert admin.delete("/v1/daemon/holds/ops").json()["holds"] == []
        assert api.loop.tick().idle_kind is None

    def test_holds_survive_a_restart_with_their_owners(self, admin: Client, tmp_path: Path) -> None:
        api = admin.api
        admin.post("/v1/daemon/holds", {"name": "deploy-1", "reason": "rolling"})
        # The process comes back over the same store: a fresh loop, a fresh
        # listener, the same hold standing under the same owner.
        again = build(tmp_path)
        with again.client:
            client = register(again, "after")
            (hold,) = client.get("/v1/daemon/holds").json()["data"]
            assert hold["name"] == "deploy-1" and hold["owner"] == "admin"
            assert hold["reason"] == "rolling" and hold["via"] == "api"
            status = client.get("/v1/status").json()
            assert status["paused"] and status["generation"] != api.loop.generation
        again.ctx.close()


class TestReadiness:
    def test_a_recovering_daemon_answers_reads_and_refuses_commands(self, tmp_path: Path) -> None:
        cold = build(tmp_path, ready=False)
        with cold.client:
            client = register(cold, "early")
            ready = cold.client.get("/health/ready")
            assert ready.status_code == 503 and ready.json()["ready"] is False
            assert cold.client.get("/health/live").status_code == 200
            assert client.get("/v1/status").status_code == 200
            refused = client.post("/v1/items", {"kind": "workload", "ask": "x"}, key="k")
            assert refused.status_code == 503 and refused.json()["code"] == "daemon_not_ready"
            assert refused.headers["Retry-After"]
            assert client.post("/v1/daemon/holds", {"name": "h"}).status_code == 503
            # Recovery ends: the same request is taken.
            cold.loop.recover()
            cold.ctx.ready.set()
            assert cold.client.get("/health/ready").json()["ready"] is True
            assert (
                client.post("/v1/items", {"kind": "workload", "ask": "x"}, key="k").status_code
                == 201
            )
        cold.ctx.close()


class TestStop:
    def test_a_stop_is_accepted_durably_and_holds_stand_through_it(self, admin: Client) -> None:
        api = admin.api
        admin.post("/v1/daemon/holds", {"name": "deploy-1"})
        stop = admin.post("/v1/daemon/stop")
        assert stop.status_code == 202, stop.text
        assert stop.json()["accepted"] and api.loop.status()["stopping"] is True
        # The record outlives the reply; the hold is untouched by the stop.
        assert (
            admin.get(f"/v1/operations/{stop.json()['operation']['id']}").json()["state"]
            == "succeeded"
        )
        assert [h["name"] for h in admin.get("/v1/daemon/holds").json()["data"]] == ["deploy-1"]
        assert "daemon.stop_requested" in admin.event_types(type_prefix="daemon.")
