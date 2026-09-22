"""DaemonAgent: the daemon's long-lived concierge sandbox.

Provisioning runs through the fake sbx (no worker install: install_workers
is False or the version probe is scripted); the point here is the
lifecycle — reuse across daemon processes when the installed worker
matches, re-provision when it does not, keep on close, delete on remove,
rate-limited drop on failure.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from sbxloop import __version__
from sbxloop.config import Config
from sbxloop.daemon.agentbox import (
    REPROVISION_MIN_INTERVAL_S,
    DaemonAgent,
    sandbox_name_for,
)
from sbxloop.errors import DaemonError, WorkerError, WorkerTimeoutError
from sbxloop.events import Event, EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.sandbox import Sandbox
from sbxloop.worker.client import WorkerClient
from tests.conftest import FakeSbx

# Both agent credentials: provisioning takes the one [agent] backend names,
# and a box built for the other backend is what the reuse gate must catch.
TOKENS = {
    "COPILOT_GITHUB_TOKEN": "github_pat_copilot",
    "ANTHROPIC_API_KEY": "sk-ant-test-key",
    "OPENAI_API_KEY": "sk-test-openai-key",
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
        assert agent_a.name.endswith("-daemon-chat-concierge")
        assert agent_a.name == sandbox_name_for(a.paths)
        assert agent_a.workspace == (a.paths.daemon / "concierge-workspace").resolve()


class TestLifecycle:
    def test_existing_concierge_keeps_its_session_name_during_upgrade(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sbxloop.sbx.naming import legacy_concierge_name

        agent = make_agent(fake_sbx, tmp_path, monkeypatch)
        old = legacy_concierge_name(agent.config.paths)
        agent.provisioner.ensure_agent_only(old, agent.workspace, run_id="concierge")
        agent.client()
        assert agent.name == old
        assert created_names(fake_sbx) == [old]

    @pytest.mark.parametrize("old_vm", [False, True])
    def test_changed_or_unknown_allocation_preserves_concierge_history(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_vm: bool
    ) -> None:
        from sbxloop.errors import ProvisionError

        first = make_agent(fake_sbx, tmp_path, monkeypatch)
        first.client()
        history = fake_sbx.sandbox_fs(first.name) / "home/agent/history.txt"
        history.write_text("conversation to preserve")
        first.close()
        if old_vm:
            for record in first.config.paths.sandbox_allocations.glob("*.json"):
                record.unlink()
        second = make_agent(fake_sbx, tmp_path, monkeypatch)
        if not old_vm:
            second.config.sandbox.concierge_cpus = 3
        with pytest.raises(ProvisionError, match="needs recreation"):
            second.call(lambda client: client)
        assert history.read_text() == "conversation to preserve"
        assert fake_sbx.invocations("rm") == []
        assert created_names(fake_sbx) == [first.name]
        assert fake_sbx.meta(first.name)["cpus"] == 2
        assert fake_sbx.meta(first.name)["memory"] == "4g"

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

    def test_a_missing_docker_session_names_sbx_login(
        self,
        fake_sbx: FakeSbx,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A Docker session that expired fails every sbx call the same way;
        the provisioning report names the remedy, not the image or the disk."""
        agent = make_agent(fake_sbx, tmp_path, monkeypatch)
        fake_sbx.fail_next(
            "create",
            stderr="ERROR: create sandbox: request failed: 401 Unauthorized: user is not "
            "authenticated to Docker: secret not found\nno valid user session found, "
            "please sign in to Docker to proceed",
        )

        with (
            caplog.at_level(logging.ERROR, logger="sbxloop.daemon.agentbox"),
            pytest.raises(DaemonError, match="not authenticated to Docker"),
        ):
            agent.client()

        (failed,) = [
            r for r in caplog.records if "concierge_sandbox.provision_failed" in r.getMessage()
        ]
        assert "`sbx login`" in failed.getMessage()
        assert "disk" not in failed.getMessage()

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

    @pytest.mark.parametrize("backend", ["claude", "codex"])
    def test_reuse_requires_the_configured_agent_backend(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
    ) -> None:
        """Switching [agent] backend must rebuild the box (#533).

        The worker is installed with the backend's extra, so a box built
        under copilot carries the Copilot SDK and no alternative runtime — while
        reporting the very same worker version. The version probe alone
        therefore kept it, and every concierge message then failed with
        BackendUnavailableError until an operator removed it by hand.
        """
        stale = make_agent(fake_sbx, tmp_path, monkeypatch)
        stale.client()
        stale.close()
        agent = make_agent(fake_sbx, tmp_path, monkeypatch, install_workers=True, backend=backend)
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
            stderr=f"backend runtime is not installed; install sbxloop-worker[{backend}]",
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

    def test_reprovision_waits_out_the_stale_box_teardown(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`sbx rm` returns once the backend accepts the teardown, not once it
        has finished. Re-creating the same name into that window loses the new
        sandbox to the old one's reaper part-way through provisioning; the
        concierge then answers nothing for a minute and a half while it
        retries, which reads from chat as a dead bridge (#952)."""
        monkeypatch.setattr("sbxloop.sbx.cli.RM_SETTLE_POLL_S", 0.01)
        first = make_agent(fake_sbx, tmp_path, monkeypatch)
        first.client()
        first.close()
        # The reuse gate refuses the box; the install is not what is under
        # test, so it is a no-op and the re-provision runs to completion.
        monkeypatch.setattr(DaemonAgent, "_is_reusable", lambda self, client: False)
        monkeypatch.setattr(WorkerClient, "install", lambda self, **kwargs: None)
        fake_sbx.linger_removals(3)

        agent = make_agent(fake_sbx, tmp_path, monkeypatch, install_workers=True)
        client = agent.client()

        assert client.sandbox.name == agent.name
        assert created_names(fake_sbx) == [first.name, agent.name]

    def test_reprovision_stops_when_the_teardown_is_unconfirmed(
        self, fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed: a teardown sbx will not confirm must not be followed
        by a create of the same name."""
        monkeypatch.setattr("sbxloop.sbx.cli.RM_SETTLE_POLL_S", 0.01)
        monkeypatch.setattr("sbxloop.sbx.cli.RM_SETTLE_TIMEOUT_S", 0.05)
        first = make_agent(fake_sbx, tmp_path, monkeypatch)
        first.client()
        first.close()
        monkeypatch.setattr(DaemonAgent, "_is_reusable", lambda self, client: False)
        fake_sbx.linger_removals(10_000)

        agent = make_agent(fake_sbx, tmp_path, monkeypatch, install_workers=True)
        with pytest.raises(DaemonError, match="still lists it"):
            agent.client()
        assert created_names(fake_sbx) == [first.name]

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


class StubSbx:
    """Just enough sandbox backend for the lease pool: which boxes exist,
    which were created and which removed. Provisioning itself is covered by
    the fake-sbx tests above."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.created: list[str] = []
        self.removed: list[str] = []
        # `rm` blocks until `rm_release` is set (it is, unless a test clears
        # it to hold a teardown in flight) and flags `rm_started` on entry.
        self.rm_started = threading.Event()
        self.rm_release = threading.Event()
        self.rm_release.set()

    def ls(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=name) for name in sorted(self.names)]

    def rm(self, name: str, *, force: bool = True, settle: bool = True) -> None:
        self.rm_started.set()
        self.rm_release.wait(10)
        self.removed.append(name)
        self.names.discard(name)


def lease_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, turns: int = 1, clock=None
) -> tuple[DaemonAgent, StubSbx]:
    sbx = StubSbx()
    config = Config.model_validate(
        {"home": str(tmp_path / "state"), "concierge": {"max_concurrent_turns": turns}}
    )
    kwargs = {"clock": clock} if clock is not None else {}
    agent = DaemonAgent(
        config,
        sbx,  # type: ignore[arg-type]
        EventBus(),
        worker_python=sys.executable,
        install_workers=False,
        **kwargs,
    )
    monkeypatch.setattr(agent.provisioner, "job_env", lambda *args, **kwargs: None)

    def ensure() -> WorkerClient:
        sbx.created.append(agent.name)
        sbx.names.add(agent.name)
        sandbox = Sandbox(sbx, agent.name)  # type: ignore[arg-type]
        agent._sandbox = sandbox
        return agent._make_client(sandbox)

    monkeypatch.setattr(agent, "_ensure", ensure)
    return agent, sbx


class TestLeases:
    """Several turns share the one concierge sandbox through leased clients:
    a bounded pool of worker clients over the same box. A lease yields the
    worker client itself; while it is held, ``lease_generation(client)``
    names the incarnation of the box it was handed out for."""

    def test_a_lease_yields_the_worker_client_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _sbx = lease_agent(tmp_path, monkeypatch)
        try:
            with agent.lease() as client:
                assert isinstance(client, WorkerClient)
                assert agent.lease_generation(client) is not None
            # Once returned, the client names no lease any more.
            assert agent.lease_generation(client) is None
        finally:
            agent.close()

    def test_the_default_width_leases_the_one_client_and_queues_the_next(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, sbx = lease_agent(tmp_path, monkeypatch)
        try:
            with agent.lease() as client:
                assert client is agent.client()
                with pytest.raises(WorkerTimeoutError), agent.lease(timeout=0.05):
                    pass
            with agent.lease() as again:
                assert again is client
            assert sbx.created == [agent.name]
        finally:
            agent.close()

    def test_concurrent_leases_get_distinct_clients_over_the_same_sandbox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, sbx = lease_agent(tmp_path, monkeypatch, turns=2)
        try:
            with agent.lease() as first, agent.lease() as second:
                assert first is not second
                assert first.sandbox.name == second.sandbox.name == agent.name
                assert second.role == "agent"
                assert second.backend == agent.config.agent.backend
                assert agent.lease_generation(first) == agent.lease_generation(second)
            assert sbx.created == [agent.name]
            # Returned clients are reused, not rebuilt.
            with agent.lease() as a, agent.lease() as b:
                assert {id(a), id(b)} == {id(first), id(second)}
        finally:
            agent.close()

    def test_a_lease_beyond_the_width_waits_for_one_to_come_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, sbx = lease_agent(tmp_path, monkeypatch, turns=2)
        got: list[WorkerClient] = []
        started = threading.Event()

        def third() -> None:
            started.set()
            with agent.lease(timeout=5) as client:
                got.append(client)

        try:
            with agent.lease() as first, agent.lease() as second:
                with pytest.raises(WorkerTimeoutError), agent.lease(timeout=0.05):
                    pass
                thread = threading.Thread(target=third)
                thread.start()
                started.wait(5)
                thread.join(0.2)
                assert thread.is_alive() and got == []  # waiting while both are out
            thread.join(5)
            assert not thread.is_alive()
            assert got[0] in (first, second)
            assert sbx.created == [agent.name]
        finally:
            agent.close()

    def test_a_failure_on_an_old_generation_leaves_the_new_box_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = [1000.0]
        agent, sbx = lease_agent(tmp_path, monkeypatch, turns=2, clock=lambda: now[0])
        try:
            with agent.lease() as old:
                old_generation = agent.lease_generation(old)
            assert agent.note_failure(WorkerError("died"), generation=old_generation) is True
            assert sbx.removed == [agent.name]
            with agent.lease() as new:
                new_generation = agent.lease_generation(new)
                assert new_generation != old_generation
                assert new is not old
            assert sbx.created == [agent.name, agent.name]
            now[0] += REPROVISION_MIN_INTERVAL_S + 1
            # A late report from a turn that ran on the old box: the box it
            # blames is already gone, so its retry may go ahead, but nothing
            # is removed.
            assert agent.note_failure(WorkerError("late"), generation=old_generation) is True
            assert sbx.removed == [agent.name]
            assert agent.exists()
            with agent.lease() as after:
                assert agent.lease_generation(after) == new_generation
                assert after is new
        finally:
            agent.close()

    def test_a_failure_is_not_torn_down_under_another_active_lease(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, sbx = lease_agent(tmp_path, monkeypatch, turns=2)
        try:
            with agent.lease() as busy:
                busy_generation = agent.lease_generation(busy)
                with agent.lease() as failed:
                    failed_generation = agent.lease_generation(failed)
                assert agent.note_failure(WorkerError("died"), generation=failed_generation)
                # The other turn is still using the box.
                assert sbx.removed == []
                assert agent.exists()
                # A second report for the same condemned box changes nothing.
                assert agent.note_failure(WorkerError("also"), generation=busy_generation)
                assert sbx.removed == []
            # The last lease came back: now the box goes, and the next lease
            # provisions a fresh one.
            assert sbx.removed == [agent.name]
            with agent.lease() as fresh:
                assert agent.lease_generation(fresh) != busy_generation
                assert fresh is not busy
            assert sbx.created == [agent.name, agent.name]
        finally:
            agent.close()

    def test_a_condemned_box_is_not_leased_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, sbx = lease_agent(tmp_path, monkeypatch, turns=2)
        try:
            with agent.lease():
                with agent.lease() as failed:
                    failed_generation = agent.lease_generation(failed)
                agent.note_failure(WorkerError("died"), generation=failed_generation)
                # The failed turn's retry must not land on the box that is
                # about to be removed; it waits for the replacement.
                with pytest.raises(WorkerTimeoutError), agent.lease(timeout=0.05):
                    pass
            assert sbx.removed == [agent.name]
        finally:
            agent.close()

    def test_clients_of_a_replaced_box_are_closed_when_returned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, _sbx = lease_agent(tmp_path, monkeypatch, turns=2)
        try:
            with agent.lease() as idle:
                pass
            with agent.lease() as outstanding:
                assert outstanding is idle
                outstanding_generation = agent.lease_generation(outstanding)
                agent.remove()
                with agent.lease() as current:
                    current_generation = agent.lease_generation(current)
                    assert current_generation != outstanding_generation
                    assert current is not outstanding
            # Neither the idle client of the old box nor the one handed back
            # after the replacement is ever leased again.
            with agent.lease() as a, agent.lease() as b:
                assert outstanding not in (a, b)
                assert current in (a, b)
                assert agent.lease_generation(a) == agent.lease_generation(b)
                assert agent.lease_generation(a) == current_generation
        finally:
            agent.close()


def run_in_thread(fn: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    """Run ``fn`` on a thread; its return value or exception lands in the list."""
    outcome: list[object] = []

    def target() -> None:
        try:
            outcome.append(fn())
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, outcome


class TestRemovalOffTheLock:
    """Removing a retired box (`sbx rm`, which can take the whole settle
    timeout, or longer on a wedged host) never holds the lease-pool lock:
    other turns' leases, their timeouts and generation reads stay responsive
    while it runs, and only the next provision waits for it to finish."""

    def held_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[DaemonAgent, StubSbx, WorkerClient, threading.Thread]:
        """A failed turn condemns the box while another turn still uses it;
        that turn's release runs the removal, which is held in `rm`."""
        agent, sbx = lease_agent(tmp_path, monkeypatch, turns=2)
        sbx.rm_release.clear()
        busy_lease = agent.lease()
        busy = busy_lease.__enter__()
        with agent.lease() as failed:
            failed_generation = agent.lease_generation(failed)
        assert agent.note_failure(WorkerError("died"), generation=failed_generation)
        releaser, _ = run_in_thread(lambda: busy_lease.__exit__(None, None, None))
        assert sbx.rm_started.wait(5)
        return agent, sbx, busy, releaser

    def test_leases_and_generation_reads_stay_responsive_during_a_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, sbx, busy, releaser = self.held_removal(tmp_path, monkeypatch)
        try:
            reader, read = run_in_thread(lambda: agent.lease_generation(busy))
            reader.join(2)
            assert not reader.is_alive(), "lease_generation() blocked behind sbx rm"
            assert read == [None]

            def attempt() -> None:
                with agent.lease(timeout=0.05):
                    pass

            waiter, outcome = run_in_thread(attempt)
            waiter.join(2)
            assert not waiter.is_alive(), "lease() blocked behind sbx rm past its timeout"
            assert isinstance(outcome[0], WorkerTimeoutError)
            # The removal is still in flight: nothing above waited it out.
            assert sbx.removed == [] and releaser.is_alive()
        finally:
            sbx.rm_release.set()
            releaser.join(5)
            agent.close()
        assert sbx.removed == [agent.name]

    def test_the_next_provision_waits_for_the_removal_to_finish(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Creating the same name while its teardown is still running loses
        the new box to the old one's reaper (#952): the replacement is
        provisioned only once the removal has finished."""
        agent, sbx, _busy, releaser = self.held_removal(tmp_path, monkeypatch)
        got: list[WorkerClient] = []

        def attempt() -> None:
            with agent.lease(timeout=5) as client:
                got.append(client)

        try:
            waiter, outcome = run_in_thread(attempt)
            waiter.join(0.3)
            assert waiter.is_alive() and got == []
            assert sbx.created == [agent.name]
            sbx.rm_release.set()
            waiter.join(5)
            assert not waiter.is_alive() and outcome == [None]
            assert sbx.removed == [agent.name]
            assert sbx.created == [agent.name, agent.name]
        finally:
            sbx.rm_release.set()
            releaser.join(5)
            agent.close()

    def test_an_explicit_remove_runs_off_the_lock_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, sbx = lease_agent(tmp_path, monkeypatch, turns=2)
        sbx.rm_release.clear()
        try:
            with agent.lease() as held:
                held_generation = agent.lease_generation(held)
                remover, _ = run_in_thread(agent.remove)
                assert sbx.rm_started.wait(5)
                reader, read = run_in_thread(lambda: agent.lease_generation(held))
                reader.join(2)
                assert not reader.is_alive(), "lease_generation() blocked behind sbx rm"
                assert read == [held_generation]
                assert sbx.removed == [] and remover.is_alive()
                sbx.rm_release.set()
                remover.join(5)
            assert sbx.removed == [agent.name]
        finally:
            sbx.rm_release.set()
            agent.close()
