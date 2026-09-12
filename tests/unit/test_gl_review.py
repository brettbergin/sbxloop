"""The GitLab backend's review threads and merge-request records (#1018):
discussions as threads with an opaque id, replies and resolution by
that id, approvals and reviewer states as review records, the bot flag
one lookup away, and request-changes degrading to a comment."""

from __future__ import annotations

import pytest

from sbxloop.errors import GithubOpsError, RoleNotImplemented
from sbxloop.vcs.gitlab.changes import (
    change_record,
    file_record,
    merge_state,
    parse_thread_id,
    thread_id_for,
)
from sbxloop.vcs.gitlab.ops import GitlabOps
from sbxloop.vcs.model import PostedFinding, ReviewComment, ReviewThread, SubmittedReview
from tests.fakes.fake_gitlab import FakeGitlab

REPO = "acme/widgets"
MR = "/projects/acme%2Fwidgets/merge_requests"


def ready(fake: FakeGitlab, iid: int = 1) -> FakeGitlab:
    fake.seed_mr(iid, source_branch="sbxloop/r1", head_sha="commit0")
    return fake


class TestFolds:
    def test_a_merge_request_is_the_loops_change_record(self) -> None:
        record = change_record(
            {
                "id": 5,
                "iid": 5,
                "project_id": 1,
                "title": "Draft: matrix",
                "description": None,
                "state": "opened",
                "draft": True,
                "source_branch": "matrix/1",
                "target_branch": "main",
                "sha": "6706251",
                "merge_commit_sha": None,
                "author": {"id": 2, "username": "dev-alice"},
                "reviewers": [{"id": 3, "username": "rev-bob"}],
                "detailed_merge_status": "draft_status",
                "diff_refs": {
                    "base_sha": "33da92ad",
                    "start_sha": "33da92ad",
                    "head_sha": "6706251",
                },
                "web_url": "https://localhost:8929/acme/widgets/-/merge_requests/5",
            }
        )
        assert record["number"] == 5 and record["node_id"] == "1!5"
        assert record["state"] == "open" and record["merged"] is False
        assert record["draft"] is True and record["mergeable_state"] == "draft"
        assert record["head"] == {"sha": "6706251", "ref": "matrix/1"}
        assert record["base"] == {"ref": "main", "sha": "33da92ad"}
        assert record["user"] == {"login": "dev-alice", "id": 2}
        assert record["requested_reviewers"] == [{"login": "rev-bob", "id": 3}]
        assert record["body"] == ""

    def test_merged_and_closed_states(self) -> None:
        merged = change_record({"iid": 1, "state": "merged", "merge_commit_sha": "m1"})
        assert merged["merged"] is True and merged["state"] == "closed"
        assert merged["merge_commit_sha"] == "m1"
        closed = change_record({"iid": 1, "state": "closed"})
        assert closed["merged"] is False and closed["state"] == "closed"

    @pytest.mark.parametrize(
        ("detailed", "expected"),
        [
            ("preparing", (None, "")),
            ("checking", (None, "")),
            ("mergeable", (True, "clean")),
            ("need_rebase", (True, "behind")),
            ("conflict", (False, "dirty")),
            ("draft_status", (True, "draft")),
            ("ci_must_pass", (True, "blocked")),
            ("discussions_not_resolved", (True, "blocked")),
            ("requested_changes", (True, "blocked")),
        ],
    )
    def test_the_detailed_merge_status_as_the_landing_reads_it(
        self, detailed: str, expected: tuple[bool | None, str]
    ) -> None:
        assert merge_state(detailed) == expected

    def test_a_diff_entry_is_a_file_record(self) -> None:
        record = file_record(
            {
                "old_path": "one.txt",
                "new_path": "one.txt",
                "diff": "@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n",
                "new_file": False,
                "renamed_file": False,
                "deleted_file": False,
            }
        )
        assert record["filename"] == "one.txt" and record["status"] == "modified"
        assert record["additions"] == 1 and record["deletions"] == 1
        assert record["patch"].startswith("@@")
        assert file_record({"new_path": "n", "new_file": True})["status"] == "added"

    def test_the_thread_id_round_trips(self) -> None:
        thread_id = thread_id_for("acme/widgets", 2, "193bff4c")
        assert parse_thread_id(thread_id) == ("acme/widgets", 2, "193bff4c")
        with pytest.raises(ValueError):
            parse_thread_id("PRRT_1")


