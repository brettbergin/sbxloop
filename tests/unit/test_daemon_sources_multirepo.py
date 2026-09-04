"""Discovery across several configured repositories, and per-repo routing."""

from __future__ import annotations

from typing import Any

import pytest

from sbxloop.config import RepoConfig
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.sources import (
    STATUS_MARKER,
    GitHubIssueSource,
    MultiRepoIssueSource,
    RepoHealth,
    build_github_source,
    permanent_failure,
)
from sbxloop.errors import GithubOpsError
from tests.fakes.github_errors import github_error

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
        # The exception a failing search raises; None is a plain HTTP 500.
        self.fail_with: GithubOpsError | None = None

    def _for_path(self, path: str) -> RecordingOps:
        parts = path.lstrip("/").split("/")
        return self.per_repo[f"{parts[1]}/{parts[2]}"]

    def search_issues(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
        repo = query.split("repo:", 1)[1].split(" ", 1)[0]
        self.searched.append(repo)
        if repo in self.fail_search:
            raise self.fail_with or GithubOpsError(f"search {repo} -> HTTP 500", http_status=500)
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

    def test_every_lifecycle_label_can_be_renamed_per_repo(self, router: RouterOps) -> None:
        """#630: the entry's six ``<kind>_label`` fields merge straight over
        the daemon-wide set; an unset one keeps the default."""
        entries = [
            RepoConfig(
                repo="o/a",
                in_progress_label="loop:working",
                failed_label="loop:failed",
                completed_label="loop:done",
                blocked_label="loop:blocked",
                gated_label="loop:gated",
            ),
            RepoConfig(repo="o/b"),
        ]
        source = build_github_source(lambda: router, entries, LABELS)  # type: ignore[arg-type]
        assert isinstance(source, MultiRepoIssueSource)
        a, b = source.sources
        assert (a.labels.trigger, a.labels.in_progress, a.labels.failed) == (
            "sbxloop:run",
            "loop:working",
            "loop:failed",
        )
        assert (a.labels.completed, a.labels.blocked, a.labels.gated) == (
            "loop:done",
            "loop:blocked",
            "loop:gated",
        )
        assert (b.labels.in_progress, b.labels.gated) == (
            "sbxloop:in-progress",
            "sbxloop:awaiting-merge",
        )

    def test_per_repo_extra_labels_are_applied_when_an_issue_is_claimed(
        self, router: RouterOps
    ) -> None:
        entries = [RepoConfig(repo="o/a", labels=["team:core"]), RepoConfig(repo="o/b")]
        source = build_github_source(lambda: router, entries, LABELS, host="db")  # type: ignore[arg-type]
        items = source.poll()
        assert source.claim(next(i for i in items if i.repo == "o/a")) is True
        assert source.claim(next(i for i in items if i.repo == "o/b")) is True
        posted = {
            repo: [b for m, p, b in ops.raw_calls if m == "POST" and p.endswith("/labels")]
            for repo, ops in router.per_repo.items()
        }
        # One POST per claim: the in-progress mark plus the repo's own labels.
        assert posted == {
            "o/a": [{"labels": ["sbxloop:in-progress", "team:core"]}],
            "o/b": [{"labels": ["sbxloop:in-progress"]}],
        }

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
        assert router.per_repo["o/a"].comments == [(4, f"Run `r1` started.\n\n{STATUS_MARKER}")]
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


class Clock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def health_source(router: RouterOps, *names: str, **kw: Any) -> tuple[Any, Clock, list, dict]:
    """A multi-repo source with a fake clock and recording persist/notify."""
    clock = Clock()
    notices: list[tuple[str, str, str]] = []
    persisted: dict[str, Any] = {}
    source = build_github_source(
        lambda: router,  # type: ignore[arg-type,return-value]
        repos(*names),
        LABELS,
        host="db",
        poll_interval_s=60.0,
        suspend_after=kw.pop("suspend_after", 3),
        persist=lambda repo, data: persisted.__setitem__(repo, data),
        notify=lambda kind, repo, text: notices.append((kind, repo, text)),
        **kw,
    )
    source._clock = clock  # the fake clock, after construction
    return source, clock, notices, persisted


class TestPerRepoHealth:
    """#516: a failing repository backs off on its own, is suspended after
    enough failures (or at once when GitHub says it is gone), and never
    slows a healthy neighbour."""

    def test_transient_failure_backs_off_that_repo_only_then_recovers(
        self, router: RouterOps
    ) -> None:
        source, clock, notices, persisted = health_source(router, "o/a", "o/b")
        router.fail_search.add("o/a")
        assert [i.repo for i in source.poll()] == ["o/b", "o/b"]
        (a, b) = source.repo_health
        assert a.state == "backoff" and a.failures == 1 and a.next_poll == clock.t + 60
        assert b.state == "ok"
        assert persisted["o/a"]["next_poll"] == clock.t + 60 and "o/b" not in persisted
        # Within the backoff o/a is not polled at all; o/b still is.
        calls_before = len(router.per_repo["o/a"].searches)
        assert [i.repo for i in source.poll()] == ["o/b", "o/b"]
        assert len(router.per_repo["o/a"].searches) == calls_before
        # Past it, a second failure doubles the wait.
        clock.t += 61
        source.poll()
        assert source.repo_health[0].failures == 2
        assert source.repo_health[0].next_poll == clock.t + 120
        # Recovery is silent apart from one info line and one notice.
        router.fail_search.discard("o/a")
        clock.t += 121
        assert [i.repo for i in source.poll()] == ["o/a", "o/b", "o/b"]
        assert source.repo_health[0] == RepoHealth("o/a")
        assert persisted["o/a"] is None
        assert [k for k, _, _ in notices] == ["source.repo_recovered"]

    def test_enough_consecutive_failures_suspend_the_repo(self, router: RouterOps) -> None:
        source, clock, notices, persisted = health_source(router, "o/a", "o/b", suspend_after=3)
        router.fail_search.add("o/a")
        for _ in range(3):
            source.poll()
            clock.t += 3600
        a = source.repo_health[0]
        assert a.suspended and a.failures == 3 and "3 consecutive poll failures" in a.reason
        assert persisted["o/a"]["suspended"] is True
        assert [k for k, _, _ in notices] == ["source.repo_suspended"]
        assert "resume-repo o/a" in notices[0][2]
        # Suspended: not polled, not a failure, forever — even once it would work.
        router.fail_search.discard("o/a")
        calls = len(router.per_repo["o/a"].searches)
        clock.t += 7200
        assert [i.repo for i in source.poll()] == ["o/b", "o/b"]
        assert len(router.per_repo["o/a"].searches) == calls
        assert len(notices) == 1, "narrated once, not per tick"

    def test_a_permanent_refusal_suspends_at_once(self, router: RouterOps) -> None:
        source, _, notices, _ = health_source(router, "o/a", "o/b")
        router.fail_search.add("o/a")
        router.fail_with = github_error("repo_missing_404")
        source.poll()
        a = source.repo_health[0]
        assert a.suspended and a.failures == 1 and "gone for this token" in a.reason
        assert [k for k, _, _ in notices] == ["source.repo_suspended"]

    def test_classification(self) -> None:
        assert permanent_failure(GithubOpsError("x", http_status=404))
        assert permanent_failure(GithubOpsError("x", http_status=410))
        assert permanent_failure(GithubOpsError("Resource not accessible", http_status=403))
        assert not permanent_failure(GithubOpsError("API rate limit exceeded", http_status=403))
        assert not permanent_failure(GithubOpsError("secondary rate limit", http_status=403))
        assert not permanent_failure(GithubOpsError("bad gateway", http_status=502))
        assert not permanent_failure(GithubOpsError("no status"))

    def test_resume_repo_polls_it_again_now(self, router: RouterOps) -> None:
        source, _clock, _notices, persisted = health_source(router, "o/a", "o/b", suspend_after=1)
        router.fail_search.add("o/a")
        source.poll()
        assert source.repo_health[0].suspended
        with pytest.raises(KeyError, match="unknown repository"):
            source.resume_repo("o/zzz")
        with pytest.raises(ValueError, match="not suspended"):
            source.resume_repo("o/b")
        router.fail_search.discard("o/a")
        health = source.resume_repo("O/A")
        assert health == RepoHealth("o/a") and persisted["o/a"] is None
        assert [i.repo for i in source.poll()] == ["o/a", "o/b", "o/b"]

    def test_all_polled_repos_failing_still_raises(self, router: RouterOps) -> None:
        source, clock, _, _ = health_source(router, "o/a", "o/b")
        router.fail_search.update({"o/a", "o/b"})
        with pytest.raises(GithubOpsError):
            source.poll()
        # Both now back off: nothing is polled, nothing fails, nothing raises.
        assert source.poll() == []
        # A suspended repo does not count as a failure of the ones polled.
        clock.t += 61
        router.fail_search.discard("o/b")
        assert [i.repo for i in source.poll()] == ["o/b", "o/b"]
