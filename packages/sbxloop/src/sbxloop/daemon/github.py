"""DaemonGithub: the daemon's long-lived github-ops sandbox.

The host never talks to GitHub with the PAT — that is the credential split
the whole project is built on — so the outer loop's polling and issue
lifecycle run through a github-role microVM the daemon owns for its whole
lifetime, provisioned lazily and re-provisioned once on failure. Every
:class:`GithubOps` the sources use is obtained through :meth:`ops`, so a
replaced sandbox is picked up transparently.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from sbxloop.config import Config
from sbxloop.errors import DaemonError, GithubOpsError, SbxError, WorkerError
from sbxloop.events import EventBus
from sbxloop.gh.ops import GithubOps
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner
from sbxloop.sbx.sandbox import Sandbox
from sbxloop.worker.client import WorkerClient

logger = logging.getLogger(__name__)

T = TypeVar("T")

SANDBOX_NAME = "sbxloop-daemon-github"
# Ops issued from the daemon (not from a run) carry this run id in events.
DAEMON_RUN_ID = "daemon"


class DaemonGithub:
    def __init__(
        self,
        config: Config,
        sbx: SbxCLI,
        bus: EventBus,
        *,
        worker_python: str,
        install_workers: bool = True,
        name: str = SANDBOX_NAME,
    ) -> None:
        self.config = config
        self.sbx = sbx
        self.bus = bus
        self.worker_python = worker_python
        self.install_workers = install_workers
        self.name = name
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
            logger.info("removed stale daemon sandbox %s", self.name)
        except SbxError:
            pass

    def ops(self) -> GithubOps:
        if self._ops is None:
            self._ops = self._provision()
        return self._ops

    def call(self, fn: Callable[[GithubOps], T]) -> T:
        """Run ``fn(ops)``; on a sandbox-level failure re-provision once and
        retry, so a dead microVM costs one hiccup, not the daemon."""
        try:
            return fn(self.ops())
        except (GithubOpsError, WorkerError, SbxError) as exc:
            logger.warning("daemon github op failed (%s); re-provisioning once", exc)
            self.close()
            return fn(self.ops())

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
                logger.warning("failed to remove daemon sandbox %s", self.name, exc_info=True)

    def _provision(self) -> GithubOps:
        try:
            sandbox = self.provisioner.ensure_github_only(self.name, self.workspace)
        except SbxError as exc:
            raise DaemonError(f"cannot provision the daemon github sandbox: {exc}") from exc
        client = WorkerClient(
            sandbox,
            self.bus,
            transport=self.config.worker_transport,
            python=self.worker_python,
            role="github",
            limits=self.config.limits,
        )
        if self.install_workers:
            try:
                client.install(extras="")
            except (WorkerError, SbxError) as exc:
                with contextlib.suppress(SbxError):
                    sandbox.rm()
                raise DaemonError(f"daemon github worker install failed: {exc}") from exc
        self._sandbox, self._client = sandbox, client
        return GithubOps(client, DAEMON_RUN_ID)
