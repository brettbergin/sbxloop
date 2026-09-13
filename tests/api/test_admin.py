"""Daemon administration over the API: attributed holds, the graceful
stop and the supervised restart, a suspended repository, the schedules —
each a recorded operation, each refused to a reader (#1040)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from sbxloop.api import ws as ws_module
from sbxloop.daemon.loop import RESTART_MARKER_KEY
from sbxloop.daemon.sources import RepoHealth
from tests.api.conftest import Api, build
from tests.unit.test_daemon_loop import FakeSource

MANAGE = frozenset({"daemon:manage", "runs:read"})
READ = frozenset({"runs:read"})


def _events(api: Api, type_: str) -> list[dict[str, Any]]:
    body = api.client.get("/v1/events", params={"type_prefix": type_}, headers=api.bearer()).json()
    return [e for e in body["data"] if e["type"] == type_]


class TestHolds:
    def test_a_hold_is_taken_attributed_and_listed(self, api: Api) -> None:
        headers = api.bearer(MANAGE)
        taken = api.client.post(
            "/v1/daemon/holds", json={"name": "deploy-1", "reason": "rolling out"}, headers=headers
        )
        assert taken.status_code == 201, taken.text
        body = taken.json()
        assert body["created"] and body["hold"] == "deploy-1"
        assert body["operation"]["action"] == "daemon.pause"
        assert body["operation"]["state"] == "succeeded"
        (hold,) = body["holds"]
        assert hold["name"] == "deploy-1" and hold["via"] == "api"
        assert hold["reason"] == "rolling out" and hold["created_at"].endswith("Z")
        assert hold["owner"] == "tester"
        assert api.loop.paused and api.loop.holds == ["deploy-1"]
        # Listed the same way, and on the status.
        listed = api.client.get("/v1/daemon/holds", headers=api.bearer(READ)).json()
        assert listed["data"] == [hold]
        assert api.client.get("/v1/status", headers=api.bearer(READ)).json()["holds"] == [hold]
        # A second take of the same name is 200: it already stands.
        again = api.client.post("/v1/daemon/holds", json={"name": "deploy-1"}, headers=headers)
        assert again.status_code == 200 and not again.json()["created"]
        # The store's row says whose it is, and which operation took it.
        (row,) = api.loop.dstore.holds()
        assert row.owner_display == "tester" and row.via == "api"
        assert row.operation_id == body["operation"]["id"] and row.reason == "rolling out"

    def test_a_hold_is_released_by_its_owner_and_overridden_only_by_force(self, api: Api) -> None:
        deployer = api.bearer(MANAGE)
        api.client.post("/v1/daemon/holds", json={"name": "deploy-1"}, headers=deployer)
        other = {"Authorization": "Bearer " + api.token(MANAGE, name="other")["access_token"]}
        refused = api.client.delete("/v1/daemon/holds/deploy-1", headers=other)
        assert refused.status_code == 409, refused.text
        problem = refused.json()
        assert problem["code"] == "hold_owned" and problem["owner"] == "tester"
        assert api.loop.holds == ["deploy-1"]
        # The refusal is on the record too.
        ops = api.client.get(
            "/v1/operations",
            params={"target_kind": "hold", "target_id": "deploy-1"},
            headers=api.bearer(),
        ).json()["data"]
        (refusal,) = [op for op in ops if op["action"] == "daemon.release"]
        assert refusal["state"] == "failed" and refusal["error_code"] == "hold_owned"
        # An override says so and is recorded as the other client's release.
        forced = api.client.delete("/v1/daemon/holds/deploy-1?force=true", headers=other)
        assert forced.status_code == 200 and forced.json()["holds"] == []
        assert forced.json()["operation"]["actor"]["display"] == "other"
        assert not api.loop.paused
        # The owner's own release needs no force; a hold that is not
        # standing is not found.
        api.client.post("/v1/daemon/holds", json={"name": "deploy-2"}, headers=deployer)
        assert api.client.delete("/v1/daemon/holds/deploy-2", headers=deployer).status_code == 200
        assert api.client.delete("/v1/daemon/holds/deploy-2", headers=deployer).status_code == 404

    def test_a_hold_never_touches_the_run_in_flight_and_survives_a_restart(
        self, api: Api, tmp_path: Path
    ) -> None:
        from tests.unit.test_daemon_loop import Harness, gh_item

        api.client.post("/v1/daemon/holds", json={"name": "deploy-1"}, headers=api.bearer(MANAGE))
        api.harness.source.items = [gh_item("1")]
        assert api.loop.tick().idle_kind == "paused"
        assert api.harness.runs == []
        # The same store, a fresh process: the hold stands, whose it was intact.
        again = Harness(tmp_path, api.loop.config)
        again.loop.recover()
        assert again.loop.holds == ["deploy-1"]
        assert again.loop.hold_details()[0]["via"] == "api"

    def test_holds_need_daemon_manage_and_a_valid_name(self, api: Api) -> None:
        reader = api.bearer(READ)
        assert (
            api.client.post("/v1/daemon/holds", json={"name": "x"}, headers=reader).status_code
            == 403
        )
        assert api.client.delete("/v1/daemon/holds/x", headers=reader).status_code == 403
        bad = api.client.post(
            "/v1/daemon/holds", json={"name": "no spaces"}, headers=api.bearer(MANAGE)
        )
        assert bad.status_code == 422 and "invalid hold name" in bad.json()["detail"]
        assert api.client.get("/v1/daemon/holds").status_code == 401

    def test_an_idempotent_take_replays_its_operation(self, api: Api) -> None:
        headers = {**api.bearer(MANAGE), "Idempotency-Key": "k1"}
        first = api.client.post("/v1/daemon/holds", json={"name": "h"}, headers=headers).json()
        second = api.client.post("/v1/daemon/holds", json={"name": "h"}, headers=headers).json()
        assert second["operation"]["id"] == first["operation"]["id"]
        conflict = api.client.post("/v1/daemon/holds", json={"name": "other"}, headers=headers)
        assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"


class TestStop:
    def test_stop_is_accepted_durably_and_takes_effect_after_the_reply(self, api: Api) -> None:
        response = api.client.post("/v1/daemon/stop", headers=api.bearer(MANAGE))
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["accepted"] and body["action"] == "stop" and body["current"] is None
        assert body["generation"] == api.loop.generation
        assert body["operation"]["action"] == "daemon.stop"
        # Accepted, and still running when the reply left: the effect
        # followed it, and the record closed with the effect.
        assert body["operation"]["state"] == "running"
        assert api.loop.status()["stopping"] is True
        op_id = body["operation"]["id"]
        assert (
            api.client.get(f"/v1/operations/{op_id}", headers=api.bearer()).json()["state"]
            == "succeeded"
        )
        # The chronology said it was coming, attributed and linked.
        (event,) = _events(api, "daemon.stop_requested")
        assert event["operation_id"] == body["operation"]["id"]
        assert event["actor"]["id"].startswith("cli_") and event["data"]["now"] is False

    def test_a_stream_hears_the_stop_then_closes(self, api: Api, monkeypatch: Any) -> None:
        monkeypatch.setattr(ws_module, "WAIT_S", 0.05)
        with api.client.websocket_connect("/v1/ws", headers=api.bearer(MANAGE)) as ws:
            hello = json.loads(ws.receive_text())
            ws.send_text(json.dumps({"type": "subscribe", "after": hello["watermark"]}))
            assert json.loads(ws.receive_text())["type"] == "subscribed"
            ws.send_text(json.dumps({"type": "command", "id": "c1", "action": "daemon.stop"}))
            reply: dict[str, Any] | None = None
            heard = False
            while reply is None or not heard:
                frame = json.loads(ws.receive_text())
                if frame["type"] == "reply":
                    reply = frame
                elif frame["type"] == "event" and frame["event"]["type"] == "daemon.stop_requested":
                    heard = True
            assert reply["ok"] and reply["result"]["action"] == "stop"
            # The reply went out before the effect: the loop is stopping now.
            assert api.loop.status()["stopping"] is True
            # The listener's close (the daemon's exit) ends the stream by name.
            api.ctx.stopping.set()
            api.ctx.hub.notify()
            while (frame := json.loads(ws.receive_text()))["type"] != "closing":
                assert frame["type"] == "event"
            assert frame == {"type": "closing", "reason": "daemon_stopping"}
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
            assert closed.value.code == 1001
        api.ctx.stopping.clear()

    def test_stop_needs_daemon_manage_and_a_ready_daemon(self, api: Api, tmp_path: Path) -> None:
        assert api.client.post("/v1/daemon/stop", headers=api.bearer(READ)).status_code == 403
        assert api.loop.status()["stopping"] is False
        cold = build(tmp_path / "cold", ready=False)
        with cold.client:
            refused = cold.client.post("/v1/daemon/stop", headers=cold.bearer(MANAGE))
            assert refused.status_code == 503 and refused.json()["code"] == "daemon_not_ready"
        cold.ctx.close()


class TestRestart:
    def test_unsupervised_is_refused_by_name_and_nothing_is_set(
        self, api: Api, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        response = api.client.post("/v1/daemon/restart", headers=api.bearer(MANAGE))
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "unsupervised"
        assert "service manager" in response.json()["detail"]
        status = api.loop.status()
        assert not status["stopping"] and not status["restarting"]
        assert api.loop.dstore.get_value(RESTART_MARKER_KEY) is None
        assert _events(api, "daemon.restart_requested") == []

    def test_supervised_leaves_the_marker_and_reports_the_generation_it_had(
        self, api: Api, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INVOCATION_ID", "abc123")
        response = api.client.post(
            "/v1/daemon/restart", json={"now": True}, headers=api.bearer(MANAGE)
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["action"] == "restart" and body["supervisor"] == "systemd" and body["now"]
        assert body["generation"] == api.loop.generation
        assert body["operation"]["action"] == "daemon.restart"
        marker = json.loads(api.loop.dstore.get_value(RESTART_MARKER_KEY) or "{}")
        assert marker["by"] == "tester" and marker["mode"] == "now"
        assert marker["supervisor"] == "systemd"
        assert api.loop.status()["restarting"] is True
        (event,) = _events(api, "daemon.restart_requested")
        assert event["data"] == {
            "now": True,
            "supervisor": "systemd",
            "generation": body["generation"],
        }
        # The generation reported is the one that accepted: a new one is
        # observed on readiness once the daemon is back, never promised here.
        ready = api.client.get("/health/ready").json()
        assert ready["generation"] == body["generation"]


class TestRepositories:
    class SuspendedSource(FakeSource):
        def __init__(self) -> None:
            super().__init__()
            self.health = [RepoHealth("o/r", 4, None, True, "gone", 1.0)]

        @property
        def repo_health(self) -> list[Any]:
            return list(self.health)

        def resume_repo(self, repo: str) -> Any:
            if repo != "o/r":
                raise KeyError(f"unknown repository {repo!r}")
            if self.health[0].state == "ok":
                raise ValueError("o/r is not suspended or backing off")
            self.health[0] = RepoHealth("o/r")
            return self.health[0]

    def test_a_suspended_repository_is_resumed_by_public_id(self, api: Api) -> None:
        api.loop.source = self.SuspendedSource()
        headers = api.bearer(MANAGE)
        (repo,) = api.client.get("/v1/repositories", headers=headers).json()["data"]
        assert repo["health"]["state"] == "suspended"
        resumed = api.client.post(f"/v1/repositories/{repo['id']}/resume", headers=headers)
        assert resumed.status_code == 200, resumed.text
        body = resumed.json()
        assert body["repository"]["id"] == repo["id"]
        assert body["repository"]["health"]["state"] == "ok"
        assert body["operation"]["action"] == "repo.resume"
        # Polling normally: refused by name. Unknown id: not found, kind concealed.
        again = api.client.post(f"/v1/repositories/{repo['id']}/resume", headers=headers)
        assert again.status_code == 409 and again.json()["code"] == "not_eligible"
        assert (
            api.client.post("/v1/repositories/repo_nope/resume", headers=headers).status_code == 404
        )
        assert (
            api.client.post(
                f"/v1/repositories/{repo['id']}/resume", headers=api.bearer(READ)
            ).status_code
            == 403
        )

    def test_a_single_repository_daemon_has_no_polling_state_to_resume(self, api: Api) -> None:
        headers = api.bearer(MANAGE)
        (repo,) = api.client.get("/v1/repositories", headers=headers).json()["data"]
        refused = api.client.post(f"/v1/repositories/{repo['id']}/resume", headers=headers)
        assert refused.status_code == 409 and "multi-repository" in refused.json()["detail"]


EVERY_HOUR = {"name": "hourly", "profile": "brief", "ask": "Summarise the hour", "every": "1h"}


@pytest.fixture
def scheduled(tmp_path: Path) -> Any:
    api = build(tmp_path, config={"workloads": [{"name": "brief"}], "schedules": [EVERY_HOUR]})
    with api.client:
        yield api
    api.ctx.close()


class TestSchedules:
    def test_schedules_are_listed_paused_and_resumed(self, scheduled: Api) -> None:
        api = scheduled
        headers = api.bearer(MANAGE)
        (row,) = api.client.get("/v1/schedules", headers=api.bearer(READ)).json()["data"]
        assert row["id"] == "hourly" and row["cadence"] == "every 1h"
        assert row["profile"] == "brief" and row["source"] == "config"
        assert row["next_due"].endswith("Z") and row["last_fired_at"] is None
        assert not row["paused"] and row["available_actions"] == ["pause", "remove"]
        paused = api.client.post("/v1/schedules/hourly/pause", headers=headers)
        assert paused.status_code == 200, paused.text
        body = paused.json()
        assert body["schedule"]["paused"] and body["schedule"]["paused_by"] == "tester"
        assert body["operation"]["action"] == "schedule.pause" and "paused" in body["message"]
        assert body["schedule"]["available_actions"] == ["resume", "remove"]
        assert api.client.get("/v1/schedules/hourly", headers=headers).json()["paused"]
        resumed = api.client.post("/v1/schedules/hourly/resume", headers=headers).json()
        assert not resumed["schedule"]["paused"]
        assert resumed["operation"]["action"] == "schedule.resume"
        # Unknown by name; a reader may look but not touch.
        assert api.client.post("/v1/schedules/ghost/pause", headers=headers).status_code == 404
        assert api.client.get("/v1/schedules/ghost", headers=headers).status_code == 404
        assert (
            api.client.post("/v1/schedules/hourly/pause", headers=api.bearer(READ)).status_code
            == 403
        )

    def test_a_schedule_is_created_and_removed_under_the_same_rules(self, scheduled: Api) -> None:
        api = scheduled
        headers = api.bearer(MANAGE)
        created = api.client.post(
            "/v1/schedules",
            json={"name": "nightly", "profile": "brief", "ask": "Night", "cron": "0 2 * * *"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["schedule"]["name"] == "nightly" and body["schedule"]["source"] == "api"
        assert body["schedule"]["created_by"] == "tester"
        assert body["operation"]["action"] == "schedule.add"
        names = [s["name"] for s in api.client.get("/v1/schedules", headers=headers).json()["data"]]
        assert names == ["hourly", "nightly"]
        # A taken name conflicts; an undeclared profile and a bad cadence are refused.
        taken = api.client.post(
            "/v1/schedules",
            json={"name": "nightly", "profile": "brief", "ask": "x", "every": "2h"},
            headers=headers,
        )
        assert taken.status_code == 422 and "already exists" in taken.json()["detail"]
        profile = api.client.post(
            "/v1/schedules",
            json={"name": "p", "profile": "nope", "ask": "x", "every": "2h"},
            headers=headers,
        )
        assert profile.status_code == 422 and "[[workloads]]" in profile.json()["detail"]
        cadence = api.client.post(
            "/v1/schedules", json={"name": "c", "profile": "brief", "ask": "x"}, headers=headers
        )
        assert cadence.status_code == 422
        removed = api.client.delete("/v1/schedules/nightly", headers=headers)
        assert removed.status_code == 200 and removed.json()["schedule"] is None
        assert removed.json()["operation"]["action"] == "schedule.remove"
        assert api.client.get("/v1/schedules/nightly", headers=headers).status_code == 404

    def test_the_socket_takes_the_same_commands(self, scheduled: Api) -> None:
        api = scheduled
        with api.client.websocket_connect("/v1/ws", headers=api.bearer(MANAGE)) as ws:
            assert json.loads(ws.receive_text())["type"] == "hello"
            ws.send_text(
                json.dumps(
                    {"type": "command", "id": "c1", "action": "schedule.pause", "target": "hourly"}
                )
            )
            reply = json.loads(ws.receive_text())
            assert reply["ok"] and reply["result"]["schedule"]["paused"]
            ws.send_text(
                json.dumps(
                    {
                        "type": "command",
                        "id": "c2",
                        "action": "daemon.hold",
                        "params": {"name": "h", "reason": "r"},
                    }
                )
            )
            reply = json.loads(ws.receive_text())
            assert reply["ok"] and reply["result"]["created"]
            ws.send_text(
                json.dumps(
                    {"type": "command", "id": "c3", "action": "daemon.release", "target": "h"}
                )
            )
            assert json.loads(ws.receive_text())["result"]["holds"] == []
            ws.send_text(json.dumps({"type": "command", "id": "c4", "action": "daemon.release"}))
            reply = json.loads(ws.receive_text())
            assert not reply["ok"] and reply["problem"]["code"] == "invalid_request"
        # A service refusal is a reply as well: another client's hold.
        api.loop.pause("theirs", by="someone", via="api", owner_id="cli_elsewhere")
        with api.client.websocket_connect("/v1/ws", headers=api.bearer(MANAGE)) as ws:
            assert json.loads(ws.receive_text())["type"] == "hello"
            ws.send_text(
                json.dumps(
                    {"type": "command", "id": "c1", "action": "daemon.release", "target": "theirs"}
                )
            )
            reply = json.loads(ws.receive_text())
            assert not reply["ok"] and reply["problem"]["code"] == "hold_owned"
            assert reply["problem"]["status"] == 409 and reply["problem"]["owner"] == "someone"
            ws.send_text(
                json.dumps(
                    {
                        "type": "command",
                        "id": "c2",
                        "action": "daemon.release",
                        "target": "theirs",
                        "params": {"force": True},
                    }
                )
            )
            assert json.loads(ws.receive_text())["result"]["holds"] == []
        with api.client.websocket_connect("/v1/ws", headers=api.bearer(READ)) as ws:
            assert json.loads(ws.receive_text())["type"] == "hello"
            ws.send_text(json.dumps({"type": "command", "id": "c1", "action": "daemon.stop"}))
            reply = json.loads(ws.receive_text())
            assert reply["problem"]["code"] == "forbidden"
            assert reply["problem"]["capability"] == "daemon:manage"
