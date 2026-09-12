"""The named operations that replaced the ``raw()`` call sites outside the
GitHub package (#1010), each exercised against :class:`FakeGithub`.

Two things are pinned per operation: the request it makes — method, path
and body, byte for byte what the call site used to spell by hand, so the
worker and the run's chronology see no difference — and the shape it hands
back, including the :class:`MalformedResponse` it raises when GitHub's
answer is not that shape."""

from __future__ import annotations

from typing import Any

import pytest

from sbxloop.errors import GithubOpsError
from sbxloop.vcs.github.ops import MalformedResponse, PaginationError
from sbxloop.vcs.github.permissions import READ_PROBES
from tests.fakes.fake_github import FakeGithub
from tests.fakes.github_errors import github_error

REPO = "o/r"


def calls(fake: FakeGithub) -> list[tuple[str, str, Any]]:
    return list(fake.raw_calls)


class Malformed(FakeGithub):
    """Every raw read answers with a shape no operation is defined for."""

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.raw_calls.append((method, path, body))
        return "not json we can use"


class TestIdentity:
    def test_rate_limit_is_the_health_read(self) -> None:
        fake = FakeGithub()
        assert fake.rate_limit()["resources"]["core"]["limit"] == 5000
        assert calls(fake) == [("GET", "/rate_limit", None)]

    def test_authenticated_user_is_the_credential(self) -> None:
        fake = FakeGithub()
        assert fake.authenticated_user() == {"login": "sbxloop-bot", "type": "User"}
        assert calls(fake) == [("GET", "/user", None)]

    def test_an_installation_token_gets_its_403(self) -> None:
        fake = FakeGithub()
        fake.fail_user_lookup = GithubOpsError(
            "Resource not accessible by integration", http_status=403
        )
        with pytest.raises(GithubOpsError) as info:
            fake.authenticated_user()
        assert info.value.http_status == 403

    def test_a_malformed_user_is_named(self) -> None:
        with pytest.raises(MalformedResponse, match="GET /user"):
            Malformed().authenticated_user()


class TestRepositories:
    def test_repo_create_under_the_user(self) -> None:
        fake = FakeGithub()
        made = fake.repo_create("o/new", private=True, for_user=True)
        assert made["html_url"] == "https://github.com/o/new"
        assert calls(fake) == [
            ("POST", "/user/repos", {"name": "new", "private": True, "auto_init": True})
        ]

    def test_repo_create_under_the_organization(self) -> None:
        fake = FakeGithub()
        fake.repo_create("acme/new", private=False)
        assert calls(fake) == [
            ("POST", "/orgs/acme/repos", {"name": "new", "private": False, "auto_init": True})
        ]

    def test_compare_lookup_answers_the_merge_base(self) -> None:
        fake = FakeGithub()
        data = fake.compare_lookup(REPO, "main", "sbxloop/r1")
        assert data == {"merge_base_commit": {"sha": "base123"}}
        assert calls(fake) == [("GET", "/repos/o/r/compare/main...sbxloop/r1", None)]

    def test_compare_lookup_answers_a_404_as_none(self) -> None:
        fake = FakeGithub()
        fake.unrelated_branches.add("orphan")
        assert fake.compare_lookup(REPO, "main", "orphan") is None
        assert fake.failed_jobs == [], "a miss is data, not a failed job"

    def test_compare_lookup_refuses_a_malformed_answer(self) -> None:
        with pytest.raises(MalformedResponse):
            Malformed().compare_lookup(REPO, "main", "x")


