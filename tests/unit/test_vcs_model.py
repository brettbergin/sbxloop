"""The shared domain model speaks no forge's dialect (#1012).

Four words had leaked from the first backend into the types every consumer
reads: a GraphQL thread node id, a merge-queue entry state in GitHub's own
enumeration, a review anchor's side, and an issue's close reason. Each is
neutral now — an opaque thread id, a fixed state vocabulary the backend
maps into, an input the backend resolves, a reason the backend spells —
and the base's blockers are phrased in the reading forge's own terms."""

from __future__ import annotations

from typing import get_args

import pytest

from sbxloop.vcs.github.ops import fold_queue_entry
from sbxloop.vcs.model import (
    BLOCKER_WORDING,
    GENERIC_WORDING,
    BaseRequirements,
    CloseReason,
    PostedFinding,
    QueueEntryState,
    ReviewThread,
)


class TestThreadIds:
    def test_a_finding_and_a_thread_carry_one_opaque_id(self) -> None:
        finding = PostedFinding("a.py:3", 1, "PRRT_1")
        thread = ReviewThread("PRRT_1", False, "a.py", 3)
        assert finding.thread_id == thread.thread_id == "PRRT_1"
        assert not hasattr(finding, "thread_node_id")
        assert not hasattr(thread, "node_id")


class TestQueueEntryState:
    @pytest.mark.parametrize(
        ("github", "ours"),
        [
            ("QUEUED", "queued"),
            ("AWAITING_CHECKS", "testing"),
            ("MERGEABLE", "mergeable"),
            ("LOCKED", "mergeable"),
            ("UNMERGEABLE", "blocked"),
        ],
    )
    def test_github_states_fold_into_the_neutral_vocabulary(self, github: str, ours: str) -> None:
        entry = fold_queue_entry({"id": "MQE_1", "state": github})
        assert entry is not None and entry.state == ours
        assert ours in get_args(QueueEntryState)

    def test_a_state_the_backend_does_not_know_is_unknown_never_mergeable(self) -> None:
        entry = fold_queue_entry({"id": "MQE_1", "state": "SOMETHING_NEW"})
        assert entry is not None and entry.state == "unknown"
        entry = fold_queue_entry({"id": "MQE_1"})
        assert entry is not None and entry.state == "unknown"


class TestCloseReason:
    def test_the_vocabulary_is_the_two_reasons_a_run_gives(self) -> None:
        assert get_args(CloseReason) == ("completed", "not_planned")


class TestBlockerWording:
    REQ = BaseRequirements(
        (),
        1,
        "protection",
        code_owner_review=True,
        last_push_approval=True,
        signed_commits=True,
    )

    def test_github_keeps_the_words_the_loop_has_always_used(self) -> None:
        reasons = self.REQ.blockers()
        assert any("(require_last_push_approval)" in r for r in reasons)
        assert any("(CODEOWNERS)" in r for r in reasons)
        assert any(
            "GitHub signs commits the loop creates through its API only when it "
            "authenticates as a GitHub App" in r
            for r in reasons
        )

    def test_another_forge_gets_its_own_or_the_generic_wording(self) -> None:
        elsewhere = self.REQ._replace(forge="bitbucket")
        reasons = elsewhere.blockers()
        assert not any("GitHub" in r or "CODEOWNERS" in r for r in reasons)
        assert any(f"({GENERIC_WORDING.code_owners_file})" in r for r in reasons)
        assert any(GENERIC_WORDING.signing in r for r in reasons)
        assert "bitbucket" not in BLOCKER_WORDING

    def test_gitea_is_read_in_its_own_words(self) -> None:
        reasons = self.REQ._replace(forge="gitea").blockers()
        assert not any("GitHub" in r for r in reasons)
        assert any("Gitea does not sign" in r for r in reasons)
        assert any("dismiss stale approvals" in r for r in reasons)

    def test_gitlab_is_read_in_its_own_words(self) -> None:
        reasons = self.REQ._replace(forge="gitlab").blockers()
        assert not any("GitHub" in r for r in reasons)
        assert any(BLOCKER_WORDING["gitlab"].signing in r for r in reasons)
        assert any("(CODEOWNERS)" in r for r in reasons)

    def test_a_forge_gating_on_the_whole_pipeline_names_no_context(self) -> None:
        # GitLab's "pipeline must succeed" (#1016 V2): nothing is named,
        # everything reported is required, and the flag says so.
        whole = BaseRequirements((), 0, "protected_branch+project", all_checks_required=True)
        assert whole.required_contexts == () and whole.all_checks_required
        assert BaseRequirements((), 0, "none").all_checks_required is False

    def test_the_forge_defaults_to_github_so_nothing_read_before_changes(self) -> None:
        assert BaseRequirements((), 0, "none").forge == "github"
        assert BaseRequirements((), 0, "none") == BaseRequirements((), 0, "none", forge="github")
