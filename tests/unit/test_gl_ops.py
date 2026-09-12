"""The GitLab backend's read paths (#1017): the transport descriptor every
job carries, the folds from GitLab CE 19.3's payload shapes (#1016) into
the loop's records, each named operation's request against the fake, and
the write roles failing closed by name."""

from __future__ import annotations

from typing import Any

import pytest

from sbxloop.errors import GithubOpsError, RoleNotImplemented
from sbxloop.vcs.gitlab.ops import (
    GitlabOps,
    check_run_record,
    fold_statuses,
    gitlab_transport,
    iso_utc,
    issue_record,
    label_event_record,
    note_record,
    repo_record,
    user_record,
)
from sbxloop.vcs.gitlab.permissions import missing_from_scopes
from sbxloop.vcs.model import ChecksVerdict, FailedCheck
from sbxloop.vcs.protocol import CAPABILITIES, Capability
from sbxloop_worker.protocol import TransportSpec
from tests.fakes.fake_gitlab import FakeGitlab
from tests.unit.test_gh_ops import StubWorkerClient

REPO = "acme/widgets"
PROJECT = "/projects/acme%2Fwidgets"


def calls(fake: FakeGitlab) -> list[tuple[str, str, Any]]:
    return list(fake.raw_calls)


class TestTransportDescriptor:
    def test_every_job_names_gitlabs_transport(self) -> None:
        client = StubWorkerClient({"raw.api": {"version": "19.3.2"}})
        ops = GitlabOps(client, "r1", transport=gitlab_transport("https://gl.example/api/v4"))  # type: ignore[arg-type]
        ops.rate_limit()
        (job,) = client.jobs
        assert job.kind == "vcs.op" and job.op == "raw.api"
        descriptor = job.params["transport"]
        assert descriptor["api_url"] == "https://gl.example/api/v4"
        assert descriptor["auth"] == "private-token"
        assert descriptor["pagination"] == "x-next-page"
        assert descriptor["accept"] == "application/json"
        assert descriptor["api_version_header"] is None
        assert descriptor["token_env"] == ["GITLAB_TOKEN"]
        assert descriptor["gh_cli"] is False
        assert "token" not in descriptor
        assert set(descriptor) == set(TransportSpec.model_fields)

    def test_a_failed_job_is_a_gitlab_error_with_its_status(self) -> None:
        client = StubWorkerClient({"raw.api": "FAIL"})
        ops = GitlabOps(client, "r1")  # type: ignore[arg-type]
        with pytest.raises(GithubOpsError, match=r"gitlab op raw\.api failed"):
            ops.rate_limit()


