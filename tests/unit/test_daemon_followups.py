"""Follow-ups from a run the daemon lands (#517, plan S-A7).

A run parked at the merge gate or on a review wait is finished by the
daemon with gh ops alone. The out-of-scope notes its reviews left are filed
then too, exactly once, with the same body and marker an engine-merged run
writes; a landing that does not merge files nothing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from sbxloop.config import Config
from sbxloop.engine.followups import (
    FollowupFiler,
    collect_followups,
    followup_key,
    followup_marker,
    issue_body,
    marker_key,
    recorded_review_rounds,
)
from sbxloop.engine.issue_lookup import LookupReceipt, fingerprint
from sbxloop.engine.review import Followup, ReviewRound, ReviewVerdict
from sbxloop.events import EventBus, HostEventTypes
from tests.fakes.fake_github import BLOCKED_405, FakeGithub, human_review
from tests.unit.test_daemon_loop import PR_URL, Harness
from tests.unit.test_daemon_merge_gate import FakeDaemonGithub, gated_harness, park
from tests.unit.test_daemon_review_hold import landed, park as park_for_review, review_harness

REPO = "o/r"
TITLE = "the retry loop never backs off"


def followup(n: int = 1, title: str = TITLE) -> Followup:
    return Followup(
        title=title,
        body="a failing call is retried at once, forever",
        path="src/client.py",
        line=40,
        lookup_id=f"lookup-{n}",
        decision="new",
        rationale="No issue covers the retry policy.",
    )


def seed_review(h: Harness, run_id: str, *notes: Followup) -> None:
    """What a run's review leaves on its record: the verdict with its
    out-of-scope notes, and the completed issue lookup for each."""
    verdict = ReviewVerdict(verdict="approve", summary="good", followups=list(notes))
    h.store.record_phase(
        run_id,
        "review",
        task_id=None,
        attempt=1,
        status="approve",
        output_json=json.dumps({"verdict": verdict.model_dump()}),
        started_at=time.time(),
    )
    for n, note in enumerate(notes, start=1):
        receipt = LookupReceipt(
            lookup_id=f"lookup-{n}",
            repo=REPO,
            fingerprint=fingerprint(note),
            queries=["retry"],
            issues=[],
        )
        h.store.record_phase(
            run_id,
            "followup_lookup",
            task_id=None,
            attempt=n,
            status="checked",
            output_json=receipt.model_dump_json(),
            started_at=time.time(),
        )


def followup_events(h: Harness, run_id: str) -> list[Any]:
    return [
        event
        for _seq, event in h.store.events(run_id)
        if event.type == HostEventTypes.RUN_FOLLOWUPS
    ]


def gate_ready(tmp_path: Path, config: Config | None = None) -> tuple[Harness, FakeGithub, str]:
    h = gated_harness(tmp_path, config)
    run_id = park(h)
    seed_review(h, run_id, followup())
    fake = FakeGithub(number=9)
    fake.pr["html_url"] = PR_URL
    h.loop.github = FakeDaemonGithub(fake)  # type: ignore[assignment]
    return h, fake, run_id


def engine_pass(h: Harness, fake: FakeGithub, run_id: str) -> None:
    """What the engine's landing does when it parks: file the follow-ups
    and record them, before the daemon ever sees the gate approved."""
    bus = EventBus()
    bus.subscribe(h.store.append_event)
    filer = FollowupFiler(
        fake, REPO, h.store, bus, h.config, trigger_label=h.config.labels_for(REPO).trigger
    )
    filer.file(
        h.store.get_run(run_id), recorded_review_rounds(h.store, run_id), issues_enabled=True
    )


def approve(h: Harness, run_id: str) -> None:
    gate = h.dstore.merge_gate_for(run_id)
    assert gate is not None and h.dstore.claim_merge_gate(run_id)
    h.loop._complete_landing(gate, "Discord user `brett`")


class TestGatedApproval:
    def test_an_approved_gate_files_the_followups(self, tmp_path: Path) -> None:
        h, fake, run_id = gate_ready(tmp_path)
        approve(h, run_id)
        assert fake.merges
        ((title, body, labels),) = fake.issues_created
        assert title == TITLE
        assert labels == ["sbxloop:follow-up"]
        assert marker_key(body) == (run_id, followup_key(TITLE))
        assert f"Out of scope for [PR #9]({PR_URL}) (issue #1)" in body
        # The daemon watches the repository: the body names its trigger.
        trigger = h.config.labels_for(REPO).trigger
        assert f"add the `{trigger}` label if you want it run" in body
        assert any(c.startswith("## Follow-ups") for c in fake.issue_comments_posted)
        (event,) = followup_events(h, run_id)
        assert event.data["mode"] == "issues"
        assert [f["title"] for f in event.data["filed"]] == [TITLE]

    def test_a_second_approval_files_nothing_twice(self, tmp_path: Path) -> None:
        h, fake, run_id = gate_ready(tmp_path)
        approve(h, run_id)
        comments = [c for c in fake.issue_comments_posted if c.startswith("## Follow-ups")]
        # A second landing of the same gate (an approval a restart
        # interrupted after the merge, approved again): land() settles
        # Landed against the merged PR, and the filing runs again.
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None
        h.loop._complete_landing(gate, "Discord user `brett`")
        assert h.dstore.merge_gate_for(run_id).state == "merged"  # type: ignore[union-attr]
        assert len(fake.issues_created) == 1
        again = [c for c in fake.issue_comments_posted if c.startswith("## Follow-ups")]
        assert again == comments
        # A pass that files nothing new reports nothing: one event, not a
        # second one calling the run's own issue "already tracked".
        (event,) = followup_events(h, run_id)
        assert "reused" not in event.data

    def test_the_engine_filing_at_the_park_is_reported_once(self, tmp_path: Path) -> None:
        """The engine files when its landing parks; the daemon's pass after
        the merge finds every note recorded and stays quiet."""
        h, fake, run_id = gate_ready(tmp_path)
        engine_pass(h, fake, run_id)
        assert len(fake.issues_created) == 1
        approve(h, run_id)
        assert fake.merges
        assert len(fake.issues_created) == 1
        (event,) = followup_events(h, run_id)
        assert [f["title"] for f in event.data["filed"]] == [TITLE]
        assert "reused" not in event.data

    def test_the_engine_checklist_at_the_park_is_reported_once(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repo": REPO},
                "landing": {"followups": "comment"},
            }
        )
        h, fake, run_id = gate_ready(tmp_path, cfg)
        engine_pass(h, fake, run_id)
        approve(h, run_id)
        assert fake.merges
        checklists = [c for c in fake.issue_comments_posted if c.startswith("## Follow-ups")]
        assert len(checklists) == 1
        (event,) = followup_events(h, run_id)
        assert event.data["mode"] == "comment"

    def test_followups_the_run_already_filed_are_not_filed_again(self, tmp_path: Path) -> None:
        """The engine files at the park too; the daemon's pass after the
        merge finds them on the repository by marker."""
        h, fake, run_id = gate_ready(tmp_path)
        fake.existing_issues = [
            {
                "number": 4,
                "title": TITLE,
                "body": "noted\n" + followup_marker(run_id, followup_key(TITLE)),
                "state": "open",
                "html_url": f"https://github.com/{REPO}/issues/4",
            }
        ]
        approve(h, run_id)
        assert fake.merges
        assert fake.issues_created == []

    def test_a_landing_that_does_not_merge_files_nothing(self, tmp_path: Path) -> None:
        h, fake, run_id = gate_ready(tmp_path)
        fake.merge_outcomes = [BLOCKED_405]
        approve(h, run_id)
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and gate.state == "open", "the landing did not merge"
        assert fake.issues_created == []
        assert followup_events(h, run_id) == []

    def test_followups_off_files_nothing(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repo": REPO},
                "landing": {"followups": "off"},
            }
        )
        h, fake, run_id = gate_ready(tmp_path, cfg)
        approve(h, run_id)
        assert fake.merges
        assert fake.issues_created == []
        assert not any(c.startswith("## Follow-ups") for c in fake.issue_comments_posted)

    def test_the_per_run_cap_holds(self, tmp_path: Path) -> None:
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repo": REPO},
                "landing": {"max_followups_per_run": 1, "followup_label": "later"},
            }
        )
        h = gated_harness(tmp_path, cfg)
        run_id = park(h)
        seed_review(h, run_id, followup(1), followup(2, "the cache is never evicted"))
        fake = FakeGithub(number=9)
        fake.pr["html_url"] = PR_URL
        h.loop.github = FakeDaemonGithub(fake)  # type: ignore[assignment]
        approve(h, run_id)
        assert [(t, labels) for t, _, labels in fake.issues_created] == [(TITLE, ["later"])]


class TestReviewApproval:
    def test_an_approved_review_wait_files_the_followups(self, tmp_path: Path) -> None:
        h, fake = review_harness(tmp_path)
        run_id = park_for_review(h)
        seed_review(h, run_id, followup())
        fake.reviews_payload = [
            human_review("alice", "APPROVED", "", id=1),
            human_review("bob", "APPROVED", "", id=2),
        ]
        h.clock.t += 600
        h.loop.tick()
        landed(h, run_id)
        assert fake.merges
        ((title, body, _labels),) = fake.issues_created
        assert title == TITLE and marker_key(body) == (run_id, followup_key(TITLE))
        assert len(followup_events(h, run_id)) == 1


class TestIssueBody:
    def _candidate(self) -> Any:
        (cand,) = collect_followups(
            [
                ReviewRound(
                    1, ReviewVerdict(verdict="approve", summary="x", followups=[followup()]), ""
                )
            ]
        )
        return cand

    def test_without_attribution_the_body_is_unchanged(self) -> None:
        cand = self._candidate()
        kwargs: dict[str, Any] = {
            "run_id": "r1",
            "repo": REPO,
            "pr_number": 7,
            "pr_url": "https://x/pull/7",
            "closes": None,
        }
        body = issue_body(cand, **kwargs)
        assert body == "\n".join(
            [
                "a failing call is retried at once, forever",
                "",
                "Where: `src/client.py:40`",
                "",
                "Out of scope for [PR #7](https://x/pull/7), noted by the review in round 1; "
                "run `r1` on `o/r`.",
                "Filed by sbxloop after that pull request merged. It is **not** queued for "
                "the loop.",
                "",
                followup_marker("r1", cand.key),
            ]
        )
        assert issue_body(cand, filed_by=None, **kwargs) == body

    def test_attribution_names_who_filed_it_and_keeps_the_marker(self) -> None:
        cand = self._candidate()
        body = issue_body(
            cand,
            run_id="r1",
            repo=REPO,
            pr_number=7,
            pr_url="",
            closes=None,
            filed_by="critic",
        )
        assert "Filed on behalf of critic." in body
        assert body.endswith(followup_marker("r1", cand.key))

    def test_the_filer_passes_attribution_through(self, tmp_path: Path) -> None:
        from sbxloop.engine.followups import FollowupFiler

        h = gated_harness(tmp_path)
        run_id = park(h)
        seed_review(h, run_id, followup())
        fake = FakeGithub(number=9)
        filer = FollowupFiler(fake, REPO, h.store, EventBus(), h.config, trigger_label=None)
        run = h.store.get_run(run_id)
        rounds = [
            ReviewRound(
                1, ReviewVerdict(verdict="approve", summary="x", followups=[followup()]), ""
            )
        ]
        filer.file(run, rounds, issues_enabled=True, attribution="critic")
        ((_title, body, _labels),) = fake.issues_created
        assert "Filed on behalf of critic." in body
