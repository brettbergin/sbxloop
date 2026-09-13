"""Watching without gaps: a snapshot's watermark then everything after it,
pages that overlap without repeating, a cursor that history has left
behind, and a stream a client resumes where it left off."""

from __future__ import annotations

import json

from tests.api.conformance.conftest import Client, tick


class TestCursors:
    def test_pages_neither_gap_nor_repeat_and_a_snapshot_seals_the_past(
        self, operator: Client
    ) -> None:
        api = operator.api
        mark = operator.get("/v1/status").json()["watermark"] or 0
        for i in range(3):
            operator.post("/v1/items", {"kind": "workload", "ask": f"ask {i}"}, key=f"k{i}")
        run_id = tick(api, "completed")
        # Everything since the snapshot, two at a time, joined by cursors.
        seen: list[str] = []
        cursor = f"evt_{mark}"
        while True:
            page = operator.get("/v1/events", after=cursor, limit=2).json()
            seen.extend(e["id"] for e in page["data"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        assert len(seen) == len(set(seen)) and seen == sorted(seen, key=lambda s: int(s[4:]))
        whole = [e["id"] for e in operator.events(after=f"evt_{mark}")]
        assert whole == seen
        # Resuming from the last id seen yields nothing until something happens.
        assert operator.events(after=seen[-1]) == []
        operator.post(f"/v1/runs/run_{run_id}/cancel")
        assert operator.event_types(after=seen[-1])[0].startswith("operation.")

    def test_a_cursor_history_has_dropped_is_gone_not_skipped(self, operator: Client) -> None:
        api = operator.api
        first = operator.events()[0]["id"] if operator.events() else None
        if first is None:
            operator.post("/v1/items", {"kind": "workload", "ask": "x"}, key="k")
            first = operator.events()[0]["id"]
        api.clock.t += 604800 + 10
        api.ctx.chronology.prune(api.clock() - 604800)
        from tests.api.conformance.conftest import register

        fresh = register(api, "fresh")  # the clock moved past the old token
        gone = fresh.get("/v1/events", after=first)
        assert gone.status_code == 410 and gone.json()["code"] == "cursor_expired"
        assert gone.json()["snapshot"] == "/v1/status"
        fresh.post("/v1/items", {"kind": "workload", "ask": "y"}, key="k2")
        mark = fresh.get("/v1/status").json()["watermark"]
        assert fresh.get("/v1/events", after=f"evt_{mark}").status_code == 200


class TestStreams:
    def test_a_socket_resumes_from_the_last_id_it_saw(self, operator: Client) -> None:
        api = operator.api
        operator.post("/v1/items", {"kind": "workload", "ask": "one"}, key="k1")
        last = operator.events()[-1]["id"]
        with api.client.websocket_connect("/v1/ws", headers=operator.headers) as ws:
            assert json.loads(ws.receive_text())["type"] == "hello"
            ws.send_text(json.dumps({"type": "subscribe", "after": last}))
            assert json.loads(ws.receive_text())["type"] == "subscribed"
            operator.post("/v1/items", {"kind": "workload", "ask": "two"}, key="k2")
            frame = json.loads(ws.receive_text())
            assert frame["type"] == "event" and int(frame["event"]["id"][4:]) > int(last[4:])

    def test_a_run_scoped_subscription_hears_only_that_run(self, operator: Client) -> None:
        api = operator.api
        operator.post("/v1/items", {"kind": "workload", "ask": "one"}, key="k1")
        run_id = tick(api, "completed")
        with api.client.websocket_connect("/v1/ws", headers=operator.headers) as ws:
            assert json.loads(ws.receive_text())["type"] == "hello"
            ws.send_text(
                json.dumps({"type": "subscribe", "run_id": f"run_{run_id}", "type_prefix": "run."})
            )
            assert json.loads(ws.receive_text())["type"] == "subscribed"
            kinds = [json.loads(ws.receive_text())["event"]["type"] for _ in range(2)]
            assert kinds == ["run.started", "run.finished"]
