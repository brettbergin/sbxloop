"""Acceptance-criteria regression tests for #374 (phantom `running` runs).

A daemon could die (or an operator cancel could land) leaving the *work
item* settled while the *run row* stayed non-terminal forever: `!sbx status`
said `current: null` while `list_runs` still showed runs as `running`.

These tests exercise the four acceptance criteria at the store+loop level:

(a) abrupt termination mid-run, then a *fresh* daemon process over the same
    state db, yields a terminal run with a reconciliation reason and an
    appended (never mutated) chronology event;
(b) cancel — plain and ``--retry`` — transitions both the run record and the
    work item, with operator attribution preserved;
(c) a genuinely in-flight run (and one queued for resume) is not reconciled
    away on startup;
(d) after recovery the daemon's control-surface view of what is active
    (``!sbx status``) equals the set of non-terminal runs in the engine
    store (``list_runs``).

The harness/fakes are the ones in ``tests/unit/test_daemon_loop.py``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

from sbxloop.config import Config
from sbxloop.daemon import control
from sbxloop.daemon.loop import DaemonLoop, RunHandle
from sbxloop.daemon.model import WorkItem
from sbxloop.engine.model import TERMINAL_RUN_STATES, RunResult
from sbxloop.errors import RunCancelledError
from sbxloop.events import Event, EventBus
from tests.unit.test_daemon_loop import Harness, gh_item


def _fresh_daemon(h: Harness) -> Harness:
    """What a new daemon *process* sees: fresh stores and loop over the same
    state dir, as if the previous process had been killed outright."""
    return Harness(h.tmp_path, Config.model_validate({"state_dir": str(h.config.state_dir)}))


def _active_runs(loop: DaemonLoop) -> set[str]:
    """What `!sbx status` reports as in flight, via the real control surface."""
    reply = control.dispatch(loop, "status", prefix="!sbx", by="brett.bergin")
    assert reply.ok and reply.status is not None
    current = reply.status["current"]
    return {current["run_id"]} if current is not None else set()


class TestAbruptTerminationRecovery:
    """(a) killed mid-run → the next start closes the run row."""

    def test_orphan_run_of_settled_item_is_failed_on_next_start(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_kill", now=2.0)
        h.store.create_run("r_kill", "x")
        h.store.set_run_state("r_kill", "building")
        h.store.append_event(Event.now("run.started", "r_kill"))
        before = [e.type for _, e in h.store.events("r_kill")]
        # The item was settled but the process died before the run row was
        # ever closed — exactly the #374 field shape.
        h.dstore.mark_done("gh:issue:1", 3.0)

        fresh = _fresh_daemon(h)
        fresh.loop.recover()

        record = fresh.store.get_run("r_kill")
        assert record.state in TERMINAL_RUN_STATES
        assert record.state == "failed"
        assert record.reason is not None and "orphaned" in record.reason
        after = [e.type for _, e in fresh.store.events("r_kill")]
        assert after[: len(before)] == before  # history appended to, never rewritten
        assert after[len(before) :] == ["run.reconciled"]

    def test_cancelled_item_run_is_cancelled_with_attribution(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_cancelled", now=2.0)
        h.store.create_run("r_cancelled", "x")
        h.store.set_run_state("r_cancelled", "building")
        h.dstore.mark_cancelled(
            "gh:issue:1", "cancelled by Discord user brett.bergin (via concierge)", now=3.0
        )

        fresh = _fresh_daemon(h)
        fresh.loop.recover()

        record = fresh.store.get_run("r_cancelled")
        assert record.state == "cancelled"
        assert record.reason is not None
        assert "work item cancelled" in record.reason and "brett.bergin" in record.reason
        events = [e for _, e in fresh.store.events("r_cancelled")]
        assert [e.type for e in events] == ["run.reconciled"]
        assert events[-1].data["previous_state"] == "building"


class TestCancelConsistency:
    """(b) cancel leaves run *and* item terminal, attribution preserved."""

    @staticmethod
    def _cancel_mid_run(h: Harness, *, requester: str, retry: bool) -> str:
        """Drive a real tick to the cancel boundary, as #246's harness does."""
        started = threading.Event()
        cancelled = threading.Event()

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            h.runs.append((run_id, resume))
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "building")
            started.set()
            assert cancelled.wait(5)
            raise RunCancelledError(f"run {run_id} interrupted")

        h.loop._runner = runner
        h.source.items = [gh_item()]
        thread = threading.Thread(target=h.loop.tick)
        thread.start()
        assert started.wait(5)
        assert h.loop.cancel_current(requester, retry=retry) is True
        cancelled.set()
        thread.join(5)
        assert not thread.is_alive()
        return h.runs[0][0]

    def test_cancel_transitions_both_run_and_item(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        run_id = self._cancel_mid_run(h, requester="brett.bergin", retry=False)

        record = h.store.get_run(run_id)
        assert record.state == "cancelled"
        assert record.reason is not None and "brett.bergin" in record.reason
        types = [e.type for _, e in h.store.events(run_id)]
        assert "run.cancelled" in types
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "cancelled"
        assert item.last_error is not None and "brett.bergin" in item.last_error
        # Nothing is left looking active.
        assert h.store.non_terminal_runs() == []
        assert _active_runs(h.loop) == set()

    def test_cancel_retry_ends_run_but_requeues_item(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        run_id = self._cancel_mid_run(h, requester="brett.bergin", retry=True)

        record = h.store.get_run(run_id)
        assert record.state in TERMINAL_RUN_STATES
        assert record.reason is not None and "brett.bergin" in record.reason
        item = h.dstore.get("gh:issue:1")
        # Re-queued to run fresh — and crucially not pinned to a phantom run.
        assert item is not None and item.state == "queued"
        assert h.store.non_terminal_runs() == []

    def test_cancel_via_control_surface_settles_run(self, tmp_path: Path) -> None:
        """The Discord/ctl path (`!sbx cancel`) reaches the same place."""
        h = Harness(tmp_path)
        started = threading.Event()
        cancelled = threading.Event()

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            h.runs.append((run_id, resume))
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, "building")
            started.set()
            assert cancelled.wait(5)
            raise RunCancelledError("boundary")

        h.loop._runner = runner
        h.source.items = [gh_item()]
        thread = threading.Thread(target=h.loop.tick)
        thread.start()
        assert started.wait(5)
        reply = control.dispatch(h.loop, "cancel", by="brett.bergin", via="discord")
        assert reply.ok
        cancelled.set()
        thread.join(5)

        record = h.store.get_run(h.runs[0][0])
        assert record.state == "cancelled"
        assert record.reason is not None and "brett.bergin" in record.reason
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "cancelled"


class TestLiveRunNotReconciled:
    """(c) reconciliation never touches work that is genuinely in flight."""

    def test_in_flight_run_survives_recover(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "building")
        h.loop._current = RunHandle(gh_item("2"), "r_live", cast(Any, None), EventBus())

        h.loop.recover()

        assert h.store.get_run("r_live").state == "building"
        assert [e.type for _, e in h.store.events("r_live")] == []

    def test_resume_pending_run_survives_recover(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_claimed("gh:issue:1", now=1.0)
        h.dstore.mark_running("gh:issue:1", "r_resume", now=2.0)
        h.store.create_run("r_resume", "x")
        h.store.set_run_state("r_resume", "building")

        h.loop.recover()

        assert h.store.get_run("r_resume").state == "building"
        item = h.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued" and item.run_id == "r_resume"
        assert [e.type for _, e in h.store.events("r_resume")] == []


class TestStatusAndListAgree:
    """(d) `!sbx status` and `list_runs` report the same active set."""

    def test_idle_daemon_reports_no_active_runs_after_recover(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        for run_id, state in (("r_orph", "building"), ("r_dec", "decomposing")):
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, state)
        h.store.create_run("r_done", "x")
        h.store.set_run_state("r_done", "completed")

        fresh = _fresh_daemon(h)
        fresh.loop.recover()

        non_terminal = {r.run_id for r in fresh.store.non_terminal_runs()}
        assert non_terminal == set()
        assert _active_runs(fresh.loop) == non_terminal
        assert fresh.store.get_run("r_done").state == "completed"

    def test_live_run_is_the_only_active_run_after_recover(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        for run_id, state in (("r_orph", "building"), ("r_dec", "decomposing")):
            h.store.create_run(run_id, "x")
            h.store.set_run_state(run_id, state)
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "building")
        h.loop._current = RunHandle(gh_item("2"), "r_live", cast(Any, None), EventBus())

        h.loop.recover()

        non_terminal = {r.run_id for r in h.store.non_terminal_runs()}
        assert non_terminal == {"r_live"}
        assert _active_runs(h.loop) == non_terminal
