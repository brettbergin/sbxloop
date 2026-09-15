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
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from sbxloop.config import VCS_KINDS, Config, VcsKind
from sbxloop.errors import (
    DaemonError,
    GithubOpsError,
    SbxError,
    SbxloopError,
    SbxNotFoundError,
    WorkerError,
)
from sbxloop.events import EventBus
from sbxloop.log import get_logger
from sbxloop.paths import SbxloopHome
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner
from sbxloop.sbx.sandbox import Sandbox
from sbxloop.vcs.backends import backend_for
from sbxloop.vcs.protocol import VcsOps
from sbxloop.worker.client import WorkerClient

log = get_logger(__name__)

T = TypeVar("T")

DAEMON_SANDBOX_PREFIX = "sbxloop-daemon"
# Backwards-compatible export for callers that identify the default GitHub
# box. Non-GitHub boxes use ``DAEMON_SANDBOX_PREFIX`` plus their forge kind.
SANDBOX_NAME_PREFIX = f"{DAEMON_SANDBOX_PREFIX}-github"
# Ops issued from the daemon (not from a run) carry this run id in events.
DAEMON_RUN_ID = "daemon"
# A dead github sandbox costs one re-provision; a GitHub outage must not
# cost one per failing call (each is a full microVM boot + worker install),
# so between re-provisions failures propagate to the caller, whose own
# backoff (source poll, report best-effort) absorbs them.
REPROVISION_MIN_INTERVAL_S = 300.0


