"""The workspace budget pool: one daily run cap and one daily token budget
shared by every run and every chat turn, and fair dispatch between
requesters when more than one run may execute.

The run cap's own behaviour (calendar day, resumes counted, the
``daily_cap`` idle reason) is pinned by ``test_daemon_loop.py``; these
tests cover what the pool adds on top of it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sbxloop.config import Config
from sbxloop.daemon.loop import day_window
from sbxloop.daemon.usagepool import Admission, UsagePool
from sbxloop.engine.model import RunResult
from sbxloop.events import EventBus
from sbxloop_worker.protocol import Usage
from tests.unit.test_daemon_concierge import make as make_concierge
from tests.unit.test_daemon_concurrent_dispatch import Gate, _harness, _item, _release_all, _tick
from tests.unit.test_daemon_loop import Harness, RecordingFrontend, gh_item

NOON = datetime(2024, 3, 5, 12, 0, tzinfo=UTC).timestamp()


def _config(tmp_path: Path, **daemon: Any) -> Config:
    return Config.model_validate(
        {"home": str(tmp_path / "state"), "github": {"repo": "o/r"}, "daemon": daemon}
    )


def _harness_at_noon(tmp_path: Path, **daemon: Any) -> Harness:
    h = Harness(tmp_path, _config(tmp_path, **daemon))
    h.clock.t = NOON
    return h


def _pool(h: Harness) -> UsagePool:
    pool = h.loop.usage_pool
    assert isinstance(pool, UsagePool)
    return pool


def _tokens(inp: int, out: int, **extra: int) -> Usage:
    return Usage(input_tokens=inp, output_tokens=out, **extra)


def test_a_refusal_reason_is_an_open_string() -> None:
    """The shared admission contract types ``reason`` as any string, so a
    later limit can refuse with its own reason."""
    assert Admission(ok=False, reason="seat_limit").reason == "seat_limit"
    assert Admission.__annotations__["reason"] == "str | None"


class TestAdmitRun:
    def test_under_every_limit_a_run_is_admitted(self, tmp_path: Path) -> None:
        h = _harness_at_noon(tmp_path, daily_token_budget=1000)
        assert _pool(h).admit_run(None, NOON) == Admission(ok=True)

    def test_the_run_cap_refuses_with_the_next_day_boundary(self, tmp_path: Path) -> None:
        h = _harness_at_noon(tmp_path, max_runs_per_day=1)
        h.source.items = [gh_item("1")]
        assert h.loop.tick().outcome == "done"
        admission = _pool(h).admit_run(None, NOON)
        assert admission == Admission(
            ok=False,
            reason="run_cap",
            retry_at=datetime(2024, 3, 6, 0, 0, tzinfo=UTC).timestamp(),
        )

    def test_the_token_budget_refuses_once_input_and_output_reach_it(self, tmp_path: Path) -> None:
        h = _harness_at_noon(tmp_path, daily_token_budget=100)
        pool = _pool(h)
        pool.charge(
            source="run", ref_id="r1", agent_slug=None, channel_id=None, usage=_tokens(60, 39)
        )
        assert pool.admit_run(None, NOON).ok
        # Cache figures are not budget: only input + output count.
        pool.charge(
            source="run",
            ref_id="r1",
            agent_slug=None,
            channel_id=None,
            usage=Usage(cache_read_tokens=5000, cache_write_tokens=5000),
        )
        assert pool.admit_run(None, NOON).ok
        pool.charge(
            source="run", ref_id="r1", agent_slug=None, channel_id=None, usage=_tokens(0, 1)
        )
        admission = pool.admit_run(None, NOON)
        assert admission.ok is False and admission.reason == "token_budget"
        assert admission.retry_at == datetime(2024, 3, 6, 0, 0, tzinfo=UTC).timestamp()

    def test_no_budget_means_tokens_never_refuse(self, tmp_path: Path) -> None:
        h = _harness_at_noon(tmp_path)
        pool = _pool(h)
        pool.charge(
            source="run",
            ref_id="r1",
            agent_slug=None,
            channel_id=None,
            usage=_tokens(10**9, 10**9),
        )
        assert pool.admit_run(None, NOON).ok

    def test_chat_turns_count_toward_the_run_budget(self, tmp_path: Path) -> None:
        h = _harness_at_noon(tmp_path, daily_token_budget=50)
        pool = _pool(h)
        pool.charge(
            source="turn",
            ref_id="turn-1",
            agent_slug="planner",
            channel_id="ch-1",
            usage=_tokens(30, 20),
        )
        assert pool.admit_run(None, NOON).reason == "token_budget"

    def test_yesterdays_tokens_do_not_count_today(self, tmp_path: Path) -> None:
        h = _harness_at_noon(tmp_path, daily_token_budget=50)
        h.clock.t = NOON - 86400
        pool = _pool(h)
        pool.charge(
            source="turn", ref_id="t", agent_slug=None, channel_id=None, usage=_tokens(50, 50)
        )
        assert pool.admit_run(None, NOON - 86400).reason == "token_budget"
        assert pool.admit_run(None, NOON).ok


class TestAdmitTurn:
    def test_an_exhausted_budget_refuses_a_turn(self, tmp_path: Path) -> None:
        h = _harness_at_noon(tmp_path, daily_token_budget=10)
        pool = _pool(h)
        assert pool.admit_turn("ch-1", "planner", NOON).ok
        pool.charge(
            source="run", ref_id="r1", agent_slug=None, channel_id=None, usage=_tokens(5, 5)
        )
        admission = pool.admit_turn("ch-1", "planner", NOON)
        assert admission.ok is False and admission.reason == "token_budget"
        assert admission.retry_at == day_window(NOON, "UTC")[1]

    def test_a_refusal_is_described_with_the_budget_and_when_it_resets(
        self, tmp_path: Path
    ) -> None:
        """What a person is told: the day's spend against the budget and
        the boundary at which chat and runs resume, in the pool's zone."""
        h = _harness_at_noon(tmp_path, daily_token_budget=10, run_cap_timezone="Europe/Paris")
        pool = _pool(h)
        pool.charge(
            source="turn", ref_id="t1", agent_slug=None, channel_id=None, usage=_tokens(7, 5)
        )
        admission = pool.admit_turn("ch-1", None, NOON)
        assert admission.ok is False
        assert pool.refusal_text(admission, NOON) == (
            "the workspace token budget is spent for today (Europe/Paris): "
            "12/10 tokens; chat turns and new runs resume at 00:00 Europe/Paris"
        )

    def test_the_run_cap_does_not_apply_to_turns(self, tmp_path: Path) -> None:
        h = _harness_at_noon(tmp_path, max_runs_per_day=1)
        h.source.items = [gh_item("1")]
        assert h.loop.tick().outcome == "done"
        assert _pool(h).admit_run(None, NOON).reason == "run_cap"
        assert _pool(h).admit_turn("ch-1", None, NOON).ok


