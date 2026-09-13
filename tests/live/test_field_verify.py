"""The field answers of #1016, held against live forges.

Each test runs one probe from :mod:`tests.live.fieldverify` and asserts the
facts a decision in ``docs/spikes/1009-vcs-backend-abstraction.md`` rests
on, so a forge upgrade that changes one fails here rather than in a run.
Every test skips, naming what is missing, unless a live forge is configured
(``tests/live/README.md``). Facts that hold only on GitLab's free tier are
asserted only when the instance says it is not an enterprise edition.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.live.env import live_forge
from tests.live.fieldverify import run


def probe(kind: str, question: str) -> dict[str, Any]:
    forge = live_forge(kind)
    if isinstance(forge, str):
        pytest.skip(forge)
    return run(kind, question).findings


class TestGitlab:
    def test_v1_a_discussion_resolves_by_api_and_keeps_its_id_across_pushes(self) -> None:
        found = probe("gitlab", "V1")
        assert found["resolvable"] is True and found["resolved_by_api"] is True
        assert found["blocking_discussions_resolved_after_resolve"] is True
        assert found["discussion_id_kind"] == "40-hex string"
        assert found["id_stable_after_push"] and found["resolved_after_push"]
        assert found["id_stable_after_line_rewrite"] and found["resolved_after_line_rewrite"]
        assert found["reopenable_by_reviewer"] is True

    def test_v2_a_developer_reads_what_the_base_requires(self) -> None:
        found = probe("gitlab", "V2")
        assert found["developer_reads_protected_branch"] == 200
        assert found["push_access_levels"] == [0]
        assert found["developer_reads_merge_settings"]["only_allow_merge_if_pipeline_succeeds"]
        states = found["detailed_merge_status"]
        assert states["no_status"] == "ci_must_pass"
        assert states["ci_running"] == "ci_still_running"
        # No named required checks: a context that never reported does not
        # hold the merge, and any red status in the pipeline does.
        assert states["ci_green_lint_absent"] == "mergeable"
        assert states["allowed_failure_red"] == "ci_must_pass"
        assert states["lint_red"] == "ci_must_pass"
        assert found["merge_refused_status"] == 405
        if found["enterprise"] is False:
            assert set(found["premium_endpoints"].values()) == {404}
            assert found["mr_approvals_status"] == 200

    def test_v3_the_bot_flag_is_on_the_user_not_on_the_author(self) -> None:
        found = probe("gitlab", "V3")
        assert found["bot_username_pattern"] is True
        assert found["users_id_bot_flag_for_bot"] is True
        assert found["users_id_bot_flag_for_human"] is False
        assert found["note_author_carries_bot"] is False
        assert found["users_search_carries_bot"] is False

    def test_v5_a_commit_without_a_checkout(self) -> None:
        found = probe("gitlab", "V5")
        assert found["multi_file_new_branch"] == 201 and found["delete_and_move"] == 201
        assert found["binary_round_trip"] and found["second_commit_parent_is_first"]
        assert found["partial_failure_status"] == 400
        assert (
            found["partial_failure_left_branch_unmoved"] and found["partial_failure_wrote_nothing"]
        )
        assert found["protected_base_status"] == 403

    def test_v6_a_token_reads_its_own_expiry(self) -> None:
        found = probe("gitlab", "V6")
        assert found["pat_self_status"] == 200 and found["pat_self_expires_at"]
        assert found["project_token_self_status"] == 200 and found["project_token_self_expires_at"]
        assert found["project_access_tokens_list_as_the_token"] in (401, 403)

    def test_the_rest_of_the_matrix(self) -> None:
        found = probe("gitlab", "matrix")
        assert found["draft_from_title_prefix"] is True
        assert found["draft_status_blocks"] == "draft_status"
        assert found["draft_cleared_by_retitle"] is True
        assert found["reviewer_state"] == ["requested_changes"]
        assert found["api_commit_signature"] == 404
        if found["merge_trains_endpoint"] == 404:  # the free tier
            assert found["author_approves_own"] == 201 and found["author_approval_counted"]
            assert found["request_changes_blocks_merge"] is False


class TestGitea:
    def test_v1_review_comments_cannot_be_resolved_or_replied_to_by_api(self) -> None:
        found = probe("gitea", "V1")
        assert found["comment_has_resolver_field"] is True
        assert found["api_paths_naming_resolve"] == []
        assert found["review_comment_reply_path"] is False

    def test_v3_a_bot_account_reads_as_a_human(self) -> None:
        found = probe("gitea", "V3")
        assert found["bot_and_human_keys_identical"] is True
        assert found["user_schema_has_type"] is False
        assert found["actions_user_lookup_status"] == 404

    def test_v4_the_required_set_is_readable_without_admin(self) -> None:
        found = probe("gitea", "V4")
        assert found["branch_protections_list_as_write"] == 403
        assert found["branch_protection_get_as_write"] == 403
        view = found["branch_view_as_write"]
        assert view["required_approvals"] == 1
        assert view["enable_status_check"] is True
        assert view["status_check_contexts"] == ["ci", "lint"]
        absent = found["observed"]["ci green, lint never reported, an unrequired docs green"]
        assert absent["combined_state"] == "success"
        assert absent["merge_status"] == 405
        assert absent["merge_message"] == "Not all required status checks successful"

    def test_v5_a_commit_without_a_checkout(self) -> None:
        found = probe("gitea", "V5")
        assert found["multi_file_new_branch"] == 201 and found["delete_and_move"] == 201
        assert found["binary_round_trip"] and found["second_commit_parent_is_first"]
        assert not 200 <= found["partial_failure_status"] < 300
        assert (
            found["partial_failure_left_branch_unmoved"] and found["partial_failure_wrote_nothing"]
        )
        assert found["protected_base_status"] == 403
        assert found["single_file_contents_path"] == 201

    def test_v6_a_token_has_no_expiry_and_cannot_read_itself(self) -> None:
        found = probe("gitea", "V6")
        assert found["token_reads_own_tokens"] == 401
        assert found["expiry_in_token_schema"] is False
        assert found["expiry_settable_at_creation"] is False

    def test_the_rest_of_the_matrix(self) -> None:
        found = probe("gitea", "matrix")
        assert found["queue_paths"] == []
        assert found["draft_create_field"] is False
        assert found["draft_from_title_prefix"] is True and found["draft_cleared_by_retitle"]
        assert found["unknown_event_status"] == 200 and found["unknown_event_becomes"] == "PENDING"
        assert found["author_approves_own"] == 422
        assert found["merge_with_requested_changes"] == 405
        assert found["merge_refusal_message"] == "There are requested changes"
        assert found["api_commit_verification"]["verified"] is False
