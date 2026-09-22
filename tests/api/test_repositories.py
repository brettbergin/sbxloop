"""Repositories are registered in the daemon's database: the file's
``[[vcs.repos]]`` entries are imported once, and ``POST`` / ``PATCH`` /
``DELETE /v1/repositories`` add, change and remove registrations live —
every one a recorded operation, every one reflected in what the daemon
admits work for. Polling follows at the next start, and the answer says
so."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.api.conftest import Api, build

MANAGE = frozenset({"runs:read", "daemon:manage"})
READ = frozenset({"runs:read"})


def _listed(api: Api, headers: dict[str, str]) -> list[dict[str, Any]]:
    return list(api.client.get("/v1/repositories", headers=headers).json()["data"])


def _notices(api: Api, kind: str) -> list[str]:
    """What the daemon narrated of ``kind``, as the chronology has it."""
    events = api.client.get("/v1/events", headers=api.bearer()).json()["data"]
    return [
        str(e["data"]["text"])
        for e in events
        if e["type"] == "daemon.notice" and e["data"].get("kind") == kind
    ]


class TestTheFileIsImportedOnce:
    def test_the_declared_entries_become_rows_with_their_provenance(self, tmp_path: Path) -> None:
        api = build(
            tmp_path,
            config={
                "github": {
                    "repos": [
                        {"repo": "o/one", "deliver_base": "main"},
                        {"repo": "o/two", "enabled": False, "trigger_label": "go"},
                    ]
                }
            },
        )
        with api.client:
            repos = _listed(api, api.bearer())
            assert [r["repository"] for r in repos] == ["o/one", "o/two"]
            assert [r["source"] for r in repos] == ["config", "config"]
            assert repos[0]["created_by"] is None and repos[0]["created_at"]
            assert repos[0]["deliver_base"] == "main" and repos[0]["enabled"]
            # The file's other settings still apply to an imported entry.
            assert repos[1]["trigger_label"] == "go" and not repos[1]["enabled"]
            assert [r["restart_required"] for r in repos] == [False, False]
            rows = api.loop.dstore.repositories()
            assert [(r.repo, r.source, r.enabled) for r in rows] == [
                ("o/one", "config", True),
                ("o/two", "config", False),
            ]
            imported = _notices(api, "daemon.repositories_imported")
            assert len(imported) == 1 and "imported o/one, o/two from" in imported[0]
        api.ctx.close()

    def test_the_database_wins_over_the_file_after_the_import(self, tmp_path: Path) -> None:
        api = build(tmp_path, config={"github": {"repos": [{"repo": "o/one"}]}})
        with api.client:
            headers = api.bearer(MANAGE)
            (repo,) = _listed(api, headers)
            changed = api.client.patch(
                f"/v1/repositories/{repo['id']}",
                json={"enabled": False, "deliver_base": "release"},
                headers=headers,
            )
            assert changed.status_code == 200, changed.text
        api.ctx.close()
        # The next start reads the same file, which still says enabled and no
        # base: the registration in the database is what the daemon runs by.
        again = build(tmp_path, config={"github": {"repos": [{"repo": "o/one"}]}})
        with again.client:
            (repo,) = _listed(again, again.bearer())
            assert repo["enabled"] is False and repo["deliver_base"] == "release"
            assert repo["source"] == "config"
            assert again.ctx.config.effective_repo("o/one").deliver_base == "release"
            assert again.ctx.config.enabled_repos() == []
            # Nothing new for this process to import (the chronology still
            # carries the first start's notice; the registry knows its own).
            assert again.loop.repositories.activate() == []
        again.ctx.close()

    def test_a_removed_registration_is_not_imported_again(self, tmp_path: Path) -> None:
        api = build(tmp_path, config={"github": {"repos": [{"repo": "o/one"}, {"repo": "o/two"}]}})
        with api.client:
            headers = api.bearer(MANAGE)
            two = next(r for r in _listed(api, headers) if r["repository"] == "o/two")
            removed = api.client.delete(f"/v1/repositories/{two['id']}", headers=headers)
            assert removed.status_code == 200, removed.text
        api.ctx.close()
        again = build(
            tmp_path, config={"github": {"repos": [{"repo": "o/one"}, {"repo": "o/two"}]}}
        )
        with again.client:
            assert [r["repository"] for r in _listed(again, again.bearer())] == ["o/one"]
        again.ctx.close()

    def test_a_new_file_entry_is_imported_at_the_next_start(self, tmp_path: Path) -> None:
        api = build(tmp_path, config={"github": {"repos": [{"repo": "o/one"}]}})
        with api.client:
            assert [r["repository"] for r in _listed(api, api.bearer())] == ["o/one"]
        api.ctx.close()
        again = build(
            tmp_path, config={"github": {"repos": [{"repo": "o/one"}, {"repo": "o/three"}]}}
        )
        with again.client:
            repos = _listed(again, again.bearer())
            assert [r["repository"] for r in repos] == ["o/one", "o/three"]
            assert again.loop.repositories.activate() == ["o/three"]
            imported = _notices(again, "daemon.repositories_imported")
            assert "imported o/three from" in imported[-1]
        again.ctx.close()


class TestRegistering:
    def test_a_repository_is_added_live_and_polled_after_a_restart(self, api: Api) -> None:
        headers = api.bearer(MANAGE)
        created = api.client.post(
            "/v1/repositories",
            json={"repository": "o/new", "deliver_base": "develop"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        repo = body["repository"]
        assert repo["repository"] == "o/new" and repo["forge"] == "github"
        assert repo["enabled"] and repo["deliver_base"] == "develop"
        assert repo["source"] == "api" and repo["created_by"] == "tester"
        assert repo["restart_required"] is True
        assert repo["id"].startswith("repo_")
        assert body["operation"]["action"] == "repo.add"
        assert body["operation"]["target"] == {"kind": "repo", "id": "o/new"}
        assert "restart" in body["message"]
        assert [r["repository"] for r in _listed(api, headers)] == ["o/r", "o/new"]
        # The daemon admits work for it now: the configuration answers by name.
        entry = api.ctx.config.find_repo("o/new")
        assert entry is not None and entry.deliver_base == "develop"
        assert api.loop.config.effective_repo("o/new").deliver_base == "develop"
        assert [r.repo for r in api.loop.dstore.repositories()] == ["o/r", "o/new"]
        notices = _notices(api, "daemon.repository_added")
        assert len(notices) == 1 and "o/new" in notices[0] and "tester" in notices[0]

    def test_a_disabled_registration_and_its_forge(self, api: Api) -> None:
        headers = api.bearer(MANAGE)
        created = api.client.post(
            "/v1/repositories",
            json={"repository": "group/sub/project", "forge": "gitlab", "enabled": False},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        repo = created.json()["repository"]
        assert repo["forge"] == "gitlab" and repo["enabled"] is False
        # Not polled at start, not enabled: nothing to restart for.
        assert repo["restart_required"] is False
        assert api.ctx.config.find_repo("group/sub/project") is not None
        assert api.ctx.config.vcs_kind_for("group/sub/project") == "gitlab"

    def test_a_taken_name_a_bad_name_and_an_unknown_forge_are_refused(self, api: Api) -> None:
        headers = api.bearer(MANAGE)
        taken = api.client.post("/v1/repositories", json={"repository": "O/R"}, headers=headers)
        assert taken.status_code == 422, taken.text
        assert taken.json()["code"] == "invalid_argument"
        assert "o/r" in taken.json()["detail"] and "already" in taken.json()["detail"]
        bad = api.client.post("/v1/repositories", json={"repository": "no-slash"}, headers=headers)
        assert bad.status_code == 422 and "owner/name" in bad.json()["detail"]
        nested = api.client.post(
            "/v1/repositories", json={"repository": "a/b/c", "forge": "github"}, headers=headers
        )
        assert nested.status_code == 422
        forge = api.client.post(
            "/v1/repositories", json={"repository": "o/x", "forge": "svn"}, headers=headers
        )
        assert forge.status_code == 422
        assert [r["repository"] for r in _listed(api, headers)] == ["o/r"]

    def test_daemon_manage_is_required(self, api: Api) -> None:
        refused = api.client.post(
            "/v1/repositories", json={"repository": "o/new"}, headers=api.bearer(READ)
        )
        assert refused.status_code == 403, refused.text
        (repo,) = _listed(api, api.bearer(READ))
        assert (
            api.client.patch(
                f"/v1/repositories/{repo['id']}", json={"enabled": False}, headers=api.bearer(READ)
            ).status_code
            == 403
        )
        assert (
            api.client.delete(
                f"/v1/repositories/{repo['id']}", headers=api.bearer(READ)
            ).status_code
            == 403
        )

    def test_an_idempotent_add_replays_its_operation(self, api: Api) -> None:
        headers = {**api.bearer(MANAGE), "Idempotency-Key": "add-1"}
        first = api.client.post("/v1/repositories", json={"repository": "o/new"}, headers=headers)
        second = api.client.post("/v1/repositories", json={"repository": "o/new"}, headers=headers)
        assert first.status_code == 201 and second.status_code == 201, second.text
        assert first.json()["operation"]["id"] == second.json()["operation"]["id"]
        assert [r["repository"] for r in _listed(api, headers)] == ["o/r", "o/new"]


class TestChanging:
    def test_enabled_and_the_base_change_live_and_say_when_polling_follows(self, api: Api) -> None:
        headers = api.bearer(MANAGE)
        (repo,) = _listed(api, headers)
        assert repo["restart_required"] is False
        paused = api.client.patch(
            f"/v1/repositories/{repo['id']}", json={"enabled": False}, headers=headers
        )
        assert paused.status_code == 200, paused.text
        body = paused.json()
        assert body["repository"]["enabled"] is False
        # Polled at start, disabled now: polling stops at the next start.
        assert body["repository"]["restart_required"] is True
        assert body["operation"]["action"] == "repo.update"
        assert api.ctx.config.enabled_repos() == []
        assert api.ctx.config.find_repo("o/r") is not None
        based = api.client.patch(
            f"/v1/repositories/{repo['id']}",
            json={"enabled": True, "deliver_base": "release"},
            headers=headers,
        )
        assert based.status_code == 200, based.text
        assert based.json()["repository"]["deliver_base"] == "release"
        assert based.json()["repository"]["restart_required"] is False
        assert api.ctx.config.effective_repo("o/r").deliver_base == "release"
        cleared = api.client.patch(
            f"/v1/repositories/{repo['id']}", json={"deliver_base": None}, headers=headers
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["repository"]["deliver_base"] is None
        untouched = api.client.patch(f"/v1/repositories/{repo['id']}", json={}, headers=headers)
        assert untouched.status_code == 200 and untouched.json()["repository"]["enabled"] is True
        assert (
            api.client.patch(
                "/v1/repositories/repo_nope", json={"enabled": False}, headers=headers
            ).status_code
            == 404
        )

    def test_the_files_other_settings_survive_a_change(self, tmp_path: Path) -> None:
        api = build(
            tmp_path,
            config={
                "github": {"repos": [{"repo": "o/one", "trigger_label": "go", "labels": ["x"]}]}
            },
        )
        with api.client:
            headers = api.bearer(MANAGE)
            (repo,) = _listed(api, headers)
            changed = api.client.patch(
                f"/v1/repositories/{repo['id']}", json={"deliver_base": "main"}, headers=headers
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["repository"]["trigger_label"] == "go"
            entry = api.ctx.config.find_repo("o/one")
            assert entry is not None and entry.labels == ["x"] and entry.deliver_base == "main"
        api.ctx.close()


class TestRemoving:
    def test_a_registration_is_removed_and_its_id_forgotten(self, api: Api) -> None:
        headers = api.bearer(MANAGE)
        created = api.client.post("/v1/repositories", json={"repository": "o/new"}, headers=headers)
        new_id = created.json()["repository"]["id"]
        removed = api.client.delete(f"/v1/repositories/{new_id}", headers=headers)
        assert removed.status_code == 200, removed.text
        body = removed.json()
        assert body["repository"] is None
        assert body["operation"]["action"] == "repo.remove"
        assert body["operation"]["target"]["id"] == "o/new"
        assert [r["repository"] for r in _listed(api, headers)] == ["o/r"]
        assert api.ctx.config.find_repo("o/new") is None
        assert api.client.delete(f"/v1/repositories/{new_id}", headers=headers).status_code == 404
        assert (
            api.client.post(f"/v1/repositories/{new_id}/resume", headers=headers).status_code == 404
        )
        notices = _notices(api, "daemon.repository_removed")
        assert len(notices) == 1 and "o/new" in notices[0]

    def test_a_removed_name_can_be_registered_again(self, api: Api) -> None:
        headers = api.bearer(MANAGE)
        (repo,) = _listed(api, headers)
        assert (
            api.client.delete(f"/v1/repositories/{repo['id']}", headers=headers).status_code == 200
        )
        assert _listed(api, headers) == []
        assert api.ctx.config.repo_list() == []
        again = api.client.post(
            "/v1/repositories", json={"repository": "o/r", "deliver_base": "v2"}, headers=headers
        )
        assert again.status_code == 201, again.text
        assert again.json()["repository"]["deliver_base"] == "v2"
        assert again.json()["repository"]["source"] == "api"
        assert [r["repository"] for r in _listed(api, headers)] == ["o/r"]


class TestTheContract:
    def test_the_feature_is_advertised(self, api: Api) -> None:
        features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
        assert "repositories.manage" in features

    def test_the_socket_takes_the_same_commands(self, api: Api) -> None:
        with api.client.websocket_connect("/v1/ws", headers=api.bearer(MANAGE)) as ws:
            assert json.loads(ws.receive_text())["type"] == "hello"
            ws.send_text(
                json.dumps(
                    {
                        "type": "command",
                        "id": "c1",
                        "action": "repository.add",
                        "params": {"repository": "o/new"},
                    }
                )
            )
            reply = json.loads(ws.receive_text())
            assert reply["ok"], reply
            new_id = reply["result"]["repository"]["id"]
            ws.send_text(
                json.dumps(
                    {
                        "type": "command",
                        "id": "c2",
                        "action": "repository.update",
                        "target": new_id,
                        "params": {"enabled": False},
                    }
                )
            )
            reply = json.loads(ws.receive_text())
            assert reply["ok"] and reply["result"]["repository"]["enabled"] is False
            ws.send_text(
                json.dumps(
                    {"type": "command", "id": "c3", "action": "repository.remove", "target": new_id}
                )
            )
            reply = json.loads(ws.receive_text())
            assert reply["ok"] and reply["result"]["repository"] is None
        assert [r["repository"] for r in _listed(api, api.bearer())] == ["o/r"]

    def test_the_committed_contract_names_the_routes(self) -> None:
        document = json.loads(
            (Path(__file__).resolve().parents[2] / "docs" / "openapi.json").read_text()
        )
        assert "post" in document["paths"]["/v1/repositories"]
        assert {"patch", "delete"} <= set(document["paths"]["/v1/repositories/{repository_id}"])
