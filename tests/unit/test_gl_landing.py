"""The GitLab backend's landing half (#1019): un-draft by retitle, the
rebase as the update, the merge with its refusals as data, reviewers by
id, merge trains behind a per-project capability, and the credential's
own expiry."""

from __future__ import annotations

import pytest

from sbxloop.errors import GithubOpsError, RoleNotImplemented
from sbxloop.vcs.gitlab.ops import GitlabOps, _undrafted_title
from sbxloop.vcs.model import CredentialInfo, MergeOutcome, QueueEntry, QueueState
from sbxloop.vcs.protocol import Capability
from tests.fakes.fake_gitlab import FakeGitlab

REPO = "acme/widgets"
MR = "/projects/acme%2Fwidgets/merge_requests"


def ready(fake: FakeGitlab, iid: int = 1, **seed: object) -> FakeGitlab:
    fake.seed_mr(iid, source_branch="sbxloop/r1", head_sha="commit0", **seed)  # type: ignore[arg-type]
    return fake


class TestUndraft:
    def test_the_prefix_is_the_whole_of_a_draft(self) -> None:
        assert _undrafted_title("Draft: sbxloop: ship it") == "sbxloop: ship it"
        assert _undrafted_title("WIP: x") == "x"
        assert _undrafted_title("[Draft] x") == "x"
        assert _undrafted_title("ready") == "ready"

    def test_ready_for_review_retitles_once(self) -> None:
        fake = ready(FakeGitlab(), title="Draft: sbxloop: ship it")
        assert fake.pr_ready_for_review("1!1") is True
        assert fake.mr_updates == [(1, {"title": "sbxloop: ship it"})]
        assert fake.pr_get(REPO, 1)["draft"] is False
        assert fake.pr_ready_for_review("1!1") is True
        assert len(fake.mr_updates) == 1, "a request not in draft is left alone"
        with pytest.raises(GithubOpsError, match="not a GitLab merge request id"):
            fake.pr_ready_for_review("PR_node7")


class TestReviewers:
    def test_reviewers_are_looked_up_by_username(self) -> None:
        fake = ready(FakeGitlab())
        fake.pr_request_reviewers(REPO, 1, ["rev-bob"])
        assert fake.mr_updates == [(1, {"reviewer_ids": [3]})]
        assert fake.pr_get(REPO, 1)["requested_reviewers"] == [{"login": "rev-bob", "id": 3}]

    def test_an_unknown_user_and_a_group_are_refused_by_name(self) -> None:
        fake = ready(FakeGitlab())
        with pytest.raises(GithubOpsError, match="no user 'nobody'"):
            fake.pr_request_reviewers(REPO, 1, ["nobody"])
        with pytest.raises(GithubOpsError, match="acme/reviewers"):
            fake.pr_request_reviewers(REPO, 1, ["acme/reviewers"])
        fake.pr_request_reviewers(REPO, 1, [])
        assert fake.mr_updates == []


class TestUpdateBranch:
    def test_a_rebase_is_the_update(self) -> None:
        fake = ready(FakeGitlab())
        assert fake.pr_update_branch(REPO, 1, expected_head_sha="commit0") is True
        assert fake.rebases == [1]
        assert fake.raw_calls[-1][:2] == ("PUT", f"{MR}/1/rebase")

    def test_a_moved_head_and_a_refusal_are_not_updates(self) -> None:
        fake = ready(FakeGitlab())
        assert fake.pr_update_branch(REPO, 1, expected_head_sha="stale") is False
        assert fake.rebases == []
        fake.rebase_ok = False
        assert fake.pr_update_branch(REPO, 1) is False


class TestMerge:
    def test_a_squash_merge_with_the_judged_head(self) -> None:
        fake = ready(FakeGitlab())
        outcome = fake.pr_merge(REPO, 1, method="squash", sha="commit0", title="t", message="m")
        assert outcome == MergeOutcome(True, "merge0001", "merged")
        (merge,) = fake.merges
        assert merge[1] == {
            "squash": True,
            "should_remove_source_branch": False,
            "sha": "commit0",
            "squash_commit_message": "m",
        }
        assert fake.pr_get(REPO, 1)["merged"] is True
        assert fake.branches["main"] == "merge0001"

    def test_a_merge_commit_carries_its_message(self) -> None:
        fake = ready(FakeGitlab())
        fake.pr_merge(REPO, 1, method="merge", title="the title")
        assert fake.merges[0][1]["squash"] is False
        assert fake.merges[0][1]["merge_commit_message"] == "the title"

    def test_the_refusals_are_data(self) -> None:
        fake = ready(FakeGitlab(), detailed_merge_status="ci_must_pass")
        blocked = fake.pr_merge(REPO, 1, sha="commit0")
        assert blocked.blocked and not blocked.merged and "405" in blocked.reason
        stale = ready(FakeGitlab(), iid=2).pr_merge(REPO, 2, sha="old")
        assert stale.stale and not stale.blocked
        conflict = ready(FakeGitlab(), iid=3, detailed_merge_status="conflict").pr_merge(REPO, 3)
        assert conflict.blocked and "406" in conflict.reason
        draft = ready(FakeGitlab(), iid=4, title="Draft: x").pr_merge(REPO, 4)
        assert draft.blocked
        forbidden = ready(FakeGitlab(), iid=5)
        forbidden.merge_ok = False
        assert forbidden.pr_merge(REPO, 5).blocked

    def test_a_merge_the_forge_did_not_confirm_is_blocked(self) -> None:
        fake = ready(FakeGitlab())
        fake.fail_once["pr_merge"] = GithubOpsError("boom", http_status=500)
        with pytest.raises(GithubOpsError):
            fake.pr_merge(REPO, 1)