class TestFolds:
    """Pure folds, pinned against the shapes GitLab CE 19.3 answered
    with in #1016."""

    def test_timestamps_take_the_claim_protocols_form(self) -> None:
        assert iso_utc("2026-09-12T21:31:48.478Z") == "2026-09-12T21:31:48Z"
        assert iso_utc("2026-09-12T21:31:48+00:00") == "2026-09-12T21:31:48Z"
        assert iso_utc("2026-09-12T21:31:48Z") == "2026-09-12T21:31:48Z"
        assert iso_utc(None) == ""

    def test_an_issue_record_carries_the_neutral_keys_and_no_pull_request(self) -> None:
        record = issue_record(
            {
                "id": 3,
                "iid": 3,
                "title": "Seeded open issue",
                "description": None,
                "state": "opened",
                "labels": ["bug"],
                "author": {"id": 2, "username": "dev-alice", "name": "dev-alice"},
                "user_notes_count": 1,
                "created_at": "2026-09-12T21:21:44.135Z",
                "web_url": "https://localhost:8929/acme/widgets/-/issues/3",
            }
        )
        assert record["number"] == 3 and record["state"] == "open"
        assert record["body"] == "" and record["state_reason"] is None
        assert record["labels"] == [{"name": "bug"}]
        assert record["user"] == {"login": "dev-alice", "id": 2}
        assert record["html_url"].endswith("/acme/widgets/-/issues/3")
        assert record["created_at"] == "2026-09-12T21:21:44Z"
        assert "pull_request" not in record
        assert issue_record({"iid": 4, "state": "closed"})["state"] == "closed"

    def test_a_user_record_never_guesses_the_kind(self) -> None:
        # The bot flag is not on an author object (#1016 V3); the kind
        # stays unknown rather than read as human.
        assert user_record({"id": 4, "username": "project_1_bot_05b4"}) == {
            "login": "project_1_bot_05b4",
            "id": 4,
        }
        assert user_record(None) == {"login": ""}

    def test_a_note_record(self) -> None:
        record = note_record(
            {
                "id": 21,
                "body": "<!-- sbxloop:claim --> claimed",
                "author": {"id": 2, "username": "sbxloop-bot"},
                "created_at": "2026-09-12T21:31:48.478Z",
            },
            issue_url="https://gl/acme/widgets/-/issues/3",
        )
        assert record["id"] == 21 and record["user"]["login"] == "sbxloop-bot"
        assert record["created_at"] == "2026-09-12T21:31:48Z"
        assert record["html_url"] == "https://gl/acme/widgets/-/issues/3#note_21"

    def test_label_events_are_the_loops_labeled_and_unlabeled(self) -> None:
        added = label_event_record(
            {
                "action": "add",
                "label": {"name": "sbxloop:run"},
                "user": {"username": "u"},
                "created_at": "2026-09-12T20:02:00.000Z",
            }
        )
        assert added == {
            "event": "labeled",
            "label": {"name": "sbxloop:run"},
            "actor": {"login": "u"},
            "created_at": "2026-09-12T20:02:00Z",
        }
        removed = label_event_record({"action": "remove", "label": {"name": "x"}})
        assert removed is not None and removed["event"] == "unlabeled"
        assert label_event_record({"action": "add", "label": None}) is None

    def test_a_repository_record_reads_merge_settings_and_access(self) -> None:
        record = repo_record(
            {
                "id": 1,
                "path": "widgets",
                "path_with_namespace": "acme/widgets",
                "web_url": "https://localhost:8929/acme/widgets",
                "default_branch": "main",
                "visibility": "private",
                "issues_enabled": True,
                "merge_method": "merge",
                "squash_option": "default_off",
                "permissions": {"project_access": {"access_level": 30}, "group_access": None},
            }
        )
        assert record["default_branch"] == "main" and record["private"] is True
        assert record["has_issues"] is True
        assert record["allow_squash_merge"] and record["allow_merge_commit"]
        assert record["allow_rebase_merge"] is False
        assert record["permissions"] == {
            "admin": False,
            "maintain": False,
            "push": True,
            "pull": True,
        }
        fast_forward = repo_record({"merge_method": "ff", "squash_option": "never"})
        assert fast_forward["allow_rebase_merge"] and not fast_forward["allow_merge_commit"]
        assert fast_forward["allow_squash_merge"] is False
        assert fast_forward["permissions"]["pull"] is False

    def test_statuses_fold_to_one_verdict(self) -> None:
        rows = [
            {"id": 1, "name": "ci", "status": "success", "allow_failure": False},
            {"id": 2, "name": "docs", "status": "failed", "allow_failure": False},
            {"id": 3, "name": "lint", "status": "running", "allow_failure": False},
        ]
        verdict = fold_statuses(rows)
        assert verdict.state == "red" and verdict.failed == ("docs",)
        assert verdict.pending == ("lint",) and verdict.passed == ("ci",)
        assert fold_statuses([]) == ChecksVerdict("green", 0, (), ())

    def test_a_retried_job_counts_once_and_the_newest_wins(self) -> None:
        rows = [
            {"id": 1, "name": "ci", "status": "failed"},
            {"id": 2, "name": "ci", "status": "success"},
        ]
        assert fold_statuses(rows).state == "green"

    def test_allowed_failures_skips_and_manual_jobs(self) -> None:
        rows = [
            {"id": 1, "name": "docs", "status": "failed", "allow_failure": True},
            {"id": 2, "name": "deploy", "status": "manual"},
            {"id": 3, "name": "opt", "status": "skipped"},
        ]
        verdict = fold_statuses(rows)
        assert verdict.state == "pending" and verdict.needs_approval == ("deploy",)
        assert set(verdict.passed) == {"docs", "opt"}
        assert check_run_record(rows[1])["conclusion"] == "action_required"
        assert check_run_record(rows[0])["conclusion"] == "neutral"

    def test_an_unknown_status_fails_closed(self) -> None:
        assert fold_statuses([{"id": 1, "name": "x", "status": "novel"}]).state == "red"