class TestTick:
    def test_an_exhausted_budget_idles_the_loop_as_budget(self, tmp_path: Path) -> None:
        h = _harness_at_noon(tmp_path, daily_token_budget=100)
        h.source.items = [gh_item("1")]
        _pool(h).charge(
            source="turn", ref_id="t", agent_slug=None, channel_id=None, usage=_tokens(100, 0)
        )
        result = h.loop.tick()
        assert result.dispatched is None and result.idle_kind == "budget"
        assert h.runs == []
        # The next calendar day starts a fresh budget.
        h.clock.t = day_window(NOON, "UTC")[1]
        assert h.loop.tick().outcome == "done"

    def test_the_run_cap_still_reads_daily_cap_when_both_are_reached(self, tmp_path: Path) -> None:
        h = _harness_at_noon(tmp_path, max_runs_per_day=1, daily_token_budget=10)
        h.source.items = [gh_item("1"), gh_item("2")]
        assert h.loop.tick().outcome == "done"
        _pool(h).charge(
            source="run", ref_id="r", agent_slug=None, channel_id=None, usage=_tokens(10, 0)
        )
        assert h.loop.tick().idle_reason == "daily_cap"

    def test_a_budget_refusal_is_announced_once_a_day(self, tmp_path: Path) -> None:
        h = _harness_at_noon(tmp_path, daily_token_budget=10)
        frontend = RecordingFrontend()
        h.loop.frontend = frontend
        h.source.items = [gh_item("1")]
        _pool(h).charge(
            source="run", ref_id="r", agent_slug=None, channel_id=None, usage=_tokens(10, 0)
        )

        def budget_notices() -> list[Any]:
            return [n for n in frontend.notices if n.kind == "daemon.token_budget"]

        assert h.loop.tick().idle_kind == "budget"
        assert h.loop.tick().idle_kind == "budget"
        h.clock.t += 4 * 3600
        assert h.loop.tick().idle_kind == "budget"
        assert len(budget_notices()) == 1
        assert "10/10" in budget_notices()[0].text
        # Tomorrow: a fresh budget, spent again, is announced again.
        tomorrow = day_window(NOON, "UTC")[1] + 3600
        h.clock.t = tomorrow
        h.source.items = []
        _pool(h).charge(
            source="run", ref_id="r2", agent_slug=None, channel_id=None, usage=_tokens(0, 10)
        )
        assert h.loop.tick().idle_kind == "budget"
        assert len(budget_notices()) == 2

    def test_the_announcement_survives_a_restart(self, tmp_path: Path) -> None:
        h = _harness_at_noon(tmp_path, daily_token_budget=10)
        h.source.items = [gh_item("1")]
        _pool(h).charge(
            source="run", ref_id="r", agent_slug=None, channel_id=None, usage=_tokens(10, 0)
        )
        h.loop.frontend = RecordingFrontend()
        assert h.loop.tick().idle_kind == "budget"
        again = Harness(tmp_path, h.config)
        again.clock.t = NOON + 60
        frontend = RecordingFrontend()
        again.loop.frontend = frontend
        assert again.loop.tick().idle_kind == "budget"
        assert [n for n in frontend.notices if n.kind == "daemon.token_budget"] == []


