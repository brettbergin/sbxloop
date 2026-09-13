"""The Gitea backend's read paths and policy (#1021): the transport
descriptor every job carries, the folds from Gitea 1.24.7's payload shapes
into the loop's records, each named operation's request against the fake,
the base's rules as a write collaborator reads them, and the operator's
bot list standing in for the bot flag Gitea does not have."""

from __future__ import annotations

from typing import Any

import pytest

from sbxloop.errors import GithubOpsError
from sbxloop.vcs.gitea.ops import PAGE_SIZE, GiteaOps, gitea_transport
from sbxloop.vcs.gitea.records import (
    change_record,
    check_run_record,
    commit_record,
    fold_statuses,
    issue_record,
    merge_state,
    parse_node_id,
    parse_thread_id,
    repo_record,
    review_record,
    split_diff,
    thread_id_for,
    timeline_event_record,
    undrafted_title,
    user_record,
)
from sbxloop.vcs.model import ChecksVerdict, CredentialInfo, FailedCheck
from sbxloop.vcs.protocol import CAPABILITIES, Capability
from sbxloop_worker.protocol import TransportSpec
from tests.fakes.fake_gitea import FakeGitea
from tests.unit.test_gh_ops import StubWorkerClient

REPO = "acme/widgets"
R = "/repos/acme/widgets"


def human(login: str) -> bool:
    return False


class TestTransportDescriptor:
    def test_every_job_names_giteas_transport(self) -> None:
        client = StubWorkerClient({"raw.api": {"version": "1.24.7"}})
        ops = GiteaOps(client, "r1", transport=gitea_transport("https://gt.example/api/v1"))  # type: ignore[arg-type]
        ops.rate_limit()
        (job,) = client.jobs
        assert job.kind == "vcs.op" and job.op == "raw.api"
        descriptor = job.params["transport"]
        assert descriptor["api_url"] == "https://gt.example/api/v1"
        assert descriptor["auth"] == "token"
        assert descriptor["pagination"] == "link"
        assert descriptor["accept"] == "application/json"
        assert descriptor["api_version_header"] is None
        assert descriptor["token_env"] == ["GITEA_TOKEN"]
        assert descriptor["gh_cli"] is False
        assert set(descriptor) == set(TransportSpec.model_fields)

    def test_lists_page_at_giteas_cap(self) -> None:
        client = StubWorkerClient({"raw.api": [{"id": n} for n in range(PAGE_SIZE)]})
        ops = GiteaOps(client, "r1")  # type: ignore[arg-type]
        with pytest.raises(GithubOpsError, match="not read to its end"):
            ops.raw_pages(f"{R}/labels")
        first = client.jobs[0].params["path"]
        assert first.endswith(f"?limit={PAGE_SIZE}&page=1")

    def test_the_bot_list_is_the_only_bot_signal(self) -> None:
        client = StubWorkerClient({})
        ops = GiteaOps(client, "r1", bot_logins=["CI-Bot", ""])  # type: ignore[arg-type]
        assert ops.kind_of("ci-bot") is True and ops.kind_of("rev-bob") is False
        assert ops.capabilities()["bot_identity"] is Capability.UNSUPPORTED