class TestRepository:
    def test_repo_get_reads_the_project_once(self) -> None:
        fake = FakeGitlab()
        assert fake.repo_get(REPO)["default_branch"] == "main"
        assert fake.default_branch(REPO) == "main"
        assert calls(fake) == [("GET", PROJECT, None)]

    def test_repo_lookup_answers_a_miss_as_data(self) -> None:
        fake = FakeGitlab()
        fake.missing_project = True
        assert fake.repo_lookup(REPO) is None
        fake.assert_no_failed_jobs()

    def test_ref_lookup_names_branches_and_a_missing_one(self) -> None:
        fake = FakeGitlab()
        assert fake.ref_lookup(REPO, "heads/main") == "base123"
        assert fake.ref_lookup(REPO, "heads/sbxloop/never") is None
        fake.assert_no_failed_jobs()
        assert calls(fake)[0] == ("GET", f"{PROJECT}/repository/branches/main", None)
        with pytest.raises(GithubOpsError, match="heads/"):
            fake.ref_lookup(REPO, "main")

    def test_an_empty_project_has_no_base(self) -> None:
        fake = FakeGitlab()
        fake.empty_repo = True
        assert fake.ref_lookup(REPO, "heads/main") is None

    def test_merge_base_and_compare(self) -> None:
        fake = FakeGitlab()
        fake.branches["sbxloop/r1"] = "commit1"
        assert fake.merge_base(REPO, "main", "sbxloop/r1") == "base123"
        compared = fake.compare_lookup(REPO, "main", "sbxloop/r1")
        assert compared is not None and compared["merge_base_commit"] == {"sha": "base123"}
        assert fake.compare_lookup(REPO, "main", "unrelated") is None
        assert fake.merge_base(REPO, "main", "unrelated") is None

    def test_contents_read_decodes_the_file(self) -> None:
        fake = FakeGitlab()
        assert fake.contents_read(REPO, "README.md") == "# widgets\n"
        _method, path, _ = calls(fake)[-1]
        assert path.startswith(f"{PROJECT}/repository/files/README.md?ref=main")

    def test_branch_delete_tolerates_a_gone_branch(self) -> None:
        fake = FakeGitlab()
        fake.branches["sbxloop/r1"] = "c"
        fake.branch_delete(REPO, "sbxloop/r1")
        fake.branch_delete(REPO, "sbxloop/r1")
        assert fake.deleted_branches == ["sbxloop/r1", "sbxloop/r1"]
        fake.assert_no_failed_jobs()


