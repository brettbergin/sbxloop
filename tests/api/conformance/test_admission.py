"""Duplicate admission through HTTP and polling, concurrent identical
requests, a changed payload under one key, and a reply that never
arrived: one item, one operation, and a conflict that says so."""

from __future__ import annotations

import threading

from tests.api.conformance.conftest import Client, tick
from tests.unit.test_daemon_sources import RecordingOps


class TestOneItem:
    def test_the_api_and_the_poll_converge_on_one_item(
        self, operator: Client, ops: RecordingOps
    ) -> None:
        api = operator.api
        body = {"kind": "issue", "repository": "o/r", "number": 4}
        first = operator.post("/v1/items", body, key="k1").json()
        # The daemon's own poll finds the label the API set and lands on the
        # row the API wrote: the queue still holds one item.
        polled = {i.item_id: i for i in api.loop.source.poll()}
        assert api.loop.dstore.upsert_new(polled["gh:issue:4"], api.clock()) is False
        items = operator.get("/v1/items").json()["data"]
        assert [i["id"] for i in items] == [first["item"]["id"]]
        # A second admission of the same issue, under a fresh key, is the
        # same item and no fresh operation effect: already queued.
        again = operator.post("/v1/items", body, key="k2")
        assert again.status_code == 200 and not again.json()["created"]
        assert again.json()["item"]["id"] == first["item"]["id"]
        assert len(operator.get("/v1/items").json()["data"]) == 1

    def test_concurrent_identical_requests_share_one_operation(self, operator: Client) -> None:
        body = {"kind": "workload", "ask": "Race", "profile": "brief"}
        results: list[tuple[int, dict[str, object]]] = []
        lock = threading.Lock()

        def submit() -> None:
            response = operator.post("/v1/items", body, key="race")
            with lock:
                results.append((response.status_code, response.json()))

        threads = [threading.Thread(target=submit) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert len(results) == 6
        # One attempt created it; every other saw the same operation — as a
        # replay once it finished, or as "still being applied" while it ran.
        assert [status for status, _ in results].count(201) == 1
        operations = set()
        for status, body in results:
            assert status in (200, 201, 409), body
            if status == 409:
                assert body["code"] == "already_in_progress"
                operations.add(str(body["operation_id"]))
            else:
                operations.add(str(body["operation"]["id"]))  # type: ignore[index]
        assert len(operations) == 1, results
        assert len(operator.get("/v1/items").json()["data"]) == 1

    def test_a_changed_payload_under_one_key_conflicts(self, operator: Client) -> None:
        first = operator.post("/v1/items", {"kind": "workload", "ask": "A"}, key="k")
        assert first.status_code == 201
        changed = operator.post("/v1/items", {"kind": "workload", "ask": "B"}, key="k")
        assert changed.status_code == 409
        problem = changed.json()
        assert problem["code"] == "idempotency_conflict"
        assert problem["operation_id"] == first.json()["operation"]["id"]
        # Another client's key space is its own: the same key, its own item.
        from tests.api.conformance.conftest import register

        other = register(operator.api, "other")
        assert other.post("/v1/items", {"kind": "workload", "ask": "B"}, key="k").status_code == 201

    def test_a_lost_reply_is_recovered_by_replaying_the_key(self, operator: Client) -> None:
        api = operator.api
        body = {"kind": "workload", "ask": "Once"}
        first = operator.post("/v1/items", body, key="lost").json()
        # The client never saw that reply; it retries, and after the daemon
        # has already run the work the replay still names the same record.
        run_id = tick(api, "completed")
        replay = operator.post("/v1/items", body, key="lost")
        assert replay.status_code == 200
        assert replay.json()["operation"]["id"] == first["operation"]["id"]
        assert replay.json()["item"]["state"] == "done"
        assert replay.json()["item"]["run_id"] == f"run_{run_id}"
        assert (
            operator.get(f"/v1/operations/{first['operation']['id']}").json()["state"]
            == "succeeded"
        )

    def test_admission_needs_a_key_and_a_known_shape(self, operator: Client) -> None:
        missing = operator.post("/v1/items", {"kind": "workload", "ask": "x"})
        assert missing.status_code == 422 and missing.json()["code"] == "idempotency_key_required"
        closed = operator.post(
            "/v1/items", {"kind": "issue", "repository": "o/r", "number": 6}, key="c"
        )
        assert closed.status_code == 409 and "not open" in closed.json()["detail"]
        unknown = operator.post(
            "/v1/items", {"kind": "issue", "repository": "o/nope", "number": 1}, key="u"
        )
        assert unknown.status_code == 404
        # A recipe is a registered name, never a command: an unknown one is
        # refused by name before anything is keyed on it.
        bad = operator.post("/v1/items", {"kind": "tool", "recipe": "rm -rf /"}, key="b")
        assert bad.status_code == 404 and "unknown recipe" in bad.json()["detail"]
        assert "rm -rf" not in str(operator.get("/v1/items").json())
