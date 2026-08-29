"""One issue, one run, one settle: the daemon end to end over a fake source
and a fake engine that narrates the pipeline the real one runs."""

from __future__ import annotations

from pathlib import Path

from sbxloop.config import Config
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.model import RunResult
from sbxloop.engine.store import StateStore
from sbxloop.events import EventBus, HostEventTypes
from tests.unit.test_daemon_loop import PR_URL, FakeSource, RecordingFrontend, gh_item

PIPELINE = ("gating", "delivering", "reviewing", "fixing", "awaiting_ci", "landing")


class FakeEngineRunner:
    """Plays the engine: walks the run through the pipeline's states on the
    bus, records the PR the way `_stage_deliver` does, and ends as told."""

    def __init__(self, store: StateStore, *, end: str = "merged") -> None:
        self.store = store
        self.end = end
        self.emitted: list[str] = []

    def __call__(
        self, item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
    ) -> RunResult:
        bus.subscribe(lambda ev: self.emitted.append(ev.type))
        self.store.create_run(run_id, item.title)
        for state in ("provisioning", "decomposing", "building", *PIPELINE):
            self.store.set_run_state(run_id, state)  # type: ignore[arg-type]
            bus.emit(HostEventTypes.RUN_STATE, run_id, state=state)
            if state == "delivering":
                self.store.set_run_pr(
                    run_id, number=9, url=PR_URL, branch=f"sbxloop/{run_id}", head_sha="abc"
                )
                bus.emit(HostEventTypes.RUN_DELIVER, run_id, repo="o/r", pr=9, url=PR_URL)
            if state == "reviewing":
                bus.emit(HostEventTypes.REVIEW_VERDICT, run_id, round=1, verdict="request_changes")
            if state == "fixing":
                self.store.bump_run_counter(run_id, "review_rounds")
                bus.emit(HostEventTypes.FIX_ROUND, run_id, round=1, kind="review")
            if state == "awaiting_ci":
                bus.emit(HostEventTypes.CI_STATUS, run_id, state="pending")
                bus.emit(HostEventTypes.CI_STATUS, run_id, state="green", total=3)
        reason = None
        if self.end == "merged":
            bus.emit(HostEventTypes.RUN_MERGED, run_id, pr=9, url=PR_URL, sha="m")
        else:
            reason = "its draft status could not be cleared"
            self.store.set_run_reason(run_id, reason)
            bus.emit(HostEventTypes.RUN_BLOCKED, run_id, pr=9, url=PR_URL, why=reason)
        self.store.set_run_state(run_id, self.end)  # type: ignore[arg-type]
        bus.emit(HostEventTypes.RUN_END, run_id, state=self.end)
        return RunResult(
            run_id=run_id,
            state=self.end,
            pr_number=9,
            pr_url=PR_URL,
            reason=reason,  # type: ignore[arg-type]
        )


def _daemon(
    tmp_path: Path, end: str
) -> tuple[DaemonLoop, FakeSource, RecordingFrontend, DaemonStore, StateStore]:
    config = Config.model_validate(
        {"state_dir": str(tmp_path / "state"), "github": {"repo": "o/r"}}
    )
    store = StateStore(config.state_dir / "state.db")
    dstore = DaemonStore(config.state_dir / "state.db")
    source = FakeSource([gh_item("12", requested_by="4242")])
    front = RecordingFrontend()
    loop = DaemonLoop(
        config,
        store=store,
        dstore=dstore,
        source=source,
        runner=FakeEngineRunner(store, end=end),
        frontend=front,
    )
    return loop, source, front, dstore, store


def test_an_issue_becomes_a_merged_pr_and_a_closed_issue(tmp_path: Path) -> None:
    loop, source, front, dstore, store = _daemon(tmp_path, "merged")
    result = loop.tick()
    assert result.dispatched == "gh:issue:12" and result.outcome == "done"
    item = dstore.get("gh:issue:12")
    assert item is not None and item.state == "done" and item.pending_report is None
    assert item.requested_by == "4242"
    # The source heard: claim, start, merge — and nothing was filed.
    assert [c[0] for c in source.calls] == ["claim", "started", "merged"]
    assert source.calls[-1] == ("merged", (9, PR_URL))
    # The report the frontend saw is the run record's.
    (_, report) = front.finished[0]
    assert isinstance(report, RunReport) and report.state == "merged"
    assert report.pr == (9, PR_URL) and report.rounds == 1 and report.succeeded
    # Notices: queued, then done — addressed to the run.
    kinds = [n.kind for n in front.notices]
    assert kinds == ["item.queued", "run.done"]
    done = front.notices[-1]
    assert done.run_id == item.run_id and done.url == PR_URL and done.item_id == "gh:issue:12"
    # Ledger closed as done; the engine row ended merged with the PR on it.
    assert store.get_run(item.run_id or "").state == "merged"  # type: ignore[arg-type]
    assert store.get_run(item.run_id or "").pr_number == 9  # type: ignore[arg-type]
    assert loop.tick().idle_reason == "no_work"


def test_a_blocked_run_hands_the_pr_to_a_human(tmp_path: Path) -> None:
    loop, source, front, dstore, _ = _daemon(tmp_path, "blocked")
    assert loop.tick().outcome == "blocked"
    item = dstore.get("gh:issue:12")
    assert item is not None and item.state == "blocked"
    assert item.last_error == "its draft status could not be cleared"
    assert source.calls[-1] == (
        "blocked",
        ("its draft status could not be cleared", 9, PR_URL),
    )
    blocked = [n for n in front.notices if n.kind == "run.blocked"]
    assert len(blocked) == 1 and blocked[0].level == "error" and blocked[0].url == PR_URL
    (_, report) = front.finished[0]
    assert report.state == "blocked" and not report.succeeded
    assert report.reason == "its draft status could not be cleared"
    # Nothing is retried on its own; the breaker did not count it.
    assert loop.status()["consecutive_failures"] == 0
    assert loop.tick().idle_reason == "no_work"


def test_the_requester_reaches_the_frontend_with_the_item(tmp_path: Path) -> None:
    loop, _, _front, _dstore, _ = _daemon(tmp_path, "merged")

    seen: list[str | None] = []

    class Front(RecordingFrontend):
        def run_started(self, item: WorkItem, *a: object) -> None:
            seen.append(item.requested_by)

    loop.frontend = Front()
    loop.tick()
    assert seen == ["4242"]
