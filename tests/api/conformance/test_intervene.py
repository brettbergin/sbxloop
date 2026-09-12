"""Intervening in a run that is in flight — and in one that no longer is:
steering that is delivered and answered, a cancel honoured at the next
boundary, a cancel that arrives after the run ended while another run
is in flight, and a direction pinned to a state that has since moved."""

from __future__ import annotations

from sbxloop_worker.protocol import Event
from tests.api.conformance.conftest import Client, fake_source, register, tick
from tests.api.test_control import in_flight
from tests.unit.test_daemon_loop import gh_item


class TestSteering:
    def test_direction_is_delivered_and_the_reply_settles_it(self, operator: Client) -> None:
        api = operator.api
        fake_source(api)
        thread, run_id, release = in_flight(api, gh_item("1"))
        try:
            run = operator.get(f"/v1/runs/run_{run_id}").json()
            assert "steer" in run["available_actions"]
            steered = operator.post(
                f"/v1/runs/run_{run_id}/steering",
                {"text": "Use the existing helper", "expected_revision": run["revision"]},
            )
            assert steered.status_code == 202, steered.text
            record = steered.json()["steering"]
            assert record["status"] == "delivered"
            # The run's engine holds the message; the agent answers on the
            # run's own chronology, and the record follows the reply.
            handle = api.loop._current
            assert handle is not None
            queued = handle.engine._chat_queue.get_nowait()
            assert queued.text == "Use the existing helper"
            api.harness.store.append_event(
                Event(
                    ts=api.clock(),
                    run_id=run_id,
                    type="chat.reply",
                    data={"message_id": queued.message_id, "reply": "Done", "action": "applied"},
                )
            )
            listed = operator.get(f"/v1/runs/run_{run_id}/steering").json()["data"]
            assert listed[0]["status"] == "handled" and listed[0]["reply"] == "Done"
            # A direction pinned to a state that moved is refused, not applied.
            stale = operator.post(
                f"/v1/runs/run_{run_id}/steering",
                {"text": "Other", "expected_revision": run["revision"] + 5},
            )
            assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
            assert handle.engine._chat_queue.empty()
        finally:
            release.set()
            thread.join(10)


class TestCancel:
    def test_a_cancel_is_honoured_at_the_boundary_and_a_late_one_is_named(
        self, operator: Client
    ) -> None:
        api = operator.api
        fake_source(api)
        thread_a, run_a, release_a = in_flight(api, gh_item("1"))
        try:
            cancelled = operator.post(f"/v1/runs/run_{run_a}/cancel")
            assert cancelled.status_code == 202, cancelled.text
            op_a = cancelled.json()["operation"]
            assert op_a["state"] == "running" and cancelled.json()["run"]["state"] == "building"
        finally:
            release_a.set()
            thread_a.join(10)
        assert operator.get(f"/v1/runs/run_{run_a}").json()["state"] == "cancelled"
        assert operator.get(f"/v1/operations/{op_a['id']}").json()["state"] == "succeeded"
        # Run B is in flight now; a delayed cancel meant for A must never
        # land on it: A is terminal, and the answer says so by name.
        thread_b, run_b, release_b = in_flight(api, gh_item("2"))
        try:
            late = operator.post(f"/v1/runs/run_{run_a}/cancel")
            assert late.status_code == 409, late.text
            assert late.json()["code"] == "already_terminal"
            assert operator.get(f"/v1/runs/run_{run_b}").json()["state"] == "building"
            # And a cancel of B pinned to a revision it has left is refused.
            stale = operator.post(f"/v1/runs/run_{run_b}/cancel", {"expected_revision": 999})
            assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        finally:
            release_b.set()
            thread_b.join(10)
        assert operator.get(f"/v1/runs/run_{run_b}").json()["state"] in ("failed", "cancelled")
        # Every attempt is on the record, refusals included.
        ops = operator.get("/v1/operations", target_kind="run", target_id=run_a).json()["data"]
        assert sorted(o["state"] for o in ops) == ["failed", "succeeded"]

    def test_a_queued_item_is_abandoned_retried_and_resumed_by_revision(
        self, operator: Client
    ) -> None:
        api = operator.api
        item = operator.post(
            "/v1/items", {"kind": "workload", "ask": "x"}, key=operator.fresh_key()
        ).json()["item"]
        stale = operator.post(
            f"/v1/items/{item['id']}/abandon", {"expected_revision": item["revision"] + 1}
        )
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        gone = operator.post(
            f"/v1/items/{item['id']}/abandon",
            {"reason": "changed my mind", "expected_revision": item["revision"]},
        )
        assert gone.status_code == 200 and gone.json()["item"]["state"] == "failed"
        back = operator.post(f"/v1/items/{item['id']}/retry")
        assert back.status_code == 200 and back.json()["item"]["state"] == "queued"
        run_id = tick(api, "failed")
        assert operator.get(f"/v1/runs/run_{run_id}").json()["state"] == "failed"
        # The daemon's own retry has already queued the second attempt, so
        # a remote resume of the failed run is refused by name: the queue
        # owns it, and never a second engine.
        after = operator.get(f"/v1/items/{item['id']}").json()
        assert after["state"] == "queued" and after["attempts"] == 1
        full = register(api, "full")
        resumed = full.post(f"/v1/runs/run_{run_id}/resume")
        assert resumed.status_code == 409, resumed.text
        assert resumed.json()["code"] == "not_eligible" and "queued" in resumed.json()["detail"]
