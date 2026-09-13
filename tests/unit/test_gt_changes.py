"""The Gitea backend's pull request (#1021): opened as a WIP draft,
reviewed GitHub's way with a comment as its own thread, landed with the
forge's refusals as data and no queue to enter, and delivered without a
checkout through the contents API with a fix round committed on top of
the branch."""

from __future__ import annotations

import base64

import pytest

from sbxloop.errors import GithubOpsError
from sbxloop.vcs.gitea.records import node_id_for
from sbxloop.vcs.model import (
    MergeOutcome,
    PostedFinding,
    QueueState,
    ReviewComment,
    ReviewThread,
    ReviewVerdict,
)
from tests.fakes.fake_gitea import FakeGitea

REPO = "acme/widgets"
R = "/repos/acme/widgets"


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def opened(fake: FakeGitea, **seed: object) -> FakeGitea:
    fake.seed_pull(1, head="sbxloop/r1", head_sha="commit0", **seed)  # type: ignore[arg-type]
    fake.trees["commit0"] = {**fake.trees["base123"], "a.py": ("100644", b"x = 1\ny = 2\nz = 3\n")}
    fake.commits["commit0"] = {"sha": "commit0", "parents": ["base123"], "message": "one"}
    return fake


class TestChange:
    def test_a_draft_is_the_wip_prefix_and_the_record_is_the_loops(self) -> None:
        fake = FakeGitea()
        fake.branches["sbxloop/r1"] = "base123"
        ref = fake.pr_create(REPO, "main", "sbxloop/r1", "sbxloop: ship it", "body", draft=True)
        assert ref.number == 1 and ref.url.endswith("/pulls/1")
        assert fake.pulls[1]["title"] == "WIP: sbxloop: ship it"
        record = fake.pr_get(REPO, 1)
        assert record["draft"] is True and record["node_id"] == node_id_for(REPO, 1)
        assert record["head"] == {"sha": "base123", "ref": "sbxloop/r1", "label": "sbxloop/r1"}
        assert record["base"]["ref"] == "main" and record["mergeable_state"] == "draft"
        assert record["state"] == "open" and record["merged"] is False
        (found,) = fake.pr_list_open(REPO, head="sbxloop/r1")
        assert found["number"] == 1
        assert fake.pr_list_open(REPO, head="other") == []
        with pytest.raises(GithubOpsError) as info:
            fake.pr_create(REPO, "main", "sbxloop/r1", "again")
        assert info.value.http_status == 409

    def test_update_comment_and_files(self) -> None:
        fake = opened(FakeGitea())
        updated = fake.pr_update(REPO, 1, title="new title", body="new body")
        assert updated["title"] == "new title" and fake.pull_patches == [
            (1, {"title": "new title", "body": "new body"})
        ]
        assert "#issuecomment-" in fake.pr_comment(REPO, 1, "hello")
        (entry,) = fake.pr_files(REPO, 1)
        assert entry["filename"] == "a.py" and entry["status"] == "added"
        assert entry["patch"].startswith("@@ -0,0 +1,3 @@")
        assert fake.text_calls == [f"{R}/pulls/1.diff"]

    def test_reviewers_ready_and_update_branch(self) -> None:
        fake = opened(FakeGitea(), title="WIP: sbxloop: ship it")
        fake.pr_request_reviewers(REPO, 1, ["rev-bob"])
        assert fake.reviewer_requests == [(1, ["rev-bob"])]
        with pytest.raises(GithubOpsError) as info:
            fake.pr_request_reviewers(REPO, 1, ["nobody"])
        assert info.value.http_status == 404
        fake.pr_request_reviewers(REPO, 1, [])
        assert fake.pr_ready_for_review(node_id_for(REPO, 1)) is True
        assert fake.pulls[1]["title"] == "sbxloop: ship it"
        assert fake.pr_ready_for_review(node_id_for(REPO, 1)) is True
        assert len(fake.pull_patches) == 1
        assert fake.pr_update_branch(REPO, 1, expected_head_sha="stale") is False
        assert fake.pr_update_branch(REPO, 1, expected_head_sha="commit0") is True
        assert fake.updates == [1]
        fake.update_ok = False
        assert fake.pr_update_branch(REPO, 1) is False


