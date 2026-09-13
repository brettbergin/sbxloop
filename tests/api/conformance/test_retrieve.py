"""Retrieving results: an artifact is a file named by an id and nothing
else — no path reaches the host — and what a run spent is telemetry."""

from __future__ import annotations

from tests.api.conformance.conftest import Client, tick


class TestArtifacts:
    def test_ids_are_the_only_way_to_a_file(self, admin: Client) -> None:
        api = admin.api
        admin.post("/v1/items", {"kind": "workload", "ask": "one"}, key="k1")
        run_id = tick(api, "completed")
        listing = admin.get(f"/v1/runs/run_{run_id}/artifacts").json()
        assert listing["data"] == [] and listing["published"][0]["sink"] == "chat"
        for attempt in (
            "/v1/artifacts/art_../../etc/passwd",
            "/v1/artifacts/../etc/passwd/content",
            "/v1/artifacts/%2e%2e%2f%2e%2e%2fetc%2fpasswd/content",
            "/v1/artifacts/art_nope/content",
            f"/v1/runs/run_{run_id}/artifacts/etc/passwd",
        ):
            response = admin.get(attempt)
            assert response.status_code == 404, attempt
            assert "passwd" not in response.text or response.json()["code"] == "not_found"

    def test_usage_is_telemetry_never_a_bill(self, admin: Client) -> None:
        api = admin.api
        admin.post("/v1/items", {"kind": "workload", "ask": "one"}, key="k1")
        run_id = tick(api, "completed")
        usage = admin.get(f"/v1/runs/run_{run_id}/usage").json()
        assert usage["recorded"] is False and usage["spend"] is None
        assert "not reported" in usage["spend_basis"]
        assert "currency" not in usage
        window = admin.get("/v1/usage", since=0, until=86400).json()
        assert window["spend"] is None and window["runs_considered"] == 0
