"""A run's artifacts by identity: catalogued once from the same scan the
host listing uses, downloaded as attachments through the run's own
directory, refused when they would escape it, gone when the run is."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sbxloop.api.artifacts import CATALOG_MAX_FILES, content_type_for, guess_media_type
from tests.api.conftest import Api
from tests.unit.test_daemon_loop import gh_item


def _finish(api: Api, key: str = "1", outcome: str = "merged", **fields: object) -> str:
    api.harness.source.items = [gh_item(key, **fields)]
    api.harness.outcomes = [outcome]
    api.clock.t += 10
    api.loop.tick()
    return api.harness.runs[-1][0]


def _artifacts_root(api: Api, run_id: str, *, mounted: bool) -> Path:
    """Give the run a workspace (a code run's tree when mounted, else the
    harvested directory) with a few files in it."""
    home = api.ctx.config.paths
    workspace = home.run_workspace(run_id)
    workspace.mkdir(parents=True, exist_ok=True)
    api.harness.store.set_run_workspace(run_id, workspace, mounted=mounted)
    root = workspace if mounted else home.run_artifacts(run_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.md").write_text("# Report\n")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "data.json").write_text('{"ok": true}')
    (root / "page.html").write_text("<script>alert(1)</script>")
    (root / "node_modules").mkdir(exist_ok=True)
    (root / "node_modules" / "dep.js").write_text("x")
    return root


class TestCatalog:
    def test_a_finished_code_run_is_catalogued_from_its_tree(self, api: Api) -> None:
        run_id = _finish(api)
        root = _artifacts_root(api, run_id, mounted=True)
        headers = api.bearer()
        page = api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=headers).json()
        paths = [a["path"] for a in page["data"]]
        assert paths == ["page.html", "report.md", "sub/data.json"]
        report = next(a for a in page["data"] if a["path"] == "report.md")
        assert report["id"].startswith("art_") and report["run_id"] == f"run_{run_id}"
        assert report["size"] == len("# Report\n")
        assert report["sha256"] == hashlib.sha256(b"# Report\n").hexdigest()
        assert report["media_type"] == "text/markdown" and report["origin"] == "workspace"
        assert report["available"] and report["task_id"] is None
        # Nothing about the host: no absolute path anywhere in the listing.
        assert (
            str(root)
            not in api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=headers).text
        )
        # Catalogued once: a second listing returns the same ids.
        again = api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=headers).json()
        assert [a["id"] for a in again["data"]] == [a["id"] for a in page["data"]]
        assert page["published"] == []

    def test_a_workload_run_lists_what_its_sink_declared(self, api: Api) -> None:
        run_id = _finish(api, "2", "completed", kind="workload")
        home = api.ctx.config.paths
        api.harness.store.set_run_workspace(run_id, home.run_data(run_id), mounted=False)
        root = home.run_artifacts(run_id)
        root.mkdir(parents=True)
        (root / "answer.txt").write_text("42")
        # The task declared the file: the catalog attributes it.
        (task,) = api.harness.store.get_tasks(run_id)
        assert task.output is not None
        task.output.files = ["answer.txt"]
        api.harness.store.update_task(run_id, task)
        headers = api.bearer()
        page = api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=headers).json()
        (entry,) = page["data"]
        assert entry["path"] == "answer.txt" and entry["origin"] == "sink"
        assert entry["task_id"] == "t1" and entry["media_type"] == "text/plain"
        # Publication is a separate fact from the catalog.
        assert page["published"] == [
            {"sink": "chat", "location": "chat", "tasks": ["t1"], "files": 0}
        ]

    def test_a_run_without_a_workspace_has_an_empty_catalog(self, api: Api) -> None:
        run_id = _finish(api)
        page = api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=api.bearer()).json()
        assert page["data"] == []

    def test_links_out_of_the_run_are_never_catalogued(self, api: Api) -> None:
        run_id = _finish(api)
        root = _artifacts_root(api, run_id, mounted=True)
        secret = api.ctx.config.paths.runs.parent / "secrets.env"
        secret.write_text("TOKEN=hunter2\n")
        (root / "escape.env").symlink_to(secret)
        (root / "inside.md").symlink_to(root / "report.md")
        page = api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=api.bearer()).json()
        paths = [a["path"] for a in page["data"]]
        assert "escape.env" not in paths and "inside.md" in paths
        # The catalog is the only door: an id names one file, a path never does.
        assert api.client.get(
            "/v1/artifacts/../../secrets.env", headers=api.bearer()
        ).status_code in (404, 400)


class TestDownload:
    def test_the_bytes_come_as_an_attachment_with_their_digest(self, api: Api) -> None:
        run_id = _finish(api)
        _artifacts_root(api, run_id, mounted=True)
        headers = api.bearer()
        page = api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=headers).json()
        report = next(a for a in page["data"] if a["path"] == "report.md")
        response = api.client.get(f"/v1/artifacts/{report['id']}/content", headers=headers)
        assert response.status_code == 200 and response.content == b"# Report\n"
        assert response.headers["content-disposition"] == 'attachment; filename="report.md"'
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-checksum-sha256"] == report["sha256"]
        assert response.headers["content-type"].startswith("text/markdown")
        one = api.client.get(f"/v1/artifacts/{report['id']}", headers=headers).json()
        assert one == report

    def test_html_is_never_served_as_html(self, api: Api) -> None:
        run_id = _finish(api)
        _artifacts_root(api, run_id, mounted=True)
        headers = api.bearer()
        page = api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=headers).json()
        html = next(a for a in page["data"] if a["path"] == "page.html")
        assert html["media_type"] == "text/html"
        response = api.client.get(f"/v1/artifacts/{html['id']}/content", headers=headers)
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment")
        assert content_type_for("image/svg+xml") == "application/octet-stream"
        assert content_type_for("image/png") == "image/png"
        assert guess_media_type("weird.unknownext") == "application/octet-stream"

    def test_a_pruned_run_is_gone_not_a_crash(self, api: Api) -> None:
        run_id = _finish(api)
        root = _artifacts_root(api, run_id, mounted=True)
        headers = api.bearer()
        page = api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=headers).json()
        report = next(a for a in page["data"] if a["path"] == "report.md")
        (root / "report.md").unlink()
        gone = api.client.get(f"/v1/artifacts/{report['id']}/content", headers=headers)
        assert gone.status_code == 410 and gone.json()["code"] == "artifact_gone"
        one = api.client.get(f"/v1/artifacts/{report['id']}", headers=headers).json()
        assert one["available"] is False and one["tombstoned_at"]
        # History keeps the entry: the listing still says what was there.
        listed = api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=headers).json()
        assert [a["available"] for a in listed["data"] if a["path"] == "report.md"] == [False]
        again = api.client.get(f"/v1/artifacts/{report['id']}/content", headers=headers)
        assert again.status_code == 410

    def test_downloads_need_their_capability_and_a_known_id(self, api: Api) -> None:
        run_id = _finish(api)
        _artifacts_root(api, run_id, mounted=True)
        reader = api.bearer(frozenset({"runs:read"}))
        refused = api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=reader)
        assert refused.status_code == 403 and refused.json()["capability"] == "artifacts:read"
        assert api.client.get("/v1/artifacts/art_nope", headers=api.bearer()).status_code == 404
        assert (
            api.client.get("/v1/artifacts/art_nope/content", headers=api.bearer()).status_code
            == 404
        )
        assert api.client.get("/v1/artifacts/art_x/content").status_code == 401


def test_the_catalog_is_bounded(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    from sbxloop.api import artifacts as module

    monkeypatch.setattr(module, "CATALOG_MAX_FILES", 2)
    run_id = _finish(api)
    _artifacts_root(api, run_id, mounted=True)
    page = api.client.get(f"/v1/runs/run_{run_id}/artifacts", headers=api.bearer()).json()
    assert len(page["data"]) == 2
    assert CATALOG_MAX_FILES == 2000
