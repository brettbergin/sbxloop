"""The typed service answers with data; the loop's sentences ride inside,
verbatim, so the prose edge can keep rendering them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import ScheduleConfig
from sbxloop.daemon.control import CommandReply, dispatch
from sbxloop.daemon.controls import ControlError, ControlService, Principal
from sbxloop.daemon.controls.results import (
    CancelOutcome,
    GateOutcome,
    ItemOutcome,
    PauseOutcome,
    ReleaseOutcome,
    RestartOutcome,
    ScheduleOutcome,
    StopOutcome,
)
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.paths import SbxloopHome
from tests.unit.test_daemon_discord import FakeLoop

OPERATOR = Principal.trusted("ops via test", "ctl")
READER = Principal(
    kind="client", id="cli_r", display="reader", via="api", capabilities=frozenset({"runs:read"})
)


class ServiceLoop(FakeLoop):
    """FakeLoop plus the verbs the chat tests never exercise directly."""

    def __init__(self, dstore: DaemonStore) -> None:
        super().__init__(dstore)
        self.running = True
        self.provider_cancels: list[tuple[str, str | None]] = []
        self.review_resumes: list[tuple[str, str | None]] = []
        self.schedule_calls: list[tuple[str, str, str | None]] = []
        self.approvals: list[tuple[str, str | None]] = []

    def cancel_current(self, requester: str | None = None, *, retry: bool = False) -> bool:
        if not self.running:
            return False
        return super().cancel_current(requester, retry=retry)

    def cancel_provider(self, target: str, by: str | None = None) -> str:
        self.provider_cancels.append((target, by))
        if target == "gh:issue:404":
            raise ValueError(f"{target!r} has no parked provider run")
        return f"{target}: cancelled; checkpoint retained for resume"

    def resume_review(self, target: str, by: str | None = None) -> str:
        self.review_resumes.append((target, by))
        if target == "nope":
            raise ValueError(f"{target!r} is not waiting for a review")
        return f"{target}: waiting for a review again"

    def schedules(self) -> list[dict[str, Any]]:
        return []

    def add_schedule(self, spec: ScheduleConfig, by: str | None, *, source: str) -> str:
        self.schedule_calls.append(("add", spec.name, by))
        return f"schedule {spec.name} created"

    def pause_schedule(self, name: str, by: str | None) -> str:
        self.schedule_calls.append(("pause", name, by))
        if name == "ghost":
            raise ValueError(f"no schedule named {name!r}")
        return f"schedule {name} paused"

    def resume_schedule(self, name: str, by: str | None) -> str:
        self.schedule_calls.append(("resume", name, by))
        return f"schedule {name} resumed"

    def remove_schedule(self, name: str, by: str | None) -> str:
        self.schedule_calls.append(("remove", name, by))
        return f"schedule {name} removed"

    def approve_merge(self, target: str, by: str | None = None) -> str:
        self.approvals.append((target, by))
        if target == "r_none":
            raise ValueError(f"no merge gate for {target!r} — nothing is awaiting approval")
        return f"✅ approved by {by or 'operator'}"

    def clock(self) -> float:
        return 1.0


@pytest.fixture
def floop(tmp_path: Path) -> ServiceLoop:
    return ServiceLoop(DaemonStore(SbxloopHome(tmp_path).state_db))


@pytest.fixture
def service(floop: ServiceLoop) -> ControlService:
    return ControlService(floop)


class TestAttribution:
    """The loop hears the principal's attribution as the ``by`` it always
    took — the same string the surface used to pass."""

    def test_pause_and_release(self, service: ControlService, floop: ServiceLoop) -> None:
        paused = service.pause(OPERATOR)
        assert paused == PauseOutcome(hold="operator", holds=["operator"])
        assert floop.hold_calls[-1] == ("pause", "operator", "ops via test")
        service.pause(OPERATOR, "deploy-1")
        released = service.release(OPERATOR, "deploy-1")
        assert released == ReleaseOutcome(hold="deploy-1", holds=["operator"])
        assert service.release(OPERATOR, everything=True) == ReleaseOutcome(hold=None, holds=[])
        assert floop.hold_calls[-1] == ("unpause", None, "ops via test")

    def test_cancel_current(self, service: ControlService, floop: ServiceLoop) -> None:
        assert service.cancel_current(OPERATOR, retry=True) == CancelOutcome(
            mode="current", retry=True
        )
        assert floop.cancel_calls == [("ops via test", True)]

    def test_cancel_provider(self, service: ControlService, floop: ServiceLoop) -> None:
        outcome = service.cancel_provider(OPERATOR, "gh:issue:3")
        assert outcome.mode == "provider" and outcome.target == "gh:issue:3"
        assert outcome.message == "gh:issue:3: cancelled; checkpoint retained for resume"
        assert floop.provider_cancels == [("gh:issue:3", "ops via test")]

    def test_item_verbs(self, service: ControlService, floop: ServiceLoop) -> None:
        floop.dstore.upsert_new(WorkItem(item_id="gh:issue:8", source_key="8", title="8"), 1.0)
        floop.dstore.mark_running("gh:issue:8", "r1", 1.0)
        floop.dstore.mark_cancelled("gh:issue:8", "cancelled by op", 2.0)
        retried = service.retry(OPERATOR, "gh:8")  # legacy spelling normalised
        assert retried == ItemOutcome(verb="retry", item=retried.item)
        assert retried.item.item_id == "gh:issue:8" and retried.item.state == "queued"
        assert floop.retried == [("gh:issue:8", "ops via test")]
        requeued = service.requeue(OPERATOR, "gh:issue:8")
        assert requeued.verb == "requeue" and requeued.item.run_id is None
        abandoned = service.abandon(OPERATOR, "gh:issue:8", "scope changed")
        assert abandoned.item.state == "failed" and abandoned.item.last_error == "scope changed"

    def test_gate_and_schedules(self, service: ControlService, floop: ServiceLoop) -> None:
        assert service.approve_gate(OPERATOR, "r1") == GateOutcome(
            target="r1", message="✅ approved by ops via test"
        )
        spec = ScheduleConfig(name="nightly", profile="p", every="24h", ask="do it")
        assert service.add_schedule(OPERATOR, spec, source="ctl") == ScheduleOutcome(
            verb="add", name="nightly", message="schedule nightly created"
        )
        assert service.schedule_control(OPERATOR, "pause", "nightly").message == (
            "schedule nightly paused"
        )
        assert floop.schedule_calls == [
            ("add", "nightly", "ops via test"),
            ("pause", "nightly", "ops via test"),
        ]

    def test_stop_and_restart_defer_their_effect(
        self, service: ControlService, floop: ServiceLoop
    ) -> None:
        stop = service.stop(OPERATOR)
        assert isinstance(stop, StopOutcome) and not getattr(floop, "stopped", False)
        stop.after()
        assert floop.stopped
        restart = service.restart(OPERATOR, now=True)
        assert restart == RestartOutcome(supervisor="systemd", now=True, after=restart.after)
        assert floop.restarts == []
        restart.after()
        assert floop.restarts[-1]["by"] == "ops via test" and floop.restarts[-1]["now"]
        # The deferred effect is not part of the record.
        assert "after" not in restart.model_dump()


class TestRefusals:
    """The loop's own sentence, kept verbatim under a stable code."""

    def test_nothing_running(self, service: ControlService, floop: ServiceLoop) -> None:
        floop.running = False
        with pytest.raises(ControlError) as excinfo:
            service.cancel_current(OPERATOR)
        assert (excinfo.value.code, excinfo.value.message) == (
            "not_eligible",
            "nothing is running.",
        )

    def test_unknown_target_versus_not_eligible(
        self, service: ControlService, floop: ServiceLoop
    ) -> None:
        with pytest.raises(ControlError) as excinfo:
            service.resume_repo(OPERATOR, "o/zzz")
        assert excinfo.value.code == "unknown_target"
        assert excinfo.value.message == "unknown repository 'o/zzz'"
        with pytest.raises(ControlError) as excinfo:
            service.grant_rounds(OPERATOR, "r_unknown", 2)
        assert excinfo.value.code == "not_eligible"
        assert excinfo.value.message == "unknown run r_unknown"
        with pytest.raises(ControlError) as excinfo:
            service.retry(OPERATOR, "gh:issue:1")
        assert excinfo.value.code == "unknown_target"

    def test_invalid_arguments_never_reach_the_loop(
        self, service: ControlService, floop: ServiceLoop
    ) -> None:
        with pytest.raises(ControlError) as excinfo:
            service.grant_rounds(OPERATOR, "r1", 0)
        assert excinfo.value.code == "invalid_argument" and floop.granted == []
        with pytest.raises(ControlError) as excinfo:
            service.pause(OPERATOR, "bad name")
        assert excinfo.value.code == "invalid_argument"
        assert excinfo.value.message.startswith("invalid hold name")

    def test_unsupervised_restart(self, service: ControlService, floop: ServiceLoop) -> None:
        floop.supervisor_kind = None
        with pytest.raises(ControlError) as excinfo:
            service.restart(OPERATOR)
        assert excinfo.value.code == "unsupervised"


