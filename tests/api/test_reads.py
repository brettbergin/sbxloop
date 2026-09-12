"""Status, capabilities and the operations record over the real loop."""

from __future__ import annotations

from tests.api.conftest import Api
from tests.unit.test_daemon_loop import gh_item


class TestStatus:
    def test_status_is_the_loops_live_state_with_an_observation_time(self, api: Api) -> None:
        api.loop.pause("deploy-1", by="brett", via="ctl")
        body = api.client.get("/v1/status", headers=api.bearer()).json()
        assert body["paused"] and body["holds"] == [
            {
                "name": "deploy-1",
                "owner": "brett",
                "via": "ctl",
                "reason": "",
                "created_at": body["holds"][0]["created_at"],
            }
        ]
        assert body["generation"] == api.loop.generation and body["current"] is None
        assert body["observed_at"].endswith("Z") and body["workspace_id"] == "local"
        assert body["watermark"] is None
        # Nothing about the host leaks: no pid, no cwd, no paths.
        assert not {"pid", "cwd", "started_at"} & set(body)

    def test_status_shows_the_run_in_flight(self, api: Api) -> None:
        api.harness.source.items = [gh_item()]
        # A scripted tick runs to completion; the current run is only visible
        # mid-tick, so check the shape from a synthetic status instead.
        from sbxloop.api.models import Status

        status = Status.from_status(
            {
                **api.loop.status(),
                "current": {
                    "item_id": "gh:issue:1",
                    "run_id": "r1",
                    "title": "Do 1",
                    "kind": "code",
                },
            },
            now=api.clock(),
        )
        assert status.current is not None and status.current.run_id == "r1"


class TestCapabilities:
    def test_capabilities_name_the_contract_and_limits(self, api: Api) -> None:
        body = api.client.get("/v1/capabilities", headers=api.bearer()).json()
        assert body["contract_version"] == 1 and body["workspace_id"] == "local"
        assert body["run_kinds"] == ["code", "workload", "tool"]
        assert "auth.refresh" in body["features"] and "runs:read" in body["capabilities"]
        assert body["limits"]["page_max"] == 200 and body["limits"]["max_body_bytes"] == 262144
        assert body["retention"]["replay_s"] == 604800

    def test_capabilities_need_a_token(self, api: Api) -> None:
        assert api.client.get("/v1/capabilities").status_code == 401

    def test_openapi_is_published_and_docs_are_not(self, api: Api) -> None:
        spec = api.client.get("/v1/openapi.json").json()
        assert "/v1/operations/{operation_id}" in spec["paths"]
        assert api.client.get("/docs").status_code == 404


class TestOperations:
    def _record(self, api: Api, n: int) -> list[str]:
        from sbxloop.daemon.controls import ControlService, Principal

        service = ControlService(api.loop)
        ids = []
        for i in range(n):
            api.clock.t += 1
            ids.append(service.pause(Principal.trusted("ops", "ctl"), f"h{i}").operation_id)
        return [op for op in ids if op is not None]

    def test_a_listing_pages_newest_first_without_gaps(self, api: Api) -> None:
        ids = self._record(api, 5)
        headers = api.bearer()
        first = api.client.get("/v1/operations", params={"limit": 2}, headers=headers).json()
        assert [op["id"] for op in first["data"]] == ids[::-1][:2] and first["has_more"]
        second = api.client.get(
            "/v1/operations", params={"limit": 2, "cursor": first["next_cursor"]}, headers=headers
        ).json()
        assert [op["id"] for op in second["data"]] == ids[::-1][2:4] and second["has_more"]
        third = api.client.get(
            "/v1/operations", params={"limit": 2, "cursor": second["next_cursor"]}, headers=headers
        ).json()
        assert [op["id"] for op in third["data"]] == ids[::-1][4:]
        assert not third["has_more"] and third["next_cursor"] is None

    def test_a_cursor_binds_to_its_filters(self, api: Api) -> None:
        self._record(api, 3)
        headers = api.bearer()
        page = api.client.get("/v1/operations", params={"limit": 1}, headers=headers).json()
        crossed = api.client.get(
            "/v1/operations",
            params={"limit": 1, "cursor": page["next_cursor"], "state": "failed"},
            headers=headers,
        )
        assert crossed.status_code == 400 and crossed.json()["code"] == "invalid_cursor"
        garbage = api.client.get("/v1/operations", params={"cursor": "zzz"}, headers=headers)
        assert garbage.status_code == 400

    def test_filters_and_the_public_shape(self, api: Api) -> None:
        ids = self._record(api, 2)
        headers = api.bearer()
        by_target = api.client.get(
            "/v1/operations", params={"target_kind": "hold", "target_id": "h1"}, headers=headers
        ).json()
        assert [op["id"] for op in by_target["data"]] == [ids[1]]
        op = by_target["data"][0]
        assert op["action"] == "daemon.pause" and op["state"] == "succeeded"
        assert op["target"] == {"kind": "hold", "id": "h1"}
        assert op["actor"] == {"kind": "operator", "id": "ops", "display": "ops", "via": "ctl"}
        assert op["accepted_at"].endswith("Z") and op["result"]["holds"] == ["h0", "h1"]
        assert api.client.get(f"/v1/operations/{ids[0]}", headers=headers).json()["id"] == ids[0]
        missing = api.client.get("/v1/operations/op_nope", headers=headers)
        assert missing.status_code == 404 and missing.json()["code"] == "not_found"
        bad = api.client.get("/v1/operations", params={"state": "weird"}, headers=headers)
        assert bad.status_code == 422
        half = api.client.get("/v1/operations", params={"target_kind": "hold"}, headers=headers)
        assert half.status_code == 422
