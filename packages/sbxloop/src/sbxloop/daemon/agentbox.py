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
this host (:meth:`WorkerClient.verify_installed`) *and* the box is equipped
for the configured ``[agent] backend``
(:meth:`WorkerClient.backend_ready`); a host upgrade, a backend switch or a
wedged VM falls through to a clean re-provision. ``sbxloop sandbox rm
--all`` still removes it; ``sandbox prune`` reports it as daemon-owned
and leaves it alone.

Several turns can run in the box at once: :meth:`DaemonAgent.lease` hands
each one a worker client of its own from a pool of at most ``[concierge]
max_concurrent_turns`` clients over the same sandbox (each client is its own
worker process with its own job ids and transport bookkeeping). A lease
remembers which *generation* of the sandbox it was handed out for, so a
failure reported by a turn that ran on a box since replaced never tears
down the replacement, and a box is never removed while another turn still
holds a lease on it.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple, TypeVar

from sbxloop.config import Config
from sbxloop.engine.store import StateStore
from sbxloop.errors import (
    DaemonError,
    SbxError,
    SbxloopError,
    WorkerError,
    WorkerTimeoutError,
    caused_by_sbx_auth,
)
from sbxloop.events import EventBus
from sbxloop.ids import new_job_id
from sbxloop.log import get_logger
from sbxloop.paths import SbxloopHome
from sbxloop.provider import ProviderRecovery
from sbxloop.sbx.allocations import require_allocation
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner
from sbxloop.sbx.sandbox import Sandbox
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import JobRequest
from sbxloop_worker.rate_limits import MAX_REPORT_BYTES, QUERY_TIMEOUT_S, RateLimitReport

log = get_logger(__name__)

T = TypeVar("T")

SANDBOX_NAME_PREFIX = "sbxloop-concierge"
# Events from the concierge's own sandbox carry this run id.
CONCIERGE_RUN_ID = "concierge"
# Same reasoning as the github box: a dead sandbox costs one re-provision,
# an outage must not cost one per failing message.
REPROVISION_MIN_INTERVAL_S = 300.0
# A lease waits this much longer than one concierge turn may run: every
# client it could be waiting for is bounded by that turn timeout.
LEASE_WAIT_MARGIN_S = 60.0


class AgentLease(NamedTuple):
    """One turn's hold on a worker client in the concierge sandbox."""

    client: WorkerClient
    #: Which incarnation of the sandbox the client talks to; a failure is
    #: reported against it (:meth:`DaemonAgent.note_failure`).
    generation: int


