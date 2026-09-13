"""Recent snapshots stay useful after retention; HTTP logs carry no payloads."""

from typing import Any


def test_latest_snapshot_preserves_replay_and_survives_pruning(api: Any) -> None:
    headers = api.bearer()
    for i in range(6):
        api.ctx.chronology.record("collaboration.test", api.clock(), data={"i": i})
    params = {"limit": 2, "type_prefix": "collaboration."}
    old = api.client.get("/v1/events", headers=headers, params=params).json()
    recent = api.client.get("/v1/events", headers=headers, params={**params, "latest": True}).json()
    assert [e["data"]["i"] for e in old["data"]] == [0, 1]
    assert [e["data"]["i"] for e in recent["data"]] == [4, 5]
    assert not recent["has_more"]
    assert api.client.get("/v1/events?latest=true&after=evt_1", headers=headers).status_code == 400
    api.ctx.chronology.prune(api.clock() + 1)
    api.ctx.chronology.record("collaboration.test", api.clock() + 2, data={"i": 6})
    assert api.client.get("/v1/events", headers=headers).status_code == 410
    fresh = api.client.get("/v1/events?latest=true", headers=headers)
    assert fresh.status_code == 200
    assert fresh.json()["data"][-1]["data"]["i"] == 6


def test_request_logging_uses_route_templates_without_credentials(api: Any, caplog: Any) -> None:
    caplog.set_level("INFO", logger="sbxloop.api.app")
    api.client.post(
        "/v1/auth/local/login?secret=query-secret",
        json={
            "username": "nobody",
            "password": "body-secret-password",
        },
        headers={"X-Request-Id": "header-secret"},
    )
    api.client.get("/path-secret")
    records = [r.getMessage() for r in caplog.records if r.name == "sbxloop.api.app"]
    assert len(records) == 2
    assert "api.request" in records[0] and "/v1/auth/local/login" in records[0]
    assert "401" in records[0]
    assert not any("secret" in record for record in records)
