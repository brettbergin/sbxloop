"""DaemonAgent: the daemon's long-lived concierge sandbox.

Provisioning runs through the fake sbx (no worker install: install_workers
is False or the version probe is scripted); the point here is the
lifecycle — reuse across daemon processes when the installed worker
matches, re-provision when it does not, keep on close, delete on remove,
rate-limited drop on failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sbxloop import __version__
from sbxloop.config import Config
from sbxloop.daemon.agentbox import (
    REPROVISION_MIN_INTERVAL_S,
    SANDBOX_NAME_PREFIX,
    DaemonAgent,
    sandbox_name_for,
)
from sbxloop.errors import DaemonError, WorkerError
from sbxloop.events import Event, EventBus
from sbxloop.sbx.cli import SbxCLI
from tests.conftest import FakeSbx

# Both agent credentials: provisioning takes the one [agent] backend names,
# and a box built for the other backend is what the reuse gate must catch.
TOKENS = {
    "COPILOT_GITHUB_TOKEN": "github_pat_copilot",
    "ANTHROPIC_API_KEY": "sk-ant-test-key",
}


def make_agent(
    fake_sbx: FakeSbx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    install_workers: bool = False,
    bus: EventBus | None = None,
    clock=None,
    backend: str | None = None,
) -> DaemonAgent:
    for key, value in TOKENS.items():
        monkeypatch.setenv(key, value)
    settings: dict[str, object] = {"home": str(tmp_path / "state")}
    if backend is not None:
        settings["agent"] = {"backend": backend}
    config = Config.model_validate(settings)
    kwargs = {"clock": clock} if clock is not None else {}
    return DaemonAgent(
        config,
        SbxCLI(binary=str(fake_sbx.binary)),
        bus or EventBus(),
        worker_python=sys.executable,
        install_workers=install_workers,
        **kwargs,
    )


def created_names(fake_sbx: FakeSbx) -> list[str]:
    return [c[1].removeprefix("--name=") for c in fake_sbx.invocations("create")]


class TestNaming:
    def test_name_is_per_state_dir_and_stable(self, tmp_path: Path) -> None:
        a = Config.model_validate({"home": str(tmp_path / "a")})
        b = Config.model_validate({"home": str(tmp_path / "b")})
        agent_a = DaemonAgent(a, sbx=object(), bus=EventBus(), worker_python="python3")  # type: ignore[arg-type]
        agent_b = DaemonAgent(b, sbx=object(), bus=EventBus(), worker_python="python3")  # type: ignore[arg-type]
        assert agent_a.name != agent_b.name
        assert agent_a.name.startswith(SANDBOX_NAME_PREFIX + "-")
        assert agent_a.name == sandbox_name_for(a.paths)
        assert agent_a.workspace == (a.paths.daemon / "concierge-workspace").resolve()


class TestLifecycle:
    def test_first_client_provisions_an_agent_sandbox(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[Event] = []
        bus = EventBus()
        bus.subscribe(events.append)
        agent = make_agent(fake_sbx, tmp_path, monkeypatch, bus=bus)
        client = agent.client()
        assert client.sandbox.name == agent.name and client.role == "agent"
        assert created_names(fake_sbx) == [agent.name]
        assert any("api.githubcopilot.com" in c for c in fake_sbx.policies())
        assert [e.type for e in events if e.type.startswith("sandbox.")] == [
            "sandbox.provision_start",
            "sandbox.ready",
        ]
        assert all(e.run_id == "concierge" for e in events)
        # Same handle on the next call, no second create.
        assert agent.client() is client
        assert created_names(fake_sbx) == [agent.name]

    def test_close_keeps_the_sandbox_and_a_new_process_reuses_it(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = make_agent(fake_sbx, tmp_path, monkeypatch)
        first.client()
        first.close()
        assert fake_sbx.invocations("rm") == []
        assert first.exists()

        events: list[Event] = []
        bus = EventBus()
        bus.subscribe(events.append)
        second = make_agent(fake_sbx, tmp_path, monkeypatch, bus=bus)
        second.client()
        assert created_names(fake_sbx) == [first.name]  # still just the one create
        assert [e.type for e in events] == ["sandbox.reused"]

    def test_reuse_requires_a_matching_worker(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A host upgrade must not trust the old sandbox's worker: the
        version probe fails → the box is removed and re-provisioned."""
        stale = make_agent(fake_sbx, tmp_path, monkeypatch)
        stale.client()
        stale.close()
        agent = make_agent(fake_sbx, tmp_path, monkeypatch, install_workers=True)
        # The version probe runs the fake's exec on the host: script an
        # answer that is NOT this host's version, then the entrypoint smoke.
        fake_sbx.script(
            f"exec {agent.name} {sys.executable} -c import sbxloop_worker",
            stdout="0.0.0-stale\n",
            once=True,
        )
        # Re-provision then installs the worker: script the install ladder
        # to succeed cheaply (venv + pip + probe + smoke).
        fake_sbx.script(f"exec {agent.name} python3 -m venv", once=True)
        fake_sbx.script(f"exec {agent.name}", stdout=f"{__version__}\n")
        with pytest.raises(DaemonError):
            # The scripted blanket exec answer makes the entrypoint smoke
            # return rc 0 (expected 64), so the install fails and the
            # rollback path runs — proving the stale box was replaced by a
            # fresh create rather than reused.
            agent.client()
        assert created_names(fake_sbx) == [stale.name, agent.name]
        assert fake_sbx.invocations("rm") != []

    def test_reuse_requires_the_configured_agent_backend(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Switching [agent] backend must rebuild the box (#533).

        The worker is installed with the backend's extra, so a box built
        under copilot carries the Copilot SDK and no Claude Code CLI — while
        reporting the very same worker version. The version probe alone
        therefore kept it, and every concierge message then failed with
        BackendUnavailableError until an operator removed it by hand.
        """
        stale = make_agent(fake_sbx, tmp_path, monkeypatch)
        stale.client()
        stale.close()
        agent = make_agent(fake_sbx, tmp_path, monkeypatch, install_workers=True, backend="claude")
        # The reuse probes are scripted to PASS — a matching version and a
        # healthy entrypoint — so the only thing that can refuse this box is
        # the backend probe. Each is consumed once, leaving the blanket
        # answer below to govern the re-provision that follows.
        fake_sbx.script(
            f"exec {agent.name} {sys.executable} -c import sbxloop_worker",
            stdout=f"{__version__}\n",
            once=True,
        )
        fake_sbx.script(
            f"exec {agent.name} {sys.executable} -m sbxloop_worker",
            returncode=64,
            once=True,
        )
        fake_sbx.script(
            f"exec {agent.name} {sys.executable} -c import sys; from sbxloop_worker.backends",
            returncode=1,
            stderr="claude-agent-sdk is not installed; install sbxloop-worker[claude]",
            once=True,
        )
        fake_sbx.script(f"exec {agent.name} python3 -m venv", once=True)
        fake_sbx.script(f"exec {agent.name}", stdout=f"{__version__}\n")
        with pytest.raises(DaemonError):
            # As above: the blanket answer makes the re-install's entrypoint
            # smoke return 0 (expected 64), so provisioning fails and rolls
            # back — proving the box was rebuilt rather than reused.
            agent.client()
        assert created_names(fake_sbx) == [stale.name, agent.name]
        assert fake_sbx.invocations("rm") != []

    def test_reuse_is_kept_when_the_backend_still_matches(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The backend probe is a gate, not a second re-provision trigger: an
        equipped box is still reused, with no new create."""
        first = make_agent(fake_sbx, tmp_path, monkeypatch)
        first.client()
        first.close()
        events: list[Event] = []
        bus = EventBus()
        bus.subscribe(events.append)
        second = make_agent(fake_sbx, tmp_path, monkeypatch, install_workers=True, bus=bus)
        fake_sbx.script(
            f"exec {second.name} {sys.executable} -c import sys; from sbxloop_worker.backends",
            returncode=0,
        )
        second.client()
        assert created_names(fake_sbx) == [first.name]
        assert [e.type for e in events] == ["sandbox.reused"]

    def test_remove_deletes_the_sandbox(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = make_agent(fake_sbx, tmp_path, monkeypatch)
        agent.client()
        agent.remove()
        assert not agent.exists()
        assert fake_sbx.invocations("rm") != []
        # remove() on a missing sandbox is quiet
        agent.remove()

    def test_missing_token_is_a_daemon_error_and_nothing_is_created(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = make_agent(fake_sbx, tmp_path, monkeypatch)
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN")
        agent.provisioner.env = {}
        with pytest.raises(DaemonError, match="COPILOT_GITHUB_TOKEN"):
            agent.client()
        assert created_names(fake_sbx) == []


class TestFailureHandling:
    def test_call_drops_and_retries_once(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = make_agent(fake_sbx, tmp_path, monkeypatch)
        attempts = 0

        def flaky(client: object) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise WorkerError("worker died")
            return "ok"

        assert agent.call(flaky) == "ok"
        assert attempts == 2
        # Dropped and re-provisioned: two creates, one rm in between.
        assert len(created_names(fake_sbx)) == 2
        assert fake_sbx.invocations("rm") != []

    def test_note_failure_is_rate_limited(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = [1000.0]
        agent = make_agent(fake_sbx, tmp_path, monkeypatch, clock=lambda: now[0])
        agent.client()
        assert agent.note_failure(WorkerError("first")) is True
        assert agent.note_failure(WorkerError("second")) is False
        now[0] += REPROVISION_MIN_INTERVAL_S + 1
        assert agent.note_failure(WorkerError("third")) is True
        with pytest.raises(WorkerError):
            # inside the window again: call() does not retry
            agent.call(lambda client: (_ for _ in ()).throw(WorkerError("boom")))