def sandbox_name_for(home: SbxloopHome) -> str:
    """Per-instance sandbox name (the home is the daemon's identity,
    see ``sbxloop.daemon.github.sandbox_name_for``)."""
    digest = hashlib.sha256(str(home.root.resolve()).encode()).hexdigest()[:8]
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
        self.name = name or sandbox_name_for(config.paths)
        self.clock = clock
        self._last_reprovision_at: float | None = None
        self.provisioner = Provisioner(sbx, config, bus=bus)
        self._sandbox: Sandbox | None = None
        self._client: WorkerClient | None = None
        self._mcp_client: WorkerClient | None = None
        self._provider_store: StateStore | None = None
        # The lease pool. `_generation` names the current incarnation of the
        # sandbox and moves on every removal; `_free` holds its idle
        # clients, `_slots` counts the clients made for it (idle or leased)
        # and `_primary_pooled` says whether `_client` is one of them.
        # `_condemned` marks a box that failed while other leases still
        # used it: it is removed when the last of them comes back.
        self._pool = threading.Condition()
        self._provision_lock = threading.RLock()
        self._generation = 0
        self._free: list[WorkerClient] = []
        self._slots = 0
        self._active = 0
        self._primary_pooled = False
        self._condemned = False

    @property
    def workspace(self) -> Path:
        # Scratch: the concierge never edits code; everything it can do is
        # a host tool. sbx create still needs a workspace mount.
        return self.config.paths.concierge_workspace.resolve()

    # -- access ------------------------------------------------------------

    def client(self) -> WorkerClient:
        if self._client is None:
            log.info("concierge_sandbox.provision_needed", sandbox=self.name)
            self._client = self._ensure()
            if self.install_workers:
                from sbxloop.modelcatalog import refresh_after_provision

                refresh_after_provision(self.config)
        return self._client

    @property
    def max_leases(self) -> int:
        return self.config.concierge.max_concurrent_turns

    @contextmanager
    def lease(self, timeout: float | None = None) -> Iterator[AgentLease]:
        """Hold a worker client of the sandbox for one turn.

        Clients are made lazily, up to :attr:`max_leases`, and reused once
        returned; with every one out this waits up to ``timeout`` seconds
        (by default a turn's timeout plus :data:`LEASE_WAIT_MARGIN_S`), then
        raises :class:`WorkerTimeoutError`. With one lease allowed, the
        lease is always :meth:`client`'s own client.
        """
        if timeout is None:
            timeout = self.config.concierge.timeout_s + LEASE_WAIT_MARGIN_S
        held = self._acquire(timeout)
        try:
            yield held
        finally:
            self._release(held)

    def _acquire(self, timeout: float) -> AgentLease:
        deadline = time.monotonic() + timeout
        with self._pool:
            while True:
                if self._condemned and self._active == 0:
                    self._drop_locked()
                if not self._condemned:
                    if self._free:
                        self._active += 1
                        return AgentLease(self._free.pop(), self._generation)
                    if self._slots < self.max_leases:
                        self._slots += 1
                        self._active += 1
                        generation = self._generation
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerTimeoutError(
                        f"no concierge session came free within {timeout:.0f}s"
                    )
                self._pool.wait(remaining)
        try:
            client = self._pooled_client()
        except BaseException:
            with self._pool:
                self._active -= 1
                if generation == self._generation:
                    self._slots -= 1
                self._pool.notify_all()
            raise
        return AgentLease(client, generation)

    def _pooled_client(self) -> WorkerClient:
        """A new client for the pool: the provisioning client first, then
        siblings in the same sandbox."""
        with self._provision_lock:
            primary = self.client()
            if not self._primary_pooled:
                self._primary_pooled = True
                return primary
            client = self._sibling(
                primary, transport=self.config.worker_transport, limits=self.config.limits
            )
            client.provider_recovery = primary.provider_recovery
            client.mcp_prepare = primary.mcp_prepare
            return client

    def _sibling(self, active: WorkerClient, **options: Any) -> WorkerClient:
        """Another worker client in ``active``'s sandbox: its own worker
        process, job ids and transport bookkeeping, with the same
        interpreter and credential delivery."""
        return WorkerClient(
            active.sandbox,
            self.bus,
            python=active.python,
            role="agent",
            backend=self.config.agent.backend,
            job_env=active.job_env,
            **options,
        )

    def _release(self, held: AgentLease) -> None:
        with self._pool:
            self._active -= 1
            if held.generation == self._generation:
                if self._condemned:
                    # Never handed out again; the box goes below or at the
                    # next acquire.
                    self._slots -= 1
                else:
                    self._free.append(held.client)
            # A client of a replaced box is closed: dropped, never reused.
            if self._condemned and self._active == 0:
                self._drop_locked()
            self._pool.notify_all()

    def call(self, fn: Callable[[WorkerClient], T]) -> T:
        """Run ``fn(client)``; on failure drop the sandbox (rate-limited, see
        :meth:`note_failure`) and retry once."""
        try:
            return fn(self.client())
        except (WorkerError, SbxError) as exc:
            if not self.note_failure(exc):
                raise
            return fn(self.client())

    def agent_rate_limits(self) -> RateLimitReport:
        """Read status beside the active turn; never provision, retry or remove.

        A new client/worker process in the SAME agent sandbox has its own
        job id and transport bookkeeping. The concierge's worker may be
        blocked waiting for this host-tool response throughout the query.
        """
        backend = self.config.agent.backend
        unavailable = RateLimitReport(
            backend=backend,
            status="unavailable",
            reason="Agent sandbox status is unavailable; capacity and resets are unknown.",
        )
        active = self._client
        if active is None:
            return unavailable
        probe = self._sibling(active, transport="stream", grace_s=2)
        job = JobRequest(
            job_id=new_job_id(),
            run_id=CONCIERGE_RUN_ID,
            kind="agent.rate_limits",
            params={"backend": backend},
            timeout_s=min(QUERY_TIMEOUT_S, self.config.concierge.timeout_s / 2),
        )
        try:
            result = probe.submit(job)
            if result.status != "ok" or not isinstance(result.output_json, dict):
                return unavailable
            if len(json.dumps(result.output_json).encode()) > MAX_REPORT_BYTES:
                return unavailable
            report = RateLimitReport.model_validate(result.output_json)
            return report if report.backend == backend else unavailable
        except WorkerTimeoutError:
            return unavailable.model_copy(
                update={"status": "timeout", "reason": "Agent sandbox status query timed out."}
            )
        except Exception:
            # Never publish transport/SDK exception text or send a query
            # failure through note_failure(): the active session stays alive.
            return unavailable

    def note_failure(self, exc: BaseException, generation: int | None = None) -> bool:
        """A caller's job failed: drop the sandbox so the next :meth:`client`
        re-provisions — at most once per :data:`REPROVISION_MIN_INTERVAL_S`.
        Returns whether the sandbox was (or will be) dropped, i.e. whether a
        retry gets a fresh one.

        ``generation`` is the failed lease's (``None``: the current box).
        When that box has already been replaced nothing is removed and the
        retry lands on the replacement. While other leases still use the
        box it is only condemned: no lease is handed out on it any more, and
        it is removed once the last of them comes back.
        """
        with self._pool:
            if generation is not None and generation != self._generation:
                log.info(
                    "concierge_sandbox.stale_failure",
                    sandbox=self.name,
                    error=str(exc),
                    action="keeping the sandbox; the failed one was already replaced",
                )
                return True
            if self._condemned:
                return True
            return self._note_current_failure(exc)

    def _note_current_failure(self, exc: BaseException) -> bool:
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
        if self._active:
            log.info(
                "concierge_sandbox.removal_deferred",
                sandbox=self.name,
                active_leases=self._active,
            )
            self._condemned = True
        else:
            self._drop_locked()
        return True

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Forget the handle; the sandbox stays for the next daemon process
        (conversation memory lives inside it)."""
        with self._pool:
            self._new_generation()
        self._sandbox, self._client = None, None
        if self._provider_store is not None:
            self._provider_store.close()
            self._provider_store = None
        self._close_mcp()

    def _close_mcp(self) -> None:
        client, self._mcp_client = self._mcp_client, None
        if client is not None:
            try:
                client.sandbox.rm()
            except SbxError:
                log.warning("concierge_mcp.remove_failed")

    def _mcp_service(self) -> WorkerClient:
        if self._mcp_client is None:
            config = self.config.model_copy(
                update={
                    "mcp": [server for server in self.config.mcp if "concierge" in server.roles],
                    "registries": [],
                }
            )
            provisioner = Provisioner(self.sbx, config, bus=self.bus)
            credentials = [cred.name for cred in config.mcp_credentials()]
            clients: list[WorkerClient] = []

            def install(sandbox: Sandbox, _role: str) -> None:
                client = WorkerClient(
                    sandbox,
                    self.bus,
                    transport=config.worker_transport,
                    python=self.worker_python,
                    role="service",
                    limits=config.limits,
                    job_env=provisioner.job_env(
                        "service", sandbox=sandbox, credentials=credentials
                    ),
                )
                if self.install_workers:
                    client.install(expect_prebaked=bool(config.sandbox.template))
                clients.append(client)

            provisioner.ensure_service(
                self.name + "-mcp",
                self.workspace,
                credentials,
                post_create=install,
            )
            self._mcp_client = clients[0]
        return self._mcp_client

    def remove(self, *, strict: bool = False) -> None:
        """Delete the sandbox (explicit: reprovision, operator cleanup).

        ``strict`` propagates a teardown sbx would not confirm, for the one
        caller that is about to create the same name again (#952).
        """
        with self._pool:
            self._drop_locked(strict=strict)

    def _new_generation(self) -> None:
        """Retire the pool's clients; outstanding leases are closed as they
        come back. Called with ``_pool`` held."""
        self._generation += 1
        self._free.clear()
        self._slots = 0
        self._primary_pooled = False
        self._condemned = False
        self._pool.notify_all()

    def _drop_locked(self, *, strict: bool = False) -> None:
        self._new_generation()
        self._discard_sandbox(strict=strict)

    def _discard_sandbox(self, *, strict: bool = False) -> None:
        self._close_mcp()
        sandbox, self._sandbox, self._client = self._sandbox, None, None
        if sandbox is None:
            sandbox = Sandbox(self.sbx, self.name)
        try:
            sandbox.rm()
            log.info("concierge_sandbox.removed", sandbox=self.name)
        except SbxError:
            if strict:
                raise
            log.debug("concierge_sandbox.remove_failed", sandbox=self.name, exc_info=True)

    def exists(self) -> bool:
        try:
            return any(info.name == self.name for info in self.sbx.ls())
        except SbxError:
            return False

    # -- provisioning ------------------------------------------------------

    def _make_client(self, sandbox: Sandbox) -> WorkerClient:
        client = WorkerClient(
            sandbox,
            self.bus,
            transport=self.config.worker_transport,
            python=self.worker_python,
            role="agent",
            backend=self.config.agent.backend,
            limits=self.config.limits,
            # The concierge box is long-lived and reused across daemon
            # restarts; passing the sandbox lets job_env re-probe stdin
            # delivery when the conformance cache was wiped in between,
            # instead of a reused box silently losing its credential (#592).
            job_env=self.provisioner.job_env("agent", sandbox=sandbox),
        )
        if self._provider_store is None:
            self._provider_store = StateStore(self.config.paths.state_db)
        client.provider_recovery = ProviderRecovery(self._provider_store, self.config.agent.backend)
        from sbxloop.worker.mcp import McpBroker

        client.mcp_prepare = McpBroker(self._mcp_service).prepare
        return client

    def _is_reusable(self, client: WorkerClient) -> bool:
        """Can the existing box be kept, or must it be rebuilt?

        Two questions, both cheap probes. The worker must match this host —
        an upgrade re-installs rather than trusting the box. And the box
        must be equipped for the *configured* backend: it was installed with
        ``extras=[agent] backend``, so switching that leaves a box whose SDK
        is the old backend's while its worker version is unchanged, which
        the version check alone happily reuses (#533). The symptom is every
        concierge message failing with BackendUnavailableError until an
        operator removes the sandbox by hand.
        """
        return client.verify_installed() and client.backend_ready(self.config.agent.backend)

    def _ensure(self) -> WorkerClient:
        started = time.monotonic()
        stale = False
        if self.exists():
            sandbox = Sandbox(self.sbx, self.name)
            # Resource changes are not worker failures: never route this refusal
            # through the destructive retry path and lose conversation history.
            require_allocation(
                self.config.paths, sandbox, self.config.sandbox_resources_for("concierge")
            )
            client = self._make_client(sandbox)
            if not self.install_workers or self._is_reusable(client):
                self._sandbox = sandbox
                self.bus.emit("sandbox.reused", CONCIERGE_RUN_ID, name=self.name, role="agent")
                log.info(
                    "concierge_sandbox.reused",
                    sandbox=self.name,
                    backend=self.config.agent.backend,
                    duration_s=round(time.monotonic() - started, 1),
                )
                return client
            log.warning(
                "concierge_sandbox.stale",
                sandbox=self.name,
                backend=self.config.agent.backend,
                action="worker or agent backend does not match this host; re-provisioning",
            )
            stale = True

        clients: list[WorkerClient] = []

        def install(sandbox: Sandbox, _role: str) -> None:
            # Inside ensure_agent_only's try: a failed install rolls back the
            # sandbox and its registered secret.
            client = self._make_client(sandbox)
            if self.install_workers:
                client.install(
                    extras=self.config.agent.backend,
                    expect_prebaked=bool(self.config.sandbox.template),
                )
            clients.append(client)

        try:
            if stale:
                # Strict, and before provision_start: the name is about to be
                # re-created, and `sbx rm` returning is not the teardown
                # finishing. Creating into that window costs the new sandbox
                # (reaped mid-install) plus a whole retry cycle of chat
                # silence (#952), so an unconfirmed teardown stops here.
                # No client uses the stale box yet: no new generation.
                self._discard_sandbox(strict=True)
            log.info(
                "concierge_sandbox.provision_start",
                sandbox=self.name,
                workspace=str(self.workspace),
                install_workers=self.install_workers,
            )
            sandbox = self.provisioner.ensure_agent_only(
                self.name, self.workspace, post_create=install, run_id=CONCIERGE_RUN_ID
            )
        except SbxloopError as exc:
            if caused_by_sbx_auth(exc):
                hint = (
                    "the sandbox backend refused every call because nobody is signed in "
                    "to Docker on the host (a session that expired, or a host that never "
                    "ran `sbx login`), so chat intake is off until someone signs in; "
                    "`sbx login` as the daemon's user through the home's sbx wrapper, then "
                    "restart the daemon — `sbxloop doctor` checks it"
                )
            else:
                hint = (
                    "the long-lived sandbox the concierge answers chat from could not "
                    "be created, so chat intake is off until it can be; the sandbox "
                    "backend, its image and the host's disk are what to check — "
                    "`sbxloop doctor`"
                )
            log.error(
                "concierge_sandbox.provision_failed",
                sandbox=self.name,
                duration_s=round(time.monotonic() - started, 1),
                error=str(exc),
                hint=hint,
                exc_info=True,
            )
            raise DaemonError(f"cannot provision the concierge sandbox: {exc}") from exc
        self._sandbox = sandbox
        log.info(
            "concierge_sandbox.ready",
            sandbox=self.name,
            duration_s=round(time.monotonic() - started, 1),
        )
        return clients[0]