class TestMergeTrains:
    def test_the_free_tier_has_none_and_says_so(self) -> None:
        fake = ready(FakeGitlab())
        assert fake._merge_trains(REPO) is Capability.UNSUPPORTED
        assert fake.capabilities()["merge_queue"] is Capability.UNSUPPORTED
        assert fake.base_requirements(REPO, "main").merge_queue is False
        assert fake.pr_queue_state(REPO, 1) == QueueState(False, False, None)
        with pytest.raises(GithubOpsError) as info:
            fake.pr_enqueue("1!1", head="commit0")
        assert info.value.http_status == 404

    def test_a_project_that_cannot_be_read_is_unknown(self) -> None:
        fake = FakeGitlab()
        fake.missing_project = True
        assert fake._merge_trains(REPO) is Capability.UNKNOWN
        assert fake.capabilities()["merge_queue"] is Capability.UNKNOWN

    def test_a_project_with_trains_enqueues_and_reads_its_entry(self) -> None:
        fake = ready(FakeGitlab())
        fake.settings["merge_trains_enabled"] = True
        assert fake._merge_trains(REPO) is Capability.SUPPORTED
        assert fake.base_requirements(REPO, "main").merge_queue is True
        entry = fake.pr_enqueue("1!1", head="commit0")
        assert entry == QueueEntry(id="901", state="queued", position=1, head="train1")
        assert fake.train_adds == [(1, {"when_pipeline_succeeds": True, "sha": "commit0"})]
        state = fake.pr_queue_state(REPO, 1)
        assert state.entry == entry and not state.merged
        fake.train[1]["status"] = "merging"
        assert fake.pr_queue_state(REPO, 1).entry is not None
        assert fake.pr_queue_state(REPO, 1).entry.state == "mergeable"  # type: ignore[union-attr]
        with pytest.raises(GithubOpsError) as info:
            fake.pr_enqueue("1!1", head="moved")
        assert info.value.http_status == 409


class TestCredential:
    def test_the_token_reads_its_own_expiry(self) -> None:
        info = FakeGitlab().credential_info()
        assert info == CredentialInfo(
            kind="GitLab access token",
            name="sbxloop",
            scopes=("api",),
            expires_at="2026-11-11",
            active=True,
        )

    def test_a_token_that_never_expires_says_so(self) -> None:
        fake = FakeGitlab()
        assert fake.token_self is not None
        fake.token_self["expires_at"] = None
        info = fake.credential_info()
        assert info is not None and info.expires_at is None and info.never_expires

    def test_a_token_that_cannot_read_itself_is_unknown(self) -> None:
        fake = FakeGitlab()
        fake.token_self = None
        assert fake.credential_info() is None


class TestBlockers:
    def test_a_base_only_maintainers_may_merge_blocks_a_developer(self) -> None:
        fake = FakeGitlab()
        fake.protected = {
            "name": "main",
            "push_access_levels": [{"access_level": 0}],
            "merge_access_levels": [
                {"access_level": 40, "access_level_description": "Maintainers"}
            ],
        }
        req = fake.base_requirements(REPO, "main")
        (reason,) = req.blockers()
        assert "Maintainers" in reason and "Developer" in reason
        fake.settings["access_level"] = 40
        fake._projects.clear()
        assert fake.base_requirements(REPO, "main").blockers() == []

    def test_nothing_of_the_landing_is_left_unimplemented(self) -> None:
        assert not any(op.startswith("ChangeOps") for op in GitlabOps.UNIMPLEMENTED_OPERATIONS)
        with pytest.raises(RoleNotImplemented):
            FakeGitlab().commit_get(REPO, "base123")
