"""Campaign GitHub transitions keep ordinary source claims authoritative."""

from __future__ import annotations

from typing import Any

import pytest

from sbxloop.daemon.campaign_source import (
    CampaignSource,
    CampaignSourceError,
    source_for_campaign,
)
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.sources import (
    CLAIM_MARKER,
    ChatSource,
    CompositeSource,
    GitHubIssueSource,
    GitHubLabels,
    MultiRepoIssueSource,
)
from sbxloop.engine.model import RunRecord
from sbxloop.errors import GithubOpsError
from tests.fakes.fake_github import FakeGithub

REPO = "customer/project"
LABELS = GitHubLabels("custom:run", "custom:active", "custom:failed", workload="custom:work")
NOW = "2026-09-07T12:00:00Z"
OLD = "2026-09-06T12:00:00Z"


def item(**overrides: Any) -> WorkItem:
    return WorkItem.model_validate(
        {"item_id": "gh:issue:3", "source_key": "3", "repo": REPO, "title": "Work", **overrides}
    )


def make(*labels: str) -> tuple[CampaignSource, FakeGithub]:
    ops = FakeGithub(repo=REPO)
    ops.issue_payloads[(REPO, 3)] = {
        "number": 3,
        "state": "open",
        "labels": [{"name": label} for label in labels],
    }
    source = GitHubIssueSource(lambda: ops, REPO, LABELS, host="test", pid=99)
    return CampaignSource(source), ops


def claim(created: str = NOW, body: str | None = None) -> dict[str, Any]:
    return {
        "id": 1,
        "created_at": created,
        "body": body or f"{CLAIM_MARKER}{'a' * 32} host=rival pid=88 started={created} -->",
    }


def epoch(created: str = NOW, label: str = LABELS.trigger) -> dict[str, Any]:
    return {"event": "labeled", "created_at": created, "label": {"name": label}}


def label_names(ops: FakeGithub) -> set[str]:
    return {label["name"] for label in ops.issue_payloads[(REPO, 3)]["labels"]}


def writes(ops: FakeGithub) -> list[tuple[str, str, Any]]:
    return [call for call in ops.raw_calls if call[0] != "GET"]


def test_validation_is_read_only_and_accepts_unlabeled_pending_issue() -> None:
    helper, ops = make("customer:label")
    helper.validate_campaign_item(item())
    assert writes(ops) == []
    assert label_names(ops) == {"customer:label"}


def test_park_and_prepare_are_idempotent_and_preserve_other_labels() -> None:
    helper, ops = make(LABELS.trigger, "customer:label")
    helper.park_campaign_item(item())
    helper.park_campaign_item(item())
    assert label_names(ops) == {"customer:label"}
    helper.prepare_campaign_item(item())
    helper.prepare_campaign_item(item())
    assert label_names(ops) == {LABELS.trigger, "customer:label"}
    assert [method for method, _, _ in writes(ops)] == ["DELETE", "POST"]
    assert "/labels/custom%3Arun" in writes(ops)[0][1]


def test_workloads_use_their_own_trigger() -> None:
    helper, ops = make(LABELS.workload)
    helper.park_campaign_item(item(kind="workload"))
    assert label_names(ops) == set()
    helper.prepare_campaign_item(item(kind="workload"))
    assert label_names(ops) == {LABELS.workload}


def test_ready_step_still_uses_the_normal_comment_claim_lock() -> None:
    helper, ops = make()
    helper.prepare_campaign_item(item())
    assert helper.source.claim(item()) is True
    assert label_names(ops) == {LABELS.in_progress}
    assert any(CLAIM_MARKER in row["body"] for row in ops.issue_comment_payloads[(REPO, 3)])


def test_parking_rechecks_ownership_after_reading_comments() -> None:
    helper, ops = make(LABELS.trigger)
    initial = dict(ops.issue_payloads[(REPO, 3)])
    owned = {**initial, "labels": [{"name": LABELS.trigger}, {"name": LABELS.in_progress}]}
    ops.issue_read_scripts[(REPO, 3)] = [initial, owned]
    with pytest.raises(CampaignSourceError, match="owned"):
        helper.park_campaign_item(item())
    assert writes(ops) == []
    assert label_names(ops) == {LABELS.trigger, LABELS.in_progress}


