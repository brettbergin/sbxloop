"""The repositories page says whether a repository is set up for sbxloop,
and one call sets it up (#630).

A repository the daemon polls needs the labels the loop applies — the
seven lifecycle labels under its own names and the follow-up label — or
the states a run reports are bare text on its issues. Until now only
``sbxloop init-repo`` on the host created them, and nothing told a remote
operator which repositories carried them. Every registered repository now
carries its label state, read back by the daemon, and
``POST /v1/repositories/{id}/labels/sync`` creates the missing ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sbxloop.vcs.github.labels import lifecycle_specs
from tests.api.conftest import Api, build
from tests.fakes.fake_github import FakeGithub

MANAGE = frozenset({"runs:read", "daemon:manage"})
READ = frozenset({"runs:read"})


class Box:
    """The daemon's forge sandbox, answered by the fake."""

    def __init__(self, ops: Any) -> None:
        self.ops_obj = ops
        self.failures: list[str] = []

    def ops(self) -> Any:
        return self.ops_obj

    def note_failure(self, exc: BaseException) -> bool:
        self.failures.append(str(exc))
        return False


def _listed(api: Api, headers: dict[str, str]) -> list[dict[str, Any]]:
    return list(api.client.get("/v1/repositories", headers=headers).json()["data"])


def _forge(api: Api, *, carries: set[str] | None = None) -> FakeGithub:
    ops = FakeGithub()
    ops.labels_existing = set(carries or set())
    api.loop.github = Box(ops)
    return ops


def _every_label(api: Api, repo: str = "o/r") -> set[str]:
    config = api.ctx.config
    return {
        spec.name
        for spec in lifecycle_specs(config.labels_for(repo), config.landing.followup_label)
    }


class TestWhatARepositoryReports:
    def test_a_repository_nobody_has_looked_at_is_unknown_never_compliant(self, api: Api) -> None:
        (repo,) = _listed(api, api.bearer())
        labels = repo["labels"]
        assert labels["state"] == "unknown"
        assert labels["checked_at"] is None
        assert labels["missing"] == []
        # The set is still named, so a console can show what a sync would
        # create; nothing is claimed about any one of them.
        assert set(labels["expected"]) == _every_label(api)
        assert [label["present"] for label in labels["labels"]] == [None] * len(labels["labels"])

    def test_a_repository_that_carries_them_all_shows_itself_compliant(self, api: Api) -> None:
        _forge(api, carries=_every_label(api))
        api.loop.tick()
        (repo,) = _listed(api, api.bearer())
        labels = repo["labels"]
        assert labels["state"] == "compliant"
        assert labels["missing"] == [] and labels["checked_at"]
        assert all(label["present"] for label in labels["labels"])
        # Each label says what it is for, so a console can explain them.
        kinds = {label["kind"] for label in labels["labels"]}
        assert {"trigger", "completed", "followup"} <= kinds

    def test_a_repository_missing_labels_names_them(self, api: Api) -> None:
        _forge(api, carries={"sbxloop:run"})
        api.loop.tick()
        (repo,) = _listed(api, api.bearer())
        labels = repo["labels"]
        assert labels["state"] == "incomplete"
        assert "sbxloop:failed" in labels["missing"]
        present = {label["name"]: label["present"] for label in labels["labels"]}
        assert present["sbxloop:run"] is True and present["sbxloop:failed"] is False

    def test_a_repository_keeps_its_own_label_names(self, tmp_path: Path) -> None:
        api = build(
            tmp_path, config={"github": {"repos": [{"repo": "o/one", "trigger_label": "go"}]}}
        )
        with api.client:
            (repo,) = _listed(api, api.bearer())
            assert "go" in repo["labels"]["expected"]
            assert "sbxloop:run" not in repo["labels"]["expected"]
        api.ctx.close()


