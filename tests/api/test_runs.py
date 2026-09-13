"""Runs and their task graphs, read back over the real stores."""

from __future__ import annotations

from tests.api.conftest import Api
from tests.unit.test_daemon_loop import gh_item


def _run(api: Api, key: str, outcome: str, **fields: object) -> str:
    api.harness.source.items = [gh_item(key, **fields)]
    api.harness.outcomes = [outcome]
    api.clock.t += 10
    assert api.loop.tick().outcome in ("done", "retry")
    run_id, _resume = api.harness.runs[-1]
    return run_id


class TestRuns:
    def test_runs_page_most_recently_touched_first(self, api: Api) -> None:
        ids = [_run(api, "1", "merged"), _run(api, "2", "failed"), _run(api, "3", "merged")]
        headers = api.bearer()
        first = api.client.get("/v1/runs", params={"limit": 2}, headers=headers).json()
        assert [r["id"] for r in first["data"]] == [f"run_{ids[2]}", f"run_{ids[1]}"]
        assert first["has_more"]
        second = api.client.get(
            "/v1/runs", params={"limit": 2, "cursor": first["next_cursor"]}, headers=headers
        ).json()
        assert [r["id"] for r in second["data"]] == [f"run_{ids[0]}"] and not second["has_more"]
        failed = api.client.get("/v1/runs", params={"state": "failed"}, headers=headers).json()
        assert [r["id"] for r in failed["data"]] == [f"run_{ids[1]}"]
        assert (
            api.client.get("/v1/runs", params={"kind": "bogus"}, headers=headers).status_code == 422
        )
        crossed = api.client.get(
            "/v1/runs", params={"cursor": first["next_cursor"], "state": "failed"}, headers=headers
        )
        assert crossed.status_code == 400

    def test_a_run_shows_its_pr_rounds_and_actions(self, api: Api) -> None:
        run_id = _run(api, "1", "exhausted")
        body = api.client.get(f"/v1/runs/run_{run_id}", headers=api.bearer()).json()
        assert body["id"] == f"run_{run_id}" and body["kind"] == "code"
        assert body["state"] == "failed" and body["stage"] == "reviewing"
        # The daemon granted its own retry rounds and queued the resume,
        # so the budget shows the grant and no longer reads as exhausted.
        assert body["rounds"] == {"review": 3, "ci": 0, "granted": 2, "exhausted": None}
        assert body["pull_request"]["number"] == 9 and body["pull_request"]["branch"]
        assert body["item_id"].startswith("itm_") and body["revision"] >= 1
        # A run pinned to a queued item can be resumed, or its resume
        # cancelled; nothing is in flight to steer.
        assert body["available_actions"] == ["cancel", "resume"]
        # Nothing about the host: no workspace path, no config, no credentials.
        assert not {"workspace", "credentials", "config"} & set(body)

    def test_a_workload_run_lists_its_tasks_and_publication(self, api: Api) -> None:
        run_id = _run(api, "2", "completed", kind="workload")
        headers = api.bearer()
        body = api.client.get(f"/v1/runs/run_{run_id}", headers=headers).json()
        assert body["kind"] == "workload" and body["state"] == "completed"
        assert body["published"] == [
            {"sink": "chat", "location": "chat", "tasks": ["t1"], "files": 0}
        ]
        tasks = api.client.get(f"/v1/runs/run_{run_id}/tasks", headers=headers).json()
        assert tasks == [
            {
                "id": "t1",
                "title": "Answer",
                "description": "",
                "state": "done",
                "depends_on": [],
                "revisions": 0,
                "replans": 0,
                "verify_suspect": False,
                "verify_reauthors": 0,
                "output": {"summary": "the answer is 42", "files": []},
            }
        ]
        assert api.client.get("/v1/runs/run_nope/tasks", headers=headers).status_code == 404
