"""DaemonGithub: the daemon's long-lived github-ops sandbox.

The host never talks to GitHub with the PAT — that is the credential split
the whole project is built on — so the outer loop's polling and issue
lifecycle run through a github-role microVM the daemon owns for its whole
lifetime, provisioned lazily and re-provisioned on failure (at most once
per :data:`REPROVISION_MIN_INTERVAL_S`). Every :class:`GithubOps` the
sources use is obtained through :meth:`ops`, so a replaced sandbox is
picked up transparently.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from sbxloop.config import Config
from sbxloop.errors import DaemonError, GithubOpsError, SbxError, SbxloopError, WorkerError
from sbxloop.events import EventBus
from sbxloop.gh.ops import GithubOps
from sbxloop.log import get_logger
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner
from sbxloop.sbx.sandbox import Sandbox
from sbxloop.worker.client import WorkerClient

log = get_logger(__name__)

T = TypeVar("T")

SANDBOX_NAME_PREFIX = "sbxloop-daemon-github"
# Ops issued from the daemon (not from a run) carry this run id in events.
DAEMON_RUN_ID = "daemon"
# A dead github sandbox costs one re-provision; a GitHub outage must not
# cost one per failing call (each is a full microVM boot + worker install),
# so between re-provisions failures propagate to the caller, whose own
# backoff (source poll, report best-effort) absorbs them.
REPROVISION_MIN_INTERVAL_S = 300.0


def sandbox_name_for(state_dir: Path) -> str:
    """Per-instance sandbox name. The name used to be fixed, and
    ``remove_stale`` deletes a same-named sandbox at startup: a second
    daemon on the same host (another project, another state dir) killed
    the first's github sandbox (#254). Two daemons sharing one state dir
    would also share a run store, which nothing supports, so the state dir
    is the instance identity."""
    digest = hashlib.sha256(str(state_dir.resolve()).encode()).hexdigest()[:8]
    return f"{SANDBOX_NAME_PREFIX}-{digest}"


class DaemonGithub:
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
        self._ops: GithubOps | None = None

    @property
    def workspace(self) -> Path:
        return (self.config.state_dir / "daemon" / "github-workspace").resolve()

    def remove_stale(self) -> None:
        """Drop a same-named sandbox left by a previous daemon process."""
        try:
            Sandbox(self.sbx, self.name).rm()
            log.info("removed stale daemon sandbox %s", self.name)
        except SbxError:
            pass

    def ops(self) -> GithubOps:
        if self._ops is None:
            self._ops = self._provision()
        return self._ops

    def call(self, fn: Callable[[GithubOps], T]) -> T:
        """Run ``fn(ops)``; on failure drop the sandbox (rate-limited, see
        :meth:`note_failure`) and retry once, so a dead microVM costs one
        hiccup, not the daemon."""
        try:
            return fn(self.ops())
        except (GithubOpsError, WorkerError, SbxError) as exc:
            if not self.note_failure(exc):
                raise
            return fn(self.ops())

    def note_failure(self, exc: BaseException) -> bool:
        """A caller's op failed: drop the sandbox so the next :meth:`ops`
        re-provisions — at most once per :data:`REPROVISION_MIN_INTERVAL_S`.
        Sources call this from their own error handling (they cannot use
        :meth:`call` — a claim is not idempotent, so it must not be replayed
        wholesale). Returns whether the sandbox was dropped."""
        now = self.clock()
        last = self._last_reprovision_at
        if last is not None and now - last < REPROVISION_MIN_INTERVAL_S:
            log.warning(
                "daemon github op failed (%s); sandbox re-provisioned %.0fs ago, not again yet",
                exc,
                now - last,
            )
            return False
        log.warning("daemon github op failed (%s); dropping the sandbox to re-provision", exc)
        self._last_reprovision_at = now
        self.close()
        return True

    def health_check(self) -> bool:
        try:
            self.ops().raw("GET", "/rate_limit")
            return True
        except (GithubOpsError, WorkerError, SbxError):
            return False

    def close(self) -> None:
        sandbox, self._sandbox, self._client, self._ops = self._sandbox, None, None, None
        if sandbox is not None:
            try:
                sandbox.rm()
            except SbxError:
                log.warning("failed to remove daemon sandbox %s", self.name, exc_info=True)

    def _provision(self) -> GithubOps:
        clients: list[WorkerClient] = []

        def install(sandbox: Sandbox, _role: str) -> None:
            # Runs inside ensure_github_only's try: a failed worker install
            # rolls back the sandbox AND its registered secrets, the same
            # way a failed pair provision does.
            client = WorkerClient(
                sandbox,
                self.bus,
                transport=self.config.worker_transport,
                python=self.worker_python,
                role="github",
                limits=self.config.limits,
            )
            if self.install_workers:
                client.install(extras="")
            clients.append(client)

        try:
            sandbox = self.provisioner.ensure_github_only(
                self.name, self.workspace, post_create=install
            )
        except SbxloopError as exc:
            # ProvisionError, WorkerError, SbxError alike: one daemon-level
            # error, and nothing left behind.
            raise DaemonError(f"cannot provision the daemon github sandbox: {exc}") from exc
        self._sandbox, self._client = sandbox, clients[0]
        return GithubOps(clients[0], DAEMON_RUN_ID)
