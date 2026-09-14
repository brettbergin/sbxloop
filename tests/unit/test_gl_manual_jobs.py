"""Optional manual GitLab jobs do not require a maintainer's action."""

import pytest

from sbxloop.config import LandingConfig
from sbxloop.engine.landing import Landed, UpdateState, land
from sbxloop.vcs.gitlab.records import check_run_record, fold_statuses
from tests.fakes.fake_gitlab import FakeGitlab


@pytest.mark.parametrize("allowed", [True, False, None])
def test_only_explicitly_optional_manual_jobs_pass(allowed: bool | None) -> None:
    row = {"id": 1, "name": "deploy", "status": "manual", "allow_failure": allowed}
    verdict = fold_statuses([row])
    record = check_run_record(row)
    if allowed is True:
        assert verdict.state == "green" and verdict.passed == ("deploy",)
        assert not verdict.needs_approval
        assert record["conclusion"] == "neutral"
    else:
        assert verdict.state == "pending" and verdict.needs_approval == ("deploy",)
        assert record["conclusion"] == "action_required"


def test_an_optional_manual_deploy_does_not_hold_landing() -> None:
    fake = FakeGitlab()
    fake.seed_mr(1, source_branch="feature", head_sha="source")
    fake.seed_status("source", "test", "success")
    fake.seed_status("source", "deploy", "manual")
    fake.statuses["source"][-1]["allow_failure"] = True
    result = land(
        fake,
        fake.repo,
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
    assert isinstance(result, Landed)
    assert len(fake.merges) == 1
