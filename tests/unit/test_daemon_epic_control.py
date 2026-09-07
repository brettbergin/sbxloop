"""The concierge admits explicit epics through the real campaign coordinator."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.campaign_source import source_for_campaign
from sbxloop.daemon.concierge import Concierge
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.sources import (
    ChatSource,
    CompositeSource,
    GitHubIssueSource,
    GitHubLabels,
    MultiRepoIssueSource,
)
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop.errors import GithubOpsError
from sbxloop.events import EventBus
from sbxloop_worker.protocol import HostToolResponse
from tests.fakes.fake_github import FakeGithub
from tests.unit.test_daemon_concierge import FakeClient, FakeHost, FakeVersions, turn

REPO = "customer/project"
CAMPAIGN = f"epic:{REPO}:40"


class UnusedDefaultGithub:
    """Intake must use the selected repository source, not the primary token."""

    def call(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("epic intake used the default GitHub session")


def issue(repo: str, number: int, body: str = "", **overrides: Any) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Step {number}",
        "body": body,
        "state": "open",
        "labels": [],
        "html_url": f"https://github.com/{repo}/issues/{number}",
        **overrides,
    }


class Harness:
    def __init__(
        self,
        path: Path,
        *,
        repos: list[dict[str, Any]] | None = None,
        concierge: dict[str, Any] | None = None,
    ) -> None:
        self.config = Config.model_validate(
            {
                "home": str(path),
                "discord": {"channel_id": 42},
                "github": {"repos": repos or [{"repo": REPO, "deliver_base": "release"}]},
                "concierge": concierge or {},
            }
        )
        self.store = StateStore(self.config.paths.state_db)
        self.dstore = DaemonStore(self.config.paths.state_db)
        self.githubs: dict[str, FakeGithub] = {}
        sources: list[GitHubIssueSource] = []
        for entry in self.config.github.repo_list():
            ops = FakeGithub(repo=entry.repo)
            ops.issue_payloads[(entry.repo, 40)] = issue(
                entry.repo,
                40,
                "Deliver a shared customer outcome.\n\n## Backlog\n- #3\n- #8\n"
                "\n## Build order\n1. #8 Prepare\n2. #3 Deliver",
                title="Customer launch",
            )
            ops.issue_payloads[(entry.repo, 8)] = issue(entry.repo, 8, "Prepare the foundation.")
            ops.issue_payloads[(entry.repo, 3)] = issue(
                entry.repo, 3, "Deliver the outcome.\n\nDepends on: #8"
            )
            self.githubs[entry.repo] = ops
            lifecycle = self.config.labels_for(entry.repo)
            if entry.enabled:
                sources.append(
                    GitHubIssueSource(
                        lambda ops=ops: ops,
                        entry.repo,
                        GitHubLabels(
                            lifecycle.trigger,
                            lifecycle.in_progress,
                            lifecycle.failed,
                            workload=lifecycle.workload,
                        ),
                    )
                )
        source = (
            CompositeSource(MultiRepoIssueSource(sources), ChatSource())
            if sources
            else ChatSource()
        )
        self.loop = DaemonLoop(
            self.config, store=self.store, dstore=self.dstore, source=source, clock=lambda: 1000.0
        )
        self.client = FakeClient([])
        self.concierge = Concierge(
            self.config,
            loop=self.loop,
            dstore=self.dstore,
            store_factory=lambda: StateStore(self.config.paths.state_db),
            github=UnusedDefaultGithub(),  # type: ignore[arg-type]
            host=FakeHost(self.client),
            bus=EventBus(),
            clock=lambda: 1000.0,
            versions=FakeVersions(),  # type: ignore[arg-type]
        )

    def call(self, **args: Any) -> HostToolResponse:
        self.client.scripts.append({"calls": [("start_epic", args)]})
        turn(self.concierge, "Run this epic", author_id="person-42")
        return self.client.responses[-1]

    @property
    def ops(self) -> FakeGithub:
        return self.githubs[REPO]

    def close(self) -> None:
        self.concierge.close()
        self.loop.campaign_runner.close()
        self.dstore.close()
        self.store.close()


@pytest.fixture
def h(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def test_preview_reads_explicit_order_and_does_not_admit_or_label(h: Harness) -> None:
    response = h.call(number=40, preview=True)
    assert response.ok and "preview" in response.text.lower()
    assert "#8" in response.text and "#3" in response.text and "release" in response.text
    assert h.loop.campaign_runner.campaigns.get(CAMPAIGN) is None
    assert h.dstore.queued() == []
    assert all(method == "GET" for method, _, _ in h.ops.raw_calls)


def test_run_epic_admits_once_with_pinned_context_and_requester(h: Harness) -> None:
    h.ops.issue_payloads[(REPO, 3)]["labels"] = [{"name": "sbxloop:run"}]
    response = h.call(number=40)
    assert response.ok and CAMPAIGN in response.text
    snapshot = h.loop.campaign_runner.campaigns.get(CAMPAIGN)
    assert snapshot is not None and snapshot.prepared
    assert snapshot.plan.source_url == f"https://github.com/{REPO}/issues/40"
    assert snapshot.plan.requested_by.endswith("(via concierge)")
    assert [step.item.source_key for step in snapshot.steps] == ["8", "3"]
    assert [step.expected_base for step in snapshot.steps] == ["release", "release"]
    for step in snapshot.steps:
        original = h.ops.issue_payloads[(REPO, int(step.item.source_key))]["body"]
        assert step.item.body.startswith(original)
        assert "Customer launch" in step.item.body
        assert "Deliver a shared customer outcome." in step.item.body
        assert snapshot.plan.source_url in step.item.body
        assert "1. #8" in step.item.body and "2. #3" in step.item.body
        assert step.item.requested_by == "person-42"
        assert f"This run handles issue #{step.item.source_key} only." in step.item.body
    assert snapshot.steps[1].prerequisites == (f"gh:{REPO}:issue:8",)
    assert h.dstore.queued() == []  # admission never starts an engine run
    assert h.ops.issue_payloads[(REPO, 3)]["labels"] == []


def test_epic_intake_claim_and_verified_merge_release_exactly_next_child(h: Harness) -> None:
    assert h.call(number=40).ok
    coordinator = h.loop.campaign_runner
    assert coordinator.enqueue_ready() == 1
    first_id, next_id = f"gh:{REPO}:issue:8", f"gh:{REPO}:issue:3"
    assert [item.item_id for item in h.dstore.queued()] == [first_id]
    assert h.ops.issue_payloads[(REPO, 8)]["labels"] == [{"name": "sbxloop:run"}]
    assert h.ops.issue_payloads[(REPO, 3)]["labels"] == []
    h.dstore.mark_claiming(first_id, "b" * 32, 1000.0)
    first = h.dstore.get(first_id)
    assert first is not None
    source = source_for_campaign(h.loop.source, first)
    assert source is not None and source.claim(first)
    h.dstore.mark_claimed(first_id, 1000.0)
    h.dstore.mark_running(first_id, "landed", 1000.0)
    assert h.ops.issue_payloads[(REPO, 8)]["labels"] == [{"name": "sbxloop:in-progress"}]
    h.store.create_run("landed", first.body)
    h.store.set_run_pr(
        "landed",
        number=7,
        url=f"https://github.com/{REPO}/pull/7",
        branch="branch",
        head_sha="head",
    )
    h.store.set_run_state("landed", "merged")
    h.dstore.mark_done(first_id, 1000.0)
    coordinator.reconcile()
    assert coordinator.enqueue_ready() == 0  # a local merged bit is insufficient
    assert h.dstore.get(next_id) is None
    h.ops.pr.update(
        {
            "merged": True,
            "state": "closed",
            "merge_commit_sha": "a" * 40,
            "base": {"ref": "release", "repo": {"full_name": REPO}},
        }
    )
    coordinator.reconcile()
    assert coordinator.enqueue_ready() == 1
    assert [item.item_id for item in h.dstore.queued()] == [next_id]
    assert h.ops.issue_payloads[(REPO, 3)]["labels"] == [{"name": "sbxloop:run"}]
    assert "1/2" in h.loop.campaign_status(CAMPAIGN)


def test_explicit_order_overrides_epic_order_without_violating_dependencies(h: Harness) -> None:
    h.ops.issue_payloads[(REPO, 3)]["body"] = "An independent step."
    response = h.call(number=40, ordered_issue_numbers=[3, 8])
    assert response.ok
    snapshot = h.loop.campaign_runner.campaigns.get(CAMPAIGN)
    assert snapshot is not None
    assert [step.item.source_key for step in snapshot.steps] == ["3", "8"]
    assert "1. #3" in snapshot.steps[0].item.body


def test_repeated_request_returns_stored_plan_without_importing_epic_edits(h: Harness) -> None:
    assert h.call(number=40).ok
    before = h.loop.campaign_runner.campaigns.get(CAMPAIGN)
    h.ops.issue_payloads[(REPO, 40)]["body"] = "## Build order\n1. #99"
    h.ops.fail_always["raw"] = GithubOpsError("unavailable")
    response = h.call(number=40)
    assert response.ok and "already admitted" in response.text
    assert h.loop.campaign_runner.campaigns.get(CAMPAIGN) == before


def test_existing_campaign_order_is_edited_through_campaign_controls(h: Harness) -> None:
    assert h.call(number=40).ok
    response = h.call(number=40, ordered_issue_numbers=[3, 8])
    assert not response.ok and "campaign move" in response.text


def test_original_admission_replays_after_pending_order_was_changed(h: Harness) -> None:
    h.ops.issue_payloads[(REPO, 3)]["body"] = "An independent step."
    assert h.call(number=40, ordered_issue_numbers=[8, 3]).ok
    h.loop.move_campaign_step(
        CAMPAIGN,
        f"gh:{REPO}:issue:3",
        before=f"gh:{REPO}:issue:8",
        by="operator",
    )
    before = h.loop.campaign_runner.campaigns.get(CAMPAIGN)
    assert before is not None
    assert [step.item.source_key for step in before.steps] == ["3", "8"]
    reads = len(h.ops.raw_calls)
    response = h.call(number=40, ordered_issue_numbers=[8, 3])
    assert response.ok and "already admitted" in response.text
    assert h.loop.campaign_status(CAMPAIGN) in response.text
    assert h.loop.campaign_runner.campaigns.get(CAMPAIGN) == before
    assert len(h.ops.raw_calls) == reads


@pytest.mark.parametrize("preview", [False, True])
def test_closed_children_are_named_and_never_re_run(h: Harness, preview: bool) -> None:
    h.ops.issue_payloads[(REPO, 8)]["state"] = "closed"
    response = h.call(number=40, preview=preview)
    assert response.ok is preview
    assert "closed" in response.text and "#8" in response.text
    assert "reconciliation" in response.text
    assert h.loop.campaign_runner.campaigns.get(CAMPAIGN) is None
    assert all(method == "GET" for method, _, _ in h.ops.raw_calls)


@pytest.mark.parametrize("order", [[3, 8], [8], [8, 8], [8, 3, 99]])
def test_invalid_sequence_leaves_no_partial_campaign(h: Harness, order: list[int]) -> None:
    response = h.call(number=40, ordered_issue_numbers=order)
    assert not response.ok
    assert h.loop.campaign_runner.campaigns.get(CAMPAIGN) is None
    assert all(method == "GET" for method, _, _ in h.ops.raw_calls)


@pytest.mark.parametrize(
    "args",
    [
        {"number": True},
        {"number": 0},
        {"number": "40"},
        {"number": 40, "preview": "false"},
        {"number": 40, "ordered_issue_numbers": "8,3"},
        {"number": 40, "ordered_issue_numbers": [8, False]},
    ],
)
def test_malformed_arguments_refuse_before_any_github_read(
    h: Harness, args: dict[str, Any]
) -> None:
    assert not h.call(**args).ok
    assert h.ops.raw_calls == []


def test_missing_member_read_fails_without_admission(h: Harness) -> None:
    del h.ops.issue_payloads[(REPO, 3)]
    response = h.call(number=40)
    assert not response.ok and "cannot read" in response.text and "3" in response.text
    assert h.loop.campaign_runner.campaigns.get(CAMPAIGN) is None


def test_actual_default_branch_is_pinned_when_no_base_is_configured(tmp_path: Path) -> None:
    h = Harness(tmp_path, repos=[{"repo": REPO}])
    try:
        h.ops.repo_payload["default_branch"] = "develop"
        assert h.call(number=40).ok
        snapshot = h.loop.campaign_runner.campaigns.get(CAMPAIGN)
        assert snapshot is not None
        assert [step.expected_base for step in snapshot.steps] == ["develop", "develop"]
        h.ops.repo_payload["default_branch"] = "changed-after-admission"
        assert h.call(number=40).ok
        assert h.loop.campaign_runner.campaigns.get(CAMPAIGN) == snapshot
    finally:
        h.close()


def test_unknown_default_branch_refuses_without_guessing(tmp_path: Path) -> None:
    h = Harness(tmp_path, repos=[{"repo": REPO}])
    try:
        h.ops.repo_payload.pop("default_branch")
        response = h.call(number=40)
        assert not response.ok and "default branch" in response.text
        assert h.loop.campaign_runner.campaigns.get(CAMPAIGN) is None
    finally:
        h.close()


def test_multirepo_intake_uses_selected_source_and_its_labels_and_base(tmp_path: Path) -> None:
    second = "customer/second"
    h = Harness(
        tmp_path,
        repos=[
            {"repo": REPO, "deliver_base": "main"},
            {
                "repo": second,
                "deliver_base": "stable",
                "trigger_label": "custom:go",
                "workload_label": "custom:work",
            },
        ],
    )
    try:
        response = h.call(number=40)
        assert not response.ok and "repo" in response.text
        assert all(not ops.raw_calls for ops in h.githubs.values())
        h.githubs[second].issue_payloads[(second, 3)]["labels"] = [{"name": "custom:go"}]
        response = h.call(number=40, repo=second)
        assert response.ok
        snapshot = h.loop.campaign_runner.campaigns.get(f"epic:{second}:40")
        assert snapshot is not None
        assert all(
            step.item.repo == second and step.expected_base == "stable" for step in snapshot.steps
        )
        assert h.ops.raw_calls == []
        assert h.githubs[second].issue_payloads[(second, 3)]["labels"] == []
    finally:
        h.close()


def test_disabled_repository_is_not_admitted(tmp_path: Path) -> None:
    h = Harness(tmp_path, repos=[{"repo": REPO, "enabled": False}])
    try:
        response = h.call(number=40, repo=REPO)
        assert not response.ok and "disabled" in response.text
        assert h.ops.raw_calls == []
    finally:
        h.close()


def test_workload_child_keeps_its_kind_without_a_merge_base(h: Harness) -> None:
    h.ops.issue_payloads[(REPO, 3)]["labels"] = [{"name": "sbxloop:workload"}]
    assert h.call(number=40).ok
    snapshot = h.loop.campaign_runner.campaigns.get(CAMPAIGN)
    assert snapshot is not None
    assert snapshot.steps[1].item.kind == "workload"
    assert snapshot.steps[1].expected_base is None


@pytest.mark.parametrize("config", [{"github_tools": False}, {"create_issues": False}])
def test_epic_admission_respects_existing_github_tool_permissions(
    tmp_path: Path, config: dict[str, Any]
) -> None:
    h = Harness(tmp_path, concierge=config)
    try:
        assert "start_epic" not in h.concierge.tool_names
    finally:
        h.close()
