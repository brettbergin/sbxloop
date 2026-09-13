"""Deciding a gate: approval binds to the revision the person saw, a
stale or repeated approval is refused by name, and the landing that
follows is observed in the chronology, never assumed."""

from __future__ import annotations

from tests.api.conformance.conftest import Client
from tests.api.test_control import gated, landed


class TestGates:
    def test_approval_binds_to_the_revision_seen(self, admin: Client) -> None:
        api = admin.api
        run_id = gated(api)
        (gate,) = admin.get("/v1/gates").json()["data"]
        assert gate["state"] == "open" and gate["run_id"] == f"run_{run_id}"
        assert gate["pull_request"]["number"] == 9 and gate["head_sha"]
        run = admin.get(f"/v1/runs/run_{run_id}").json()
        assert "gate_approve" in run["available_actions"]
        stale = admin.post(
            f"/v1/gates/{gate['id']}/approve", {"expected_revision": gate["revision"] + 1}
        )
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        unpinned = admin.post(f"/v1/gates/{gate['id']}/approve", {})
        assert unpinned.status_code == 422
        approved = admin.post(
            f"/v1/gates/{gate['id']}/approve", {"expected_revision": gate["revision"]}
        )
        assert approved.status_code == 202, approved.text
        assert approved.json()["gate"]["state"] in ("approving", "merged")
        # A second decision on the same gate is refused, whatever it says.
        repeated = admin.post(
            f"/v1/gates/{gate['id']}/approve", {"expected_revision": gate["revision"]}
        )
        assert repeated.status_code == 409
        assert repeated.json()["code"] in ("stale_revision", "already_in_progress", "not_eligible")
        landed(api, run_id)
        final = admin.get(f"/v1/gates/{gate['id']}").json()
        assert final["state"] == "merged"
        assert admin.get(f"/v1/runs/run_{run_id}").json()["state"] == "merged"
        # The landing was observed: opened, then resolved, in that order.
        types = admin.event_types(type_prefix="gate.")
        assert types.index("gate.opened") < types.index("gate.resolved")
        after = admin.post(
            f"/v1/gates/{gate['id']}/approve", {"expected_revision": final["revision"]}
        )
        assert after.status_code == 409