class TestRecords:
    def test_a_user_is_human_until_listed(self) -> None:
        assert user_record({"id": 3, "login": "rev-bob"}, human) == {
            "login": "rev-bob",
            "type": "User",
            "id": 3,
        }
        assert user_record({"login": "ci-bot"}, lambda login: login == "ci-bot")["type"] == "Bot"
        assert user_record(None, human) == {"login": ""}

    def test_an_issue_drops_the_null_pull_request_key(self) -> None:
        record = issue_record(
            {
                "number": 10,
                "id": 55,
                "title": "t",
                "body": None,
                "state": "open",
                "labels": [{"id": 1, "name": "bug"}],
                "user": {"id": 2, "login": "dev-alice"},
                "comments": 0,
                "created_at": "2026-09-12T23:42:00Z",
                "pull_request": None,
            },
            human,
        )
        assert "pull_request" not in record
        assert record["labels"] == [{"name": "bug"}] and record["state"] == "open"
        assert record["created_at"] == "2026-09-12T23:42:00Z"
        assert "pull_request" in issue_record(
            {"number": 9, "pull_request": {"merged": False}}, human
        )

    def test_a_label_event_comes_from_the_timeline(self) -> None:
        added = timeline_event_record(
            {"type": "label", "label": {"name": "bug"}, "body": "1", "user": {"login": "a"}}, human
        )
        assert added is not None and added["event"] == "labeled"
        assert added["label"] == {"name": "bug"} and added["actor"]["login"] == "a"
        removed = timeline_event_record(
            {"type": "label", "label": {"name": "bug"}, "body": ""}, human
        )
        assert removed is not None and removed["event"] == "unlabeled"
        assert timeline_event_record({"type": "comment", "body": "hi"}, human) is None

    def test_the_repository_record_reads_the_tokens_permissions(self) -> None:
        record = repo_record(
            {
                "id": 1,
                "name": "widgets",
                "full_name": REPO,
                "html_url": "https://gitea.example/acme/widgets",
                "default_branch": "main",
                "private": False,
                "has_issues": True,
                "empty": False,
                "allow_merge_commits": True,
                "allow_squash_merge": False,
                "allow_rebase": True,
                "permissions": {"admin": False, "push": True, "pull": True},
            }
        )
        assert record["permissions"] == {
            "admin": False,
            "maintain": False,
            "push": True,
            "pull": True,
        }
        assert record["allow_squash_merge"] is False and record["allow_merge_commit"] is True

    def test_the_change_record_and_its_merge_state(self) -> None:
        payload = {
            "id": 7,
            "number": 9,
            "title": "WIP: x",
            "state": "open",
            "draft": True,
            "mergeable": False,
            "merged": False,
            "html_url": "u",
            "user": {"login": "dev-alice"},
            "head": {"ref": "sbxloop/r1", "sha": "abc", "label": "sbxloop/r1"},
            "base": {"ref": "main", "sha": "base", "repo": {"full_name": REPO}},
            "requested_reviewers": [{"login": "rev-bob"}],
        }
        record = change_record(payload, human)
        assert record["node_id"] == "acme/widgets#9" and record["draft"] is True
        assert (record["mergeable"], record["mergeable_state"]) == (None, "draft")
        assert record["head"]["sha"] == "abc" and record["base"]["ref"] == "main"
        assert record["requested_reviewers"] == [{"login": "rev-bob", "type": "User"}]
        assert merge_state({"mergeable": True, "title": "x"}) == (True, "clean")
        assert merge_state({"mergeable": False, "title": "x"}) == (None, "unknown")
        assert merge_state({"merged": True}) == (False, "merged")
        assert parse_node_id("acme/widgets#9") == (REPO, 9)
        with pytest.raises(GithubOpsError, match="not a Gitea pull request id"):
            parse_node_id("PR_kwDO")

    def test_review_words_are_githubs(self) -> None:
        assert (
            review_record({"id": 1, "state": "REQUEST_CHANGES", "user": {"login": "b"}}, human)[
                "state"
            ]
            == "CHANGES_REQUESTED"
        )
        assert (
            review_record({"id": 2, "state": "APPROVED", "dismissed": True}, human)["state"]
            == "DISMISSED"
        )
        assert review_record({"id": 3, "state": "COMMENT"}, human)["state"] == "COMMENTED"
        assert review_record({"id": 4, "state": "PENDING"}, human)["state"] == "PENDING"

    def test_a_thread_id_names_the_comment(self) -> None:
        assert thread_id_for(REPO, 9, 29) == "acme/widgets#9:29"
        assert parse_thread_id("acme/widgets#9:29") == (REPO, 9, 29)
        with pytest.raises(ValueError, match="not a Gitea thread id"):
            parse_thread_id("acme/widgets#9")

    def test_statuses_fold_from_the_status_key(self) -> None:
        rows = [
            {"context": "ci", "status": "success"},
            {"context": "lint", "status": "pending"},
            {"context": "docs", "status": "warning"},
        ]
        verdict = fold_statuses(rows)
        assert verdict == ChecksVerdict("red", 3, ("lint",), ("docs",), ("ci",))
        assert fold_statuses([]).state == "green"
        assert fold_statuses([{"context": "ci", "state": "success"}]).state == "green"
        run = check_run_record({"id": 1, "context": "ci", "status": "failure", "target_url": "u"})
        assert run["conclusion"] == "failure" and run["html_url"] == "u"

    def test_a_diff_splits_per_file_into_hunks(self) -> None:
        text = (
            "diff --git a/a.py b/a.py\nnew file mode 100644\nindex 0000000..c1827f0\n"
            "--- /dev/null\n+++ b/a.py\n@@ -0,0 +1 @@\n+a2\n"
            "diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\n"
            "--- a/gone.txt\n+++ /dev/null\n"
            "@@ -1 +0,0 @@\n-bye\n"
        )
        files = split_diff(text)
        assert set(files) == {"a.py", "gone.txt"}
        assert files["a.py"] == "@@ -0,0 +1 @@\n+a2\n"
        assert files["gone.txt"].startswith("@@ -1 +0,0 @@")

    def test_a_commit_record_addresses_the_tree_by_the_commit(self) -> None:
        record = commit_record(
            {
                "sha": "abc",
                "tree": None,
                "parents": [{"sha": "p"}],
                "commit": {"message": "m\n", "tree": {"sha": "t"}},
                "html_url": "u",
            }
        )
        assert record == {
            "sha": "abc",
            "tree": {"sha": "abc"},
            "parents": [{"sha": "p"}],
            "message": "m\n",
            "html_url": "u",
        }

    def test_wip_prefixes(self) -> None:
        assert undrafted_title("WIP: ship it") == "ship it"
        assert undrafted_title("[WIP] ship it") == "ship it"
        assert undrafted_title("ship it") == "ship it"