class TestIssues:
    def test_lifecycle(self) -> None:
        fake = FakeGitlab()
        ref = fake.issue_create(REPO, "the checks never run", "body", labels=["sbxloop:run"])
        assert ref.number == 901 and ref.url.endswith("/-/issues/901")
        assert calls(fake)[-1] == (
            "POST",
            f"{PROJECT}/issues",
            {"title": "the checks never run", "description": "body", "labels": "sbxloop:run"},
        )
        issue = fake.issue_get(REPO, 901)
        assert issue["state"] == "open" and issue["labels"] == [{"name": "sbxloop:run"}]
        fake.issue_labels_add(REPO, 901, ["sbxloop:in-progress"])
        fake.issue_label_remove(REPO, 901, "sbxloop:run")
        fake.issue_label_remove(REPO, 901, "never-there")
        assert calls(fake)[-1] == ("PUT", f"{PROJECT}/issues/901", {"remove_labels": "never-there"})
        url = fake.issue_comment(REPO, 901, "claimed")
        assert url.endswith("/-/issues/901#note_1")
        fake.issue_close(REPO, 901, reason="not_planned")
        assert calls(fake)[-1] == ("PUT", f"{PROJECT}/issues/901", {"state_event": "close"})
        assert fake.issue_get(REPO, 901)["state"] == "closed"
        events = fake.issue_events(REPO, 901)
        assert [e["event"] for e in events] == ["labeled"]
        assert events[0]["label"] == {"name": "sbxloop:in-progress"}

    def test_comments_leave_system_notes_out_and_delete_by_issue(self) -> None:
        fake = FakeGitlab()
        fake.seed_issue(5, "x")
        fake.seed_note(5, "added ~label", system=True)
        note = fake.seed_note(5, "a person's word", author_id=3)
        rows = fake.issue_comments(REPO, 5)
        assert [r["body"] for r in rows] == ["a person's word"]
        assert rows[0]["user"] == {"login": "rev-bob", "id": 3} and "type" not in rows[0]["user"]
        fake.issue_comment_delete(REPO, note)
        assert fake.notes_deleted == [(5, note)]
        fake.issue_comment_delete(REPO, 77, number=5)
        assert fake.notes_deleted[-1] == (5, 77)
        with pytest.raises(GithubOpsError, match="which issue"):
            FakeGitlab().issue_comment_delete(REPO, 78)

    def test_listing_filters_by_state_and_labels(self) -> None:
        fake = FakeGitlab()
        fake.seed_issue(1, "queued", ["sbxloop:run"])
        fake.seed_issue(2, "done", ["sbxloop:run"], state="closed")
        fake.seed_issue(3, "other", ["bug"])
        listed = fake.issues_list(REPO, labels=["sbxloop:run"])
        assert [i["number"] for i in listed] == [1]
        assert "state=opened" in calls(fake)[-1][1] and "labels=sbxloop%3Arun" in calls(fake)[-1][1]
        everything = fake.issues_list(
            REPO, state="all", labels=["sbxloop:run"], sort="updated", direction="desc"
        )
        assert {i["number"] for i in everything} == {1, 2}
        assert "order_by=updated_at" in calls(fake)[-1][1] and "sort=desc" in calls(fake)[-1][1]

    def test_the_daemons_search_becomes_a_list(self) -> None:
        fake = FakeGitlab()
        fake.seed_issue(1, "queued", ["sbxloop:run"])
        found = fake.search_issues(f'repo:{REPO} is:issue is:open label:"sbxloop:run"')
        assert [i["number"] for i in found] == [1]
        with pytest.raises(GithubOpsError):
            fake.search_issues("is:issue is:open")

    def test_the_follow_up_search_says_when_it_may_be_incomplete(self) -> None:
        fake = FakeGitlab()
        fake.seed_issue(1, "flaky release gate", description="the gate flakes")
        fake.seed_issue(2, "unrelated")
        answer = fake.issue_search(f"repo:{REPO} is:issue in:title,body flaky gate", per_page=20)
        assert answer["total_count"] == 1 and answer["incomplete_results"] is False
        assert answer["items"][0]["number"] == 1
        assert "scope=issues" in calls(fake)[-1][1]
        full = fake.issue_search(f"repo:{REPO} is:issue in:title,body flaky gate", per_page=1)
        assert full["incomplete_results"] is True

    def test_labels(self) -> None:
        fake = FakeGitlab()
        assert fake.label_lookup(REPO, "sbxloop:run") is None
        made = fake.label_create(REPO, name="sbxloop:run", color="0e8a16", description="d")
        assert made["name"] == "sbxloop:run" and made["color"] == "0e8a16"
        assert calls(fake)[-1][2] == {"name": "sbxloop:run", "color": "#0e8a16", "description": "d"}
        assert fake.label_lookup(REPO, "SBXLOOP:RUN") == made
        with pytest.raises(GithubOpsError, match="already exists") as info:
            fake.label_create(REPO, name="sbxloop:run", color="0e8a16", description="d")
        assert info.value.http_status == 409
        assert [lb["name"] for lb in fake.labels_list(REPO)] == ["sbxloop:run"]

    def test_issues_disabled_refuses_creation(self) -> None:
        fake = FakeGitlab()
        fake.settings["issues_enabled"] = False
        assert fake.repo_get(REPO)["has_issues"] is False
        with pytest.raises(GithubOpsError) as info:
            fake.issue_create(REPO, "t")
        assert info.value.http_status == 403