class TestMergeRequests:
    def test_create_get_and_list(self) -> None:
        fake = FakeGitlab()
        ref = fake.pr_create(REPO, "main", "sbxloop/r1", "sbxloop: ship it", "body", draft=True)
        assert ref.number == 1 and ref.url.endswith("/-/merge_requests/1")
        assert fake.mr_created == [
            {
                "source_branch": "sbxloop/r1",
                "target_branch": "main",
                "title": "Draft: sbxloop: ship it",
                "description": "body",
            }
        ]
        change = fake.pr_get(REPO, 1)
        assert change["draft"] is True and change["head"]["sha"] == "commit0"
        assert change["node_id"] == "1!1" and change["state"] == "open"
        (found,) = fake.pr_list_open(REPO, head="sbxloop/r1")
        assert found["number"] == 1
        assert fake.pr_list_open(REPO, head="other") == []
        query = fake.raw_calls[-1][1]
        assert "state=opened" in query and "source_branch=other" in query

    def test_a_second_open_request_from_the_branch_is_a_409(self) -> None:
        fake = ready(FakeGitlab())
        with pytest.raises(GithubOpsError) as info:
            fake.pr_create(REPO, "main", "sbxloop/r1", "again")
        assert info.value.http_status == 409

    def test_update_comment_and_files(self) -> None:
        fake = ready(FakeGitlab())
        fake.pr_update(REPO, 1, title="renamed", body="b")
        assert fake.mr_updates == [(1, {"title": "renamed", "description": "b"})]
        url = fake.pr_comment(REPO, 1, "hello")
        assert url.endswith("/-/merge_requests/1#note_1")
        (entry,) = fake.pr_files(REPO, 1)
        assert entry["filename"] == "hello.txt" and entry["patch"].startswith("@@")

    def test_the_remote_commit_is_still_not_implemented(self) -> None:
        fake = ready(FakeGitlab())
        with pytest.raises(RoleNotImplemented) as info:
            fake.commit_get(REPO, "base123")
        assert info.value.operation == "commit_get"
        assert "ContentOps.commit_get" in GitlabOps.UNIMPLEMENTED_OPERATIONS
        assert "ReviewOps" not in " ".join(GitlabOps.UNIMPLEMENTED_OPERATIONS)