@pytest.mark.parametrize(
    "action,method,path",
    [
        ("park_campaign_item", "DELETE", f"/repos/{REPO}/issues/3/labels/custom%3Arun"),
        ("prepare_campaign_item", "POST", f"/repos/{REPO}/issues/3/labels"),
    ],
)
def test_label_write_failure_is_reported_and_retry_is_safe(
    action: str, method: str, path: str
) -> None:
    helper, ops = make(*([LABELS.trigger] if method == "DELETE" else []))
    original = label_names(ops)
    ops.raw_failures[(method, path)] = GithubOpsError("permission denied", http_status=403)
    with pytest.raises(CampaignSourceError, match="permission"):
        getattr(helper, action)(item())
    assert label_names(ops) == original
    ops.raw_failures.clear()
    getattr(helper, action)(item())
    assert label_names(ops) == (set() if method == "DELETE" else {LABELS.trigger})


@pytest.mark.parametrize(
    "action", ["validate_campaign_item", "park_campaign_item", "prepare_campaign_item"]
)
@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"state": "closed"}, "open"),
        ({"state": "unknown"}, "open"),
        ({"number": 4}, "identity"),
        ({"pull_request": {}}, "pull request"),
        ({"labels": None}, "labels"),
        ({"labels": ["unknown"]}, "labels"),
        ({"labels": [{"name": LABELS.in_progress}]}, "owned"),
        ({"labels": [{"name": LABELS.gated}]}, "owned"),
        ({"labels": [{"name": LABELS.workload}]}, "kind"),
        ({"labels": [{"name": LABELS.trigger}, {"name": LABELS.workload}]}, "kind"),
    ],
)
def test_unsafe_live_issue_never_changes_labels(
    action: str, overrides: dict[str, Any], reason: str
) -> None:
    helper, ops = make(LABELS.trigger)
    ops.issue_payloads[(REPO, 3)].update(overrides)
    with pytest.raises(CampaignSourceError, match=reason):
        getattr(helper, action)(item())
    assert writes(ops) == []


@pytest.mark.parametrize("action", ["park_campaign_item", "prepare_campaign_item"])
@pytest.mark.parametrize("body", [None, f"{CLAIM_MARKER}malformed -->"])
def test_current_claim_in_flight_refuses_without_rotating_trigger_epoch(
    action: str, body: str | None
) -> None:
    helper, ops = make(LABELS.trigger)
    ops.issue_comment_payloads[(REPO, 3)] = [claim(body=body)]
    ops.issue_event_payloads[(REPO, 3)] = [epoch(OLD)]
    with pytest.raises(CampaignSourceError, match="claim"):
        getattr(helper, action)(item())
    assert writes(ops) == []


def test_old_claim_before_latest_trigger_does_not_block_parking() -> None:
    helper, ops = make(LABELS.trigger)
    ops.issue_comment_payloads[(REPO, 3)] = [claim(OLD)]
    ops.issue_event_payloads[(REPO, 3)] = [epoch(NOW)]
    helper.park_campaign_item(item())
    assert label_names(ops) == set()


@pytest.mark.parametrize(
    "overrides", [{"claimed": True}, {"claim_token": "a" * 32}, {"state": "running"}]
)
def test_existing_local_claim_uses_original_recovery_path(overrides: dict[str, Any]) -> None:
    helper, ops = make()
    with pytest.raises(CampaignSourceError, match="recovery"):
        helper.prepare_campaign_item(item(**overrides))
    assert ops.raw_calls == []


@pytest.mark.parametrize(
    "comments,events,reason",
    [
        ({}, [], "comments"),
        ([{}], [], "comment"),
        ([{"body": f"{CLAIM_MARKER}unknown -->"}], [], "timestamp"),
        ([claim()], {}, "events"),
        ([claim()], [{"event": "labeled", "label": None}], "event"),
        ([claim()], [epoch("not-a-time")], "timestamp"),
    ],
)
def test_unreadable_ownership_is_not_an_unclaimed_issue(
    comments: Any, events: Any, reason: str
) -> None:
    helper, ops = make(LABELS.trigger)
    ops.issue_comment_payloads[(REPO, 3)] = comments
    ops.issue_event_payloads[(REPO, 3)] = events
    with pytest.raises(CampaignSourceError, match=reason):
        helper.park_campaign_item(item())
    assert writes(ops) == []