class TestProviderResume:
    @pytest.mark.parametrize("run_cap", [1, 5])
    def test_a_provider_held_resume_is_still_refused_by_an_exhausted_budget(
        self, tmp_path: Path, run_cap: int
    ) -> None:
        """A resume waiting on a provider is exempt from the run cap, never
        from the token budget, whichever limit the pool looks at first."""
        h = _harness_at_noon(tmp_path, max_runs_per_day=run_cap, daily_token_budget=10)
        now = h.clock()
        h.dstore.upsert_new(gh_item(), now=now)
        h.dstore.mark_claimed("gh:issue:1", now=now)
        h.dstore.mark_running("gh:issue:1", "r_live", now=now)
        h.store.create_run("r_live", "x")
        h.store.set_run_state("r_live", "building")
        h.loop.recover()

        class PendingRecovery:
            def hold(self) -> None:
                return None

            def pending(self, run_id: str) -> bool:
                return True

        h.loop._provider_recovery = PendingRecovery  # type: ignore[assignment,method-assign]
        _pool(h).charge(
            source="turn", ref_id="t", agent_slug=None, channel_id=None, usage=_tokens(10, 0)
        )
        result = h.loop.tick()
        assert result.idle_kind == "budget"
        assert h.runs == []


class TestRunCharging:
    def test_a_runs_usage_events_are_charged_to_the_pool(self, tmp_path: Path) -> None:
        h = _harness_at_noon(tmp_path, daily_token_budget=1000)
        h.source.items = [gh_item("1")]
        inner = h.runner

        def runner(item: Any, cfg: Config, run_id: str, bus: EventBus, resume: bool) -> RunResult:
            # Without an assignment the event names only the run role.
            bus.emit("agent.usage", run_id, agent="builder", input_tokens=300, output_tokens=40)
            # An assigned agent's slug wins over the role it plays.
            bus.emit(
                "agent.usage",
                run_id,
                agent="builder",
                agent_slug="critic",
                input_tokens=100,
                output_tokens=60,
                cache_read_tokens=7,
            )
            bus.emit("run.state", run_id, state="building")
            return inner(item, cfg, run_id, bus, resume)

        h.loop._runner = runner
        assert h.loop.tick().outcome == "done"
        run_id = h.runs[-1][0]
        snapshot = _pool(h).snapshot(NOON)
        assert snapshot["runs_tokens_today"] == 500
        assert snapshot["turns_tokens_today"] == 0
        assert snapshot["tokens_today"] == 500
        rows = _pool(h).entries(since=0)
        assert [(r.source, r.ref_id, r.agent_slug) for r in rows] == [
            ("run", run_id, "builder"),
            ("run", run_id, "critic"),
        ]
        assert rows[1].cache_read_tokens == 7