class TestReviewThreads:
    def test_a_review_with_an_inline_finding_then_reply_and_resolve(self) -> None:
        fake = ready(FakeGitlab())
        finding = ReviewComment(path="hello.txt", line=1, body="[major] this leaks")
        review = fake.pr_review_create(REPO, 1, "COMMENT", "one leak", [finding])
        assert isinstance(review, SubmittedReview) and review.event == "COMMENT"
        (posted,) = review.posted
        assert isinstance(posted, PostedFinding) and posted.anchor == "hello.txt:1"
        assert posted.comment_id and posted.thread_id
        assert fake.discussions_posted == [(1, "hello.txt:1", "[major] this leaks")]
        assert fake.mr_notes_posted == [(1, "one leak")]
        # The position carries the merge request's three shas and the line.
        position = next(c for c in fake.raw_calls if c[1] == f"{MR}/1/discussions")[2]["position"]
        assert position["head_sha"] == "commit0" and position["new_line"] == 1
        assert position["new_path"] == "hello.txt" and position["position_type"] == "text"

        (thread,) = fake.pr_review_threads(REPO, 1)
        assert isinstance(thread, ReviewThread) and thread.thread_id == posted.thread_id
        assert thread.anchor == "hello.txt:1" and not thread.is_resolved
        assert thread.comments[0].login == "sbxloop-bot" and thread.comments[0].is_bot is False

        fake.pr_comment_reply(REPO, 1, posted.comment_id, "fixed in the next push")
        assert fake.replies[0][2] == "fixed in the next push"
        (thread,) = fake.pr_review_threads(REPO, 1)
        assert len(thread.comments) == 2 and thread.has_reply_from("sbxloop-bot", False)

        assert fake.resolve_review_thread(posted.thread_id) is True
        (thread,) = fake.pr_review_threads(REPO, 1)
        assert thread.is_resolved
        assert fake.raw_calls[-2][:2] == ("PUT", f"{MR}/1/discussions/{fake.resolved[0][0]}")

    def test_a_left_side_anchor_positions_the_old_line(self) -> None:
        fake = ready(FakeGitlab())
        finding = ReviewComment(path="hello.txt", line=1, body="gone", side="LEFT")
        fake.pr_review_comments_create(REPO, 1, [finding], commit_id="commit0")
        position = fake.raw_calls[-1][2]["position"]
        assert position["old_line"] == 1 and "new_line" not in position

    def test_a_refused_position_fails_its_own_finding_only(self) -> None:
        fake = ready(FakeGitlab())
        fake.refuse_positions.add("hello.txt:9")
        posted = fake.pr_review_comments_create(
            REPO,
            1,
            [
                ReviewComment(path="hello.txt", line=9, body="off the diff"),
                ReviewComment(path="hello.txt", line=1, body="on it"),
            ],
            commit_id="commit0",
        )
        assert posted[0] == PostedFinding("hello.txt:9")
        assert posted[1].comment_id is not None and posted[1].thread_id

    def test_a_moved_head_refuses_every_anchor(self) -> None:
        fake = ready(FakeGitlab())
        with pytest.raises(GithubOpsError, match="no longer matches"):
            fake.pr_review_comments_create(
                REPO, 1, [ReviewComment(path="hello.txt", line=1, body="x")], commit_id="stale"
            )

    def test_no_diff_refs_yet_is_named(self) -> None:
        fake = ready(FakeGitlab())
        fake.diff_refs_pending = True
        with pytest.raises(GithubOpsError, match="no diff refs yet"):
            fake.pr_review_create(
                REPO, 1, "COMMENT", "b", [ReviewComment(path="hello.txt", line=1, body="x")]
            )

    def test_review_locations_are_the_right_side_ranges(self) -> None:
        fake = ready(FakeGitlab())
        assert fake.pr_review_locations(REPO, 1, commit_id="commit0") == {
            "hello.txt": (range(1, 4),)
        }
        with pytest.raises(GithubOpsError, match="no longer matches"):
            fake.pr_review_locations(REPO, 1, commit_id="other")

    def test_a_reply_finds_the_discussion_it_never_saw(self) -> None:
        fake = ready(FakeGitlab())
        discussion_id, note_id = fake.seed_discussion(1, "hello.txt", 2, "Why two?", author_id=3)
        url = fake.pr_comment_reply(REPO, 1, note_id, "because")
        assert fake.replies == [(1, discussion_id, "because")]
        assert url.endswith(f"#note_{note_id + 1}")
        with pytest.raises(GithubOpsError, match="not on a discussion"):
            fake.pr_comment_reply(REPO, 1, 999, "lost")

    def test_plain_notes_are_not_threads(self) -> None:
        fake = ready(FakeGitlab())
        fake.pr_issue_comment(REPO, 1, "a summary")
        fake.seed_discussion(1, "", None, "a plain note", author_id=3)
        assert fake.pr_review_threads(REPO, 1) == []
        assert fake.pr_review_comments(REPO, 1) == []