def test_claim_beyond_first_page_is_seen() -> None:
    helper, ops = make(LABELS.trigger)
    ops.issue_comment_payloads[(REPO, 3)] = [
        {"id": index, "body": "Discussion"} for index in range(100)
    ] + [claim()]
    with pytest.raises(CampaignSourceError, match="claim"):
        helper.park_campaign_item(item())
    assert any("page=2" in path for _, path, _ in ops.raw_calls)
    assert writes(ops) == []


def test_read_error_is_named_and_leaves_labels_untouched() -> None:
    helper, ops = make(LABELS.trigger)
    ops.fail_always["raw"] = GithubOpsError("permission denied", http_status=403)
    with pytest.raises(CampaignSourceError, match="permission denied"):
        helper.park_campaign_item(item())
    assert writes(ops) == []


def run(**overrides: Any) -> RunRecord:
    return RunRecord.model_validate(
        {
            "run_id": "r1",
            "outcome": "Deliver",
            "state": "merged",
            "created_at": 1,
            "updated_at": 2,
            "pr_number": 7,
            "pr_url": f"https://github.com/{REPO}/pull/7",
            **overrides,
        }
    )


def landed(ops: FakeGithub) -> None:
    ops.pr.update(
        {
            "merged": True,
            "state": "closed",
            "merge_commit_sha": "a" * 40,
            "base": {"ref": "release", "repo": {"full_name": REPO}},
        }
    )


def test_code_delivery_requires_live_merge_evidence_on_the_pinned_base() -> None:
    helper, ops = make()
    landed(ops)
    evidence = helper.verify_code_delivery(REPO, "release", run())
    assert evidence.repo == REPO and evidence.base == "release"
    assert evidence.run_id == "r1" and evidence.pr_number == 7
    assert evidence.merge_commit_sha == "a" * 40
    assert evidence.pr_url == f"https://github.com/{REPO}/pull/7"
    assert writes(ops) == []


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"merged": False}, "merged"),
        ({"merged": "true"}, "merged"),
        ({"state": "open"}, "merged"),
        ({"number": 8}, "identity"),
        ({"merge_commit_sha": None}, "merge commit"),
        ({"merge_commit_sha": " "}, "merge commit"),
        ({"base": None}, "base"),
        ({"base": {"ref": "main", "repo": {"full_name": REPO}}}, "base"),
        ({"base": {"ref": "release", "repo": None}}, "repository"),
        ({"base": {"ref": "release", "repo": {"full_name": "other/project"}}}, "repository"),
        ({"html_url": "https://github.com/other/project/pull/7"}, "identity"),
    ],
)
def test_missing_or_wrong_delivery_evidence_never_advances(
    overrides: dict[str, Any], reason: str
) -> None:
    helper, ops = make()
    landed(ops)
    ops.pr.update(overrides)
    with pytest.raises(CampaignSourceError, match=reason):
        helper.verify_code_delivery(REPO, "release", run())


@pytest.mark.parametrize(
    "overrides",
    [{"state": "gated"}, {"state": "failed"}, {"kind": "workload"}, {"pr_number": None}],
)
def test_open_gate_or_unfinished_run_is_not_delivery(overrides: dict[str, Any]) -> None:
    helper, ops = make()
    landed(ops)
    with pytest.raises(CampaignSourceError):
        helper.verify_code_delivery(REPO, "release", run(**overrides))


def test_delivery_read_failure_refuses() -> None:
    helper, ops = make()
    ops.fail_always["pr_get"] = GithubOpsError("unavailable")
    with pytest.raises(CampaignSourceError, match="unavailable"):
        helper.verify_code_delivery(REPO, "release", run())


def test_source_selection_is_explicit_and_never_uses_unknown_repo_fallback() -> None:
    helper, ops = make()
    other = GitHubIssueSource(lambda: ops, "other/project", LABELS)
    multi = MultiRepoIssueSource([helper.source, other])
    composite = CompositeSource(multi, ChatSource())
    assert source_for_campaign(composite, item()) is helper.source
    with pytest.raises(CampaignSourceError, match="repository"):
        source_for_campaign(composite, item(repo="missing/project"))
    with pytest.raises(CampaignSourceError, match="repository"):
        source_for_campaign(composite, item(repo=None))
    assert (
        source_for_campaign(
            composite,
            item(item_id="chat:message", source_key="message", repo=None, kind="workload"),
        )
        is None
    )
    assert ops.raw_calls == []
