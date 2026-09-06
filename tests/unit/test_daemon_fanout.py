"""FanoutFrontend: every bridge hears every Frontend call, one bridge's
failure never starves another, and the concierge's two callbacks resolve
across bridges."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sbxloop.config import Config
from sbxloop.daemon.fanout import FanoutFrontend, build_frontend
from sbxloop.daemon.local import LocalBridge
from sbxloop.daemon.model import DaemonNotice, RunReport, WorkItem
from sbxloop.daemon.store import ChatThread, DaemonStore
from sbxloop.errors import DaemonError
from sbxloop.events import EventBus


class Recorder:
    backend = "discord"

    def __init__(self, *, fail: bool = False, watch_note: str | None = "no id") -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail = fail
        self.watch_note = watch_note
        self.concierge: Any = None
        self.closed = False

    def _rec(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.fail:
            raise RuntimeError("boom")

    def start(self, *, connect_wait_s: float = 15.0) -> None:
        self._rec("start")

    def close(self, *, drain_wait_s: float = 20.0) -> None:
        self.closed = True

    def daemon_notice(self, notice: DaemonNotice) -> None:
        self._rec("daemon_notice", notice)

    def run_started(self, item: WorkItem, run_id: str, engine: Any, bus: EventBus) -> None:
        self._rec("run_started", run_id)

    def run_finished(self, item: WorkItem, report: RunReport) -> None:
        self._rec("run_finished", report.run_id)

    def merge_gate_opened(self, item: WorkItem, run_id: str, gate: Any) -> None:
        self._rec("merge_gate_opened", run_id)

    def merge_gate_resolved(
        self, item: WorkItem, run_id: str, gate: Any, outcome: str, by: Any, detail: Any = None
    ) -> None:
        self._rec("merge_gate_resolved", run_id, outcome)

    def on_watch(self, run_id: str, requester: str) -> str | None:
        self.calls.append(("on_watch", (run_id, requester)))
        return self.watch_note

    def thread_link(self, thread: ChatThread) -> str:
        return f"{self.backend}:{thread.thread_id}"


class LocalRecorder(Recorder):
    backend = "local"


def test_every_call_reaches_every_bridge_and_a_failure_is_isolated() -> None:
    bad = Recorder(fail=True)
    good = LocalRecorder()
    frontend = FanoutFrontend([bad, good])  # type: ignore[list-item]
    item = WorkItem(item_id="gh:issue:1", source_key="1", title="One")
    frontend.daemon_notice(DaemonNotice(kind="daemon.started", text="up"))
    frontend.run_started(item, "r1", object(), EventBus())  # type: ignore[arg-type]
    frontend.run_finished(item, RunReport("r1", "completed", "1/1"))
    frontend.merge_gate_opened(item, "r1", object())
    frontend.merge_gate_resolved(item, "r1", object(), "merged", "b")
    names = [c[0] for c in good.calls]
    assert names == [
        "daemon_notice",
        "run_started",
        "run_finished",
        "merge_gate_opened",
        "merge_gate_resolved",
    ]
    assert [c[0] for c in bad.calls] == names, "the failing bridge was still asked each time"


def test_start_failure_closes_what_started_and_reraises() -> None:
    first = Recorder()

    class Broken(Recorder):
        backend = "slack"

        def start(self, *, connect_wait_s: float = 15.0) -> None:
            raise DaemonError("no token")

    frontend = FanoutFrontend([first, Broken()])  # type: ignore[list-item]
    with pytest.raises(DaemonError, match="no token"):
        frontend.start()
    assert first.closed


def test_watch_registers_when_any_bridge_knows_the_requester() -> None:
    external = Recorder(watch_note="no mentionable id")
    local = LocalRecorder(watch_note=None)
    frontend = FanoutFrontend([external, local])  # type: ignore[list-item]
    assert frontend.on_watch("r1", "TUI user `brett`") is None
    nobody = FanoutFrontend([Recorder(watch_note="a"), LocalRecorder(watch_note="b")])  # type: ignore[list-item]
    assert nobody.on_watch("r1", "x") == "a"


def test_thread_link_dispatches_on_the_threads_backend() -> None:
    frontend = FanoutFrontend([Recorder(), LocalRecorder()])  # type: ignore[list-item]
    assert frontend.thread_link(ChatThread("c", "t1", None, None, "local")) == "local:t1"
    assert frontend.thread_link(ChatThread("c", "t2", None, None, "discord")) == "discord:t2"
    assert frontend.thread_link(ChatThread("c", "t3", None, None, "slack")) == "discord:t3"
    assert frontend.primary is frontend.bridges[0]
    assert frontend.bridge_for("local") is frontend.bridges[1]


def test_set_concierge_reaches_every_bridge() -> None:
    a, b = Recorder(), LocalRecorder()
    frontend = FanoutFrontend([a, b])  # type: ignore[list-item]
    token = object()
    frontend.set_concierge(token)  # type: ignore[arg-type]
    assert a.concierge is token and b.concierge is token


def test_build_frontend_headless_is_the_local_bridge_alone(tmp_path: Path) -> None:
    config = Config.model_validate({"home": str(tmp_path / "state")})
    dstore = DaemonStore(config.paths.state_db)
    frontend = build_frontend(config, dstore)
    assert [b.backend for b in frontend.bridges] == ["local"]
    assert isinstance(frontend.bridge_for("local"), LocalBridge)
    assert frontend.primary is frontend.bridge_for("local")


def test_build_frontend_starts_the_local_bridge_first(tmp_path: Path) -> None:
    """The local bridge is ready at once and must not wait behind a gateway
    connect; the external bridge is still the primary for prose links."""
    config = Config.model_validate({"home": str(tmp_path / "state"), "discord": {"channel_id": 42}})
    dstore = DaemonStore(config.paths.state_db)
    frontend = build_frontend(config, dstore)
    assert [b.backend for b in frontend.bridges] == ["local", "discord"]
    assert frontend.primary is not None and frontend.primary.backend == "discord"


def test_a_failed_start_names_the_bridge() -> None:
    class Broken(Recorder):
        backend = "slack"

        def start(self, *, connect_wait_s: float = 15.0) -> None:
            raise RuntimeError("socket refused")

    frontend = FanoutFrontend([Broken()])  # type: ignore[list-item]
    with pytest.raises(DaemonError, match="slack bridge failed to start: socket refused"):
        frontend.start()