class TestIssues:
    def test_issue_get(self) -> None:
        fake = FakeGithub()
        fake.existing_issues = [{"number": 4, "title": "t", "state": "open"}]
        assert fake.issue_get(REPO, 4)["title"] == "t"
        assert fake.issue_get(REPO, "4")["title"] == "t", "a source's string number works too"
        assert calls(fake) == [("GET", "/repos/o/r/issues/4", None)] * 2

    def test_issue_get_404_is_the_status_not_a_shape(self) -> None:
        with pytest.raises(GithubOpsError) as info:
            FakeGithub().issue_get(REPO, 99)
        assert info.value.http_status == 404
        assert not isinstance(info.value, MalformedResponse)

    def test_issue_comments_and_events_walk_pages(self) -> None:
        fake = FakeGithub()
        fake.issue_comments_posted = ["one", "two"]
        fake.issue_events_payload = [{"event": "labeled", "label": {"name": "sbxloop:run"}}]
        assert [c["body"] for c in fake.issue_comments(REPO, 4)] == ["one", "two"]
        assert fake.issue_events(REPO, 4)[0]["event"] == "labeled"
        assert calls(fake) == [
            ("GET", "/repos/o/r/issues/4/comments?per_page=100&page=1", None),
            ("GET", "/repos/o/r/issues/4/events?per_page=100&page=1", None),
        ]

    def test_issues_list_spells_the_query(self) -> None:
        fake = FakeGithub()
        fake.existing_issues = [{"number": 1}, {"number": 2}]
        assert fake.issues_list(REPO) == [{"number": 1}, {"number": 2}]
        assert (
            fake.issues_list(
                REPO,
                state="all",
                labels=["sbxloop:follow-up"],
                per_page=5,
                page=2,
                sort="updated",
                direction="desc",
            )
            == []
        )
        assert calls(fake) == [
            ("GET", "/repos/o/r/issues?state=open&per_page=100&page=1", None),
            (
                "GET",
                "/repos/o/r/issues?state=all&per_page=5&sort=updated&direction=desc"
                "&labels=sbxloop%3Afollow-up&page=2",
                None,
            ),
        ]

    def test_issues_list_refuses_a_malformed_answer(self) -> None:
        fake = FakeGithub()
        fake.issue_list_payload = {"message": "unexpected"}
        with pytest.raises(MalformedResponse) as info:
            fake.issues_list(REPO)
        assert info.value.data == {"message": "unexpected"}

    def test_issue_search_is_the_envelope(self) -> None:
        fake = FakeGithub()
        fake.existing_issues = [
            {
                "number": 3,
                "title": "checks never run",
                "html_url": "https://github.com/o/r/issues/3",
            }
        ]
        data = fake.issue_search("repo:o/r is:issue in:title,body checks", per_page=10)
        assert data["total_count"] == 1 and data["incomplete_results"] is False
        assert calls(fake) == [
            (
                "GET",
                "/search/issues?q=repo%3Ao%2Fr+is%3Aissue+in%3Atitle%2Cbody+checks&per_page=10",
                None,
            )
        ]

    def test_issue_search_refuses_a_malformed_answer(self) -> None:
        fake = FakeGithub()
        fake.issue_search_payload = []
        with pytest.raises(MalformedResponse):
            fake.issue_search("anything", per_page=10)

    def test_label_writes(self) -> None:
        fake = FakeGithub()
        fake.existing_issues = [{"number": 4, "labels": [{"name": "sbxloop:run"}]}]
        fake.issue_labels_add(REPO, 4, ["sbxloop:in-progress", "team"])
        fake.issue_label_remove(REPO, "4", "sbxloop:run")
        assert fake.labels_removed == [(4, "sbxloop:run")]
        assert fake.existing_issues[0]["labels"] == []
        assert calls(fake) == [
            ("POST", "/repos/o/r/issues/4/labels", {"labels": ["sbxloop:in-progress", "team"]}),
            ("DELETE", "/repos/o/r/issues/4/labels/sbxloop%3Arun", None),
        ]

    def test_removing_an_absent_label_is_not_a_failed_job(self) -> None:
        class Gone(FakeGithub):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if method == "DELETE":
                    self.raw_calls.append((method, path, body))
                    raise self._failed_op(
                        "raw.api", method, path, github_error("label_missing_404")
                    )
                return super().raw(method, path, body)

        fake = Gone()
        fake.issue_label_remove(REPO, 4, "sbxloop:run")  # no raise
        assert fake.failed_jobs == []

    def test_issue_close_and_comment_delete(self) -> None:
        fake = FakeGithub()
        fake.existing_issues = [{"number": 4, "state": "open"}]
        fake.issue_close(REPO, 4)
        fake.issue_close(REPO, "5", reason="not_planned")
        fake.issue_comment_delete(REPO, 100)
        assert fake.issues_closed == [(4, "completed"), (5, "not_planned")]
        assert fake.existing_issues[0]["state"] == "closed"
        assert fake.comments_deleted == [100]
        assert calls(fake) == [
            ("PATCH", "/repos/o/r/issues/4", {"state": "closed", "state_reason": "completed"}),
            ("PATCH", "/repos/o/r/issues/5", {"state": "closed", "state_reason": "not_planned"}),
            ("DELETE", "/repos/o/r/issues/comments/100", None),
        ]


