"""Admitting work through the API: an existing issue through the GitHub
source's own rules, an inline workload under an allowed profile, a
registered recipe with validated parameters — recorded as operations,
idempotent under a key, converging with polling on one item."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop.daemon.sources import (
    ApiSource,
    CompositeSource,
    GitHubIssueSource,
    MultiRepoIssueSource,
    ScheduleSource,
)
from tests.api.conftest import Api, build
from tests.unit.test_daemon_sources import FIXTURE_NOW, LABELS, RecordingOps, issue

KEY = {"Idempotency-Key": "k1"}


def _github(ops: RecordingOps, repo: str = "o/r", *, qualify: bool = False) -> GitHubIssueSource:
    return GitHubIssueSource(
        lambda: ops,  # type: ignore[arg-type]
        repo,
        LABELS,
        host="db",
        clock=lambda: FIXTURE_NOW,
        qualify_ids=qualify,
    )


@pytest.fixture
def ops() -> RecordingOps:
    return RecordingOps({"4": issue(4), "5": issue(5, "sbxloop:run"), "6": issue(6, "closed")})


@pytest.fixture
def api(tmp_path: Path, ops: RecordingOps) -> Any:
    ops.issues["6"]["state"] = "closed"
    built = build(
        tmp_path,
        config={
            "daemon": {"trigger_label": "sbxloop:run", "in_progress_label": "sbxloop:in-progress"},
            "workloads": [{"name": "research", "sinks": ["chat", "artifact"]}],
        },
    )
    built.loop.source = CompositeSource(_github(ops), None, ScheduleSource(), ApiSource())
    with built.client:
        yield built
    built.ctx.close()


class TestIssueIntake:
    def test_admits_by_labelling_and_polling_converges(self, api: Api, ops: RecordingOps) -> None:
        headers = {**api.bearer(), **KEY}
        response = api.client.post(
            "/v1/items", json={"kind": "issue", "repository": "o/r", "number": 4}, headers=headers
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["created"] and response.headers["Location"] == f"/v1/items/{body['item']['id']}"
        assert body["item"]["state"] == "queued" and body["item"]["kind"] == "code"
        assert (
            body["item"]["origin"]["number"] == 4 and body["item"]["origin"]["repository"] == "o/r"
        )
        assert body["operation"]["action"] == "item.admit"
        assert body["operation"]["state"] == "succeeded"
        assert body["operation"]["target"] == {"kind": "item", "id": "o/r#4"}
        assert body["operation"]["actor"]["kind"] == "client"
        # The issue was labelled as a person would label it …
        assert [lb["name"] for lb in ops.issues["4"]["labels"]] == ["sbxloop:run"]
        # … so the next poll finds it and the queue still holds one item.
        polled = {i.item_id: i for i in api.loop.source.poll()}
        assert "gh:issue:4" in polled
        assert api.loop.dstore.upsert_new(polled["gh:issue:4"], api.clock()) is False
        assert len(api.loop.dstore.items()) == 1
        # The detail names who admitted it.
        detail = api.client.get(f"/v1/items/{body['item']['id']}", headers=headers).json()
        assert detail["admitted_by"]["kind"] == "client" and detail["admitted_by"]["via"] == "api"

    def test_an_already_labelled_issue_is_queued_without_relabelling(
        self, api: Api, ops: RecordingOps
    ) -> None:
        headers = {**api.bearer(), **KEY}
        response = api.client.post(
            "/v1/items", json={"kind": "issue", "repository": "o/r", "number": 5}, headers=headers
        )
        assert response.status_code == 201
        assert not any(m == "POST" for m, _, _ in ops.raw_calls)
        # Once polling has queued it, the same admission is a 200.
        again = api.client.post(
            "/v1/items",
            json={"kind": "issue", "repository": "o/r", "number": 5},
            headers={**api.bearer(), "Idempotency-Key": "k2"},
        )
        assert again.status_code == 200 and not again.json()["created"]
        assert len(api.loop.dstore.items()) == 1

    def test_refusals_are_named(self, api: Api, ops: RecordingOps) -> None:
        headers = {**api.bearer(), **KEY}

        def post(number: int, **extra: Any) -> Any:
            return api.client.post(
                "/v1/items",
                json={"kind": "issue", "repository": "o/r", "number": number, **extra},
                headers={**headers, "Idempotency-Key": f"n{number}{extra}"},
            )

        closed = post(6)
        assert closed.status_code == 409 and closed.json()["code"] == "not_eligible"
        assert "not open" in closed.json()["detail"]
        ops.issues["7"] = issue(7, "sbxloop:in-progress")
        claimed = post(7)
        assert claimed.status_code == 409 and "in progress" in claimed.json()["detail"]
        ops.issues["8"] = issue(8, "sbxloop:workload")
        crossed = post(8)
        assert crossed.status_code == 409 and "queued as a workload" in crossed.json()["detail"]
        missing = post(99)
        assert missing.status_code == 409  # the stub answers a closed issue for an unknown number
        unknown_repo = api.client.post(
            "/v1/items",
            json={"kind": "issue", "repository": "o/other", "number": 4},
            headers={**headers, "Idempotency-Key": "r"},
        )
        assert unknown_repo.status_code == 404 and unknown_repo.json()["code"] == "unknown_target"
        both = api.client.post(
            "/v1/items",
            json={"kind": "issue", "repository": "o/r", "repository_id": "repo_x", "number": 4},
            headers={**headers, "Idempotency-Key": "b"},
        )
        assert both.status_code == 422
        # Every refusal is on the record too.
        listed = api.client.get(
            "/v1/operations", params={"state": "failed"}, headers=api.bearer()
        ).json()
        assert {op["error_code"] for op in listed["data"]} >= {"not_eligible", "unknown_target"}

    def test_a_workload_label_admits_a_workload_run(self, api: Api, ops: RecordingOps) -> None:
        response = api.client.post(
            "/v1/items",
            json={"kind": "issue", "repository": "o/r", "number": 4, "run_kind": "workload"},
            headers={**api.bearer(), **KEY},
        )
        assert response.status_code == 201 and response.json()["item"]["kind"] == "workload"
        assert [lb["name"] for lb in ops.issues["4"]["labels"]] == ["sbxloop:workload"]

    def test_by_repository_id_across_two_repositories(self, tmp_path: Path) -> None:
        one, two = RecordingOps({"4": issue(4)}), RecordingOps({"4": issue(4)})
        api = build(tmp_path, config={"github": {"repos": [{"repo": "o/one"}, {"repo": "o/two"}]}})
        api.loop.source = CompositeSource(
            MultiRepoIssueSource(
                [_github(one, "o/one", qualify=True), _github(two, "o/two", qualify=True)]
            ),
            None,
            ScheduleSource(),
            ApiSource(),
        )
        with api.client:
            headers = api.bearer()
            repos = {
                r["repository"]: r["id"]
                for r in api.client.get("/v1/repositories", headers=headers).json()["data"]
            }
            admitted = []
            for name, key in (("o/one", "a"), ("o/two", "b")):
                response = api.client.post(
                    "/v1/items",
                    json={"kind": "issue", "repository_id": repos[name], "number": 4},
                    headers={**headers, "Idempotency-Key": key},
                )
                assert response.status_code == 201, response.text
                admitted.append(response.json()["item"])
            assert admitted[0]["id"] != admitted[1]["id"]
            assert [i["origin"]["ref"] for i in admitted] == [
                "gh:o/one:issue:4",
                "gh:o/two:issue:4",
            ]
            assert [lb["name"] for lb in two.issues["4"]["labels"]] == ["sbxloop:run"]
        api.ctx.close()


class TestIdempotency:
    def test_the_key_is_required_and_replays_answer_the_same_operation(self, api: Api) -> None:
        headers = api.bearer()
        body = {"kind": "workload", "ask": "Summarise the week", "profile": "research"}
        missing = api.client.post("/v1/items", json=body, headers=headers)
        assert missing.status_code == 422
        assert missing.json()["code"] == "idempotency_key_required"
        first = api.client.post("/v1/items", json=body, headers={**headers, **KEY})
        assert first.status_code == 201, first.text
        replay = api.client.post("/v1/items", json=body, headers={**headers, **KEY})
        assert replay.status_code == 200 and not replay.json()["created"]
        assert replay.json()["operation"]["id"] == first.json()["operation"]["id"]
        assert replay.json()["item"]["id"] == first.json()["item"]["id"]
        assert len(api.loop.dstore.items()) == 1
        # A different payload under the same key is a conflict.
        conflict = api.client.post(
            "/v1/items", json={**body, "ask": "Something else"}, headers={**headers, **KEY}
        )
        assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
        assert conflict.json()["operation_id"] == first.json()["operation"]["id"]
        # The key is scoped to the principal: another client's same key is its own.
        other = api.client.post("/v1/items", json=body, headers={**api.bearer(), **KEY})
        assert other.status_code == 201 and len(api.loop.dstore.items()) == 2

    def test_a_replayed_refusal_is_the_refusal(self, api: Api) -> None:
        headers = {**api.bearer(), **KEY}
        body = {"kind": "workload", "ask": "x", "profile": "nope"}
        first = api.client.post("/v1/items", json=body, headers=headers)
        assert first.status_code == 422 and first.json()["code"] == "invalid_argument"
        replay = api.client.post("/v1/items", json=body, headers=headers)
        assert replay.status_code == 422 and replay.json()["operation_id"]


class TestWorkloadIntake:
    def test_an_inline_workload_rides_the_api_source(self, api: Api) -> None:
        response = api.client.post(
            "/v1/items",
            json={
                "kind": "workload",
                "ask": "Summarise\nthe week",
                "profile": "research",
                "sink": "artifact",
            },
            headers={**api.bearer(), **KEY},
        )
        assert response.status_code == 201, response.text
        item = response.json()["item"]
        assert item["kind"] == "workload" and item["profile"] == "research"
        assert item["title"] == "Summarise" and item["origin"]["kind"] == "api"
        assert item["origin"]["ref"].startswith("api:") and item["origin"]["repository"] is None
        stored = api.loop.dstore.get(item["origin"]["ref"])
        assert stored is not None and stored.body.endswith(
            "Deliver the result through the `artifact` sink."
        )
        assert stored.requested_by is None
        assert api.loop.source.for_item(stored).name == "api"
        # The daemon's own outcome text names where it came from.
        assert "remote API" in api.loop.outcome_text(stored)

    def test_profiles_and_sinks_are_checked(self, api: Api) -> None:
        headers = api.bearer()

        def post(key: str, **body: Any) -> Any:
            return api.client.post(
                "/v1/items",
                json={"kind": "workload", **body},
                headers={**headers, "Idempotency-Key": key},
            )

        unknown = post("a", ask="x", profile="nope")
        assert unknown.status_code == 422 and "not declared" in unknown.json()["detail"]
        sink = post("b", ask="x", profile="research", sink="pr")
        assert sink.status_code == 409 and sink.json()["code"] == "not_eligible"
        bad_sink = post("c", ask="x", profile="research", sink="fax")
        assert bad_sink.status_code == 422
        blank = post("d", ask="   ", profile="research")
        assert blank.status_code == 422
        # No profile and no default: a run with no profile is still allowed.
        assert post("e", ask="x").status_code == 201


class TestToolIntake:
    def test_a_registered_recipe_with_a_configured_target(self, api: Api) -> None:
        response = api.client.post(
            "/v1/items",
            json={"kind": "tool", "recipe": "entrygraph", "parameters": {"repository": "o/r"}},
            headers={**api.bearer(), **KEY},
        )
        assert response.status_code == 201, response.text
        item = response.json()["item"]
        assert item["kind"] == "tool" and item["recipe"] == "entrygraph"
        assert item["recipe_target"] == "o/r" and item["origin"]["repository"] == "o/r"
        assert item["origin"]["kind"] == "api"
        # A queued item's own controls; steering and grants are a run's,
        # and a tool run never gets them (see the eligibility matrix).
        assert set(item["available_actions"]) == {"retry", "requeue", "abandon"}

    def test_parameters_are_validated_against_the_registry(self, api: Api) -> None:
        headers = api.bearer()

        def post(key: str, **body: Any) -> Any:
            return api.client.post(
                "/v1/items",
                json={"kind": "tool", **body},
                headers={**headers, "Idempotency-Key": key},
            )

        unknown = post("a", recipe="shell", parameters={"command": "rm -rf /"})
        assert unknown.status_code == 404 and unknown.json()["code"] == "unknown_target"
        extra = post("b", recipe="entrygraph", parameters={"repository": "o/r", "command": "ls"})
        assert extra.status_code == 422 and extra.json()["parameters"] == ["command"]
        neither = post("c", recipe="entrygraph", parameters={})
        assert neither.status_code == 422 and "exactly one" in neither.json()["detail"]
        elsewhere = post("d", recipe="entrygraph", parameters={"repository": "o/elsewhere"})
        assert elsewhere.status_code == 422 and "not an enabled" in elsewhere.json()["detail"]
        assert api.loop.dstore.items() == []

    def test_a_disabled_recipe_is_refused(self, tmp_path: Path) -> None:
        api = build(tmp_path, config={"entrygraph": {"enabled": False}})
        with api.client:
            headers = api.bearer()
            recipes = api.client.get("/v1/recipes", headers=headers).json()["data"]
            assert recipes[0]["enabled"] is False
            response = api.client.post(
                "/v1/items",
                json={"kind": "tool", "recipe": "entrygraph", "parameters": {"repository": "o/r"}},
                headers={**headers, **KEY},
            )
            assert response.status_code == 409 and response.json()["code"] == "not_eligible"
        api.ctx.close()


class TestPermissions:
    def test_intake_needs_items_create(self, api: Api) -> None:
        response = api.client.post(
            "/v1/items",
            json={"kind": "workload", "ask": "x"},
            headers={**api.bearer(frozenset({"runs:read", "runs:control"})), **KEY},
        )
        assert response.status_code == 403 and response.json()["capability"] == "items:create"

    def test_intake_waits_for_a_ready_daemon(self, tmp_path: Path) -> None:
        api = build(tmp_path, ready=False)
        with api.client:
            response = api.client.post(
                "/v1/items", json={"kind": "workload", "ask": "x"}, headers={**api.bearer(), **KEY}
            )
            assert response.status_code == 503 and response.json()["code"] == "daemon_not_ready"
        api.ctx.close()


class TestItemCommands:
    def _admit(self, api: Api) -> dict[str, Any]:
        response = api.client.post(
            "/v1/items", json={"kind": "workload", "ask": "x"}, headers={**api.bearer(), **KEY}
        )
        assert response.status_code == 201
        return dict(response.json()["item"])

    def test_abandon_retry_and_requeue_through_the_shared_service(self, api: Api) -> None:
        item = self._admit(api)
        headers = api.bearer()
        abandoned = api.client.post(
            f"/v1/items/{item['id']}/abandon", json={"reason": "scope changed"}, headers=headers
        )
        assert abandoned.status_code == 200, abandoned.text
        assert abandoned.json()["item"]["state"] == "failed"
        assert abandoned.json()["item"]["last_error"] == "scope changed"
        assert abandoned.json()["operation"]["action"] == "item.abandon"
        assert abandoned.json()["operation"]["actor"]["via"] == "api"
        retried = api.client.post(f"/v1/items/{item['id']}/retry", headers=headers)
        assert retried.status_code == 200 and retried.json()["item"]["state"] == "queued"
        assert retried.json()["item"]["attempts"] == 0
        requeued = api.client.post(f"/v1/items/{item['id']}/requeue", headers=headers)
        assert requeued.status_code == 200 and requeued.json()["item"]["run_id"] is None
        # A settled item cannot be requeued: the store's own refusal, as a 409.
        api.client.post(f"/v1/items/{item['id']}/abandon", headers=headers)
        again = api.client.post(f"/v1/items/{item['id']}/requeue", headers=headers)
        assert again.status_code == 409 and again.json()["code"] == "not_eligible"
        assert "only running or queued" in again.json()["detail"]

    def test_expected_revision_guards_a_command(self, api: Api) -> None:
        item = self._admit(api)
        headers = api.bearer()
        stale = api.client.post(
            f"/v1/items/{item['id']}/abandon",
            json={"expected_revision": item["revision"] + 5},
            headers=headers,
        )
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        assert stale.json()["revision"] == item["revision"]
        fresh = api.client.post(
            f"/v1/items/{item['id']}/abandon",
            json={"expected_revision": item["revision"]},
            headers=headers,
        )
        assert fresh.status_code == 200

    def test_commands_need_runs_control_and_a_known_item(self, api: Api) -> None:
        item = self._admit(api)
        reader = api.bearer(frozenset({"runs:read", "items:create"}))
        assert api.client.post(f"/v1/items/{item['id']}/retry", headers=reader).status_code == 403
        missing = api.client.post("/v1/items/itm_nope/retry", headers=api.bearer())
        assert missing.status_code == 404 and missing.json()["code"] == "not_found"

    def test_a_keyed_command_replays(self, api: Api) -> None:
        item = self._admit(api)
        headers = {**api.bearer(), "Idempotency-Key": "ab-1"}
        first = api.client.post(f"/v1/items/{item['id']}/abandon", headers=headers)
        replay = api.client.post(f"/v1/items/{item['id']}/abandon", headers=headers)
        assert first.status_code == replay.status_code == 200
        assert first.json()["operation"]["id"] == replay.json()["operation"]["id"]
