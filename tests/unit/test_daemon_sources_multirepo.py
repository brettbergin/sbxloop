"""Discovery across several configured repositories, and per-repo routing."""

from __future__ import annotations

from typing import Any

import pytest

from sbxloop.config import RepoConfig
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.sources import (
    GitHubIssueSource,
    MultiRepoIssueSource,
    build_github_source,
)
from sbxloop.errors import GithubOpsError

from .test_daemon_sources import LABELS, RecordingOps, issue


class RouterOps:
    """One GithubOps stand-in fronting a RecordingOps per repository.

    The daemon has a single github-ops sandbox; every call carries the repo
    in its query or path, which is what this router keys on.
    """

    def __init__(self, per_repo: dict[str, RecordingOps]) -> None:
        self.per_repo = per_repo
        self.searched: list[str] = []
        self.fail_search: set[str] = set()

    def _for_path(self, path: str) -> RecordingOps:
        parts = path.lstrip("/").split("/")
        return self.per_repo[f"{parts[1]}/{parts[2]}"]

    def search_issues(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
        repo = query.split("repo:", 1)[1].split(" ", 1)[0]
        self.searched.append(repo)
        if repo in self.fail_search:
            raise GithubOpsError(f"search {repo} -> HTTP 500")
        return self.per_repo[repo].search_issues(query, per_page=per_page)

    def raw(self, method: str, path: str, body: Any = None) -> Any:
        return self._for_path(path).raw(method, path, body)

    def issue_comment(self, repo: str, number: int, body: str) -> str:
        return self.per_repo[repo].issue_comment(repo, number, body)


def repos(*names: str, disabled: tuple[str, ...] = ()) -> list[RepoConfig]:
    return [RepoConfig(repo=name, enabled=name not in disabled) for name in names]


@pytest.fixture
def router() -> RouterOps:
    return RouterOps(
        {
            "o/a": RecordingOps({"4": issue(4, "sbxloop:run"), "5": issue(5, "other")}),
            "o/b": RecordingOps({"4": issue(4, "sbxloop:run"), "9": issue(9, "sbxloop:run")}),
        }
    )


def build(router: RouterOps, *names: str, disabled: tuple[str, ...] = ()) -> Any:
    return build_github_source(
        lambda: router,  # type: ignore[arg-type,return-value]
        repos(*names, disabled=disabled),
        LABELS,
        host="db",
    )


class TestBuild:
    def test_single_repo_builds_the_plain_source_with_legacy_ids(self, router: RouterOps) -> None:
        source = build(router, "o/a")
        assert isinstance(source, GitHubIssueSource)
        assert source.repo == "o/a" and source.qualify_ids is False
        items = source.poll()
        # Byte-for-byte the pre-multi-repo behaviour.
        assert [i.item_id for i in items] == ["gh:issue:4"]
        assert router.searched == ["o/a"]

    def test_only_enabled_repositories_are_polled(self, router: RouterOps) -> None:
        source = build(router, "o/a", "o/b", disabled=("o/b",))
        assert isinstance(source, GitHubIssueSource)
        source.poll()
        assert router.searched == ["o/a"]

    def test_no_enabled_repository_is_an_error(self, router: RouterOps) -> None:
        with pytest.raises(ValueError, match="no enabled repository"):
            build(router, "o/a", disabled=("o/a",))

    def test_per_repo_trigger_label_overrides_the_daemon_default(self, router: RouterOps) -> None:
        entries = [
            RepoConfig(repo="o/a", trigger_label="do-it"),
            RepoConfig(repo="o/b"),
        ]
        source = build_github_source(lambda: router, entries, LABELS)  # type: ignore[arg-type]
        assert isinstance(source, MultiRepoIssueSource)
        assert [s.labels.trigger for s in source.sources] == ["do-it", "sbxloop:run"]

    def test_empty_source_list_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            MultiRepoIssueSource([])


class TestMultiRepoDiscovery:
    def test_poll_spans_every_repo_in_configuration_order(self, router: RouterOps) -> None:
        source = build(router, "o/a", "o/b")
        assert isinstance(source, MultiRepoIssueSource)
        items = source.poll()
        assert router.searched == ["o/a", "o/b"]
        # Deterministic across repos; ids are repo-qualified so the two
        # issue #4s cannot collide.
        assert [i.item_id for i in items] == [
            "gh:o/a:issue:4",
            "gh:o/b:issue:4",
            "gh:o/b:issue:9",
        ]
        assert [i.repo for i in items] == ["o/a", "o/b", "o/b"]

    def test_one_failing_repo_does_not_drop_the_others(self, router: RouterOps) -> None:
        router.fail_search.add("o/a")
        source = build(router, "o/a", "o/b")
        items = source.poll()
        assert [i.repo for i in items] == ["o/b", "o/b"]

    def test_every_repo_failing_still_raises_so_the_loop_backs_off(self, router: RouterOps) -> None:
        router.fail_search.update({"o/a", "o/b"})
        source = build(router, "o/a", "o/b")
        with pytest.raises(GithubOpsError):
            source.poll()


class TestRouting:
    def test_claim_targets_the_items_own_repo(self, router: RouterOps) -> None:
        source = build(router, "o/a", "o/b")
        item = next(i for i in source.poll() if i.repo == "o/b")
        assert source.claim(item) is True
        assert all(p.startswith("/repos/o/b/") for _, p, _ in router.per_repo["o/b"].raw_calls)
        assert router.per_repo["o/a"].raw_calls == []
        assert router.per_repo["o/a"].comments == []
        assert router.per_repo["o/b"].comments

    def test_reports_comment_on_the_items_own_repo(self, router: RouterOps) -> None:
        source = build(router, "o/a", "o/b")
        item = next(i for i in source.poll() if i.repo == "o/a")
        source.report_started(item, "r1")
        assert router.per_repo["o/a"].comments == [(4, "Run `r1` started.")]
        assert router.per_repo["o/b"].comments == []

    def test_merged_report_closes_the_issue_in_its_own_repo(self, router: RouterOps) -> None:
        source = build(router, "o/a", "o/b")
        item = next(i for i in source.poll() if i.repo == "o/b")
        assert source.report_merged(item, 9, "https://x/pull/9") is True
        assert (
            "PATCH",
            "/repos/o/b/issues/4",
            {"state": "closed", "state_reason": "completed"},
        ) in router.per_repo["o/b"].raw_calls

    def test_legacy_unqualified_item_falls_back_to_the_first_repo(self, router: RouterOps) -> None:
        source = build(router, "o/a", "o/b")
        assert isinstance(source, MultiRepoIssueSource)
        legacy = WorkItem(item_id="gh:4", source_key="4", title="x")
        assert source.for_item(legacy).repo == "o/a"

    def test_unknown_repo_falls_back_rather_than_crashing(self, router: RouterOps) -> None:
        source = build(router, "o/a", "o/b")
        assert isinstance(source, MultiRepoIssueSource)
        stray = WorkItem(item_id="gh:o/z:issue:4", source_key="4", title="x", repo="o/z")
        assert source.for_item(stray).repo == "o/a"

    def test_repo_qualified_id_routes_even_without_the_repo_field(self, router: RouterOps) -> None:
        source = build(router, "o/a", "o/b")
        assert isinstance(source, MultiRepoIssueSource)
        item = WorkItem(item_id="gh:o/b:issue:4", source_key="4", title="x")
        assert source.for_item(item).repo == "o/b"