class TestPullRequests:
    def test_pr_list_open_by_head(self) -> None:
        fake = FakeGithub()
        assert fake.pr_list_open(REPO, head="sbxloop/r1") == []
        fake.pr_created = True
        (pull,) = fake.pr_list_open(REPO, head="sbxloop/r1")
        assert pull["number"] == 7
        assert calls(fake) == [("GET", "/repos/o/r/pulls?state=open&head=o:sbxloop/r1", None)] * 2

    def test_pr_list_open_reads_a_non_list_as_none_open(self) -> None:
        assert Malformed().pr_list_open(REPO, head="x") == []

    def test_pr_update_sends_only_what_changes(self) -> None:
        fake = FakeGithub()
        assert fake.pr_update(REPO, 7, title="new")["title"] == "new"
        fake.pr_update(REPO, 7, body="text")
        assert calls(fake) == [
            ("PATCH", "/repos/o/r/pulls/7", {"title": "new"}),
            ("PATCH", "/repos/o/r/pulls/7", {"body": "text"}),
        ]

    def test_pr_files_reviews_and_review_comments_walk_pages(self) -> None:
        fake = FakeGithub()
        fake.reviews_payload = [{"state": "APPROVED"}]
        fake.comments_payload = [{"body": "nit"}]
        assert fake.pr_files(REPO, 7)[0]["filename"] == "hello.txt"
        assert fake.pr_reviews(REPO, 7) == [{"state": "APPROVED"}]
        assert fake.pr_review_comments(REPO, 7) == [{"body": "nit"}]
        assert calls(fake) == [
            ("GET", "/repos/o/r/pulls/7/files?per_page=100&page=1", None),
            ("GET", "/repos/o/r/pulls/7/reviews?per_page=100&page=1", None),
            ("GET", "/repos/o/r/pulls/7/comments?per_page=100&page=1", None),
        ]

    def test_check_runs_are_the_enveloped_list(self) -> None:
        fake = FakeGithub()
        fake.check_runs_payload = [{"name": "ci", "conclusion": "success"}]
        assert fake.check_runs(REPO, "abc") == [{"name": "ci", "conclusion": "success"}]
        assert calls(fake) == [
            ("GET", "/repos/o/r/commits/abc/check-runs?per_page=100&page=1", None)
        ]