class TestChecks:
    def test_a_green_and_a_red_head_with_its_trace(self) -> None:
        fake = FakeGitlab()
        assert fake.pr_checks(REPO, "commit0") == ChecksVerdict("green", 0, (), ())
        fake.seed_verdict(
            "commit0",
            ChecksVerdict("red", 2, (), ("ci",), ("lint",)),
            logs=[FailedCheck("ci", "failure", "AssertionError: expected 2, got 3", "")],
        )
        verdict = fake.pr_checks(REPO, "commit0")
        assert verdict.state == "red" and verdict.failed == ("ci",) and verdict.passed == ("lint",)
        (failed,) = fake.checks_failed_logs(REPO, "commit0")
        assert failed.name == "ci" and "expected 2, got 3" in failed.excerpt
        assert fake.text_calls == [f"{PROJECT}/jobs/{failed and 102}/trace"]

    def test_a_red_status_without_a_trace_keeps_its_description(self) -> None:
        fake = FakeGitlab()
        fake.seed_status(
            "commit0",
            "external",
            "failed",
            description="the vendor said no",
            target_url="https://ci.example/1",
        )
        (failed,) = fake.checks_failed_logs(REPO, "commit0")
        assert failed.excerpt == "the vendor said no" and failed.url == "https://ci.example/1"
        fake.assert_no_failed_jobs()

    def test_check_runs_and_status_create(self) -> None:
        fake = FakeGitlab()
        fake.seed_status("commit0", "ci", "running")
        (run,) = fake.check_runs(REPO, "commit0")
        assert run["name"] == "ci" and run["conclusion"] is None and run["status"] == "in_progress"
        fake.status_create(REPO, "commit0", "failure", context="sbxloop", description="d")
        assert fake.statuses_posted == [
            ("commit0", {"state": "failed", "name": "sbxloop", "description": "d"})
        ]

    def test_no_per_change_rollup(self) -> None:
        with pytest.raises(GithubOpsError, match="no per-change"):
            FakeGitlab().pr_required_checks(REPO, 1)

    def test_workflows_are_the_ci_file_and_pipelines(self) -> None:
        fake = FakeGitlab()
        assert fake.workflows_list(REPO) == []
        fake.ci_file = True
        assert fake.workflows_list(REPO)[0]["state"] == "active"
        fake.pipelines = [
            {
                "id": 5,
                "ref": "main",
                "status": "success",
                "sha": "base123",
                "web_url": "https://gl/p/5",
            }
        ]
        (run,) = fake.workflow_runs(REPO, branch="main")
        assert run["conclusion"] == "success" and run["name"] == "pipeline"


