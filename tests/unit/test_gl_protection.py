"""Effective GitLab review requirements, including wildcard and MR rules."""

from __future__ import annotations

from sbxloop.config import LandingConfig
from sbxloop.engine.checks import check_policy_reader
from sbxloop.engine.landing import AwaitingReview, UpdateState, land
from tests.fakes.fake_gitlab import FakeGitlab

REPO = "acme/widgets"


def test_wildcard_protection_and_approval_rules_apply_to_the_base() -> None:
    fake = FakeGitlab()
    fake.enterprise = True
    fake.protected = {
        "name": "*",
        "code_owner_approval_required": True,
        "merge_access_levels": [{"access_level": 30}],
    }
    fake.approval_rules = [
        {
            "name": "Owners",
            "approvals_required": 2,
            "protected_branches": [{"name": "*"}],
        }
    ]
    requirements = fake.base_requirements(REPO, "main")
    assert requirements.code_owner_review
    assert requirements.approvals_required == 2


def test_mr_rules_park_for_the_missing_eligible_approval() -> None:
    fake = FakeGitlab()
    fake.enterprise = True
    fake.seed_mr(1, source_branch="feature", head_sha="head", detailed_merge_status="not_approved")
    fake.seed_status("head", "test", "success")
    fake.seed_approval(1, 3)
    fake.merge_requests[1]["approval_state"] = {
        "rules": [
            {
                "name": "Engineering",
                "approvals_required": 1,
                "approved": True,
                "approved_by": [fake._user(3)],
            },
            {"name": "Security", "approvals_required": 1, "approved": False, "approved_by": []},
        ]
    }
    cfg = LandingConfig()
    policy = check_policy_reader(fake, REPO, "main", cfg=cfg, number=1)
    result = land(
        fake,
        REPO,
        1,
        cfg=cfg,
        login=fake.user_login,
        branch="feature",
        node_id="1!1",
        update=UpdateState(),
        on_update=lambda _: None,
        tick=lambda _: None,
        emit=lambda *args, **kwargs: None,
        policy_for=policy,
    )
    assert isinstance(result, AwaitingReview)
    assert result.approvals_have == 0
    assert "Security" in result.wanted
    assert not fake.merges


def test_unreadable_approval_rules_are_named_and_fail_closed() -> None:
    fake = FakeGitlab()
    fake.enterprise = True
    fake.approval_rules = [{"approvals_required": "unreadable"}]
    requirements = fake.base_requirements(REPO, "main")
    assert requirements.approvals_required is None
    assert "approval_rules" in requirements.unread


def test_rules_on_later_pages_and_overlapping_wildcards_are_kept() -> None:
    fake = FakeGitlab()
    fake.enterprise = True
    fake.protected_rules = [
        {"name": f"unused-{n}", "code_owner_approval_required": False} for n in range(100)
    ] + [
        {
            "name": "*",
            "code_owner_approval_required": True,
            "merge_access_levels": [{"access_level": 0}],
        },
        {"name": "main", "merge_access_levels": [{"access_level": 30}]},
    ]
    fake.approval_rules = [{"approvals_required": 0} for _ in range(100)] + [
        {"approvals_required": 3}
    ]
    req = fake.base_requirements(REPO, "main")
    assert req.approvals_required == 3 and req.code_owner_review
    assert not req.extra_blockers
    assert any("protected_branches?per_page=100&page=2" in path for _, path, _ in fake.raw_calls)


def test_approval_progress_is_refreshed_even_when_the_head_does_not_move() -> None:
    fake = FakeGitlab()
    fake.enterprise = True
    fake.seed_mr(1, source_branch="feature", head_sha="head")
    rule = {"name": "Security", "approvals_required": 1, "approved": False, "approved_by": []}
    fake.merge_requests[1]["approval_state"] = {"rules": [rule]}
    policy = check_policy_reader(fake, REPO, "main", cfg=LandingConfig(), number=1)
    before = policy("head").requirements.approval_rules
    assert before and not before[0].satisfied
    rule.update(approved=True, approved_by=[fake._user(3)])
    after = policy("head").requirements.approval_rules
    assert after and after[0].satisfied and after[0].have == 1