class TestRepository:
    def test_lookup_default_branch_and_refs(self) -> None:
        fake = FakeGitea()
        assert fake.repo_lookup(REPO) is not None
        assert fake.repo_lookup("acme/nope") is None
        assert fake.default_branch(REPO) == "main"
        assert fake.ref_lookup(REPO, "heads/main") == "base123"
        assert fake.ref_lookup(REPO, "heads/never") is None
        assert fake.ref_lookup(REPO, "tags/v1") is None
        with pytest.raises(GithubOpsError, match="heads/<branch> or tags/<tag>"):
            fake.ref_lookup(REPO, "main")

    def test_a_missing_repository_is_none_and_an_empty_one_has_no_branch(self) -> None:
        fake = FakeGitea()
        fake.missing_repo = True
        assert fake.repo_lookup(REPO) is None
        fake = FakeGitea()
        fake.empty = True
        assert fake.ref_lookup(REPO, "heads/main") is None

    def test_contents_and_the_merge_base(self) -> None:
        fake = FakeGitea()
        assert fake.contents_read(REPO, "README.md") == "# widgets\n"
        with pytest.raises(GithubOpsError, match="has no 'nope'"):
            fake.contents_read(REPO, "nope")
        fake.seed_pull(1, head="sbxloop/r1", head_sha="base123")
        assert fake.merge_base(REPO, "main", "sbxloop/r1") == "base123"
        compare = fake.compare_lookup(REPO, "main", "sbxloop/r1")
        assert compare is not None and compare["merge_base_commit"] == {"sha": "base123"}
        assert compare["status"] == "identical"
        assert fake.compare_lookup(REPO, "main", "nope") is None

    def test_the_merge_base_without_a_pull_request_walks_the_comparison(self) -> None:
        fake = FakeGitea()
        fake.contents_put(REPO, "b.txt", message="m", content_b64="Yg==", branch="feature")
        head = fake.branches["feature"]
        assert fake.merge_base(REPO, "main", "feature") == "base123"
        compare = fake.compare_lookup(REPO, "main", "feature")
        assert compare is not None and compare["status"] == "ahead"
        assert compare["commits"][0]["sha"] == head

    def test_branch_delete_tolerates_a_missing_branch(self) -> None:
        fake = FakeGitea()
        fake.branch_delete(REPO, "never")
        fake.branches["gone"] = "base123"
        fake.branch_delete(REPO, "gone")
        assert "gone" not in fake.branches