class TestReview:
    def test_a_review_with_inline_findings_is_githubs_shape(self) -> None:
        fake = opened(FakeGitea())
        finding = ReviewComment(path="a.py", line=3, body="[major] this leaks")
        review = fake.pr_review_create(REPO, 1, "REQUEST_CHANGES", "one leak", [finding])
        assert review.event == "REQUEST_CHANGES" and review.gates_merge
        (_number, posted_body) = fake.reviews_posted[0]
        assert posted_body["event"] == "REQUEST_CHANGES" and posted_body["commit_id"] == "commit0"
        assert posted_body["comments"] == [
            {"path": "a.py", "body": "[major] this leaks", "new_position": 3}
        ]
        (posted,) = review.posted
        assert isinstance(posted, PostedFinding) and posted.anchor == "a.py:3"
        assert posted.comment_id and posted.thread_id == f"{REPO}#1:{posted.comment_id}"
        left = ReviewComment(path="a.py", line=1, body="gone", side="LEFT")
        fake.pr_review_comments_create(REPO, 1, [left], commit_id="commit0")
        assert fake.reviews_posted[1][1]["comments"] == [
            {"path": "a.py", "body": "gone", "old_position": 1}
        ]
        with pytest.raises(GithubOpsError, match="no longer matches"):
            fake.pr_review_comments_create(REPO, 1, [left], commit_id="moved")

    def test_the_recorded_state_is_the_event_the_caller_reads(self) -> None:
        fake = opened(FakeGitea())
        own = fake.pr_review_create(REPO, 1, "APPROVE", "lgtm")
        assert own.event == "COMMENT", "the author's approval is a 422; it stands as a comment"
        assert [b["event"] for _n, b in fake.reviews_posted] == ["APPROVED", "COMMENT"]
        fake.user_login = "rev-bob"
        theirs = fake.pr_review_create(REPO, 1, "APPROVE", "lgtm")
        assert theirs.event == "APPROVE"
        assert fake.pr_review_state(REPO, 1) == "APPROVED"
        assert fake.pr_review_verdicts(REPO, 1) == (ReviewVerdict("rev-bob", "APPROVED", False),)

    def test_a_refused_review_falls_back_to_a_comment(self) -> None:
        fake = opened(FakeGitea())
        fake.fail_once["review_create"] = GithubOpsError("forbidden", http_status=403)
        review = fake.pr_review_create(REPO, 1, "REQUEST_CHANGES", "no")
        assert review.event == "COMMENT" and len(fake.reviews_posted) == 2
        fake.fail_once["review_create"] = GithubOpsError("forbidden", http_status=403)
        with pytest.raises(GithubOpsError):
            fake.pr_review_create(REPO, 1, "COMMENT", "no")

    def test_each_comment_is_its_own_thread_and_cannot_be_resolved(self) -> None:
        fake = opened(FakeGitea(bot_logins=["ci-bot"]))
        fake.seed_review(
            1,
            "REQUEST_CHANGES",
            author="rev-bob",
            body="needs work",
            comments=[("a.py", 3, "[major] no")],
        )
        fake.seed_review(1, "COMMENT", author="ci-bot", comments=[("a.py", 1, "nit")])
        threads = fake.pr_review_threads(REPO, 1)
        assert len(threads) == 2 and all(isinstance(t, ReviewThread) for t in threads)
        first = threads[0]
        assert first.anchor == "a.py:3" and not first.is_resolved
        assert first.comments[0].login == "rev-bob" and first.comments[0].is_bot is False
        assert threads[1].opened_by_bot, "the operator's list is the bot signal"
        comments = fake.pr_review_comments(REPO, 1)
        assert [(c["path"], c["line"], c["user"]["type"]) for c in comments] == [
            ("a.py", 3, "User"),
            ("a.py", 1, "Bot"),
        ]
        assert fake.resolve_review_thread(first.thread_id) is False
        with pytest.raises(GithubOpsError, match="not a Gitea thread id"):
            fake.resolve_review_thread("nope")
        url = fake.pr_comment_reply(REPO, 1, first.root_comment_id or 0, "fixed in the next push")
        assert "#issuecomment-" in url
        (reply,) = fake.comments[1]
        assert reply["body"] == "Re `a.py:3`:\n\nfixed in the next push"
        feedback = fake.pr_review_feedback(REPO, 1, exclude_login="ci-bot", exclude_is_bot=True)
        assert (
            "needs work" in feedback
            and "`a.py:3`: [major] no" in feedback
            and "nit" not in feedback
        )

    def test_review_locations_come_from_the_diff(self) -> None:
        fake = opened(FakeGitea())
        locations = fake.pr_review_locations(REPO, 1, commit_id="commit0")
        assert locations == {"a.py": (range(1, 4),)}
        with pytest.raises(GithubOpsError, match="no longer matches"):
            fake.pr_review_locations(REPO, 1, commit_id="moved")


