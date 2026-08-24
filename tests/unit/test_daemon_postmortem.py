"""Post-mortems the daemon files for its own failures (discovery lane)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sbxloop.config import Config
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.postmortem import DOSSIER_MAX_CHARS, build_dossier, postmortem_marker
from sbxloop.daemon.sources import PrSnapshot
from sbxloop.engine.model import TaskRecord, TaskSpec
from sbxloop.engine.store import StateStore
from sbxloop.events import Event
from sbxloop.gh.ops import ChecksVerdict
from tests.unit.test_daemon_loop import FakeSource, Harness, RecordingFrontend, inbox_item


def gh_item(kind: str = "patch") -> WorkItem:
    return WorkItem(
        item_id="gh:4",
        source="github",
        source_key="4",
        kind=kind,  # type: ignore[arg-type]
        title="Fix the thing",
        url="https://x/4",
        attempts=2,
    )


def seed_failed_run(store: StateStore, run_id: str) -> None:
    store.create_run(run_id, "outcome")
    spec = TaskSpec(
        id="t1",
        title="Do it",
        verify_commands=["uv run pytest -q", "sh -c \"git status | awk '{print $2}'\""],
    )
    store.save_tasks(run_id, [spec])
    store.update_task(
        run_id,
        TaskRecord(
            spec=spec,
            state="failed",
            revisions=2,
            replans=1,
            last_feedback="verify keeps failing",
        ),
    )
    store.record_phase(
        run_id,
        "verify",
        task_id="t1",
        attempt=1,
        status="failed",
        output_json=json.dumps(
            {"results": "$ uv run pytest -q\n(exit 0)\n\n$ sh -c ...\n(exit 1)\n M README.md"}
        ),
        started_at=1.0,
    )
    store.append_event(
        Event.now(
            "phase.end",
            run_id,
            task_id="t1",
            phase="verify",
            status="failed",
            message="verify command failed: `sh -c ...` (exit 1)",
        )
    )
    store.append_event(Event.now("agent.message_delta", run_id, delta="noise"))
    store.set_run_state(run_id, "failed")


class TestDossier:
    def test_carries_plan_transcript_failures_and_marker_ready_body(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "state.db")
        seed_failed_run(store, "r1")
        text = build_dossier(
            store, gh_item(), ["r1"], "abandoned: run ended failed", state_dir="/s"
        )
        assert text.startswith("# Post-mortem: Fix the thing")
        assert "ended **abandoned: run ended failed** after 2 attempt(s)" in text
        assert "## Run `r1`" in text and "State: **failed**" in text
        assert "Verify commands (decomposer-authored, run under `sh -c`):" in text
        assert "sh -c \"git status | awk '{print $2}'\"" in text
        assert "Last verify attempt (failed, attempt 1):" in text and "M README.md" in text
        assert "verify command failed" in text  # failure events section
        assert "noise" not in text  # deltas are filtered out
        assert "SBXLOOP_STATE_DIR=/s sbxloop logs <run>" in text
        assert "File ONE finding per distinct root cause" in text
        assert postmortem_marker("r1") == "<!-- sbxloop-postmortem r1 -->"

    def test_missing_run_and_clipping(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "state.db")
        text = build_dossier(store, gh_item(), ["nope"], "abandoned")
        assert "no persisted run" in text
        # Per-run sections are already tailed; many runs still add up, and
        # the whole must fit an issue body.
        for i in range(12):
            store.create_run(f"big{i}", "o")
            for j in range(40):
                store.append_event(Event.now("worker.error", f"big{i}", message="x" * 300 + str(j)))
            store.set_run_state(f"big{i}", "failed")
        clipped = build_dossier(store, gh_item(), [f"big{i}" for i in range(12)], "abandoned")
        assert len(clipped) <= DOSSIER_MAX_CHARS
        assert "dossier clipped" in clipped


class GithubLikeSource(FakeSource):
    """A fake that also speaks the github source's post-mortem method."""

    name = "github"

    def __init__(self) -> None:
        super().__init__()
        self.postmortems: list[tuple[str, str, str]] = []

    def file_postmortem(self, item: WorkItem, dossier: str, run_id: str) -> str:
        self.postmortems.append((item.item_id, run_id, dossier))
        return f"gh:{900 + len(self.postmortems)}"


def github_harness(tmp_path: Path, **daemon: Any) -> Harness:
    cfg = Config.model_validate(
        {
            "state_dir": str(tmp_path / "state"),
            "github": {"repo": "o/r"},
            "daemon": {"max_attempts_per_item": 1, **daemon},
        }
    )
    h = Harness(tmp_path, cfg)
    h.source = GithubLikeSource()
    h.loop.sources = [h.source]
    return h