class TestIssues:
    def test_lifecycle_with_labels_by_name(self) -> None:
        fake = FakeGitea()
        fake._ensure_label("sbxloop:run")
        fake._ensure_label("sbxloop:in-progress")
        ref = fake.issue_create(REPO, "the checks never run", "body", labels=["sbxloop:run"])
        assert ref.number == 1 and ref.url.endswith("/issues/1")
        issue = fake.issue_get(REPO, 1)
        assert issue["state"] == "open" and issue["labels"] == [{"name": "sbxloop:run"}]
        assert "pull_request" not in issue
        fake.issue_labels_add(REPO, 1, ["sbxloop:in-progress"])
        fake.issue_label_remove(REPO, 1, "sbxloop:run")
        fake.issue_label_remove(REPO, 1, "never-there")
        assert [lb["name"] for lb in fake.issue_get(REPO, 1)["labels"]] == ["sbxloop:in-progress"]
        events = fake.issue_events(REPO, 1)
        assert [(e["event"], e["label"]["name"]) for e in events] == [
            ("labeled", "sbxloop:in-progress"),
            ("unlabeled", "sbxloop:run"),
        ]
        url = fake.issue_comment(REPO, 1, "claimed")
        assert "#issuecomment-" in url
        (comment,) = fake.issue_comments(REPO, 1)
        assert comment["body"] == "claimed" and comment["user"]["login"] == "sbxloop-bot"
        fake.issue_comment_delete(REPO, comment["id"])
        assert fake.issue_comments(REPO, 1) == []
        fake.issue_close(REPO, 1)
        assert fake.issue_get(REPO, 1)["state"] == "closed"

    def test_a_label_the_repository_lacks_is_refused_by_name(self) -> None:
        fake = FakeGitea()
        with pytest.raises(GithubOpsError, match="has no label 'nope'"):
            fake.issue_create(REPO, "t", labels=["nope"])
        fake.seed_issue(1, "t")
        with pytest.raises(GithubOpsError, match="has no label 'nope'"):
            fake.issue_labels_add(REPO, 1, ["nope"])
        assert fake.label_posts == [(1, ["nope"])], "Gitea dropped it silently; the answer said so"

    def test_listing_search_and_labels(self) -> None:
        fake = FakeGitea()
        fake.seed_issue(41, "queued work", ["sbxloop:run"])
        fake.seed_issue(42, "other", ["bug"], state="closed")
        listed = fake.issues_list(REPO, labels=["sbxloop:run"])
        assert [i["number"] for i in listed] == [41]
        assert [i["number"] for i in fake.issues_list(REPO, state="all")] == [41, 42]
        found = fake.search_issues(f"repo:{REPO} is:open label:sbxloop:run queued")
        assert [i["number"] for i in found] == [41]
        result = fake.issue_search(f"repo:{REPO} queued", per_page=10)
        assert result["total_count"] == 1 and result["incomplete_results"] is False
        with pytest.raises(GithubOpsError, match="needs a repo"):
            fake.issue_search("queued", per_page=10)
        assert fake.label_lookup(REPO, "BUG") == {
            "id": 1,
            "name": "bug",
            "color": "ee0701",
            "description": "",
        }
        assert fake.label_lookup(REPO, "nope") is None
        created = fake.label_create(REPO, name="sbxloop:done", color="#00ff00", description="d")
        assert created["color"] == "00ff00"
        with pytest.raises(GithubOpsError, match="already exists"):
            fake.label_create(REPO, name="sbxloop:done", color="00ff00", description="")
        assert [lb["name"] for lb in fake.labels_list(REPO)] == [
            "bug",
            "sbxloop:run",
            "sbxloop:done",
        ]


class TestChecks:
    def test_statuses_fold_and_the_failed_ones_carry_their_description(self) -> None:
        fake = FakeGitea()
        fake.status_create(REPO, "abc", "success", context="ci")
        fake.status_create(
            REPO, "abc", "failure", context="lint", description="E501 too long", target_url="u"
        )
        assert fake.statuses_posted[0] == ("abc", {"state": "success", "context": "ci"})
        verdict = fake.pr_checks(REPO, "abc")
        assert verdict.state == "red" and verdict.failed == ("lint",) and verdict.passed == ("ci",)
        (failed,) = fake.checks_failed_logs(REPO, "abc")
        assert failed == FailedCheck("lint", "failure", "E501 too long", "u")
        assert [r["name"] for r in fake.check_runs(REPO, "abc")] == ["ci", "lint"]
        assert fake.pr_checks(REPO, "none").state == "green"

    def test_required_checks_come_from_the_base_branch(self) -> None:
        fake = FakeGitea()
        fake.seed_pull(1)
        assert fake.pr_required_checks(REPO, 1) == ()
        fake.branch_rules = {"enable_status_check": True, "status_check_contexts": ["ci", "lint"]}
        assert fake.pr_required_checks(REPO, 1) == ("ci", "lint")

    def test_workflows_and_runs(self) -> None:
        fake = FakeGitea()
        assert fake.workflows_list(REPO) == []
        fake.workflows = [
            {"id": "ci.yml", "name": "ci", "state": "active", "path": ".gitea/workflows/ci.yml"}
        ]
        assert fake.workflows_list(REPO)[0]["name"] == "ci"
        fake.runs = [
            {
                "id": 1,
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "head_branch": "main",
                "head_sha": "x",
            },
            {"id": 2, "name": "ci", "status": "running", "head_branch": "other"},
        ]
        (run,) = fake.workflow_runs(REPO, branch="main")
        assert run["status"] == "completed" and run["conclusion"] == "success"


