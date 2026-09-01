"""DaemonAgent: the daemon's long-lived agent-role sandbox.

The Discord concierge — an LLM session that answers the control channel
and drives the daemon through host tools — runs in a sandbox like every
other agent session (the microVM is the boundary), but not in a run's
pair: it lives for the daemon's lifetime, provisioned lazily on the first
message and re-provisioned on failure at most once per
:data:`REPROVISION_MIN_INTERVAL_S` (the :class:`~sbxloop.daemon.github.DaemonGithub`
pattern).

Unlike the github box it is **reused across daemon restarts**: the SDK's
session store — the concierge's conversation memory — lives inside the VM,
and a fresh microVM plus Copilot install costs minutes at every restart.
So :meth:`DaemonAgent.client` first looks for the deterministic name in
``sbx ls`` and keeps the sandbox when the installed worker still matches
this host (:meth:`WorkerClient.verify_installed`); a host upgrade or a
wedged VM falls through to a clean re-provision. ``sbxloop sandbox rm
--all`` still removes it; ``sandbox prune`` reports it as daemon-owned
and leaves it alone.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from sbxloop.config import Config
from sbxloop.errors import DaemonError, SbxError, SbxloopError, WorkerError
from sbxloop.events import EventBus
from sbxloop.log import get_logger
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner
from sbxloop.sbx.sandbox import Sandbox
from sbxloop.worker.client import WorkerClient

log = get_logger(__name__)

T = TypeVar("T")

SANDBOX_NAME_PREFIX = "sbxloop-concierge"
# Events from the concierge's own sandbox carry this run id.
CONCIERGE_RUN_ID = "concierge"
# Same reasoning as the github box: a dead sandbox costs one re-provision,
# an outage must not cost one per failing message.
REPROVISION_MIN_INTERVAL_S = 300.0


def sandbox_name_for(state_dir: Path) -> str:
    """Per-instance sandbox name (the state dir is the daemon's identity,
    see ``sbxloop.daemon.github.sandbox_name_for``)."""
    digest = hashlib.sha256(str(state_dir.resolve()).encode()).hexdigest()[:8]
    return f"{SANDBOX_NAME_PREFIX}-{digest}"


class DaemonAgent:
    def __init__(
        self,
        config: Config,
        sbx: SbxCLI,
        bus: EventBus,
        *,
        worker_python: str,
        install_workers: bool = True,
        name: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.sbx = sbx
        self.bus = bus
        self.worker_python = worker_python
        self.install_workers = install_workers
        self.name = name or sandbox_name_for(config.state_dir)
        self.clock = clock
        self._last_reprovision_at: float | None = None
        self.provisioner = Provisioner(sbx, config, bus=bus)
        self._sandbox: Sandbox | None = None
        self._client: WorkerClient | None = None

    @property
    def workspace(self) -> Path:
        # Scratch: the concierge never edits code; everything it can do is
        # a host tool. sbx create still needs a workspace mount.
        return (self.config.state_dir / "daemon" / "concierge-workspace").resolve()

    # -- access ------------------------------------------------------------

    def client(self) -> WorkerClient:
        if self._client is None:
            log.info("concierge_sandbox.provision_needed", sandbox=self.name)
            self._client = self._ensure()
        return self._client

    def call(self, fn: Callable[[WorkerClient], T]) -> T:
        """Run ``fn(client)``; on failure drop the sandbox (rate-limited, see
        :meth:`note_failure`) and retry once."""
        try:
            return fn(self.client())
        except (WorkerError, SbxError) as exc:
            if not self.note_failure(exc):
                raise
            return fn(self.client())

    def note_failure(self, exc: BaseException) -> bool:
        """A caller's job failed: drop the sandbox so the next :meth:`client`
        re-provisions — at most once per :data:`REPROVISION_MIN_INTERVAL_S`.
        Returns whether the sandbox was dropped."""
        now = self.clock()
        last = self._last_reprovision_at
        if last is not None and now - last < REPROVISION_MIN_INTERVAL_S:
            log.warning(
                "concierge_sandbox.job_failed",
                sandbox=self.name,
                error=str(exc),
                reprovisioned_ago_s=round(now - last),
                min_interval_s=REPROVISION_MIN_INTERVAL_S,
                action="keeping the sandbox; re-provisioned too recently",
            )
            return False
        log.warning(
            "concierge_sandbox.job_failed",
            sandbox=self.name,
            error=str(exc),
            action="dropping the sandbox; the next message re-provisions",
        )
        self._last_reprovision_at = now
        self.remove()
        return True

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Forget the handle; the sandbox stays for the next daemon process
        (conversation memory lives inside it)."""
        self._sandbox, self._client = None, None

    def remove(self) -> None:
        """Delete the sandbox (explicit: reprovision, operator cleanup)."""
        sandbox, self._sandbox, self._client = self._sandbox, None, None
        if sandbox is None:
            sandbox = Sandbox(self.sbx, self.name)
        try:
            sandbox.rm()
            log.info("concierge_sandbox.removed", sandbox=self.name)
        except SbxError:
            log.debug("concierge_sandbox.remove_failed", sandbox=self.name, exc_info=True)

    def exists(self) -> bool:
        try:
            return any(info.name == self.name for info in self.sbx.ls())
        except SbxError:
            return False

    # -- provisioning ------------------------------------------------------

    def _make_client(self, sandbox: Sandbox) -> WorkerClient:
        return WorkerClient(
            sandbox,
            self.bus,
            transport=self.config.worker_transport,
            python=self.worker_python,
            role="agent",
            limits=self.config.limits,
            # The concierge box is long-lived and reused across daemon
            # restarts; passing the sandbox lets job_env re-probe stdin
            # delivery when the conformance cache was wiped in between,
            # instead of a reused box silently losing its credential (#592).
            job_env=self.provisioner.job_env("agent", sandbox=sandbox),
        )

    def _ensure(self) -> WorkerClient:
        started = time.monotonic()
        if self.exists():
            sandbox = Sandbox(self.sbx, self.name)
            client = self._make_client(sandbox)
            if not self.install_workers or client.verify_installed():
                self._sandbox = sandbox
                self.bus.emit("sandbox.reused", CONCIERGE_RUN_ID, name=self.name, role="agent")
                log.info(
                    "concierge_sandbox.reused",
                    sandbox=self.name,
                    duration_s=round(time.monotonic() - started, 1),
                )
                return client
            log.warning(
                "concierge_sandbox.stale",
                sandbox=self.name,
                action="worker does not match this host; re-provisioning",
            )
            self.remove()

        clients: list[WorkerClient] = []

        def install(sandbox: Sandbox, _role: str) -> None:
            # Inside ensure_agent_only's try: a failed install rolls back the
            # sandbox and its registered secret.
            client = self._make_client(sandbox)
            if self.install_workers:
                client.install(extras="copilot", expect_prebaked=bool(self.config.sandbox.template))
            clients.append(client)

        log.info(
            "concierge_sandbox.provision_start",
            sandbox=self.name,
            workspace=str(self.workspace),
            install_workers=self.install_workers,
        )
        try:
            sandbox = self.provisioner.ensure_agent_only(
                self.name, self.workspace, post_create=install, run_id=CONCIERGE_RUN_ID
            )
        except SbxloopError as exc:
            log.error(
                "concierge_sandbox.provision_failed",
                sandbox=self.name,
                duration_s=round(time.monotonic() - started, 1),
                error=str(exc),
            )
            raise DaemonError(f"cannot provision the concierge sandbox: {exc}") from exc
        self._sandbox = sandbox
        log.info(
            "concierge_sandbox.ready",
            sandbox=self.name,
            duration_s=round(time.monotonic() - started, 1),
        )
        return clients[0]
