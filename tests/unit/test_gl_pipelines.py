"""Merge request checks follow the pipeline's tested commit and pipeline ID."""

from __future__ import annotations

import pytest

from sbxloop.config import LandingConfig
from sbxloop.engine.landing import Landed, NeedsFix, UpdateState, land
from sbxloop.errors import GithubOpsError
from tests.fakes.fake_gitlab import FakeGitlab

REPO = "acme/widgets"


def merged_pipeline(status: str = "failed") -> FakeGitlab:
    fake = FakeGitlab()
    fake.seed_mr(
        1, source_branch="feature", head_sha="source", detailed_merge_status="ci_must_pass"
    )
    fake.merge_requests[1]["head_pipeline"] = {
        "id": 7,
        "sha": "merged-result",
        "status": status,
        "web_url": "https://gitlab.example/acme/widgets/-/pipelines/7",
    }
    fake.commits["merged-result"] = {"id": "merged-result", "parent_ids": ["base123", "source"]}
    fake.seed_status("source", "test", "success")
    fake.seed_status("merged-result", "test", status, pipeline_id=7, trace="merged result failed")
    fake.seed_status("merged-result", "test", "success", pipeline_id=8)
    return fake


def test_merged_results_failure_enters_the_ci_fix_loop_with_its_log() -> None:
    fake = merged_pipeline()
    result = land(
        fake,
        REPO,
        1,
        cfg=LandingConfig(),
        login=fake.user_login,
        branch="feature",
        node_id="1!1",
        update=UpdateState(),
        on_update=lambda _: None,
        tick=lambda _: None,
        emit=lambda *args, **kwargs: None,
    )
    assert isinstance(result, NeedsFix) and result.kind == "ci"
    assert result.failed_checks[0].excerpt == "merged result failed"
    assert not fake.merges


@pytest.mark.parametrize(
    "status, state", [("running", "pending"), ("failed", "red"), ("success", "green")]
)
def test_change_checks_are_scoped_to_the_mr_pipeline(status: str, state: str) -> None:
    fake = merged_pipeline(status)
    assert fake.change_checks(REPO, 1, "source").state == state
    assert fake.pr_checks(REPO, "source").state == "green"


def test_pending_pipeline_cannot_disappear_when_jobs_have_not_registered() -> None:
    fake = merged_pipeline("running")
    fake.statuses["merged-result"] = []
    assert fake.change_checks(REPO, 1, "source").state == "pending"


def test_an_old_pipeline_cannot_validate_a_new_source_head() -> None:
    fake = merged_pipeline("success")
    fake.merge_requests[1]["sha"] = "new-source"
    assert fake.change_checks(REPO, 1, "new-source").state == "pending"


def test_landing_waits_for_the_merged_pipeline_and_then_merges() -> None:
    fake = merged_pipeline("running")
    fake.merge_requests[1]["detailed_merge_status"] = "ci_still_running"
    ticks = []

    def tick(reason: str) -> None:
        ticks.append(reason)
        fake.merge_requests[1]["head_pipeline"]["status"] = "success"
        fake.merge_requests[1]["detailed_merge_status"] = "mergeable"
        fake.statuses["merged-result"][0]["status"] = "success"

    result = land(
        fake,
        REPO,
        1,
        cfg=LandingConfig(),
        login=fake.user_login,
        branch="feature",
        node_id="1!1",
        update=UpdateState(),
        on_update=lambda _: None,
        tick=tick,
        emit=lambda *args, **kwargs: None,
    )
    assert isinstance(result, Landed)
    assert ticks and len(fake.merges) == 1


def test_an_unreadable_pipeline_fails_closed() -> None:
    fake = merged_pipeline()
    fake.merge_requests[1]["head_pipeline"] = {"status": "success"}
    with pytest.raises(GithubOpsError, match="incomplete merge request pipeline"):
        fake.change_checks(REPO, 1, "source")
