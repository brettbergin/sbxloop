"""Usage as the backend reported it: per run and per window, unknowns
kept unknown, and never a currency."""

from __future__ import annotations

import time

from sbxloop_worker.protocol import Event
from tests.api.conftest import Api
from tests.unit.test_daemon_loop import gh_item


def _finish(api: Api, key: str = "1") -> str:
    api.harness.source.items = [gh_item(key)]
    api.harness.outcomes = ["merged"]
    api.clock.t += 10
    api.loop.tick()
    return api.harness.runs[-1][0]


def _sample(api: Api, run_id: str, agent: str, **fields: object) -> None:
    # Run records and samples carry the wall clock, as they do in a live
    # daemon; the harness clock only drives the daemon's own bookkeeping.
    data = {"agent": agent, "model": "m-1", "backend": "echo", **fields}
    api.harness.store.append_event(
        Event(ts=time.time(), run_id=run_id, job_id=f"j-{agent}", type="agent.usage", data=data)
    )


class TestRunUsage:
    def test_a_run_folds_its_samples_by_persona_with_unknowns_kept(self, api: Api) -> None:
        run_id = _finish(api)
        _sample(api, run_id, "planner", input_tokens=100, output_tokens=10, agent_phase="plan")
        _sample(api, run_id, "builder", input_tokens=200, output_tokens=20, cache_read_tokens=50)
        _sample(api, run_id, "builder", input_tokens=300, output_tokens=30)
        body = api.client.get(f"/v1/runs/run_{run_id}/usage", headers=api.bearer()).json()
        assert body["recorded"] and body["turns"] == 3 and body["run_id"] == f"run_{run_id}"
        assert body["total"] == {
            "input_tokens": 600,
            "output_tokens": 60,
            "cache_read_tokens": 50,
            "cache_write_tokens": None,
        }
        by_agent = {row["agent"]: row for row in body["by_agent"]}
        assert by_agent["builder"]["turns"] == 2 and by_agent["builder"]["jobs"] == 1
        assert by_agent["planner"]["usage"]["input_tokens"] == 100
        assert body["spend"] is None and "not reported" in body["spend_basis"]
        assert body["models"] == ["echo:m-1"] or body["models"]

    def test_no_samples_is_not_zero(self, api: Api) -> None:
        run_id = _finish(api)
        body = api.client.get(f"/v1/runs/run_{run_id}/usage", headers=api.bearer()).json()
        assert body["recorded"] is False and body["turns"] == 0
        assert body["total"] == {
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
        }
        assert api.client.get("/v1/runs/run_nope/usage", headers=api.bearer()).status_code == 404


class TestWindow:
    def test_the_window_folds_the_runs_it_touches(self, api: Api) -> None:
        first = _finish(api, "1")
        _sample(api, first, "builder", input_tokens=10, output_tokens=1)
        second = _finish(api, "2")
        headers = api.bearer()
        window = {"since": time.time() - 3600, "until": time.time() + 3600}
        body = api.client.get("/v1/usage", params=window, headers=headers).json()
        assert body["runs_considered"] == 2 and body["runs_recorded"] == 1
        assert body["total"]["input_tokens"] == 10 and body["turns"] == 1
        rows = {r["run_id"]: r for r in body["runs"]}
        assert rows[f"run_{first}"]["recorded"] and not rows[f"run_{second}"]["recorded"]
        assert body["spend"] is None and body["observed_at"].endswith("Z")
        # A window before both runs holds nothing — the default one, the
        # daemon's current calendar day, included: its clock reads 1970 here.
        earlier = api.client.get(
            "/v1/usage", params={"since": 0, "until": api.clock() - 1000}, headers=headers
        ).json()
        assert earlier["runs_considered"] == 0
        today = api.client.get("/v1/usage", headers=headers).json()
        assert today["runs_considered"] == 0 and today["since"].startswith("1970-01-12")
        # RFC 3339 bounds work too.
        iso = api.client.get(
            "/v1/usage",
            params={"since": "1970-01-12T00:00:00Z", "until": "1970-01-14T00:00:00Z"},
            headers=headers,
        )
        assert iso.status_code == 200

    def test_bad_windows_are_refused(self, api: Api) -> None:
        headers = api.bearer()
        assert (
            api.client.get("/v1/usage", params={"since": "yesterday"}, headers=headers).status_code
            == 422
        )
        assert (
            api.client.get(
                "/v1/usage", params={"since": 100, "until": 50}, headers=headers
            ).status_code
            == 422
        )
        wide = api.client.get(
            "/v1/usage", params={"since": 0, "until": 40 * 86400}, headers=headers
        )
        assert wide.status_code == 422 and "31 days" in wide.json()["detail"]
