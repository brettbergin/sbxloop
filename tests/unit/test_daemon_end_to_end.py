"""One issue, one run, one settle: the daemon end to end over a fake source
and a fake engine that narrates the pipeline the real one runs.

The other end-to-end path — a concierge exchange over the Discord bridge, where
a clarifying question is answered by *clicking* a choice rather than typing
(#564) — lives in :mod:`tests.unit.test_daemon_clarify_end_to_end`, a sibling
module to read alongside this one.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.model import RunReport, WorkItem
from sbxloop.daemon.sources import CLAIM_MARKER, GitHubIssueSource
from sbxloop.daemon.store import DaemonStore, PriorAttempt
from sbxloop.engine.model import RunResult
from sbxloop.engine.store import StateStore
from sbxloop.errors import RunCancelledError
from sbxloop.events import EventBus, HostEventTypes
from tests.unit.test_daemon_loop import (
    PR_URL,
    Clock,
    FakeSource,
    RecordingFrontend,
    gh_item,
)
from tests.unit.test_daemon_sources import FIXTURE_NOW, LABELS, RecordingOps, issue

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


# --------------------------------------------------------------------------
# #600: re-adding the trigger label restarts a finished item, and the restart
# continues the branch and PR the previous attempt already pushed to origin.
#
# The daemon runs against the real `GitHubIssueSource` here — the label trail
# and the claim comments are what a human sees on the issue, and the point of
# the change is that *only* re-adding the label is needed: no issue edit, no
# `!sbx retry`. The GitHub end is `RecordingOps` from test_daemon_sources,
# whose label writes actually move labels the way GitHub does.
# --------------------------------------------------------------------------


class IssueOps(RecordingOps):
    """`RecordingOps` with the two things a *restart* needs GitHub to model:
    comments that carry increasing timestamps, and a ``labeled`` event each
    time the trigger label goes back on — the event the source reads to find
    where the current claim cycle starts. Without it, a second claim on the
    same issue is judged against the first cycle's claim comment and loses
    the race to itself."""

    def __init__(self, issues: dict[str, dict[str, Any]]) -> None:
        super().__init__(issues)
        self._minute = 0

    def _stamp(self) -> str:
        self._minute += 1
        return f"2026-08-15T10:{self._minute:02d}:00Z"

    def issue_comment(self, repo: str, number: int, body: str) -> str:
        self.comments.append((number, body))
        self.add_comment(body, created_at=self._stamp())
        return "https://c"

    def raw(self, method: str, path: str, body: Any = None) -> Any:
        result = super().raw(method, path, body)
        base = path.partition("?")[0]
        if method == "POST" and base.endswith("/labels"):
            number = base.rsplit("/", 2)[-2]
            for name in (body or {}).get("labels") or []:
                self.events.setdefault(number, []).append(
                    {"event": "labeled", "label": {"name": name}, "created_at": self._stamp()}
                )
        return result

    def add_trigger_label(self, number: str, label: str) -> None:
        """A human clicking the label back on, event and all."""
        row = self.issues[number]
        names = {lb["name"] for lb in row["labels"]}
        row["labels"] = [{"name": n} for n in sorted(names | {label})]
        self.events.setdefault(number, []).append(
            {"event": "labeled", "label": {"name": label}, "created_at": self._stamp()}
        )


class RestartHarness:
    """One issue, one daemon, a scripted engine — driven over the real
    GitHub issue source so poll → claim → dispatch is exercised whole."""

    def __init__(self, tmp_path: Path) -> None:
        self.config = Config.model_validate(
            {
                "state_dir": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": {"max_attempts_per_item": 1, "refresh_workspace": False},
            }
        )
        self.store = StateStore(self.config.state_dir / "state.db")
        self.dstore = DaemonStore(self.config.state_dir / "state.db")
        self.ops = IssueOps({"12": issue(12, "sbxloop:run")})
        self.source = GitHubIssueSource(
            lambda: self.ops,  # type: ignore[arg-type,return-value]
            "o/r",
            LABELS,
            host="db",
            clock=lambda: FIXTURE_NOW,
        )
        self.clock = Clock()
        # What each dispatch was handed and what it did: (run_id, prior).
        self.dispatches: list[tuple[str, PriorAttempt | None]] = []
        self.outcomes: list[str] = []
        self.loop = DaemonLoop(
            self.config,
            store=self.store,
            dstore=self.dstore,
            source=self.source,
            runner=self.runner,
            clock=self.clock,
        )

    # -- the engine, as far as the daemon can see it -------------------------

    def runner(
        self, item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
    ) -> RunResult:
        """Deliver a PR, then end as the script says.

        A restart adopts the previous attempt's branch and PR exactly as
        :meth:`LoopEngine._adopt_prior_artifacts` does: the daemon offers
        them through ``dstore.prior_attempt`` (that is what the real
        ``_default_runner`` reads), and a delivery onto an adopted branch
        keeps the branch name and the PR number rather than minting new ones.
        """
        prior = self.dstore.prior_attempt(item.item_id)
        self.dispatches.append((run_id, prior))
        self.store.create_run(run_id, item.title)
        branch = (prior.branch if prior else None) or f"sbxloop/{run_id}"
        number = (prior.pr_number if prior else None) or 9
        self.store.set_run_pr(
            run_id, number=number, url=PR_URL, branch=branch, head_sha=f"sha-{run_id}"
        )
        kind = self.outcomes.pop(0) if self.outcomes else "merged"
        if kind == "cancelled":
            # Mid-flight, so the persisted run stays resumable: this is the
            # shape an operator `!sbx cancel` leaves behind.
            self.store.set_run_state(run_id, "building")
            self.cancel_now()
            raise RunCancelledError(f"run {run_id} interrupted")
        if kind == "failed":
            self.store.set_run_reason(run_id, "the build never went green")
            self.store.set_run_state(run_id, "failed")
            return RunResult(
                run_id=run_id,
                state="failed",
                pr_number=number,
                pr_url=PR_URL,
                reason="the build never went green",
            )
        if kind == "blocked":
            self.store.set_run_reason(run_id, "its draft status could not be cleared")
            self.store.set_run_state(run_id, "blocked")
            return RunResult(
                run_id=run_id,
                state="blocked",
                pr_number=number,
                pr_url=PR_URL,
                reason="its draft status could not be cleared",
            )
        self.store.set_run_state(run_id, "merged")
        return RunResult(run_id=run_id, state="merged", pr_number=number, pr_url=PR_URL)

    def cancel_now(self) -> None:
        """The operator's `!sbx cancel`, from inside the run's own thread:
        the loop settles it as cancelled without a retry."""
        self.loop.cancel_current("Discord user `brett`")

    # -- what a human does on the issue --------------------------------------

    def readd_trigger_label(self) -> None:
        """A human clicks the trigger label back on. The issue body and
        title are untouched — that is the whole point of #600 — and no
        other label is added: the trigger label is the only restart
        gesture there is."""
        assert LABELS.trigger not in self.labels_now, (
            "the claim should have taken the trigger label off"
        )
        self.ops.add_trigger_label("12", LABELS.trigger)

    # -- what the issue looks like afterwards --------------------------------

    @property
    def labels_now(self) -> set[str]:
        return {label["name"] for label in self.ops.issues["12"]["labels"]}

    @property
    def comments(self) -> list[str]:
        return [body for _, body in self.ops.comments]

    def claims(self) -> list[str]:
        return [body for body in self.comments if CLAIM_MARKER in body]


def _first_attempt(h: RestartHarness, ending: str) -> tuple[str, str]:
    """Run the item once and leave it finished in ``ending``. Returns the
    run id and the branch that attempt pushed."""
    h.outcomes = [ending]
    if ending == "cancelled":
        t = threading.Thread(target=h.loop.tick)
        t.start()
        t.join(10)
        assert not t.is_alive()
    else:
        h.loop.tick()
    item = h.dstore.get("gh:issue:12")
    assert item is not None and item.state == ending, item
    run_id = h.dispatches[0][0]
    return run_id, f"sbxloop/{run_id}"


@pytest.mark.parametrize("ending", ["cancelled", "failed", "blocked"])
def test_readding_the_label_restarts_the_item_and_keeps_its_pushed_work(
    tmp_path: Path, ending: str
) -> None:
    """The whole of #600 in one pass, for each way an attempt can end.

    Before: the issue text was unchanged, so the store deduplicated the
    re-triggered issue and the label sat there inert — an operator `!sbx
    retry` was the only way back in, and it threw away the branch.
    """
    h = RestartHarness(tmp_path)
    first_run, branch = _first_attempt(h, ending)
    # The attempt pushed a branch and opened PR #9; the daemon remembered
    # both against the item so a restart can continue them.
    prior = h.dstore.prior_attempt("gh:issue:12")
    assert prior is not None
    assert prior.run_id == first_run and prior.branch == branch and prior.pr_number == 9
    # Nothing more happens on its own: the label came off with the claim.
    assert LABELS.trigger not in h.labels_now
    assert h.loop.tick().dispatched is None

    # A human re-adds the trigger label. Nothing else: the issue is not
    # edited, and no operator command is issued.
    h.readd_trigger_label()
    h.outcomes = ["merged"]
    h.clock.t += 100_000  # past any retry backoff
    result = h.loop.tick()

    # The next poll claimed it and dispatched a *new* run.
    assert result.dispatched == "gh:issue:12" and result.outcome == "done"
    assert len(h.dispatches) == 2
    second_run, offered = h.dispatches[1]
    assert second_run != first_run
    # …pinned to the previous attempt's branch and PR, not fresh ones.
    assert offered is not None
    assert offered.branch == branch and offered.pr_number == 9
    record = h.store.get_run(second_run)
    assert record.branch == branch and record.pr_number == 9
    # The first attempt's run row is untouched: its commits' branch is the
    # one the second run continued, so nothing was re-created from scratch.
    assert h.store.get_run(first_run).branch == branch

    # The issue trail records the new claim: two claim comments, the second
    # naming the restart and the work it continues.
    claims = h.claims()
    assert len(claims) == 2
    assert "Restarted by re-adding `sbxloop:run`" in claims[1]
    assert f"branch `{branch}`" in claims[1] and "PR #9" in claims[1]
    assert "sbxloop daemon claimed this issue" in claims[1]
    # …and the labels moved: the trigger was consumed by the claim and the
    # previous attempt's terminal label (if any) was cleared for it.
    assert LABELS.trigger not in h.labels_now
    assert LABELS.failed not in h.labels_now and LABELS.blocked not in h.labels_now
    # No operator command was involved: nothing on the issue asked for one
    # as the way back in, and the daemon's own comments say re-adding the
    # label is what restarts it.
    assert not any(c.strip().startswith("!sbx retry") for c in h.comments)
    settle = [c for c in h.comments if "sbxloop:run" in c and CLAIM_MARKER not in c]
    assert settle, "the finished attempt should have said how to restart it"
    assert all("an unchanged issue is deduplicated" not in c for c in settle)
    assert h.dstore.get("gh:issue:12").state == "done"  # type: ignore[union-attr]


def test_the_restart_needs_no_operator_retry_command(tmp_path: Path) -> None:
    """The label alone is sufficient: no `!sbx retry`, no `retry_item`."""
    h = RestartHarness(tmp_path)
    _first_attempt(h, "cancelled")

    called: list[str] = []
    original = h.loop.retry_item
    h.loop.retry_item = lambda *a, **k: (  # type: ignore[assignment,method-assign]
        called.append("retry"),
        original(*a, **k),
    )[1]

    h.readd_trigger_label()
    h.outcomes = ["merged"]
    h.clock.t += 100_000
    assert h.loop.tick().outcome == "done"
    assert called == []


def test_a_restart_with_nothing_on_origin_starts_fresh(tmp_path: Path) -> None:
    """No branch was ever pushed (the attempt died before delivering): the
    restart still happens, it simply starts from nothing."""
    h = RestartHarness(tmp_path)

    def barren(item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool) -> RunResult:
        h.dispatches.append((run_id, h.dstore.prior_attempt(item.item_id)))
        h.store.create_run(run_id, item.title)
        h.store.set_run_reason(run_id, "the sandbox never came up")
        h.store.set_run_state(run_id, "failed")
        return RunResult(run_id=run_id, state="failed", reason="the sandbox never came up")

    h.loop._runner = barren
    h.loop.tick()
    assert h.dstore.get("gh:issue:12").state == "failed"  # type: ignore[union-attr]
    assert h.dstore.prior_attempt("gh:issue:12") is None  # nothing was pushed

    h.readd_trigger_label()
    h.loop._runner = h.runner
    h.outcomes = ["merged"]
    h.clock.t += 100_000
    assert h.loop.tick().outcome == "done"
    # Claimed and dispatched all the same, with no branch or PR to reuse: the
    # restart starts fresh rather than erroring out.
    assert len(h.dispatches) == 2
    offered = h.dispatches[1][1]
    assert offered is None or (offered.branch is None and offered.pr_number is None)
    assert h.claims()[1].endswith("starting fresh.")