class TestPolicy:
    def test_identity_and_token(self) -> None:
        fake = FakeGitlab()
        user = fake.authenticated_user()
        assert user["login"] == "sbxloop-bot" and user["type"] == "User"
        fake.users[2]["bot"] = True
        assert fake.authenticated_user()["type"] == "Bot"
        assert fake.token_scopes() == ("api",)
        fake.token_self = None
        assert fake.token_scopes() is None
        assert fake.rate_limit()["version"] == "19.3.2"

    def test_the_bot_flag_is_one_lookup_per_user(self) -> None:
        fake = FakeGitlab()
        assert fake.user_is_bot(4) is True and fake.user_is_bot(3) is False
        assert fake.user_is_bot(4) is True
        assert calls(fake).count(("GET", "/users/4", None)) == 1
        assert fake.user_is_bot(99) is None

    def test_permission_probes_use_gitlabs_paths(self) -> None:
        fake = FakeGitlab()
        assert fake.permission_probe("contents", REPO, "main") is True
        assert fake.permission_probe("contents", REPO, "") is None
        fake.fail_always["permission_probe"] = GithubOpsError("forbidden", http_status=403)
        assert fake.permission_probe("issues", REPO, "main") is False

    def test_scopes_cover_the_needs(self) -> None:
        assert missing_from_scopes(["api"]) == ()
        assert {n.permission for n in missing_from_scopes(["read_api"])} == {
            "contents",
            "pull_requests",
            "issues",
        }
        assert {n.permission for n in missing_from_scopes(["read_user"])} == {
            "metadata",
            "contents",
            "pull_requests",
            "issues",
            "checks",
            "actions",
        }

    def test_the_capability_report_is_the_verified_matrix(self) -> None:
        report = FakeGitlab().capabilities()
        assert set(report) == set(CAPABILITIES)
        assert report["review_threads"] is Capability.SUPPORTED
        assert report["draft_changes"] is Capability.SUPPORTED
        assert report["remote_commit"] is Capability.SUPPORTED
        assert report["bot_identity"] is Capability.SUPPORTED
        assert report["required_checks_introspection"] is Capability.SUPPORTED
        assert report["request_changes_review"] is Capability.UNSUPPORTED
        assert report["short_lived_token"] is Capability.UNSUPPORTED
        assert report["signed_api_commits"] is Capability.UNSUPPORTED
        assert report["merge_queue"] is Capability.UNKNOWN


class TestBaseRequirements:
    def test_an_unprotected_base_on_ce_is_an_answer(self) -> None:
        req = FakeGitlab().base_requirements(REPO, "main")
        assert req.required_contexts == () and req.approvals_required == 0
        assert req.source == "project" and req.forge == "gitlab"
        assert req.all_checks_required is False and req.blockers() == []

    def test_pipeline_must_succeed_gates_the_whole_pipeline(self) -> None:
        fake = FakeGitlab()
        fake.settings["only_allow_merge_if_pipeline_succeeds"] = True
        fake.settings["only_allow_merge_if_all_discussions_are_resolved"] = True
        fake.protected = {
            "name": "main",
            "push_access_levels": [{"access_level": 0}],
            "merge_access_levels": [{"access_level": 30}],
        }
        req = fake.base_requirements(REPO, "main")
        assert req.source == "protected_branch+project"
        assert req.required_contexts == () and req.all_checks_required is True
        assert req.conversation_resolution is True and req.approvals_required == 0

    def test_an_unreadable_source_leaves_the_base_unknown_naming_it(self) -> None:
        fake = FakeGitlab()
        fake.protected_forbidden = True
        req = fake.base_requirements(REPO, "main")
        assert req.source == "unknown" and req.required_contexts is None
        assert req.unread == ("protected_branch",)

    def test_an_enterprise_instance_reads_its_approval_rules(self) -> None:
        fake = FakeGitlab()
        fake.enterprise = True
        fake.approval_rules = [
            {"name": "any", "approvals_required": 1, "protected_branches": []},
            {
                "name": "main only",
                "approvals_required": 2,
                "protected_branches": [{"name": "main"}],
            },
            {"name": "elsewhere", "approvals_required": 9, "protected_branches": [{"name": "dev"}]},
        ]
        req = fake.base_requirements(REPO, "main")
        assert req.approvals_required == 2
        assert any("2 approving reviews" in r for r in req.blockers())

    def test_an_unknown_edition_leaves_approvals_unknown(self) -> None:
        fake = FakeGitlab()
        fake.enterprise = None
        assert fake.base_requirements(REPO, "main").approvals_required is None


class TestNotImplementedRoles:
    def test_every_role_is_answered_and_the_typed_error_stays_for_the_next_backend(
        self,
    ) -> None:
        assert GitlabOps.UNIMPLEMENTED_OPERATIONS == ()
        error = FakeGitlab()._unimplemented("ContentOps", "commit_get")
        assert isinstance(error, RoleNotImplemented) and isinstance(error, GithubOpsError)
        assert error.kind == "gitlab" and "ContentOps.commit_get" in str(error)
