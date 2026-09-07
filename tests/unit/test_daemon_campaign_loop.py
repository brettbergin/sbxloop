"""Serial campaigns use real persisted run records and ordinary daemon controls."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop.config import Config
from sbxloop.daemon.campaigns import CampaignPlan, CampaignStepPlan
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.sources import ChatSource, GitHubIssueSource, GitHubLabels
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import Published, RunResult, RunState, TaskOutput, TaskSpec
from sbxloop.engine.store import StateStore
from sbxloop.events import EventBus
from tests.fakes.fake_github import FakeGithub


def work(number: int) -> WorkItem:
    return WorkItem(
        item_id=f"chat:{number}",
        source_key=str(number),
        title=f"Step {number}",
        body=f"Pinned ask {number}",
        kind="workload",
        requested_by="human",
    )


def plan(*numbers: int) -> CampaignPlan:
    return CampaignPlan(
        campaign_id="launch",
        title="Launch",
        requested_by="human",
        steps=tuple(CampaignStepPlan(item=work(number)) for number in numbers),
    )


class RefusingChat(ChatSource):
    def claim(self, item: WorkItem) -> bool:
        return False


class Harness:
    def __init__(self, path: Path, *, attempts: int = 1) -> None:
        self.config = Config.model_validate(
            {
                "home": str(path),
                "daemon": {
                    "max_attempts_per_item": attempts,
                    "retry_backoff_s": 10,
                    "max_consecutive_failures": 10,
                },
            }
        )
        self.store = StateStore(self.config.paths.state_db)
        self.dstore = DaemonStore(self.config.paths.state_db)
        self.now = 1000.0
        self.outcomes: list[RunState] = []
        self.ran: list[WorkItem] = []
        self.receipts = True
        self.loop = DaemonLoop(
            self.config,
            store=self.store,
            dstore=self.dstore,
            source=ChatSource(),
            runner=self.runner,
            clock=lambda: self.now,
        )

    def runner(
        self, item: WorkItem, config: Config, run_id: str, bus: EventBus, resume: bool
    ) -> RunResult:
        self.ran.append(item)
        outcome = self.outcomes.pop(0) if self.outcomes else "completed"
        if not resume:
            self.store.create_run(run_id, item.body, kind=item.kind)
        if item.kind == "workload" and outcome in ("completed", "held"):
            self.store.save_tasks(run_id, [TaskSpec(id="answer", title="Answer")])
            task = self.store.get_tasks(run_id)[0]
            task.state, task.output = "done", TaskOutput(summary="The result")
            self.store.update_task(run_id, task)
            if outcome == "completed" and self.receipts:
                self.store.add_run_published(
                    run_id, Published(sink="chat", location="chat", tasks=["answer"])
                )
        self.store.set_run_state(run_id, outcome)
        return RunResult(run_id=run_id, state=outcome, kind=item.kind)


def test_two_steps_advance_only_after_publication(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.loop.admit_campaign(plan(1, 2))
    assert h.dstore.queued() == []
    assert h.loop.tick().dispatched == "chat:1"
    assert h.dstore.get("chat:2") is None
    assert "1/2" in h.loop.campaign_status("launch")
    assert h.loop.tick().dispatched == "chat:2"
    assert "2/2" in h.loop.campaign_status("launch")
    assert h.loop.tick().dispatched is None


def test_later_label_discovery_and_existing_queue_cannot_bypass_frontier(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.dstore.upsert_new(work(2), h.now)
    h.loop.admit_campaign(plan(1, 2))
    assert h.dstore.get("chat:2") is None
    h.dstore.upsert_new(work(2), h.now)
    h.loop.hold_campaign("launch", "human", "wait")
    h.dstore.upsert_new(work(9), h.now)
    assert h.loop.tick().dispatched == "chat:9"
    assert not h.dstore.get("chat:2").claim_token  # type: ignore[union-attr]


def test_terminal_failure_waits_while_unrelated_work_can_run(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.outcomes = ["failed"]
    h.loop.admit_campaign(plan(1, 2))
    assert h.loop.tick().outcome == "failed"
    assert "failed" in h.loop.campaign_status("launch")
    h.dstore.upsert_new(work(9), h.now)
    assert h.loop.tick().dispatched == "chat:9"
    assert h.dstore.get("chat:2") is None


def test_current_step_retries_after_backoff_without_releasing_successor(tmp_path: Path) -> None:
    h = Harness(tmp_path, attempts=2)
    h.outcomes = ["failed", "completed"]
    h.loop.admit_campaign(plan(1, 2))
    assert h.loop.tick().outcome == "retry"
    assert h.loop.tick().dispatched is None
    h.now += 11
    assert h.loop.tick().dispatched == "chat:1"
    assert [item.item_id for item in h.ran] == ["chat:1", "chat:1"]
    assert h.loop.tick().dispatched == "chat:2"


def test_completion_without_publication_receipts_never_advances(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.receipts = False
    h.loop.admit_campaign(plan(1, 2))
    h.loop.tick()
    assert "publication" in h.loop.campaign_status("launch")
    assert h.loop.tick().dispatched is None
    assert h.dstore.get("chat:2") is None


def test_hold_resume_and_replay_preserve_the_admitted_ask(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.loop.admit_campaign(plan(1, 2))
    h.loop.hold_campaign("launch", "human", "wait")
    h.loop.admit_campaign(plan(1, 2))
    assert h.loop.tick().dispatched is None
    h.loop.resume_campaign("launch", "human")
    h.dstore.upsert_new(work(1).model_copy(update={"body": "Changed after admission"}), h.now)
    h.loop.tick()
    assert h.ran[0].body == "Pinned ask 1"


def test_claim_failure_preserves_admission_and_names_the_hold(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.loop.source = RefusingChat()
    h.loop.admit_campaign(plan(1, 2))
    assert h.loop.tick().outcome == "failed"
    assert h.dstore.get("chat:1") is not None
    assert "claim" in h.loop.campaign_status("launch")
    assert h.loop.tick().dispatched is None


def test_recovery_checkpoints_finished_run_before_next_dispatch(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.loop.admit_campaign(plan(1, 2))
    h.dstore.upsert_new(work(1), h.now)
    h.dstore.mark_claimed("chat:1", h.now)
    h.dstore.mark_running("chat:1", "interrupted", h.now)
    h.runner(work(1), h.config, "interrupted", EventBus(), False)
    h.loop.campaign_runner.close()
    h.loop = DaemonLoop(
        h.config,
        store=h.store,
        dstore=h.dstore,
        source=ChatSource(),
        runner=h.runner,
        clock=lambda: h.now,
    )
    h.loop.recover()
    assert "1/2" in h.loop.campaign_status("launch")
    assert h.loop.tick().dispatched == "chat:2"


def test_explicit_queue_admission_is_available_without_epic_intake(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    for number in (1, 2):
        h.dstore.upsert_new(work(number), h.now)
    assert "launch" in h.loop.start_campaign("launch", ["chat:2", "chat:1"], "human")
    assert "launch" in h.loop.start_campaign("launch", ["chat:2", "chat:1"], "human")
    with pytest.raises(ValueError, match="different plan"):
        h.loop.start_campaign("launch", ["chat:1", "chat:2"], "human")
    assert h.loop.tick().dispatched == "chat:2"


def test_publication_receipts_must_cover_the_tasks(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.receipts = False
    h.loop.admit_campaign(plan(1, 2))
    h.loop.tick()
    run_id = h.ran[0].run_id
    assert run_id is not None
    h.store.add_run_published(run_id, Published(sink="chat", location="chat", tasks=["other"]))
    assert h.loop.tick().dispatched is None
    assert "receipt missing for task answer" in h.loop.campaign_status("launch")
    h.store.add_run_published(run_id, Published(sink="chat", location="chat", tasks=["answer"]))
    assert h.loop.tick().dispatched == "chat:2"


def test_held_workload_does_not_release_the_next_step(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.outcomes = ["held"]
    h.loop.admit_campaign(plan(1, 2))
    assert h.loop.tick().outcome == "held"
    assert h.loop.tick().dispatched is None
    assert "gated" in h.loop.campaign_status("launch")
    assert h.dstore.get("chat:2") is None


def test_manual_retry_releases_only_the_failed_frontier(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.outcomes = ["failed"]
    h.loop.admit_campaign(plan(1, 2))
    h.loop.tick()
    h.loop.retry_item("chat:1", "human")
    assert h.loop.tick().dispatched == "chat:1"
    assert h.dstore.get("chat:2") is None


def test_campaign_move_cannot_override_explicit_prerequisite(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    bounded = CampaignPlan(
        campaign_id="launch",
        title="Launch",
        requested_by="admitter",
        steps=(
            CampaignStepPlan(item=work(1)),
            CampaignStepPlan(item=work(2), prerequisites=("chat:1",)),
        ),
    )
    h.loop.admit_campaign(bounded)
    with pytest.raises(ValueError, match="prerequisite"):
        h.loop.move_campaign_step("launch", "chat:2", before="chat:1", by="human")
    assert "Admitted by admitter" in h.loop.outcome_text(work(1))


def test_moving_pending_frontier_parks_its_old_queue_entry(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.dstore.upsert_new(work(9), h.now)
    h.loop.admit_campaign(plan(1, 2))
    assert h.loop.tick().dispatched == "chat:9"
    assert h.dstore.get("chat:1") is not None
    h.loop.move_campaign_step("launch", "chat:2", before="chat:1", by="human")
    assert h.dstore.get("chat:1") is None
    assert h.loop.tick().dispatched == "chat:2"


def test_invalid_admission_does_not_absorb_any_existing_queue_item(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.dstore.upsert_new(work(1), h.now)
    with pytest.raises((KeyError, ValueError)):
        h.loop.start_campaign("launch", ["chat:1", "chat:missing"], "human")
    assert [item.item_id for item in h.dstore.queued()] == ["chat:1"]


def test_code_frontier_requires_merged_pr_on_pinned_base(tmp_path: Path) -> None:
    config = Config.model_validate({"home": str(tmp_path), "github": {"repo": "o/r"}})
    store, dstore = StateStore(config.paths.state_db), DaemonStore(config.paths.state_db)
    github = FakeGithub(repo="o/r", number=7)
    issue = WorkItem(item_id="gh:issue:1", source_key="1", repo="o/r", title="Pinned")
    github.issue_payloads[("o/r", 1)] = {"number": 1, "state": "open", "labels": []}
    source = GitHubIssueSource(
        lambda: github, "o/r", GitHubLabels("sbxloop:run", "sbxloop:in-progress", "sbxloop:failed")
    )
    loop = DaemonLoop(config, store=store, dstore=dstore, source=source, clock=lambda: 1000.0)
    loop.admit_campaign(
        CampaignPlan(
            campaign_id="code",
            title="Code",
            requested_by="human",
            steps=(CampaignStepPlan(item=issue, expected_base="release"),),
        )
    )
    admitted = loop.campaign_runner.campaigns.get("code")
    assert admitted is not None
    pinned = admitted.steps[0].item
    dstore.upsert_new(pinned, 1000.0)
    dstore.mark_running(pinned.item_id, "landed", 1000.0)
    store.create_run("landed", "Pinned")
    store.set_run_pr(
        "landed", number=7, url="https://github.com/o/r/pull/7", branch="branch", head_sha="head"
    )
    store.set_run_state("landed", "merged")
    dstore.mark_done(pinned.item_id, 1000.0)
    github.pr.update(
        {
            "number": 7,
            "merged": True,
            "state": "closed",
            "merge_commit_sha": "abc",
            "html_url": "https://github.com/o/r/pull/7",
            "base": {"ref": "main", "repo": {"full_name": "o/r"}},
        }
    )
    loop.campaign_runner.reconcile()
    assert "pinned base" in loop.campaign_status("code")
    github.pr["base"]["ref"] = "release"
    loop.campaign_runner.reconcile()
    assert "1/1" in loop.campaign_status("code")
    assert loop._item_config(pinned).github.effective_repo("o/r").deliver_base == "release"  # type: ignore[union-attr]


def test_admission_pins_issue_discussion_and_replay_keeps_it(tmp_path: Path) -> None:
    config = Config.model_validate({"home": str(tmp_path), "github": {"repo": "o/r"}})
    store, dstore = StateStore(config.paths.state_db), DaemonStore(config.paths.state_db)
    github = FakeGithub(repo="o/r")
    issue = WorkItem(item_id="gh:issue:1", source_key="1", repo="o/r", title="Pinned")
    github.issue_payloads[("o/r", 1)] = {"number": 1, "state": "open", "labels": []}
    github.issue_comment_payloads[("o/r", 1)] = [
        {"body": "Preserve the existing integration", "user": {"login": "maintainer"}}
    ]
    source = GitHubIssueSource(
        lambda: github, "o/r", GitHubLabels("sbxloop:run", "sbxloop:in-progress", "sbxloop:failed")
    )
    loop = DaemonLoop(config, store=store, dstore=dstore, source=source, clock=lambda: 1000.0)
    admitted = CampaignPlan(
        campaign_id="code",
        title="Code",
        requested_by="human",
        steps=(CampaignStepPlan(item=issue, expected_base="main"),),
    )
    loop.admit_campaign(admitted)
    text = loop.outcome_text(issue)
    assert "@maintainer" in text
    assert "Preserve the existing integration" in text
    github.issue_comment_payloads[("o/r", 1)] = []
    before_replay = len(github.raw_calls)
    loop.admit_campaign(admitted)
    assert loop.outcome_text(issue) == text
    assert len(github.raw_calls) == before_replay
    changed = admitted.model_copy(
        update={
            "steps": (
                CampaignStepPlan(
                    item=issue.model_copy(update={"body": "Other ask"}), expected_base="main"
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="different plan"):
        loop.admit_campaign(changed)