class TestSyncing:
    def test_the_sync_creates_the_missing_labels_and_the_repository_turns_compliant(
        self, api: Api
    ) -> None:
        ops = _forge(api, carries={"sbxloop:run"})
        headers = api.bearer(MANAGE)
        (repo,) = _listed(api, headers)
        response = api.client.post(f"/v1/repositories/{repo['id']}/labels/sync", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["labels"]["state"] == "compliant"
        assert "sbxloop:failed" in body["created"]
        assert body["repository"]["labels"]["state"] == "compliant"
        assert body["operation"]["action"] == "repo.labels_sync"
        assert set(ops.label_creates) == _every_label(api) - {"sbxloop:run"}
        # And the listing says so too, without another forge call.
        (listed,) = _listed(api, headers)
        assert listed["labels"]["state"] == "compliant"

    def test_syncing_a_compliant_repository_creates_nothing(self, api: Api) -> None:
        ops = _forge(api, carries=_every_label(api))
        headers = api.bearer(MANAGE)
        (repo,) = _listed(api, headers)
        response = api.client.post(f"/v1/repositories/{repo['id']}/labels/sync", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == [] and body["labels"]["state"] == "compliant"
        assert "already carries" in body["message"]
        assert ops.label_creates == []

    def test_the_sync_is_narrated_for_the_humans_watching(self, api: Api) -> None:
        _forge(api, carries={"sbxloop:run"})
        headers = api.bearer(MANAGE)
        (repo,) = _listed(api, headers)
        api.client.post(f"/v1/repositories/{repo['id']}/labels/sync", headers=headers)
        events = api.client.get("/v1/events", headers=api.bearer()).json()["data"]
        notices = [
            str(e["data"]["text"])
            for e in events
            if e["type"] == "daemon.notice"
            and e["data"].get("kind") == "daemon.repository_labels_synced"
        ]
        assert len(notices) == 1 and "sbxloop:failed" in notices[0]

    def test_reading_a_repository_is_not_enough_to_change_it(self, api: Api) -> None:
        _forge(api)
        (repo,) = _listed(api, api.bearer())
        response = api.client.post(
            f"/v1/repositories/{repo['id']}/labels/sync", headers=api.bearer(READ)
        )
        assert response.status_code == 403

    def test_an_unknown_repository_is_a_404(self, api: Api) -> None:
        _forge(api)
        response = api.client.post(
            "/v1/repositories/repo_nope/labels/sync", headers=api.bearer(MANAGE)
        )
        assert response.status_code == 404

    def test_a_daemon_with_no_forge_sandbox_says_what_to_run_instead(self, api: Api) -> None:
        headers = api.bearer(MANAGE)
        (repo,) = _listed(api, headers)
        response = api.client.post(f"/v1/repositories/{repo['id']}/labels/sync", headers=headers)
        assert response.status_code == 409, response.text
        assert "init-repo o/r" in response.json()["detail"]

    def test_a_forge_that_will_not_answer_leaves_the_state_alone(self, api: Api) -> None:
        from sbxloop.errors import GithubOpsError

        ops = _forge(api, carries=_every_label(api))
        api.loop.tick()
        headers = api.bearer(MANAGE)
        (repo,) = _listed(api, headers)
        assert repo["labels"]["state"] == "compliant"
        ops.fail_always["labels_list"] = GithubOpsError("gh api failed", http_status=403)
        response = api.client.post(f"/v1/repositories/{repo['id']}/labels/sync", headers=headers)
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "source_unavailable"
        # The last reading stands, dated; nothing is claimed afresh.
        (listed,) = _listed(api, headers)
        assert listed["labels"]["checked_at"] == repo["labels"]["checked_at"]

    def test_an_idempotent_sync_replays_its_operation(self, api: Api) -> None:
        _forge(api, carries={"sbxloop:run"})
        headers = {**api.bearer(MANAGE), "Idempotency-Key": "labels-1"}
        (repo,) = _listed(api, headers)
        first = api.client.post(f"/v1/repositories/{repo['id']}/labels/sync", headers=headers)
        second = api.client.post(f"/v1/repositories/{repo['id']}/labels/sync", headers=headers)
        assert first.status_code == 200 and second.status_code == 200, second.text
        assert first.json()["operation"]["id"] == second.json()["operation"]["id"]


class TestTheContract:
    def test_the_feature_is_advertised(self, api: Api) -> None:
        features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
        assert "repositories.labels" in features

    def test_the_socket_takes_the_same_command(self, api: Api) -> None:
        _forge(api, carries={"sbxloop:run"})
        headers = api.bearer(MANAGE)
        (repo,) = _listed(api, headers)
        with api.client.websocket_connect("/v1/ws", headers=headers) as ws:
            assert json.loads(ws.receive_text())["type"] == "hello"
            ws.send_text(
                json.dumps(
                    {
                        "type": "command",
                        "id": "c1",
                        "action": "repository.labels_sync",
                        "target": repo["id"],
                    }
                )
            )
            reply = json.loads(ws.receive_text())
            assert reply["ok"], reply
            assert reply["result"]["labels"]["state"] == "compliant"

    def test_the_committed_contract_names_the_route(self) -> None:
        document = json.loads(
            (Path(__file__).resolve().parents[2] / "docs" / "openapi.json").read_text()
        )
        assert "post" in document["paths"]["/v1/repositories/{repository_id}/labels/sync"]