class TestLanding:
    def test_a_merge_with_the_judged_head(self) -> None:
        fake = opened(FakeGitea())
        outcome = fake.pr_merge(REPO, 1, method="squash", sha="commit0", title="t", message="m")
        assert outcome.merged and outcome.reason == "merged" and outcome.sha.startswith("merge")
        (merge,) = fake.merges
        assert merge[1] == {
            "Do": "squash",
            "delete_branch_after_merge": False,
            "head_commit_id": "commit0",
            "MergeTitleField": "t",
            "MergeMessageField": "m",
        }
        assert fake.pr_get(REPO, 1)["merged"] is True and fake.pr_get(REPO, 1)["state"] == "closed"

    def test_the_refusals_are_data(self) -> None:
        draft = opened(FakeGitea(), title="WIP: x").pr_merge(REPO, 1)
        assert draft.blocked and "Work in progress" in draft.reason
        stale = opened(FakeGitea()).pr_merge(REPO, 1, sha="old")
        assert stale.stale and not stale.blocked
        fake = opened(FakeGitea())
        fake.branch_rules = {
            "required_approvals": 1,
            "enable_status_check": True,
            "status_check_contexts": ["ci"],
        }
        missing = fake.pr_merge(REPO, 1, sha="commit0")
        assert missing.blocked and "required status checks" in missing.reason
        fake.seed_status("commit0", "ci", "success")
        unapproved = fake.pr_merge(REPO, 1, sha="commit0")
        assert unapproved.blocked and "approvals" in unapproved.reason
        fake.seed_review(1, "APPROVED", author="rev-bob")
        assert fake.pr_merge(REPO, 1, sha="commit0").merged
        forbidden = opened(FakeGitea())
        forbidden.merge_ok = False
        assert forbidden.pr_merge(REPO, 1).blocked
        conflict = opened(FakeGitea(), mergeable=False).pr_merge(REPO, 1)
        assert conflict.blocked and "conflict" in conflict.reason.lower()

    def test_there_is_no_queue(self) -> None:
        fake = opened(FakeGitea())
        with pytest.raises(GithubOpsError, match="no merge queue"):
            fake.pr_enqueue(node_id_for(REPO, 1), head="commit0")
        assert fake.pr_queue_state(REPO, 1) == QueueState(False, False, None)
        fake.pr_merge(REPO, 1)
        state = fake.pr_queue_state(REPO, 1)
        assert state.merged and state.closed and state.merge_sha.startswith("merge")

    def test_a_merge_that_did_not_take_is_blocked(self) -> None:
        fake = opened(FakeGitea())
        fake.fail_once["pr_merge"] = GithubOpsError("boom", http_status=500)
        with pytest.raises(GithubOpsError):
            fake.pr_merge(REPO, 1)
        assert isinstance(fake.pr_merge(REPO, 1), MergeOutcome)


