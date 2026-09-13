"""Work items, the queue and the catalog as a client reads them: opaque
ids that never alias, bounded pages, and actions advertised only where
they apply."""

from __future__ import annotations

from pathlib import Path

from sbxloop.api.publicids import PublicIds, item_key
from sbxloop.daemon.model import WorkItem
from tests.api.conftest import Api, build
from tests.unit.test_daemon_loop import gh_item


def _queue(api: Api, *items: WorkItem) -> None:
    for item in items:
        api.clock.t += 1
        assert api.loop.dstore.upsert_new(item, api.clock())


class TestPublicIds:
    def test_items_get_stable_opaque_ids_on_first_read(self, api: Api) -> None:
        _queue(api, gh_item("1"))
        headers = api.bearer()
        first = api.client.get("/v1/items", headers=headers).json()["data"]
        second = api.client.get("/v1/items", headers=headers).json()["data"]
        assert first == second and first[0]["id"].startswith("itm_")
        # The internal ids never appear as identifiers; the origin names them.
        assert first[0]["origin"] == {
            "kind": "issue",
            "repository_id": None,
            "repository": None,
            "number": 1,
            "url": "https://x/issues/1",
            "ref": "gh:issue:1",
        }

    def test_two_repositories_issue_numbers_never_alias(self, api: Api) -> None:
        _queue(
            api,
            gh_item("7", item_id="gh:o/one:issue:7", repo="o/one"),
            gh_item("7", item_id="gh:o/two:issue:7", repo="o/two"),
        )
        body = api.client.get("/v1/items", headers=api.bearer()).json()["data"]
        ids = {row["id"] for row in body}
        assert len(ids) == 2
        assert {row["origin"]["repository"] for row in body} == {"o/one", "o/two"}
        repo_ids = {row["origin"]["repository_id"] for row in body}
        assert len(repo_ids) == 2 and all(r.startswith("repo_") for r in repo_ids)
        # The mapping keys on repository and item together.
        one, two = (api.loop.dstore.get(i) for i in ("gh:o/one:issue:7", "gh:o/two:issue:7"))
        assert item_key(one) != item_key(two)
        ids_store = PublicIds(api.loop.dstore)
        assert ids_store.item_id(one, 0.0) != ids_store.item_id(two, 0.0)

    def test_a_legacy_spelling_maps_to_the_same_id(self, api: Api) -> None:
        _queue(api, gh_item("3"))  # stored as the legacy `gh:3`
        stored = api.loop.dstore.get("gh:issue:3")
        assert stored is not None
        ids = PublicIds(api.loop.dstore)
        assert ids.item_id(stored, 0.0) == ids.item_id(
            stored.model_copy(update={"item_id": "gh:3"}), 0.0
        )

    def test_unknown_ids_are_concealed_alike(self, api: Api) -> None:
        headers = api.bearer()
        for path in ("/v1/items/itm_nope", "/v1/items/run_nope", "/v1/runs/run_nope", "/v1/runs/x"):
            response = api.client.get(path, headers=headers)
            assert response.status_code == 404, path
            assert response.json() == {
                **response.json(),
                "code": "not_found",
                "detail": "no such resource",
            }


class TestListing:
    def test_pages_newest_first_without_gaps(self, api: Api) -> None:
        _queue(api, *(gh_item(str(n)) for n in range(1, 6)))
        headers = api.bearer()
        seen: list[int] = []
        cursor = None
        for _ in range(3):
            params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            page = api.client.get("/v1/items", params=params, headers=headers).json()
            seen += [row["origin"]["number"] for row in page["data"]]
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        assert seen == [5, 4, 3, 2, 1] and cursor is None

    def test_filters_by_state_kind_and_repository(self, api: Api) -> None:
        _queue(
            api,
            gh_item("1", item_id="gh:o/r:issue:1", repo="o/r"),
            gh_item("2", item_id="gh:o/r:issue:2", repo="o/r", kind="workload"),
        )
        api.loop.dstore.mark_running("gh:o/r:issue:1", "r1", api.clock())
        headers = api.bearer()
        running = api.client.get("/v1/items", params={"state": "running"}, headers=headers).json()
        assert [r["origin"]["number"] for r in running["data"]] == [1]
        workloads = api.client.get("/v1/items", params={"kind": "workload"}, headers=headers).json()
        assert [r["origin"]["number"] for r in workloads["data"]] == [2]
        repo_id = running["data"][0]["origin"]["repository_id"]
        by_repo = api.client.get(
            "/v1/items", params={"repository_id": repo_id}, headers=headers
        ).json()
        assert len(by_repo["data"]) == 2
        assert (
            api.client.get("/v1/items", params={"state": "weird"}, headers=headers).status_code
            == 422
        )
        assert (
            api.client.get(
                "/v1/items", params={"repository_id": "repo_nope"}, headers=headers
            ).status_code
            == 404
        )

    def test_reads_need_runs_read(self, api: Api) -> None:
        headers = api.bearer(frozenset({"audit:read"}))
        for path in ("/v1/items", "/v1/queue", "/v1/runs", "/v1/repositories"):
            assert api.client.get(path, headers=headers).status_code == 403, path


