"""The opt-in merge gate ([landing] merge_gate): park, approve, decline.

The daemon-side half: a run that ends ``gated`` parks its item with a
durable gate row, the source hears about it once, nothing dispatches the
parked item, and one approval completes the landing with gh ops alone —
no engine, no sandbox. The landing-side half (``land(gate=True)``) is
pinned in test_engine_landing_gate.py.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.control import dispatch
from sbxloop.daemon.model import WorkItem
from sbxloop.gh.ops import ChecksVerdict
from tests.fakes.fake_github import BLOCKED_405, FakeGithub, human_review
from tests.unit.test_daemon_loop import (
    PR_URL,
    FakeSource,
    Harness,
    RecordingFrontend,
    gh_item,
)


class GatedSource(FakeSource):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.gated_ok = True

    def report_gated(self, item: WorkItem, pr_number: int | None, pr_url: str) -> bool:
        self.calls.append(("gated", (pr_number, pr_url)))
        return self.gated_ok


class GateFrontend(RecordingFrontend):
    def __init__(self) -> None:
        super().__init__()
        self.gates_opened: list[tuple[str, Any]] = []
        self.gates_resolved: list[tuple[str, str, str | None]] = []

    def merge_gate_opened(self, item: WorkItem, run_id: str, gate: Any) -> None:
        self.gates_opened.append((run_id, gate))

    def merge_gate_resolved(
        self,
        item: WorkItem,
        run_id: str,
        gate: Any,
        outcome: str,
        by: str | None,
        detail: str | None = None,
    ) -> None:
        self.gates_resolved.append((run_id, outcome, by))


class FakeProvisioner:
    def gh_bot_login(self, repo: str | None = None) -> str | None:
        return None


class FakeDaemonGithub:
    """Just enough of DaemonGithub for the approve path."""

    def __init__(self, fake: FakeGithub) -> None:
        self.fake = fake
        self.provisioner = FakeProvisioner()
        self.failures: list[str] = []

    def ops(self) -> FakeGithub:
        return self.fake

    def note_failure(self, exc: BaseException) -> bool:
        self.failures.append(str(exc))
        return False


def gated_harness(tmp_path: Path, config: Config | None = None) -> Harness:
    h = Harness(tmp_path, config)
    h.source = GatedSource()
    h.loop.source = h.source
    h.loop.frontend = GateFrontend()
    return h


def park(h: Harness, key: str = "1", **item_overrides: Any) -> str:
    """Dispatch one item that ends gated; returns its run id."""
    h.source.items = [gh_item(key, **item_overrides)]
    h.outcomes = ["gated"]
    result = h.loop.tick()
    assert result.outcome == "gated", result
    item = h.dstore.get(f"gh:issue:{key}")
    assert item is not None and item.run_id is not None
    return item.run_id


class TestParkOnGated:
    def test_the_item_parks_with_a_gate_row_and_a_reset_breaker(self, tmp_path: Path) -> None:
        h = gated_harness(tmp_path)
        run_id = park(h)
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "gated"
        assert item.run_id == run_id, "the run stays pinned to the item"
        gate = h.dstore.merge_gate_for("gh:issue:1")
        assert gate is not None and gate.state == "open"
        assert gate.pr_number == 9 and gate.pr_url == PR_URL
        assert h.loop.status()["breaker_open"] is False

    def test_the_source_hears_the_park_once(self, tmp_path: Path) -> None:
        h = gated_harness(tmp_path)
        park(h)
        assert [c[0] for c in h.source.calls] == ["claim", "started", "gated"]
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.pending_report is None

    def test_a_failed_park_report_stays_owed(self, tmp_path: Path) -> None:
        h = gated_harness(tmp_path)
        source = h.source
        assert isinstance(source, GatedSource)
        source.gated_ok = False
        park(h)
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.pending_report == "gated"

    def test_the_frontend_sees_the_gate_and_who_to_ping(self, tmp_path: Path) -> None:
        h = gated_harness(tmp_path)
        park(h, requested_by="U123")
        front = h.loop.frontend
        assert isinstance(front, GateFrontend)
        ((_, gate),) = front.gates_opened
        assert gate.notify_ids == ("U123",)
        assert any(n.kind == "run.gated" for n in front.notices)

    def test_a_gated_item_is_never_dispatched(self, tmp_path: Path) -> None:
        h = gated_harness(tmp_path)
        park(h)
        assert h.loop.tick().idle_reason == "no_work"


class TestApprove:
    def approve_ready(self, tmp_path: Path) -> tuple[Harness, FakeGithub, str]:
        h = gated_harness(tmp_path)
        run_id = park(h)
        fake = FakeGithub(number=9)
        fake.pr["html_url"] = PR_URL
        h.loop.github = FakeDaemonGithub(fake)  # type: ignore[assignment]
        return h, fake, run_id

    def test_approve_completes_the_landing_with_gh_ops_only(self, tmp_path: Path) -> None:
        h, fake, run_id = self.approve_ready(tmp_path)
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and h.dstore.claim_merge_gate(run_id)
        h.loop._complete_landing(gate, "Discord user `brett`")
        assert fake.merges, "the daemon merged through the shared ops box"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "done"
        assert h.store.get_run(run_id).state == "merged"
        fresh = h.dstore.merge_gate_for(run_id)
        assert fresh is not None and fresh.state == "merged"
        assert any(c[0] == "merged" for c in h.source.calls)
        front = h.loop.frontend
        assert isinstance(front, GateFrontend)
        assert front.gates_resolved and front.gates_resolved[0][1] == "merged"

    def test_approve_judges_the_checks_against_the_prs_base(self, tmp_path: Path) -> None:
        """#611: the approve path makes the run's own judgment — a red the
        PR did not cause is merged over and named, against the base the
        PR actually targets."""
        h, fake, run_id = self.approve_ready(tmp_path)
        fake.pr["base"] = {"ref": "release/2"}
        fake.checks_by_sha["base123"] = ChecksVerdict("red", 1, (), ("flaky",))
        fake.checks = [ChecksVerdict("red", 1, (), ("flaky",))]
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and h.dstore.claim_merge_gate(run_id)
        h.loop._complete_landing(gate, "Discord user `brett`")
        assert fake.merges
        assert any("`flaky` — already red on base123" in c for c in fake.issue_comments)
        assert any(p.endswith("/branches/release/2/protection") for _, p, _ in fake.raw_calls)

    def test_approve_without_a_pr_base_asks_the_repository(self, tmp_path: Path) -> None:
        """#672: a PR payload with no base ref falls back to the repository's
        reported default branch, never to a literal `main`."""
        h, fake, run_id = self.approve_ready(tmp_path)
        fake.pr.pop("base", None)
        fake.repo_payload["default_branch"] = "develop"
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and h.dstore.claim_merge_gate(run_id)
        h.loop._complete_landing(gate, "Discord user `brett`")
        assert fake.merges
        assert any(p.endswith("/branches/develop/protection") for _, p, _ in fake.raw_calls)
        assert not any("/branches/main/" in p for _, p, _ in fake.raw_calls)

    def test_approve_merges_over_a_bots_standing_review(self, tmp_path: Path) -> None:
        """#613: the run already had its one bot round; on approve the
        daemon reads that from the store and merges over the still-standing
        bot review rather than asking for a round it cannot run."""
        h, fake, run_id = self.approve_ready(tmp_path)
        h.store.record_bot_round(run_id)
        fake.reviews_payload = [
            human_review("coderabbitai[bot]", "CHANGES_REQUESTED", "nits", id=41, bot=True)
        ]
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and h.dstore.claim_merge_gate(run_id)
        h.loop._complete_landing(gate, "Discord user `brett`")
        assert fake.merges
        assert any("bots do not dismiss their reviews" in c for c in fake.issue_comments)

    def test_approve_merge_spawns_and_answers(self, tmp_path: Path) -> None:
        h, _fake, _run_id = self.approve_ready(tmp_path)
        text = h.loop.approve_merge("gh:issue:1", by="brett")
        assert "approved by brett" in text
        deadline = time.time() + 10
        item = None
        while time.time() < deadline:
            item = h.dstore.get("gh:issue:1")
            if item is not None and item.state == "done":
                break
            time.sleep(0.05)
        assert item is not None and item.state == "done"

    def test_a_double_approve_loses_the_cas(self, tmp_path: Path) -> None:
        h, _fake, run_id = self.approve_ready(tmp_path)
        assert h.dstore.claim_merge_gate(run_id)
        assert not h.dstore.claim_merge_gate(run_id)
        text = h.loop.approve_merge(run_id, by="two")
        assert "already being merged" in text

    def test_refusals_name_the_problem(self, tmp_path: Path) -> None:
        h = gated_harness(tmp_path)
        with pytest.raises(ValueError, match="no merge gate"):
            h.loop.approve_merge("gh:issue:404")

    def test_a_failed_merge_reopens_the_gate(self, tmp_path: Path) -> None:
        h, fake, run_id = self.approve_ready(tmp_path)
        fake.merge_outcomes = [BLOCKED_405]
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and h.dstore.claim_merge_gate(run_id)
        h.loop._complete_landing(gate, "brett")
        fresh = h.dstore.merge_gate_for(run_id)
        assert fresh is not None and fresh.state == "open", "the gate is back up"
        assert fresh.detail is not None
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "gated", "the park stands"
        front = h.loop.frontend
        assert isinstance(front, GateFrontend)
        assert front.gates_resolved and front.gates_resolved[0][1] == "failed"

    def test_a_pr_merged_by_hand_settles_done_without_a_merge_call(self, tmp_path: Path) -> None:
        h, fake, run_id = self.approve_ready(tmp_path)
        fake.pr["merged"] = True
        fake.pr["merge_commit_sha"] = "human42"
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and h.dstore.claim_merge_gate(run_id)
        h.loop._complete_landing(gate, "brett")
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "done"
        assert fake.merges == [], "no merge call: the human already merged"


class TestDeclineAndRecovery:
    def test_abandon_dismisses_a_parked_gate(self, tmp_path: Path) -> None:
        h = gated_harness(tmp_path)
        run_id = park(h)
        h.loop.abandon_item("gh:issue:1", "not this one")
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and gate.state == "dismissed"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "failed"
        front = h.loop.frontend
        assert isinstance(front, GateFrontend)
        assert front.gates_resolved and front.gates_resolved[0][1] == "dismissed"

    def test_recover_re_parks_a_run_that_ended_gated(self, tmp_path: Path) -> None:
        """Crash between the engine ending gated and the settle: recovery
        re-runs the settle and the INSERT OR IGNORE keeps the gate row."""
        h = gated_harness(tmp_path)
        run_id = park(h)
        h.dstore.set_state("gh:issue:1", "running", h.clock())
        h.loop.recover()
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "gated"
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and gate.state == "open"

    def test_boot_reopens_an_interrupted_approving_gate(self, tmp_path: Path) -> None:
        h = gated_harness(tmp_path)
        run_id = park(h)
        assert h.dstore.claim_merge_gate(run_id)
        h.loop.recover()
        gate = h.dstore.merge_gate_for(run_id)
        assert gate is not None and gate.state == "open"
        assert gate.detail is not None and "restart" in gate.detail


class _MergeLoop:
    """The one attribute the control verb needs."""

    def __init__(self) -> None:
        self.approved: list[tuple[str, str | None]] = []

    def approve_merge(self, target: str, by: str | None = None) -> str:
        self.approved.append((target, by))
        if target == "gh:issue:404":
            raise ValueError("no merge gate for 'gh:issue:404'")
        return "✅ approved by tester — completing the landing"


class TestMergeCommand:
    def test_the_verb_dispatches_with_attribution(self) -> None:
        loop = _MergeLoop()
        reply = dispatch(loop, "merge gh:issue:1", by="brett", via="test")
        assert reply.ok and "approved" in reply.text
        assert loop.approved == [("gh:issue:1", "brett")]

    def test_approve_is_an_alias(self) -> None:
        loop = _MergeLoop()
        assert dispatch(loop, "approve r123abc", via="test").ok
        assert loop.approved == [("r123abc", None)]

    def test_refusals_come_back_not_ok(self) -> None:
        reply = dispatch(_MergeLoop(), "merge gh:issue:404", via="test")
        assert not reply.ok and "no merge gate" in reply.text

    def test_usage_lists_the_verb(self) -> None:
        reply = dispatch(_MergeLoop(), "bogus", via="test")
        assert not reply.known and "merge <item|run>" in reply.text

    def test_bad_arity_is_usage(self) -> None:
        reply = dispatch(_MergeLoop(), "merge", via="test")
        assert not reply.ok and "usage" in reply.text


class TestGateStore:
    def test_gate_rows_survive_a_pre_change_db(self, tmp_path: Path) -> None:
        """The house migration rule: a db created before the table exists
        upgrades on open, rows intact."""
        import sqlite3

        from sbxloop.daemon.store import DaemonStore

        db = tmp_path / "state.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO daemon_state VALUES ('k', 'v')")
        conn.commit()
        conn.close()
        store = DaemonStore(db)
        try:
            assert store.get_value("k") == "v"
            assert store.open_merge_gates() == []
        finally:
            store.close()

    def test_gate_lookup_accepts_run_and_item_ids(self, tmp_path: Path) -> None:
        from sbxloop.daemon.store import DaemonStore

        store = DaemonStore(tmp_path / "state.db")
        try:
            store.create_merge_gate(
                "r1", "gh:issue:7", "o/r", 9, PR_URL, "sbxloop/r1", ["U1"], "tok", 1.0
            )
            by_run = store.merge_gate_for("r1")
            by_item = store.merge_gate_for("gh:issue:7")
            assert by_run is not None and by_item is not None
            assert by_run.run_id == by_item.run_id == "r1"
            assert by_run.notify_ids == ("U1",)
        finally:
            store.close()

    def test_create_is_insert_or_ignore(self, tmp_path: Path) -> None:
        from sbxloop.daemon.store import DaemonStore

        store = DaemonStore(tmp_path / "state.db")
        try:
            store.create_merge_gate("r1", "gh:issue:7", "o/r", 9, PR_URL, None, [], "tok", 1.0)
            assert store.claim_merge_gate("r1")
            store.create_merge_gate("r1", "gh:issue:7", "o/r", 9, PR_URL, None, [], "tok2", 2.0)
            gate = store.merge_gate_for("r1")
            assert gate is not None and gate.state == "approving", "re-settle must not clobber"
        finally:
            store.close()
