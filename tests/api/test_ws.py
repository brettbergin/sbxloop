"""The WebSocket: authenticated by header or first frame, the same events
by cursor, the same commands with the same idempotency."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from sbxloop.api import ws as ws_module
from tests.api.conftest import Api, build


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_module, "WAIT_S", 0.05)
    monkeypatch.setattr(ws_module, "AUTH_TIMEOUT_S", 0.2)


def _recv(ws: Any, *, kind: str | None = None) -> dict[str, Any]:
    """The next frame, skipping others when a kind is wanted."""
    while True:
        frame = json.loads(ws.receive_text())
        if kind is None or frame["type"] == kind:
            return dict(frame)


class TestAuth:
    def test_header_auth_says_hello_with_the_watermark(self, api: Api) -> None:
        seq = api.ctx.chronology.record("daemon.notice", api.clock(), data={})
        with api.client.websocket_connect("/v1/ws", headers=api.bearer()) as ws:
            hello = _recv(ws)
            assert hello["type"] == "hello" and hello["watermark"] == f"evt_{seq}"
            assert hello["workspace_id"] == "local" and hello["generation"]
            ws.send_text(json.dumps({"type": "ping"}))
            assert _recv(ws)["type"] == "pong"

    def test_an_auth_frame_works_and_the_query_string_never_does(self, api: Api) -> None:
        token = api.token()["access_token"]
        with api.client.websocket_connect("/v1/ws") as ws:
            ws.send_text(json.dumps({"type": "auth", "token": token}))
            assert _recv(ws)["type"] == "hello"
        with api.client.websocket_connect(f"/v1/ws?access_token={token}") as ws:
            ws.send_text(json.dumps({"type": "subscribe"}))
            error = _recv(ws)
            assert error["type"] == "error" and error["code"] == "unauthenticated"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
            assert closed.value.code == 4401

    def test_no_frame_in_time_is_refused(self, api: Api) -> None:
        with api.client.websocket_connect("/v1/ws") as ws:
            error = _recv(ws)
            assert error["code"] == "unauthenticated" and "no auth frame" in error["detail"]
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()

    def test_a_bad_token_or_a_missing_capability_is_refused(self, api: Api) -> None:
        with api.client.websocket_connect("/v1/ws", headers={"Authorization": "Bearer x"}) as ws:
            assert _recv(ws)["code"] == "invalid_token"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
            assert closed.value.code == 4401
        headers = api.bearer(frozenset({"audit:read"}))
        with api.client.websocket_connect("/v1/ws", headers=headers) as ws:
            assert _recv(ws)["code"] == "forbidden"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
            assert closed.value.code == 4403


class TestSubscription:
    def test_events_after_a_cursor_as_they_land(self, api: Api) -> None:
        first = api.ctx.chronology.record("daemon.notice", api.clock(), data={"n": 1})
        with api.client.websocket_connect("/v1/ws", headers=api.bearer()) as ws:
            _recv(ws, kind="hello")
            ws.send_text(json.dumps({"type": "subscribe", "after": f"evt_{first}"}))
            assert _recv(ws)["type"] == "subscribed"
            second = api.ctx.chronology.record("daemon.notice", api.clock(), data={"n": 2})
            api.ctx.hub.notify()
            event = _recv(ws, kind="event")["event"]
            assert event["id"] == f"evt_{second}" and event["data"] == {"n": 2}
            ws.send_text(json.dumps({"type": "unsubscribe"}))
            assert _recv(ws)["type"] == "unsubscribed"
            ws.send_text(json.dumps({"type": "subscribe", "after": "bogus"}))
            assert _recv(ws)["code"] == "invalid_cursor"
            ws.send_text(json.dumps({"type": "nonsense"}))
            assert _recv(ws)["code"] == "invalid_frame"
            ws.send_text("not json")
            assert _recv(ws)["code"] == "invalid_frame"

    def test_a_run_filter_and_a_type_prefix(self, api: Api) -> None:
        from tests.unit.test_daemon_loop import gh_item

        api.harness.source.items = [gh_item("1")]
        api.loop.tick()
        run_id = api.harness.runs[-1][0]
        with api.client.websocket_connect("/v1/ws", headers=api.bearer()) as ws:
            _recv(ws, kind="hello")
            ws.send_text(
                json.dumps({"type": "subscribe", "run_id": f"run_{run_id}", "type_prefix": "run."})
            )
            assert _recv(ws)["type"] == "subscribed"
            started = _recv(ws, kind="event")["event"]
            finished = _recv(ws, kind="event")["event"]
            assert (started["type"], finished["type"]) == ("run.started", "run.finished")
            assert started["run_id"] == f"run_{run_id}"

    def test_a_revoked_token_closes_the_connection(
        self, api: Api, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ws_module, "ACCESS_RECHECK_S", 0.0)
        pair = api.token()
        headers = {"Authorization": f"Bearer {pair['access_token']}"}
        with api.client.websocket_connect("/v1/ws", headers=headers) as ws:
            _recv(ws, kind="hello")
            api.auth.revoke_client(pair["client_id"], api.clock())
            ws.send_text(json.dumps({"type": "ping"}))
            closing = _recv(ws, kind="closing")
            assert closing["reason"] == "client_revoked"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
            assert closed.value.code == 4401

    def test_connections_are_bounded(self, tmp_path: Path) -> None:
        api = build(tmp_path, max_stream_clients=1)
        with api.client:
            headers = api.bearer()
            with api.client.websocket_connect("/v1/ws", headers=headers) as first:
                _recv(first, kind="hello")
                with api.client.websocket_connect("/v1/ws", headers=headers) as second:
                    assert _recv(second)["code"] == "too_many_streams"
                    with pytest.raises(WebSocketDisconnect) as closed:
                        second.receive_text()
                    assert closed.value.code == 4429
        api.ctx.close()


class TestCommands:
    def test_admission_over_the_socket_is_the_rest_command(self, api: Api) -> None:
        with api.client.websocket_connect("/v1/ws", headers=api.bearer()) as ws:
            _recv(ws, kind="hello")
            frame = {
                "type": "command",
                "id": "c1",
                "action": "item.admit",
                "params": {"kind": "workload", "ask": "Summarise"},
                "idempotency_key": "k1",
            }
            ws.send_text(json.dumps(frame))
            reply = _recv(ws, kind="reply")
            assert reply["id"] == "c1" and reply["ok"]
            item = reply["result"]["item"]
            assert item["kind"] == "workload" and reply["result"]["created"]
            op_id = reply["result"]["operation"]["id"]
            # The same key replays the same operation; a changed body conflicts.
            ws.send_text(json.dumps({**frame, "id": "c2"}))
            replay = _recv(ws, kind="reply")
            assert replay["ok"] and not replay["result"]["created"]
            assert replay["result"]["operation"]["id"] == op_id
            ws.send_text(
                json.dumps({**frame, "id": "c3", "params": {"kind": "workload", "ask": "Other"}})
            )
            conflict = _recv(ws, kind="reply")
            assert not conflict["ok"] and conflict["problem"]["code"] == "idempotency_conflict"
            # The key is required for admission, as it is over HTTP.
            ws.send_text(json.dumps({**frame, "id": "c4", "idempotency_key": None}))
            assert _recv(ws, kind="reply")["problem"]["code"] == "idempotency_key_required"
            # Then an item command on what was admitted.
            ws.send_text(
                json.dumps(
                    {
                        "type": "command",
                        "id": "c5",
                        "action": "item.abandon",
                        "target": item["id"],
                        "params": {"reason": "changed my mind"},
                        "expected_revision": item["revision"],
                    }
                )
            )
            abandoned = _recv(ws, kind="reply")
            assert abandoned["ok"] and abandoned["result"]["item"]["state"] == "failed"
            assert abandoned["result"]["operation"]["action"] == "item.abandon"
            assert len(api.loop.dstore.items()) == 1
            # An item command needs its target.
            ws.send_text(json.dumps({"type": "command", "id": "c6", "action": "item.retry"}))
            missing = _recv(ws, kind="reply")
            assert missing["problem"]["code"] == "invalid_request"

    def test_refusals_are_replies_not_closes(self, api: Api) -> None:
        reader = api.bearer(frozenset({"runs:read"}))
        with api.client.websocket_connect("/v1/ws", headers=reader) as ws:
            _recv(ws, kind="hello")
            ws.send_text(
                json.dumps(
                    {
                        "type": "command",
                        "id": "c1",
                        "action": "item.admit",
                        "params": {"kind": "workload", "ask": "x"},
                        "idempotency_key": "k",
                    }
                )
            )
            forbidden = _recv(ws, kind="reply")
            assert forbidden["problem"]["code"] == "forbidden"
            assert forbidden["problem"]["capability"] == "items:create"
            ws.send_text(json.dumps({"type": "command", "id": "c2", "action": "daemon.explode"}))
            assert _recv(ws, kind="reply")["problem"]["code"] == "unknown_action"
            ws.send_text(json.dumps({"type": "command", "action": "item.retry"}))
            assert _recv(ws)["code"] == "invalid_frame"
            ws.send_text(
                json.dumps({"type": "command", "id": "c3", "action": "item.retry", "params": []})
            )
            assert _recv(ws)["code"] == "invalid_frame"
            # The capability is checked before the arguments: a reader hears
            # "forbidden", never what a retry would have needed.
            ws.send_text(json.dumps({"type": "command", "id": "c4", "action": "item.retry"}))
            assert _recv(ws, kind="reply")["problem"]["code"] == "forbidden"
            ws.send_text(json.dumps({"type": "ping"}))
            assert _recv(ws)["type"] == "pong"

    def test_commands_wait_for_a_ready_daemon(self, tmp_path: Path) -> None:
        api = build(tmp_path, ready=False)
        with api.client, api.client.websocket_connect("/v1/ws", headers=api.bearer()) as ws:
            _recv(ws, kind="hello")
            ws.send_text(
                json.dumps({"type": "command", "id": "c1", "action": "item.retry", "target": "x"})
            )
            assert _recv(ws, kind="reply")["problem"]["code"] == "daemon_not_ready"
        api.ctx.close()
