"""Admit, watch, finish, retrieve — the whole loop for each of the three
run kinds, through the public contract alone."""

from __future__ import annotations

from tests.api.conformance.conftest import Client, fake_source, register, tick
from tests.api.test_control import in_flight
from tests.unit.test_daemon_loop import gh_item
from tests.unit.test_daemon_sources import RecordingOps


class TestCodeRun:
    def test_an_issue_becomes_a_merged_pull_request_a_client_can_follow(
        self, operator: Client, ops: RecordingOps
    ) -> None:
        api = operator.api
        # Discover: what this installation offers, and where the client stands.
        offer = operator.get("/v1/capabilities").json()
        assert "intake.issue" in offer["features"] and offer["contract_version"] == 1
        me = operator.get("/v1/me").json()
        assert "items:create" in me["capabilities"] and "daemon:manage" not in me["capabilities"]
        watermark = operator.get("/v1/status").json()["watermark"] or 0
        # Admit: the issue is labelled as a person would label it.
        admitted = operator.post(
            "/v1/items",
            {"kind": "issue", "repository": "o/r", "number": 4},
            key=operator.fresh_key(),
        )
        assert admitted.status_code == 201, admitted.text
        item = admitted.json()["item"]
        assert item["state"] == "queued" and item["kind"] == "code"
        assert [lb["name"] for lb in ops.issues["4"]["labels"]] == ["sbxloop:run"]
        queue = operator.get("/v1/queue").json()
        assert [entry["item"]["id"] for entry in queue["data"]] == [item["id"]]
        # The daemon works: one tick, one merged run.
        run_id = tick(api, "merged")
        run = operator.get(f"/v1/runs/run_{run_id}").json()
        assert run["state"] == "merged" and run["kind"] == "code"
        assert run["pull_request"]["number"] == 9 and run["item_id"] == item["id"]
        detail = operator.get(f"/v1/items/{item['id']}").json()
        assert detail["state"] == "done" and detail["runs"] == [f"run_{run_id}"]
        assert detail["admitted_by"]["kind"] == "client"
        # Watch, after the fact: the chronology from the snapshot's mark has
        # the admission, the start and the finish, in order, with no gap.
        types = operator.event_types(after=f"evt_{watermark}")
        assert types.index("operation.finished") < types.index("run.started")
        assert types.index("run.started") < types.index("run.finished")
        run_events = operator.get(f"/v1/runs/run_{run_id}/events").json()["data"]
        assert {e["run_id"] for e in run_events} == {f"run_{run_id}"}
        # Retrieve: tasks, artifacts (none: this run left no files), usage
        # (nothing reported is not zero), and the record of every command.
        assert operator.get(f"/v1/runs/run_{run_id}/tasks").status_code == 200
        artifacts = operator.get(f"/v1/runs/run_{run_id}/artifacts").json()
        assert artifacts["data"] == [] and "published" in artifacts
        usage = operator.get(f"/v1/runs/run_{run_id}/usage").json()
        assert usage["recorded"] is False and usage["spend"] is None
        # A finished run takes no more steering or grants; the read says so.
        assert "steer" not in run["available_actions"]
        steer = operator.post(f"/v1/runs/run_{run_id}/steering", {"text": "more"})
        assert steer.status_code == 409 and steer.json()["code"] == "not_eligible"


class TestWorkloadRun:
    def test_an_ask_is_published_and_the_publication_is_readable(self, operator: Client) -> None:
        api = operator.api
        admitted = operator.post(
            "/v1/items",
            {"kind": "workload", "ask": "Summarise the week", "profile": "brief"},
            key=operator.fresh_key(),
        )
        assert admitted.status_code == 201, admitted.text
        item = admitted.json()["item"]
        assert item["kind"] == "workload" and item["origin"]["kind"] == "api"
        run_id = tick(api, "completed")
        run = operator.get(f"/v1/runs/run_{run_id}").json()
        assert run["state"] == "completed" and run["kind"] == "workload"
        assert [p["sink"] for p in run["published"]] == ["chat"]
        tasks = operator.get(f"/v1/runs/run_{run_id}/tasks").json()
        assert tasks[0]["state"] == "done" and tasks[0]["output"]["summary"]
        assert operator.get(f"/v1/items/{item['id']}").json()["state"] == "done"


class TestToolRun:
    def test_a_recipe_runs_and_takes_no_agent_controls(self, operator: Client) -> None:
        api = operator.api
        recipes = operator.get("/v1/recipes").json()["data"]
        assert [r["id"] for r in recipes] == ["entrygraph"]
        admitted = operator.post(
            "/v1/items",
            {"kind": "tool", "recipe": "entrygraph", "parameters": {"repository": "o/r"}},
            key=operator.fresh_key(),
        )
        assert admitted.status_code == 201, admitted.text
        item = admitted.json()["item"]
        assert set(item["available_actions"]) == {"retry", "requeue", "abandon"}
        run_id = tick(api, "completed")
        run = operator.get(f"/v1/runs/run_{run_id}").json()
        assert run["kind"] == "tool" and run["state"] == "completed"
        # A fixed recipe has nothing to steer and no rounds to grant, ever.
        assert not {"steer", "grant_rounds", "gate_approve"} & set(run["available_actions"])
        grant = operator.post(f"/v1/runs/run_{run_id}/round-grants", {"rounds": 1})
        assert grant.status_code == 403  # the operator grant lacks budgets:grant
        full = register(api, "full")
        assert full.post(f"/v1/runs/run_{run_id}/round-grants", {"rounds": 1}).status_code == 409
        # In flight, the refusal is the kind's, not the state's.
        fake_source(api)
        thread, live, release = in_flight(
            api, gh_item("9", kind="tool", recipe="entrygraph", recipe_target="o/r")
        )
        try:
            assert full.post(f"/v1/runs/run_{live}/round-grants", {"rounds": 1}).status_code == 409
            steer = full.post(f"/v1/runs/run_{live}/steering", {"text": "x"})
            assert steer.status_code == 409 and steer.json()["code"] == "unsupported_for_kind"
        finally:
            release.set()
            thread.join(10)