class TestFiling:
    def test_abandoned_patch_item_files_one_postmortem(self, tmp_path: Path) -> None:
        h = github_harness(tmp_path)
        front = RecordingFrontend()
        h.loop.frontend = front  # type: ignore[assignment]
        h.source.items = [gh_item()]
        h.outcomes = ["failed"]
        assert h.loop.tick().outcome == "abandoned"
        assert len(h.source.postmortems) == 1  # type: ignore[attr-defined]
        item_id, run_id, dossier = h.source.postmortems[0]  # type: ignore[attr-defined]
        assert item_id == "gh:4" and dossier.startswith("# Post-mortem: Fix the thing")
        # The notice names the item (so the bridge threads it) and links the charter.
        assert front.seen[-1] == (
            "🔎 post-mortem [#901](https://github.com/o/r/issues/901) filed for gh:4"
            " · abandoned: run ended failed"
        )
        assert h.dstore.postmortem_filed(run_id)
        # never twice for the same run
        h.loop._file_postmortem(gh_item(), run_id, "again")
        assert len(h.source.postmortems) == 1  # type: ignore[attr-defined]

    def test_delivery_failure_files_too(self, tmp_path: Path) -> None:
        h = github_harness(tmp_path)
        h.source.items = [gh_item()]
        h.outcomes = ["deliver_fail"]
        assert h.loop.tick().outcome == "delivery_failed"
        assert len(h.source.postmortems) == 1  # type: ignore[attr-defined]
        assert "delivery failed" in h.source.postmortems[0][2]  # type: ignore[attr-defined]

    def test_never_for_audit_items_or_when_disabled(self, tmp_path: Path) -> None:
        h = github_harness(tmp_path)
        h.source.items = [gh_item(kind="audit")]
        h.outcomes = ["failed"]
        assert h.loop.tick().outcome == "abandoned"
        assert h.source.postmortems == []  # type: ignore[attr-defined]
        h2 = github_harness(tmp_path / "b", postmortems=False)
        h2.source.items = [gh_item()]
        h2.outcomes = ["failed"]
        h2.loop.tick()
        assert h2.source.postmortems == []  # type: ignore[attr-defined]

    def test_daily_cap(self, tmp_path: Path) -> None:
        h = github_harness(tmp_path, postmortems_per_day=1)
        h.dstore.record_postmortem("earlier", "gh:1", "gh:800", h.clock.t)
        h.source.items = [gh_item()]
        h.outcomes = ["failed"]
        h.loop.tick()
        assert h.source.postmortems == []  # type: ignore[attr-defined]

    def test_inbox_source_has_no_postmortem_path(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)  # FakeSource: no file_postmortem
        h.source.items = [inbox_item()]
        h.outcomes = ["failed"]
        h.loop.tick()  # must not raise


class ReviewingSource(GithubLikeSource):
    def __init__(self) -> None:
        super().__init__()
        self.reviews: list[tuple[str, int, str]] = []
        self.checks = ChecksVerdict("green", 1, (), ())

    def file_review(self, item: WorkItem, pr_number: int, pr_url: str, run_id: str) -> str:
        self.reviews.append((item.item_id, pr_number, run_id))
        return f"gh:{800 + len(self.reviews)}"

    def pr_state(self, pr_number: int) -> PrSnapshot:
        return PrSnapshot(self.checks, "NONE", False, "open")


def reviewing_harness(tmp_path: Path, **daemon: Any) -> Harness:
    h = github_harness(tmp_path, **daemon)
    h.source = ReviewingSource()
    h.loop.sources = [h.source]
    return h


class TestDeliveryReviews:
    """The loop evaluating the code it wrote — but only once the build has
    reported. CI is GitHub's compute and is free; a review run spent
    on a red PR is spent on work that has to change anyway.
    """

    def test_a_review_is_filed_only_once_the_pr_is_green(self, tmp_path: Path) -> None:
        h = reviewing_harness(tmp_path)
        front = RecordingFrontend()
        h.loop.frontend = front  # type: ignore[assignment]
        h.source.items = [gh_item()]
        # Delivery alone reviews nothing: the item waits for its checks.
        assert h.loop.tick().outcome == "reviewing"
        assert h.source.reviews == []  # type: ignore[attr-defined]
        # Green: now the review run is worth spending, and exactly once.
        h.loop.tick()
        assert h.source.reviews == [("gh:4", 9, h.runs[0][0])]  # type: ignore[attr-defined]
        h.loop.tick()
        assert len(h.source.reviews) == 1  # type: ignore[attr-defined]
        assert any("review" in t and "#801" in t for t in front.seen)

    def test_a_red_pr_is_never_reviewed(self, tmp_path: Path) -> None:
        """The saving: a red PR spends a fix round, not a review run and then
        a fix round."""
        h = reviewing_harness(tmp_path)
        h.source.checks = ChecksVerdict("red", 1, (), ("lint",))  # type: ignore[attr-defined]
        h.source.items = [gh_item()]
        assert h.loop.tick().outcome == "reviewing"
        h.loop.tick()
        assert h.source.reviews == []  # type: ignore[attr-defined]
        assert h.dstore.get("gh:4").state == "queued"  # type: ignore[union-attr]

    def test_not_for_audits_no_delivery_or_disabled(self, tmp_path: Path) -> None:
        h = reviewing_harness(tmp_path)
        h.source.items = [gh_item(kind="audit")]
        h.loop.tick()
        h.loop.tick()
        assert h.source.reviews == []  # type: ignore[attr-defined]  # audits have no PR
        h2 = reviewing_harness(tmp_path / "b", review_deliveries=False)
        h2.source.items = [gh_item()]
        h2.loop.tick()
        h2.loop.tick()
        assert h2.source.reviews == []  # type: ignore[attr-defined]