def sandbox_name_for(home: SbxloopHome, kind: VcsKind = "github") -> str:
    """Per-instance sandbox name. The name used to be fixed, and
    ``remove_stale`` deletes a same-named sandbox before provisioning: a second
    daemon on the same host (another home) killed
    the first's github sandbox (#254). Two daemons sharing one home
    would also share a run store, which nothing supports, so the home is
    the instance identity."""
    digest = hashlib.sha256(str(home.root.resolve()).encode()).hexdigest()[:8]
    return f"{DAEMON_SANDBOX_PREFIX}-{kind}-{digest}"


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
        repo: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        # The repository this box's credentials are scoped to; None keeps the
        # daemon-wide token (and is the only sane default when several
        # repositories are polled from one box).
        self.repo = repo
        self.sbx = sbx
        self.bus = bus
        self.worker_python = worker_python
        self.install_workers = install_workers
        kind = config.vcs_kind_for(repo)
        self.name = name or sandbox_name_for(config.paths, kind)
        # The same home's box under every other forge: what a `[vcs] kind`
        # switch (in any direction) or a pre-forge-naming upgrade leaves
        # behind. A caller-named box (doctor, init-repo) owns no such names.
        self._previous_forge_names: tuple[str, ...] = (
            ()
            if name is not None
            else tuple(
                sandbox_name_for(config.paths, other) for other in VCS_KINDS if other != kind
            )
        )
        self._previous_forges_cleared = False
        self.clock = clock
        self._last_reprovision_at: float | None = None
        self.provisioner = Provisioner(sbx, config, bus=bus)
        self._sandbox: Sandbox | None = None
        self._client: WorkerClient | None = None
        self._ops: VcsOps | None = None
        self._lifecycle_lock = threading.RLock()

    @property
    def workspace(self) -> Path:
        return self.config.paths.github_workspace.resolve()

    def remove_stale(self) -> None:
        """Remove only this instance's stale box; uncertainty must propagate.

        Inventory, rather than a generic 'not found' exception, proves
        absence: Docker authentication errors can say 'secret not found'.
        Run this before every provision so a failed cleanup is retried when
        authentication or the sandbox service recovers.

        Only this box's own name: create would collide with it. A previous
        forge's box has a different name and is cleared after the new box is
        ready (:meth:`_clear_previous_forges`), so a wedged old box can never
        keep the configured forge from being polled.
        """
        if not self._listed():
            log.debug("github_sandbox.no_stale", sandbox=self.name)
            return
        try:
            Sandbox(self.sbx, self.name).rm()
        except SbxNotFoundError:
            # Listed a moment ago, "not found" now: a teardown already in
            # flight (seen while sandboxd recovered). Absent is what this
            # wanted, but only the inventory proves it (see close).
            if not self._absent():
                raise
            log.info("github_sandbox.stale_already_gone", sandbox=self.name)
            return
        log.info("github_sandbox.stale_removed", sandbox=self.name)

    def _clear_previous_forges(self) -> None:
        """Best effort, once per instance: remove this home's box under any
        other forge.

        Never raises. The field failure it replaces: after a switch to
        GitLab, removing the old GitHub box timed out on every attempt, and
        because that removal ran before the GitLab box was created, polling
        stayed down for hours over a box nothing needed any more. A failure
        here is logged with the command that clears it, and the next daemon
        start tries again.
        """
        if self._previous_forges_cleared:
            return
        self._previous_forges_cleared = True
        try:
            listed = {info.name for info in self.sbx.ls()}
        except SbxError as exc:
            log.warning(
                "github_sandbox.previous_forge_check_failed",
                sandbox=self.name,
                error=str(exc),
            )
            return
        for name in self._previous_forge_names:
            if name not in listed:
                continue
            try:
                # settle=False: nothing re-creates this name while the
                # configured forge is a different one.
                self.sbx.rm(name, force=True, settle=False)
            except SbxNotFoundError:
                log.info("github_sandbox.previous_forge_already_gone", sandbox=name)
            except SbxError as exc:
                log.warning(
                    "github_sandbox.previous_forge_remove_failed",
                    sandbox=name,
                    error=str(exc),
                    hint=f"a box this daemon used under another [vcs] kind is still "
                    f"present; `sbxloop sandbox rm {name}` removes it, and the next "
                    "daemon start retries",
                )
            else:
                log.info("github_sandbox.previous_forge_removed", sandbox=name, kind=self.kind)

    def _listed(self) -> bool:
        """Whether ``sbx ls`` lists this instance's box right now."""
        return any(info.name == self.name for info in self.sbx.ls())

    def ops(self) -> VcsOps:
        # Polling and control requests can arrive together. Cleanup belongs
        # to one provision, never to a competing request's new sandbox.
        with self._lifecycle_lock:
            if self._ops is None:
                # Lazy: the first GitHub call of the process (or the first after
                # a drop) pays for a microVM boot + worker install here — say
                # so, or that call looks like a hang.
                log.info("github_sandbox.provision_needed", sandbox=self.name)
                self._ops = self._provision()
            return self._ops

    def call(self, fn: Callable[[VcsOps], T]) -> T:
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
                "github_sandbox.op_failed",
                sandbox=self.name,
                error=str(exc),
                reprovisioned_ago_s=round(now - last),
                min_interval_s=REPROVISION_MIN_INTERVAL_S,
                action="keeping the sandbox; re-provisioned too recently",
            )
            return False
        log.warning(
            "github_sandbox.op_failed",
            sandbox=self.name,
            error=str(exc),
            action="dropping the sandbox; the next call re-provisions",
        )
        self._last_reprovision_at = now
        self.close()
        return True

    def health_check(self) -> bool:
        try:
            self.ops().rate_limit()
            return True
        except (GithubOpsError, WorkerError, SbxError) as exc:
            log.warning("github_sandbox.unhealthy", sandbox=self.name, error=str(exc))
            return False

    def close(self) -> None:
        with self._lifecycle_lock:
            sandbox, self._sandbox, self._client, self._ops = self._sandbox, None, None, None
            if sandbox is None:
                return
            try:
                sandbox.rm()
            except SbxNotFoundError:
                # The box this daemon was tearing down is gone already: the
                # state the teardown wanted, not a fault to report. But the
                # answer alone proves nothing (a Docker authentication
                # failure also says "not found", see remove_stale), so only
                # an inventory that no longer lists the name settles it;
                # anything else stays the failure it looks like.
                if self._absent():
                    log.info("github_sandbox.already_gone", sandbox=self.name)
                    return
                log.warning("github_sandbox.remove_failed", sandbox=self.name, exc_info=True)
            except SbxError:
                log.warning("github_sandbox.remove_failed", sandbox=self.name, exc_info=True)
            else:
                log.info("github_sandbox.removed", sandbox=self.name)

    def _absent(self) -> bool:
        """Whether the inventory confirms this instance's box is gone.

        Fails closed: an inventory that cannot be read confirms nothing.
        """
        try:
            return not self._listed()
        except SbxError:
            return False

    def _provision(self) -> VcsOps:
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
                # App auth: this box lives for the daemon's whole lifetime,
                # far past one installation token; None under a PAT — and
                # under stdin delivery, where job_env re-mints per job.
                credential_refresh=self.provisioner.gh_refresher(sandbox, self.repo),
                job_env=self.provisioner.job_env("github", self.repo, sandbox=sandbox),
            )
            if self.install_workers:
                client.install(extras="")
            clients.append(client)

        started = time.monotonic()
        log.info(
            "github_sandbox.provision_start",
            sandbox=self.name,
            workspace=str(self.workspace),
            install_workers=self.install_workers,
        )
        try:
            self.remove_stale()
            sandbox = self.provisioner.ensure_github_only(
                self.name, self.workspace, post_create=install, repo=self.repo
            )
        except SbxloopError as exc:
            # ProvisionError, WorkerError, SbxError alike: one daemon-level
            # error, and nothing left behind.
            log.error(
                "github_sandbox.provision_failed",
                sandbox=self.name,
                duration_s=round(time.monotonic() - started, 1),
                error=str(exc),
                hint="the long-lived sandbox the daemon makes its GitHub calls from "
                "could not be created, so polling and delivery cannot run; the sandbox "
                "backend, its image and the host's disk are what to check — `sbxloop doctor`",
            )
            raise DaemonError(f"cannot provision the daemon github sandbox: {exc}") from exc
        self._sandbox, self._client = sandbox, clients[0]
        log.info(
            "github_sandbox.ready",
            sandbox=self.name,
            duration_s=round(time.monotonic() - started, 1),
        )
        self._clear_previous_forges()
        return self.backend(clients[0])

    @property
    def kind(self) -> VcsKind:
        """The forge this box speaks to (#1017): the one its repository
        lives on, else ``[vcs] kind``."""
        return self.config.vcs_kind_for(self.repo)

    def backend(self, client: WorkerClient) -> VcsOps:
        """The backend for this box's forge over ``client``, with the
        transport descriptor the configuration derives."""
        kind = self.kind
        return backend_for(kind, client, DAEMON_RUN_ID, api_url=self.config.vcs_api_url_for(kind))