class TestDetail:
    def test_the_detail_carries_the_body_the_runs_and_the_actions(self, api: Api) -> None:
        api.harness.source.items = [gh_item("1", body="please do it")]
        api.harness.outcomes = ["blocked"]
        assert api.loop.tick().outcome == "blocked"
        headers = api.bearer()
        listed = api.client.get("/v1/items", headers=headers).json()["data"][0]
        detail = api.client.get(f"/v1/items/{listed['id']}", headers=headers).json()
        assert detail["body"] == "please do it" and detail["state"] == "blocked"
        assert detail["runs"] == [detail["run_id"]] and detail["run_id"].startswith("run_")
        assert detail["admitted_by"] is None  # a poll, not a recorded admission
        # A blocked item can be retried or abandoned, not requeued.
        assert detail["available_actions"] == ["retry", "abandon"]
        assert detail["revision"] >= 1 and detail["updated_at"].endswith("Z")


class TestQueue:
    def test_the_queue_is_in_dispatch_order_with_eligibility(self, api: Api) -> None:
        _queue(api, gh_item("1"), gh_item("2"))
        # A failed attempt waits its backoff; an interrupted run goes first.
        api.loop.dstore.mark_running("gh:issue:1", "r1", api.clock())
        api.loop.dstore.mark_failed("gh:issue:1", "boom", api.clock(), requeue=True)
        _queue(api, gh_item("3"))
        api.loop.dstore.mark_running("gh:issue:3", "r3", api.clock())
        api.loop.dstore.mark_resume_pending("gh:issue:3", api.clock())
        api.loop.pause("deploy", by="ops", via="ctl")
        body = api.client.get("/v1/queue", headers=api.bearer()).json()
        numbers = [e["item"]["origin"]["number"] for e in body["data"]]
        # Dispatch order is the resume first, then FIFO by discovery — an
        # ineligible item keeps its place and is skipped, not moved.
        assert numbers == [3, 1, 2]
        assert [e["position"] for e in body["data"]] == [1, 2, 3]
        first, second, third = body["data"]
        assert (
            first["reason"] == "interrupted run awaiting resume; goes first" and first["eligible"]
        )
        assert not second["eligible"] and "backoff" in second["reason"]
        assert second["eligible_at"] > body["observed_at"]
        assert third["eligible"] and third["reason"] is None
        assert body["paused"] is True and body["has_more"] is False


class TestCatalog:
    def test_repositories_profiles_and_recipes(self, tmp_path: Path) -> None:
        api = build(
            tmp_path,
            config={
                "github": {
                    "repos": [
                        {"repo": "o/one", "deliver_base": "main"},
                        {"repo": "o/two", "enabled": False, "trigger_label": "go"},
                    ]
                },
                "workloads": [{"name": "research", "sinks": ["chat", "artifact"]}],
                "workload": {"default": "research"},
            },
        )
        with api.client:
            headers = api.bearer()
            repos = api.client.get("/v1/repositories", headers=headers).json()["data"]
            assert [r["repository"] for r in repos] == ["o/one", "o/two"]
            assert repos[0]["id"].startswith("repo_") and repos[0]["forge"] == "github"
            assert repos[0]["deliver_base"] == "main" and repos[0]["enabled"]
            assert repos[1]["trigger_label"] == "go" and not repos[1]["enabled"]
            assert repos[0]["trigger_label"] == api.ctx.config.daemon.trigger_label
            profiles = api.client.get("/v1/profiles", headers=headers).json()["data"]
            assert profiles == [
                {
                    "id": "research",
                    "name": "research",
                    "description": "",
                    "sinks": ["chat", "artifact"],
                    "publish": "auto",
                    "repo": False,
                    "default": True,
                }
            ]
            recipes = api.client.get("/v1/recipes", headers=headers).json()["data"]
            assert recipes == [
                {
                    "id": "entrygraph",
                    "name": "entrygraph",
                    "parameters": ["repository", "url"],
                    "enabled": True,
                }
            ]
        api.ctx.close()
