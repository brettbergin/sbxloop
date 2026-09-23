"""Workspace access follows the durable job binding across every surface."""

from sbxloop_worker.protocol import Event
from tests.api.conftest import Api
from tests.api.test_artifacts import _artifacts_root, _finish
from tests.api.test_channel_access import _invite
from tests.api.test_collaboration import bearer, register


def test_a_member_reads_external_history_and_files_after_reconciliation(api: Api) -> None:
    owner = register(api)
    guest = _invite(api, "member", "guest")
    api.ctx.project_work()
    run_id = _finish(api)
    _artifacts_root(api, run_id, mounted=True)
    api.harness.store.append_event(
        Event(ts=api.clock(), run_id=run_id, type="phase.start", data={"phase": "plan"})
    )
    api.ctx.chronology.project(api.clock())  # History predates the association.
    headers = bearer(guest)
    route = f"/v1/runs/run_{run_id}"
    assert api.client.get(route + "/events", headers=headers).json()["data"] == []
    assert api.client.get(route + "/artifacts", headers=headers).status_code == 403

    api.ctx.project_work()
    listing = api.client.get("/v1/channels", headers=headers)
    assert listing.status_code == 200, listing.text
    channels = listing.json()["items"]
    assert len(channels) == 1
    channel_id = channels[0]["id"]
    jobs = api.client.get(f"/v1/channels/{channel_id}/jobs", headers=headers)
    assert jobs.status_code == 200, jobs.text
    assert jobs.json()[0]["run_id"] == f"run_{run_id}"
    events = api.client.get(route + "/events", headers=headers)
    assert events.status_code == 200, events.text
    assert "phase.start" in {event["type"] for event in events.json()["data"]}
    files = api.client.get(route + "/artifacts", headers=headers)
    assert files.status_code == 200, files.text
    report = next(entry for entry in files.json()["data"] if entry["path"] == "report.md")
    assert (
        api.client.get(f"/v1/artifacts/{report['id']}/content", headers=headers).content
        == b"# Report\n"
    )

    removed = api.client.delete(f"/v1/channels/{channel_id}", headers=bearer(owner))
    assert removed.status_code == 204, removed.text
    api.ctx.project_work()
    assert api.client.get("/v1/channels", headers=headers).json()["items"] == []
    assert api.client.get(route + "/events", headers=headers).json()["data"] == []
    assert api.client.get(route + "/artifacts", headers=headers).status_code == 403
    assert api.client.get(f"/v1/channels/{channel_id}/jobs", headers=headers).status_code == 404
