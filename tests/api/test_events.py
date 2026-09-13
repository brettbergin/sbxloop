"""Durable replay: a snapshot's watermark, everything after it in
sequence, per run or for the workspace, and pruned history refused."""

from __future__ import annotations

from tests.api.conftest import Api
from tests.unit.test_daemon_loop import gh_item


def _tick(api: Api, key: str = "1", outcome: str = "merged") -> str:
    api.harness.source.items = [gh_item(key)]
    api.harness.outcomes = [outcome]
    api.clock.t += 10
    api.loop.tick()
    return api.harness.runs[-1][0]


class TestReplay:
    def test_a_snapshot_watermark_then_everything_after_it(self, api: Api) -> None:
        headers = api.bearer()
        before = api.client.get("/v1/status", headers=headers).json()["watermark"]
        run_id = _tick(api)
        after = f"evt_{before}" if before is not None else None
        page = api.client.get(
            "/v1/events", params={"after": after} if after else {}, headers=headers
        ).json()
        types = [e["type"] for e in page["data"]]
        # The queue notice, the start, the finish and the done notice, in
        # the order the daemon narrated them.
        assert types.index("daemon.notice") < types.index("run.started")
        assert types.index("run.started") < types.index("run.finished")
        seqs = [int(e["id"].removeprefix("evt_")) for e in page["data"]]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        started = next(e for e in page["data"] if e["type"] == "run.started")
        assert started["run_id"] == f"run_{run_id}" and started["item_id"].startswith("itm_")
        assert started["actor"] == {
            "kind": "system",
            "id": "daemon",
            "display": "daemon",
            "via": "daemon",
        }
        assert started["data"]["kind"] == "code" and started["data"]["title"] == "Do 1"
        assert started["occurred_at"].endswith("Z") and started["workspace_id"] == "local"
        finished = next(e for e in page["data"] if e["type"] == "run.finished")
        assert finished["data"]["state"] == "merged" and finished["data"]["pr_number"] == 9
        assert not page["has_more"] and page["next_cursor"] is None
        # The status watermark now names the last of them.
        now = api.client.get("/v1/status", headers=headers).json()["watermark"]
        assert f"evt_{now}" == page["data"][-1]["id"]
        # And readiness reports the projection as caught up.
        assert api.client.get("/health/ready").json()["projection_lag"] == 0

    def test_engine_events_are_projected_with_their_native_sequence(self, api: Api) -> None:
        run_id = _tick(api)
        headers = api.bearer()
        # The scripted runner writes no engine events itself; write the
        # kind a run does, then read the run's chronology.
        from sbxloop_worker.protocol import Event

        api.harness.store.append_event(
            Event(ts=api.clock(), run_id=run_id, type="worker.stdout", data={"line": "hello"})
        )
        api.harness.store.append_event(
            Event(ts=api.clock(), run_id=run_id, type="phase.start", data={"phase": "build"})
        )
        page = api.client.get(f"/v1/runs/run_{run_id}/events", headers=headers).json()
        types = [e["type"] for e in page["data"]]
        assert types[:1] == ["run.started"] and types[-2:] == ["worker.stdout", "phase.start"]
        stdout = page["data"][-2]
        assert stdout["native_seq"] == 1 and stdout["data"] == {"line": "hello"}
        assert stdout["actor"] is None and stdout["run_id"] == f"run_{run_id}"
        only = api.client.get(
            f"/v1/runs/run_{run_id}/events", params={"type_prefix": "worker."}, headers=headers
        ).json()
        assert [e["type"] for e in only["data"]] == ["worker.stdout"]
        assert api.client.get("/v1/runs/run_nope/events", headers=headers).status_code == 404

    def test_pages_overlap_without_duplicates(self, api: Api) -> None:
        for i in range(5):
            api.ctx.chronology.record("daemon.notice", api.clock(), data={"i": i})
        headers = api.bearer()
        seen: list[str] = []
        cursor: str | None = None
        while True:
            params = {"limit": 2, **({"after": cursor} if cursor else {})}
            page = api.client.get("/v1/events", params=params, headers=headers).json()
            seen += [e["id"] for e in page["data"]]
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        assert len(seen) == len(set(seen)) == 5
        # Re-reading after any cursor repeats nothing before it.
        again = api.client.get("/v1/events", params={"after": seen[2]}, headers=headers).json()
        assert [e["id"] for e in again["data"]] == seen[3:]

    def test_operations_ride_the_same_chronology_with_their_actor(self, api: Api) -> None:
        headers = api.bearer()
        api.client.post(
            "/v1/items",
            json={"kind": "workload", "ask": "x"},
            headers={**headers, "Idempotency-Key": "k"},
        )
        page = api.client.get(
            "/v1/events", params={"type_prefix": "operation."}, headers=headers
        ).json()
        types = [e["type"] for e in page["data"]]
        assert types == ["operation.accepted", "operation.finished"]
        accepted = page["data"][0]
        assert accepted["actor"]["kind"] == "client" and accepted["actor"]["via"] == "api"
        assert accepted["operation_id"].startswith("op_")
        assert accepted["item_id"].startswith("itm_")

    def test_a_pruned_cursor_is_gone_not_skipped(self, api: Api) -> None:
        headers = api.bearer()
        for _ in range(3):
            api.clock.t += 1
            api.ctx.chronology.record("daemon.notice", api.clock())
        first = api.client.get("/v1/events", params={"limit": 1}, headers=headers).json()
        cursor = first["data"][0]["id"]
        api.clock.t += 604800 + 10
        api.ctx.chronology.prune(api.clock() - 604800)
        headers = api.bearer()  # the earlier token expired with the clock
        gone = api.client.get("/v1/events", params={"after": cursor}, headers=headers)
        assert gone.status_code == 410 and gone.json()["code"] == "cursor_expired"
        assert gone.json()["snapshot"] == "/v1/status"
        assert api.client.get("/v1/events", headers=headers).status_code == 410
        # A fresh snapshot's watermark is a valid cursor again (a fresh
        # token too: the clock moved past the old one's expiry).
        api.ctx.chronology.record("daemon.notice", api.clock())
        headers = api.bearer()
        mark = api.client.get("/v1/status", headers=headers).json()["watermark"]
        after_mark = api.client.get("/v1/events", params={"after": f"evt_{mark}"}, headers=headers)
        assert after_mark.status_code == 200 and after_mark.json()["data"] == []

    def test_bad_cursors_and_permissions(self, api: Api) -> None:
        headers = api.bearer()
        bad = api.client.get("/v1/events", params={"after": "op_1"}, headers=headers)
        assert bad.status_code == 400 and bad.json()["code"] == "invalid_cursor"
        assert api.client.get("/v1/events").status_code == 401
        reader = api.bearer(frozenset({"audit:read"}))
        assert api.client.get("/v1/events", headers=reader).status_code == 403