class TestVerdicts:
    def test_approvals_and_requested_changes_fold_to_review_records(self) -> None:
        fake = ready(FakeGitlab())
        fake.seed_approval(1, 3)
        fake.seed_reviewer_state(1, 4, "requested_changes")
        records = fake.pr_reviews(REPO, 1)
        assert [(r["user"]["login"], r["state"]) for r in records] == [
            ("rev-bob", "APPROVED"),
            ("project_1_bot_05b4", "CHANGES_REQUESTED"),
        ]
        assert records[0]["user"]["type"] == "User" and records[1]["user"]["type"] == "Bot"
        assert records[1]["id"] == "reviewer-4-requested_changes"
        verdicts = fake.pr_review_verdicts(REPO, 1, exclude=("sbxloop-bot", False))
        assert {(v.login, v.state, v.is_bot) for v in verdicts} == {
            ("rev-bob", "APPROVED", False),
            ("project_1_bot_05b4", "CHANGES_REQUESTED", True),
        }
        assert fake.pr_review_state(REPO, 1) == "CHANGES_REQUESTED"
        assert fake.pr_review_state(REPO, 1, login="rev-bob") == "APPROVED"

    def test_a_reviewer_who_requested_changes_then_approved_stands_approved(self) -> None:
        fake = ready(FakeGitlab())
        fake.seed_reviewer_state(1, 3, "reviewed")
        fake.seed_approval(1, 3)
        assert fake.pr_review_state(REPO, 1, login="rev-bob") == "APPROVED"

    def test_the_review_comment_records_and_feedback(self) -> None:
        fake = ready(FakeGitlab())
        fake.seed_discussion(1, "hello.txt", 2, "Why two?", author_id=3)
        fake.seed_discussion(1, "hello.txt", 3, "mine", author_id=2)
        (theirs, mine) = fake.pr_review_comments(REPO, 1)
        assert theirs["user"]["login"] == "rev-bob" and theirs["path"] == "hello.txt"
        assert theirs["line"] == 2 and theirs["original_line"] == 2
        assert mine["user"]["login"] == "sbxloop-bot"
        feedback = fake.pr_review_feedback(
            REPO, 1, exclude_login="sbxloop-bot", exclude_is_bot=False
        )
        assert feedback == "- `hello.txt:2`: Why two?"

    def test_the_bot_flag_is_read_once_per_user(self) -> None:
        fake = ready(FakeGitlab())
        fake.seed_discussion(1, "hello.txt", 2, "beep", author_id=4)
        fake.seed_discussion(1, "hello.txt", 3, "boop", author_id=4)
        threads = fake.pr_review_threads(REPO, 1)
        assert all(t.opened_by_bot for t in threads)
        assert sum(1 for c in fake.raw_calls if c[1] == "/users/4") == 1


class TestSubmittedVerdicts:
    def test_an_approval_is_posted_with_the_head(self) -> None:
        fake = ready(FakeGitlab())
        review = fake.pr_review_create(REPO, 1, "APPROVE", "looks right")
        assert review.event == "APPROVE" and review.gates_merge
        assert fake.approvals_posted == [(1, {"sha": "commit0"})]
        assert fake.pr_review_state(REPO, 1, login="sbxloop-bot") == "APPROVED"

    def test_a_refused_approval_stands_as_a_comment(self) -> None:
        fake = ready(FakeGitlab())
        fake.approve_ok = False
        review = fake.pr_review_create(REPO, 1, "APPROVE", "looks right")
        assert review.event == "COMMENT" and not review.gates_merge
        assert fake.mr_notes_posted == [(1, "looks right")]

    def test_request_changes_degrades_to_a_comment_with_the_count(self) -> None:
        fake = ready(FakeGitlab())
        findings = [
            ReviewComment(path="hello.txt", line=1, body="a"),
            ReviewComment(path="hello.txt", line=2, body="b"),
        ]
        review = fake.pr_review_create(REPO, 1, "REQUEST_CHANGES", "two leaks", findings)
        assert review.event == "COMMENT" and len(review.inline) == 2
        (note,) = fake.mr_notes_posted
        assert note[1].startswith("two leaks") and "2 finding(s) inline" in note[1]
        assert fake.approvals_posted == []
