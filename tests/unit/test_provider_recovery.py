"""Synthetic provider failures and a fake clock; no SDK, sandbox or account."""

from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace

import pytest

from sbxloop.engine.store import StateStore
from sbxloop.events import EventBus
from sbxloop.provider import ProviderHeldError, ProviderRecovery
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import ErrorInfo, JobRequest, JobResult, ProviderFailure, Usage


def job(**overrides):
    return JobRequest.model_validate(
        {
            "run_id": "r1",
            "job_id": "j1",
            "kind": "agent.session",
            "prompt": "Build the task",
            **overrides,
        }
    )


def rejected(category="throttle", **fields):
    return JobResult(
        job_id="j1",
        status="error",
        session_id="s1",
        output_text="Wrote the output file",
        usage=Usage(backend="claude", input_tokens=20),
        turns=2,
        error=ErrorInfo(
            type="ProviderFailure",
            message="Provider limit",
            provider=ProviderFailure(
                backend="claude",
                category=category,
                reason="Provider limit",
                partial_progress=True,
                **fields,
            ),
        ),
    )


@pytest.fixture
def recovery(tmp_path):
    store = StateStore(tmp_path / "state.db")
    now = [1000.0]
    manager = ProviderRecovery(store, "claude", clock=lambda: now[0], jitter=lambda: 0.5)
    yield manager, now
    store.close()


def test_worker_boundary_blocks_text_and_json_before_transport(recovery, monkeypatch):
    manager, now = recovery
    client = WorkerClient(SimpleNamespace(name="agent"))
    client.provider_recovery = manager
    calls = []

    def transport(request):
        calls.append(request)
        return rejected("quota", http_status=400, reset_at=now[0] + 3600)

    monkeypatch.setattr(client, "_submit", transport)
    with pytest.raises(ProviderHeldError):
        client.submit(job())
    with pytest.raises(ProviderHeldError):
        client.submit(job(job_id="j2", run_id="r2", expect="json"))
    assert len(calls) == 1
    assert manager.pending("r1")
    assert manager.hold().next_at == 4600


def test_throttle_timing_and_retry_bound_survive_new_store(recovery):
    manager, now = recovery
    for attempt in range(4):
        hold = manager.record(job(), rejected(retry_at=now[0] + 600))
        assert hold.attempts == attempt + 1
        if attempt < 3:
            assert hold.next_at == now[0] + 600
            with pytest.raises(ProviderHeldError):
                manager.check()
            now[0] = hold.next_at
            manager.check()
        else:
            assert hold.next_at is None
    # A new reader sees the same cooldown and checkpoint.
    again = ProviderRecovery(manager.store, "claude", clock=lambda: now[0])
    with pytest.raises(ProviderHeldError):
        again.check()
    assert again.checkpoint("r1", manager.job_key(job())).session_id == "s1"


@pytest.mark.parametrize("category", ["quota", "billing"])
def test_hard_limit_unknown_reset_requires_operator(recovery, category):
    manager, now = recovery
    manager.record(job(), rejected(category))
    now[0] += 60 * 60 * 24 * 30
    with pytest.raises(ProviderHeldError, match="reset unknown"):
        manager.check()
    ProviderRecovery(manager.store, "copilot", clock=lambda: now[0]).check()
    manager.release()
    manager.check()


def test_resume_preserves_usage_and_requires_same_session(recovery):
    manager, now = recovery
    manager.record(job(), rejected())
    now[0] = manager.hold().next_at
    calls = []

    def recovered(request):
        calls.append(request)
        return JobResult(
            job_id=request.job_id,
            status="ok",
            session_id="s1",
            output_text="Complete",
            usage=Usage(input_tokens=7),
            turns=1,
        )

    result = manager.submit(job(job_id="new-job"), recovered, EventBus())
    assert calls[0].resume_session_id == "s1"
    assert calls[0].require_resume
    assert "do not repeat" in calls[0].prompt
    assert result.usage.input_tokens == 27
    assert result.turns == 3
    assert not manager.pending("r1")
    assert manager.hold() is None


def test_missing_session_with_partial_work_never_replays(recovery):
    manager, now = recovery
    manager.record(job(), rejected().model_copy(update={"session_id": None}))
    now[0] = manager.hold().next_at
    with pytest.raises(ProviderHeldError, match="no resumable session"):
        manager.submit(job(), lambda _: pytest.fail("must not call agent"), EventBus())