class TestGitData:
    def test_the_commit_path(self) -> None:
        fake = FakeGithub()
        assert fake.commit_get(REPO, "base123")["tree"]["sha"] == "basetree"
        tree = fake.tree_create(REPO, base_tree="basetree", entries=[{"path": "a", "sha": "b1"}])
        commit = fake.commit_create(REPO, message="m", tree=tree["sha"], parents=["base123"])
        fake.ref_create(REPO, "refs/heads/sbxloop/r1", commit["sha"])
        fake.ref_force_update(REPO, "sbxloop/r1", "commit2")
        assert calls(fake) == [
            ("GET", "/repos/o/r/git/commits/base123", None),
            (
                "POST",
                "/repos/o/r/git/trees",
                {"base_tree": "basetree", "tree": [{"path": "a", "sha": "b1"}]},
            ),
            (
                "POST",
                "/repos/o/r/git/commits",
                {"message": "m", "tree": "tree456", "parents": ["base123"]},
            ),
            ("POST", "/repos/o/r/git/refs", {"ref": "refs/heads/sbxloop/r1", "sha": "commit1"}),
            ("PATCH", "/repos/o/r/git/refs/heads/sbxloop/r1", {"sha": "commit2", "force": True}),
        ]
        assert fake.head_sha == "commit2"

    def test_a_ref_that_exists_is_githubs_422(self) -> None:
        fake = FakeGithub()
        fake.branches.add("sbxloop/r1")
        with pytest.raises(GithubOpsError) as info:
            fake.ref_create(REPO, "refs/heads/sbxloop/r1", "c")
        assert info.value.http_status == 422

    def test_contents_put(self) -> None:
        fake = FakeGithub()
        fake.contents_put(REPO, "README.md", message="init", content_b64="aGk=", branch="main")
        assert fake.contents_written == [
            ("README.md", {"message": "init", "content": "aGk=", "branch": "main"})
        ]
        assert calls(fake) == [
            (
                "PUT",
                "/repos/o/r/contents/README.md",
                {"message": "init", "content": "aGk=", "branch": "main"},
            )
        ]

    def test_malformed_git_data_answers_are_named(self) -> None:
        fake = Malformed()
        with pytest.raises(MalformedResponse, match="GET /repos/o/r/git/commits/x"):
            fake.commit_get(REPO, "x")
        with pytest.raises(MalformedResponse, match="POST /repos/o/r/git/trees"):
            fake.tree_create(REPO, base_tree="t", entries=[])
        with pytest.raises(MalformedResponse, match="POST /repos/o/r/git/commits"):
            fake.commit_create(REPO, message="m", tree="t", parents=[])


class TestCredentialAndCi:
    def test_permission_probe_reads_each_permissions_endpoint(self) -> None:
        fake = FakeGithub()
        for permission in READ_PROBES:
            assert fake.permission_probe(permission, "acme/alpha", "main") is True
        assert [path for _, path, _ in calls(fake)] == [
            "/repos/acme/alpha/commits?per_page=1&sha=main",
            "/repos/acme/alpha/issues?per_page=1",
            "/repos/acme/alpha/pulls?per_page=1",
            "/repos/acme/alpha/commits/main/check-runs?per_page=1",
            "/repos/acme/alpha/actions/runs?per_page=1",
        ]

    def test_permission_probe_reads_a_403_as_missing_and_no_base_as_unanswerable(self) -> None:
        class Forbidden(FakeGithub):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                if "/pulls" in path:
                    raise GithubOpsError("Resource not accessible", http_status=403)
                if "/issues" in path:
                    raise GithubOpsError("Not Found", http_status=404)
                return super().raw(method, path, body)

        fake = Forbidden()
        assert fake.permission_probe("pull_requests", REPO, "main") is False
        assert fake.permission_probe("issues", REPO, "main") is True, "a 404 is not a refusal"
        assert fake.permission_probe("contents", REPO, "") is None
        assert fake.permission_probe("checks", REPO, "") is None

    def test_workflows_and_runs(self) -> None:
        fake = FakeGithub()
        fake.workflows_payload = [{"name": "CI", "state": "active"}]
        fake.workflow_runs_payload = [{"name": "CI", "conclusion": "success"}]
        assert fake.workflows_list(REPO) == [{"name": "CI", "state": "active"}]
        assert fake.workflow_runs(REPO, branch="main") == [{"name": "CI", "conclusion": "success"}]
        assert calls(fake) == [
            ("GET", "/repos/o/r/actions/workflows?per_page=100", None),
            ("GET", "/repos/o/r/actions/runs?branch=main&per_page=1", None),
        ]

    def test_malformed_listings_are_named(self) -> None:
        with pytest.raises(MalformedResponse, match="actions/workflows"):
            Malformed().workflows_list(REPO)
        with pytest.raises(MalformedResponse, match="actions/runs"):
            Malformed().workflow_runs(REPO, branch="main")


class TestPagedReadsStayBounded:
    def test_a_list_past_the_page_cap_is_refused(self) -> None:
        class Endless(FakeGithub):
            def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                self.raw_calls.append((method, path, body))
                return [{"id": i} for i in range(100)]

        with pytest.raises(PaginationError):
            Endless().issue_comments(REPO, 4)