class TestPolicy:
    def test_an_unprotected_base_requires_nothing(self) -> None:
        req = FakeGitea().base_requirements(REPO, "main")
        assert req.required_contexts == () and req.approvals_required == 0
        assert req.source == "branch" and req.forge == "gitea" and req.blockers() == []

    def test_a_write_collaborator_reads_the_branch_not_the_rule(self) -> None:
        fake = FakeGitea()
        fake.branch_rules = {
            "required_approvals": 1,
            "enable_status_check": True,
            "status_check_contexts": ["ci", "lint"],
            "user_can_merge": True,
        }
        req = fake.base_requirements(REPO, "main")
        assert req.required_contexts == ("ci", "lint") and req.approvals_required == 1
        assert req.source == "branch" and req.unread == ("protection",)
        assert req.signed_commits is False and req.dismiss_stale_reviews is False
        assert any("approv" in r for r in req.blockers())

    def test_an_admin_reads_the_rule_too(self) -> None:
        fake = FakeGitea()
        fake.admin = True
        fake.branch_rules = {"required_approvals": 0, "enable_status_check": False}
        fake.protection_rule["require_signed_commits"] = True
        req = fake.base_requirements(REPO, "main")
        assert req.source == "branch+protection" and req.unread == ()
        assert req.signed_commits is True and req.dismiss_stale_reviews is True

    def test_a_token_that_may_not_merge_is_a_blocker(self) -> None:
        fake = FakeGitea()
        fake.branch_rules = {"required_approvals": 0, "user_can_merge": False}
        (reason,) = fake.base_requirements(REPO, "main").blockers()
        assert "may not merge into main" in reason

    def test_an_unreadable_branch_is_unknown(self) -> None:
        fake = FakeGitea()
        fake.missing_repo = True
        req = fake.base_requirements(REPO, "main")
        assert req.source == "unknown" and req.unread == ("branch",)

    def test_the_credential_never_expires_and_cannot_read_its_scopes(self) -> None:
        fake = FakeGitea()
        assert fake.token_scopes() is None
        info = fake.credential_info()
        assert info == CredentialInfo(kind="Gitea access token", expires_at=None, active=None)
        assert info is not None and info.never_expires
        assert fake.rate_limit() == {"version": "1.24.7"}
        me = fake.authenticated_user()
        assert me["login"] == "sbxloop-bot" and me["type"] == "User"

    def test_permission_probes_read_giteas_paths(self) -> None:
        fake = FakeGitea()
        assert fake.permission_probe("issues", REPO, "main") is True
        assert fake.permission_probe("contents", REPO, "") is None
        fake.fail_once["permission_probe"] = GithubOpsError("forbidden", http_status=403)
        assert fake.permission_probe("pull_requests", REPO, "main") is False
        assert any(c[1] == f"{R}/pulls?limit=1" for c in fake.raw_calls)


class TestTheClass:
    def test_the_matrix_is_the_verified_one(self) -> None:
        report = FakeGitea().capabilities()
        assert set(report) == set(CAPABILITIES)
        assert report["merge_queue"] is Capability.UNSUPPORTED
        assert report["review_threads"] is Capability.UNSUPPORTED
        assert report["bot_identity"] is Capability.UNSUPPORTED
        assert report["request_changes_review"] is Capability.SUPPORTED
        assert report["remote_commit"] is Capability.SUPPORTED
        assert GiteaOps.UNIMPLEMENTED_OPERATIONS == ()

    def test_a_failed_job_is_a_gitea_error_with_its_status(self) -> None:
        client = StubWorkerClient({"raw.api": "FAIL"})
        ops = GiteaOps(client, "r1")  # type: ignore[arg-type]
        with pytest.raises(GithubOpsError, match=r"gitea op raw\.api failed") as info:
            ops.rate_limit()
        assert isinstance(info.value.http_status, int | type(None))


def calls(fake: FakeGitea) -> list[tuple[str, str, Any]]:
    return list(fake.raw_calls)