class TestTurnCharging:
    def test_a_concierge_turn_charges_its_reported_usage(self, tmp_path: Path) -> None:
        concierge, client, _, _, dstore = make_concierge(tmp_path, [{"text": "hi"}])
        original = client.submit

        def submit(job: Any, **kwargs: Any) -> Any:
            result = original(job, **kwargs)
            return result.model_copy(update={"usage": _tokens(12, 8)})

        client.submit = submit  # type: ignore[method-assign]
        reply = concierge.submit_turn(
            "hello",
            author="someone",
            channel_id="ch-9",
            agent_slug="planner",
        ).result(timeout=10)
        assert reply.ok
        rows = UsagePool(dstore, lambda: concierge.config).entries(since=0)
        assert [(r.source, r.channel_id, r.agent_slug) for r in rows] == [
            ("turn", "ch-9", "planner")
        ]
        assert (rows[0].input_tokens, rows[0].output_tokens) == (12, 8)

    def test_a_turn_without_reported_usage_charges_nothing(self, tmp_path: Path) -> None:
        concierge, _, _, _, dstore = make_concierge(tmp_path, [{"text": "hi"}])
        assert concierge.submit_turn("hello", author="someone").result(timeout=10).ok
        assert UsagePool(dstore, lambda: concierge.config).entries(since=0) == []


class TestFairness:
    def test_a_requester_with_a_live_run_waits_behind_one_without(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, max_concurrent_runs=2)
        gate = Gate(h)
        h.loop._runner = gate.runner
        h.source.items = [
            _item("o/a", "1", requested_by="alice"),
            _item("o/b", "2", requested_by="alice"),
            _item("o/b", "3", requested_by="bob"),
        ]
        try:
            # alice's first item is live, so bob's newer item takes the
            # second slot ahead of alice's second.
            assert _tick(h).launched == ("gh:o/a:issue:1", "gh:o/b:issue:3")
        finally:
            _release_all(h, gate)

    def test_one_run_at_a_time_keeps_plain_fifo(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.source.items = [
            gh_item("1", requested_by="alice"),
            gh_item("2", requested_by="alice"),
            gh_item("3", requested_by="bob"),
        ]
        order = [h.loop.tick().dispatched for _ in range(3)]
        assert order == ["gh:issue:1", "gh:issue:2", "gh:issue:3"]

    def test_the_oldest_item_still_starts_when_every_requester_is_busy(
        self, tmp_path: Path
    ) -> None:
        h = _harness(tmp_path, max_concurrent_runs=2)
        gate = Gate(h)
        h.loop._runner = gate.runner
        h.source.items = [
            _item("o/a", "1", requested_by="alice"),
            _item("o/b", "2", requested_by="alice"),
        ]
        try:
            result = _tick(h)
            assert result.launched == ("gh:o/a:issue:1", "gh:o/b:issue:2")
        finally:
            _release_all(h, gate)


class TestKnob:
    def test_the_budget_is_unset_by_default(self, tmp_path: Path) -> None:
        assert Config.model_validate({"home": str(tmp_path)}).daemon.daily_token_budget is None

    @pytest.mark.parametrize("value", [0, -5])
    def test_a_non_positive_budget_is_refused(self, tmp_path: Path, value: int) -> None:
        with pytest.raises(ValidationError):
            Config.model_validate({"home": str(tmp_path), "daemon": {"daily_token_budget": value}})


def test_wall_clock_is_the_default(tmp_path: Path) -> None:
    """A pool built without a clock stamps charges with the wall clock."""
    h = _harness_at_noon(tmp_path)
    pool = UsagePool(h.dstore, lambda: h.config)
    before = time.time()
    pool.charge(source="turn", ref_id="t", agent_slug=None, channel_id=None, usage=_tokens(1, 1))
    (row,) = pool.entries(since=0)
    assert row.ts >= before