class TestCapabilities:
    """A principal without the capability is refused before the loop is
    touched — the loop never learns the request existed."""

    @pytest.mark.parametrize(
        ("call", "capability"),
        [
            (lambda s: s.pause(READER), "daemon:manage"),
            (lambda s: s.release(READER), "daemon:manage"),
            (lambda s: s.cancel_current(READER), "runs:control"),
            (lambda s: s.cancel_provider(READER, "x"), "runs:control"),
            (lambda s: s.resume_review(READER, "x"), "runs:control"),
            (lambda s: s.grant_rounds(READER, "r1", 1), "budgets:grant"),
            (lambda s: s.approve_gate(READER, "r1"), "gates:approve"),
            (lambda s: s.abandon(READER, "gh:issue:1", None), "runs:control"),
            (lambda s: s.retry(READER, "gh:issue:1"), "runs:control"),
            (lambda s: s.requeue(READER, "gh:issue:1"), "runs:control"),
            (lambda s: s.resume_repo(READER, "o/r"), "daemon:manage"),
            (lambda s: s.schedule_control(READER, "pause", "n"), "daemon:manage"),
            (lambda s: s.stop(READER), "daemon:manage"),
            (lambda s: s.restart(READER), "daemon:manage"),
            (
                lambda s: s.log_tail(READER, tail=5, level=None, grep=None, max_chars=None),
                "diagnostics:read",
            ),
        ],
    )
    def test_mutations_need_their_capability(
        self, service: ControlService, floop: ServiceLoop, call: Any, capability: str
    ) -> None:
        with pytest.raises(ControlError) as excinfo:
            call(service)
        assert excinfo.value.code == "forbidden"
        assert excinfo.value.detail["capability"] == capability
        assert floop.hold_calls == [] and floop.cancel_calls == [] and floop.granted == []
        assert floop.approvals == [] and floop.schedule_calls == [] and floop.restarts == []

    def test_reads_need_runs_read(self, service: ControlService) -> None:
        assert service.status(READER).status["queued"] == 2
        assert service.queue(READER).items == []
        assert service.items(READER).items == []
        assert service.schedules(READER).rows == []
        nobody = Principal(kind="client", id="c", display=None, via="api", capabilities=frozenset())
        with pytest.raises(ControlError):
            service.status(nobody)


class TestProseEdge:
    """``dispatch`` derives a trusted principal from ``by`` — the surfaces
    it serves are trusted — and renders this layer's own refusals with a
    sentence of their own, since the loop never composed one."""

    def test_by_becomes_a_trusted_principal(self, floop: ServiceLoop) -> None:
        assert dispatch(floop, "pause --hold x", by="brett via ctl").ok
        assert floop.hold_calls == [("pause", "x", "brett via ctl")]

    def test_an_explicit_principal_wins(self, floop: ServiceLoop) -> None:
        reply = dispatch(floop, "pause", by="ignored", principal=READER)
        assert reply == CommandReply("pause refused: cli_r (via api) lacks daemon:manage", ok=False)
        assert floop.hold_calls == []
        assert dispatch(floop, "status", principal=READER).ok