@pytest.mark.parametrize("kind", ["code", "workload"])
def test_daemon_holds_without_reclaim_or_repair_budgets(tmp_path, kind):
    from sbxloop.config import Config
    from sbxloop.daemon.control import dispatch
    from sbxloop.engine.model import RunResult
    from tests.unit.test_daemon_loop import Harness, gh_item

    config = Config.model_validate(
        {"home": str(tmp_path / "state"), "agent": {"backend": "claude"}, "github": {"repo": "o/r"}}
    )
    h = Harness(tmp_path, config)
    h.source.items = [gh_item(kind=kind)]
    calls = []

    def runner(item, cfg, run_id, bus, resume):
        calls.append((run_id, resume))
        if not resume:
            h.store.create_run(run_id, "Do the task", kind=kind)
            h.store.set_run_state(run_id, "building" if kind == "code" else "executing")
            request = job(run_id=run_id)
            hold = h.loop._provider_recovery().record(request, rejected("billing"))
            raise ProviderHeldError(hold)
        return RunResult(
            run_id=run_id, state="merged" if kind == "code" else "completed", kind=kind
        )

    h.loop._runner = runner
    first = h.loop.tick()
    assert first.outcome == "provider_held"
    run_id = calls[0][0]
    assert h.store.get_run(run_id).state == "provider_held"
    assert h.store.get_run(run_id).stage == ("building" if kind == "code" else "executing")
    assert h.dstore.get("gh:1").attempts == 1
    h.loop.recover()
    for _ in range(3):
        h.clock.t += 3600
        assert h.loop.tick().idle_kind == "provider_held"
    assert len(calls) == 1
    assert not [c for c in h.source.calls if c[0] in ("retry", "abandoned")]
    assert "provider" in dispatch(h.loop, "status").text
    assert dispatch(h.loop, f"cancel {run_id}").ok
    assert h.dstore.get("gh:1").state == "cancelled"
    assert dispatch(h.loop, f"resume {run_id}").ok
    assert h.loop.tick().outcome == "done"
    assert calls == [(run_id, False), (run_id, True)]
    assert h.dstore.resumes_for_item("gh:1") == 0
    assert h.loop.status()["consecutive_failures"] == 0


def test_restart_opens_same_durable_hold_and_checkpoint(tmp_path):
    path = tmp_path / "state.db"
    with closing(StateStore(path)) as store:
        manager = ProviderRecovery(store, "claude", clock=lambda: 1000)
        manager.record(job(), rejected("quota", reset_at=2000))
    with closing(StateStore(path)) as reopened:
        manager = ProviderRecovery(reopened, "claude", clock=lambda: 1500)
        with pytest.raises(ProviderHeldError):
            manager.check()
        assert manager.pending("r1")
        result = manager.checkpoint("r1", manager.job_key(job()))
        assert result.session_id == "s1"
        assert result.usage.input_tokens == 20


def test_changed_request_cannot_replay_partial_work(recovery):
    manager, now = recovery
    manager.record(job(), rejected())
    now[0] = manager.hold().next_at
    with pytest.raises(ProviderHeldError, match="request changed"):
        manager.submit(job(prompt="Something else"), lambda _: pytest.fail("no replay"), EventBus())
    assert manager.hold().next_at is None


def test_inflight_success_does_not_clear_another_requests_hold(recovery):
    manager, _ = recovery

    def succeed(request):
        manager.record(job(run_id="another"), rejected("billing"))
        return JobResult(job_id=request.job_id, status="ok", output_text="done")

    manager.submit(job(), succeed, EventBus())
    with pytest.raises(ProviderHeldError):
        manager.check()


@pytest.mark.parametrize("phase", ["build", "steer", "operator_execute", "operator_judge"])
def test_all_phase_entry_points_raise_typed_failures_without_json_repair(phase):
    from sbxloop.config import Config
    from sbxloop.engine.phases import PhaseRunner

    calls = []

    def submit(request, **kwargs):
        calls.append(request)
        return rejected("billing")

    phases = PhaseRunner(SimpleNamespace(submit=submit), Config(), "r1", "do the task")
    with pytest.raises(ProviderHeldError):
        phases._agent_job("prompt", phase=phase, permission_mode="auto", expect="json")
    assert len(calls) == 1


def test_steering_hold_persists_the_message_without_spending_attempt(tmp_path):
    from sbxloop.config import Config
    from sbxloop.engine.engine import LoopEngine

    engine = LoopEngine(Config.model_validate({"home": str(tmp_path / "state")}))
    engine.store.create_run("r1", "do the task")
    engine.post_user_message("Keep the existing output")
    manager = ProviderRecovery(engine.store, "claude")

    def steer(*args, **kwargs):
        raise ProviderHeldError(manager.record(job(), rejected("quota")))

    with pytest.raises(ProviderHeldError):
        engine._process_chat("r1", SimpleNamespace(steer=steer), None)
    pending = engine.store.last_event("r1", "chat.provider_pending")
    assert pending.data["text"] == "Keep the existing output"
    assert engine._steer_attempts == 0
    assert engine.store.phase_attempts("r1") == []