def deliver(fake: FakeGitea, branch: str, files: dict[str, bytes | None], message: str) -> str:
    base = fake.ref_lookup(REPO, "heads/main")
    assert base
    base_tree = str(fake.commit_get(REPO, base)["tree"]["sha"])
    uploads = {path: raw for path, raw in files.items() if raw is not None}
    shas = fake.blobs_create_many(
        REPO, [{"path": p, "content_b64": b64(raw)} for p, raw in uploads.items()]
    )
    entries = [
        {
            "path": path,
            "mode": "100644",
            "type": "blob",
            "sha": shas[path] if raw is not None else None,
        }
        for path, raw in files.items()
    ]
    tree = fake.tree_create(REPO, base_tree=base_tree, entries=entries)
    commit = fake.commit_create(REPO, message=message, tree=str(tree["sha"]), parents=[base])
    if fake.ref_lookup(REPO, f"heads/{branch}") is None:
        fake.ref_create(REPO, f"refs/heads/{branch}", str(commit["sha"]))
    else:
        fake.ref_force_update(REPO, branch, str(commit["sha"]))
    return str(commit["sha"])


class TestContent:
    def test_the_first_delivery_writes_one_changeset_on_a_pending_branch(self) -> None:
        fake = FakeGitea()
        sha = deliver(fake, "sbxloop/r7", {"hello.txt": b"hi\n", "README.md": None}, "deliver")
        assert fake.ref_lookup(REPO, "heads/sbxloop/r7") == sha
        assert fake.trees[sha] == {"hello.txt": ("100644", b"hi\n")}
        (post,) = fake.content_posts
        assert post["branch"].startswith("sbxloop/pending/") and post["message"] == "deliver"
        assert [(f["operation"], f["path"]) for f in post["files"]] == [
            ("create", "hello.txt"),
            ("delete", "README.md"),
        ]
        assert fake.branch_creates[0][1] == "base123", "the pending branch is cut at the parent"
        assert fake.branch_creates[1] == ("sbxloop/r7", sha)
        assert all(not b.startswith("sbxloop/pending/") for b in fake.branches)
        assert fake.commit_get(REPO, sha)["parents"] == [{"sha": "base123"}]

    def test_a_fix_round_commits_the_difference_on_top_of_the_branch(self) -> None:
        fake = FakeGitea()
        first = deliver(fake, "sbxloop/r7", {"hello.txt": b"hi\n", "old.txt": b"old\n"}, "deliver")
        fake.seed_pull(1, head="sbxloop/r7", head_sha=first)
        second = deliver(fake, "sbxloop/r7", {"hello.txt": b"hi again\n"}, "deliver again")
        head = fake.ref_lookup(REPO, "heads/sbxloop/r7")
        assert head and head not in (first, second)
        assert (
            fake.trees[head]
            == fake.trees[second]
            == {"README.md": ("100644", b"# widgets\n"), "hello.txt": ("100644", b"hi again\n")}
        )
        assert fake.commits[head]["parents"] == [first], "one more commit, not a moved branch"
        last = fake.content_posts[-1]
        assert last["branch"] == "sbxloop/r7" and last["message"] == "deliver again"
        assert [(f["operation"], f["path"]) for f in last["files"]] == [
            ("update", "hello.txt"),
            ("delete", "old.txt"),
        ]
        assert "sbxloop/r7" not in fake.deleted_branches and fake.pulls[1]["state"] == "open"
        assert not [b for b in fake.branches if b.startswith("sbxloop/pending/")]

    def test_refusals_by_name(self) -> None:
        fake = FakeGitea()
        with pytest.raises(GithubOpsError, match="submodule pointer"):
            fake.tree_create(
                REPO,
                base_tree="base123",
                entries=[{"path": "lib", "mode": "160000", "type": "commit", "sha": "x"}],
            )
        with pytest.raises(GithubOpsError, match="no symlink"):
            fake.tree_create(
                REPO,
                base_tree="base123",
                entries=[{"path": "l", "mode": "120000", "type": "blob", "sha": "x"}],
            )
        with pytest.raises(GithubOpsError, match="not staged"):
            fake.tree_create(
                REPO,
                base_tree="base123",
                entries=[{"path": "a", "mode": "100644", "type": "blob", "sha": "nope"}],
            )
        with pytest.raises(GithubOpsError, match="not staged"):
            fake.commit_create(REPO, message="m", tree="tree:nope", parents=["base123"])
        with pytest.raises(GithubOpsError, match="refs/heads/<branch>"):
            fake.ref_create(REPO, "heads/x", "base123")
        fake.branches["taken"] = "base123"
        with pytest.raises(GithubOpsError) as info:
            fake.ref_create(REPO, "refs/heads/taken", "base123")
        assert info.value.http_status == 409 and "already exists" in str(info.value)

    def test_a_branch_at_the_commit_a_missing_branch_and_a_foreign_commit(self) -> None:
        fake = FakeGitea()
        sha = deliver(fake, "sbxloop/r7", {"hello.txt": b"hi\n"}, "deliver")
        before = len(fake.content_posts)
        fake.ref_force_update(REPO, "sbxloop/r7", sha)
        assert len(fake.content_posts) == before
        fake.ref_force_update(REPO, "sbxloop/r8", "base123")
        assert fake.branches["sbxloop/r8"] == "base123"
        # A commit this backend did not write: its tree is read from Gitea.
        fake.ref_force_update(REPO, "sbxloop/r8", sha)
        head = fake.branches["sbxloop/r8"]
        assert fake.trees[head] == fake.trees[sha]

    def test_an_executable_lands_plain_and_says_so(self) -> None:
        fake = FakeGitea()
        shas = fake.blobs_create_many(
            REPO, [{"path": "run.sh", "content_b64": b64(b"#!/bin/sh\n")}]
        )
        tree = fake.tree_create(
            REPO,
            base_tree="base123",
            entries=[{"path": "run.sh", "mode": "100755", "type": "blob", "sha": shas["run.sh"]}],
        )
        staged = fake._trees[(REPO, str(tree["sha"]))]
        assert staged.wanted["run.sh"][0] == "100644"

    def test_contents_put_creates_then_replaces_and_cuts_a_new_branch(self) -> None:
        fake = FakeGitea()
        fake.branches["sbxloop/r1"] = "base123"
        written = fake.contents_put(
            REPO, "notes.md", message="add", content_b64=b64(b"# n\n"), branch="sbxloop/r1"
        )
        assert written["commit"]["sha"] == fake.branches["sbxloop/r1"]
        assert fake.content_posts[0]["files"][0]["operation"] == "create"
        fake.contents_put(
            REPO, "notes.md", message="again", content_b64=b64(b"# m\n"), branch="sbxloop/r1"
        )
        assert fake.content_posts[1]["files"][0]["operation"] == "update"
        fake.contents_put(
            REPO, "README.md", message="new branch", content_b64=b64(b"# r\n"), branch="feature"
        )
        assert (
            fake.content_posts[2]["new_branch"] == "feature"
            and fake.content_posts[2]["branch"] == "main"
        )
        assert fake.content_posts[2]["files"][0]["operation"] == "update", (
            "README.md exists on main"
        )
        empty = FakeGitea()
        empty.empty = True
        empty.branches.clear()
        empty.contents_put(
            REPO, "README.md", message="init", content_b64=b64(b"# r"), branch="main"
        )
        assert "new_branch" not in empty.content_posts[0] and empty.branches["main"]
