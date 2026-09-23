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

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from sbxloop.config import VCS_KINDS, Config, VcsKind
from sbxloop.errors import (
    DaemonError,
    GithubOpsError,
    ProvisionError,
    SbxAuthError,
    SbxError,
    SbxloopError,
    SbxNotFoundError,
    SbxSettleTimeoutError,
    WorkerError,
    caused_by_sbx_auth,
)
from sbxloop.events import EventBus
from sbxloop.log import get_logger
from sbxloop.paths import SbxloopHome
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.naming import daemon_vcs_name, legacy_daemon_vcs_name
from sbxloop.sbx.provision import Provisioner
from sbxloop.sbx.sandbox import Sandbox
from sbxloop.vcs.backends import backend_for
from sbxloop.vcs.protocol import VcsOps
from sbxloop.worker.client import WorkerClient

log = get_logger(__name__)

T = TypeVar("T")

# Ops issued from the daemon (not from a run) carry this run id in events.
DAEMON_RUN_ID = "daemon"
# The kind is a config value; prose written for a person names the product.
_FORGE_NAMES: dict[str, str] = {"github": "GitHub", "gitlab": "GitLab", "gitea": "Gitea"}
# A dead github sandbox costs one re-provision; a GitHub outage must not
# cost one per failing call (each is a full microVM boot + worker install),
# so between re-provisions failures propagate to the caller, whose own
# backoff (source poll, report best-effort) absorbs them.
REPROVISION_MIN_INTERVAL_S = 300.0


def sandbox_name_for(home: SbxloopHome, kind: VcsKind = "github") -> str:
    """Per-instance forge-operations sandbox name."""
    return daemon_vcs_name(home, kind)


def generation_name(base: str, generation: int) -> str:
    """The box's name in its ``generation``-th incarnation: the base name,
    then ``<base>-g1``, ``<base>-g2``... A generation is taken when the
    backend will not give the previous one back (#1165): a hung microVM
    whose ``sbx rm`` never completes, or a name it refuses to re-create
    over state a crashed backend left behind. The daemon carries on under
    the next name instead of waiting on the old one every poll."""
    return base if generation == 0 else f"{base}-g{generation}"


def is_generation_of(name: str, base: str) -> bool:
    """Whether ``name`` is ``base`` or one of its generations."""
    if name == base:
        return True
    suffix = name.removeprefix(f"{base}-g")
    return suffix != name and suffix.isdigit()


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
        # The base name is the identity; ``name`` is the generation in use,
        # and moves on when the backend will not give a generation back.
        self._base_name = name or sandbox_name_for(config.paths, kind)
        self.name = self._base_name
        self._legacy_base_name = (
            legacy_daemon_vcs_name(config.paths, kind) if name is None else None
        )
        # Generations this process gave up on: a removal that failed, a
        # create the backend refused. Never retried in this process; the
        # next daemon start tries the base name again.
        self._unusable: set[str] = set()
        # Names whose removal this cleanup started but the backend had not
        # finished reaping when the settle wait ran out: slow, not wedged, so
        # skipped only until the next cleanup retries them.
        self._settling: set[str] = set()
        # The same home's box under every other forge: what a `[vcs] kind`
        # switch (in any direction) or a pre-forge-naming upgrade leaves
        # behind. A caller-named box (doctor, init-repo) owns no such names.
        self._previous_forge_names: tuple[str, ...] = (
            ()
            if name is not None
            else tuple(
                candidate
                for other in VCS_KINDS
                if other != kind
                for candidate in (
                    sandbox_name_for(config.paths, other),
                    legacy_daemon_vcs_name(config.paths, other),
                )
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
        """Remove this instance's stale boxes and settle on the name to create.

        Inventory, rather than a generic 'not found' exception, proves
        absence: Docker authentication errors can say 'secret not found'.
        Run this before every provision so a failed cleanup is retried when
        authentication or the sandbox service recovers.

        Only this instance's own names, base and generations: create would
        collide with the one it picks. A box the backend cannot remove (a
        hung microVM whose ``sbx rm`` times out) is reported once and left
        to the backend, and the daemon carries on under the next generation
        of the name (#1165): the field failure was a poll that waited out
        that timeout every half hour for a day. A removal sbx accepted but
        had not finished when the settle wait ran out is slow, not wedged: it
        is a warning, the name is not retired, and only this provision steps
        past it, so the next cleanup retries it. A previous forge's box has
        a different name and is cleared after the new box is ready
        (:meth:`_clear_previous_forges`).
        """
        listed = [
            info.name
            for info in self.sbx.ls()
            if is_generation_of(info.name, self._base_name)
            or (
                self._legacy_base_name is not None
                and is_generation_of(info.name, self._legacy_base_name)
            )
        ]
        if not listed:
            log.debug("github_sandbox.no_stale", sandbox=self._base_name)
        self._settling = set()
        for name in listed:
            if name in self._unusable:
                continue
            try:
                Sandbox(self.sbx, name).rm()
            except SbxNotFoundError:
                # Listed a moment ago, "not found" now: a teardown already in
                # flight (seen while sandboxd recovered). Absent is what this
                # wanted, but only the inventory proves it (see close).
                if not self._absent(name):
                    raise
                log.info("github_sandbox.stale_already_gone", sandbox=name)
                continue
            except SbxAuthError:
                # Nobody is signed in: every name fails alike, nothing is
                # wedged, and the next poll retries once someone is.
                raise
            except SbxSettleTimeoutError as exc:
                # Accepted and still being reaped: creating over it would
                # collide (#952), so this provision takes another name, but
                # the name stays this daemon's and the next cleanup retries.
                self._settling.add(name)
                log.warning(
                    "github_sandbox.stale_settling",
                    sandbox=name,
                    error=str(exc),
                    action="the backend is still removing this box; the next cleanup retries it",
                )
                continue
            except SbxError as exc:
                self._give_up_on(name, exc, reason="remove_failed")
                continue
            log.info("github_sandbox.stale_removed", sandbox=name)
        self.name = self._free_name()

    def _free_name(self) -> str:
        """The lowest generation this process has not given up on and whose
        removal is not still settling."""
        skip = self._unusable | self._settling
        generation = 0
        while generation_name(self._base_name, generation) in skip:
            generation += 1
        return generation_name(self._base_name, generation)

    def _give_up_on(self, name: str, exc: BaseException, *, reason: str) -> None:
        """Retire ``name`` for this process, reporting it the first time."""
        if name in self._unusable:
            return
        self._unusable.add(name)
        log.error(
            "github_sandbox.wedged",
            sandbox=name,
            reason=reason,
            kind=self.kind,
            error=str(exc),
            hint="the sandbox backend would not remove or re-create this box, so the "
            "daemon carries on under the next generation of its name and the box is "
            "left to the backend; restart sbx-sandboxd, then `sbxloop sandbox rm "
            "<name>` clears it — a backend that wedges repeatedly is the host, not an item",
        )

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
        for name in sorted(listed):
            if not any(is_generation_of(name, base) for base in self._previous_forge_names):
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

    def _listed(self, name: str | None = None) -> bool:
        """Whether ``sbx ls`` lists this instance's box (or ``name``) right now."""
        target = self.name if name is None else name
        return any(info.name == target for info in self.sbx.ls())

    @property
    def provisioned(self) -> bool:
        """Whether a sandbox is up right now.

        What a background reader asks before spending a call, so its work
        never pays for a microVM boot of its own: the poll's boot is the
        one the daemon exists for, and a reading that can wait rides it
        rather than racing it.
        """
        with self._lifecycle_lock:
            return self._ops is not None

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
            except SbxSettleTimeoutError as exc:
                # Slow, not wedged: the name is not retired, and the next
                # provision's cleanup retries the removal.
                log.warning(
                    "github_sandbox.remove_settling",
                    sandbox=sandbox.name,
                    error=str(exc),
                    action="the backend is still removing this box; the next provision retries it",
                )
            except SbxError as exc:
                log.warning("github_sandbox.remove_failed", sandbox=self.name, exc_info=True)
                # A box that will not go away is not worth another 120s
                # timeout at the next provision; that one starts a new
                # generation. A sign-in failure is the host, not the box.
                if not isinstance(exc, SbxAuthError):
                    self._give_up_on(sandbox.name, exc, reason="remove_failed")
            else:
                log.info("github_sandbox.removed", sandbox=self.name)

    def _absent(self, name: str | None = None) -> bool:
        """Whether the inventory confirms this instance's box (or ``name``)
        is gone.

        Fails closed: an inventory that cannot be read confirms nothing.
        """
        try:
            return not self._listed(name)
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
                # A baked template (`[sandbox] template`) carries the worker:
                # probe it instead of running the ladder on every provision.
                client.install(extras="", expect_prebaked=bool(self.config.sandbox.template))
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
            sandbox = self._ensure(install)
        except SbxloopError as exc:
            # ProvisionError, WorkerError, SbxError alike: one daemon-level
            # error, and nothing left behind. The prose names the configured
            # forge: under GitLab this box ends in daemon-vcs-gitlab, and
            # a report that said "GitHub" sent its reader to the wrong place.
            forge = _FORGE_NAMES.get(self.kind, self.kind)
            if caused_by_sbx_auth(exc):
                hint = (
                    "the sandbox backend refused every call because nobody is signed in "
                    "to Docker on the host (a session that expired, or a host that never "
                    "ran `sbx login`), so polling and delivery cannot run; `sbx login` as "
                    "the daemon's user through the home's sbx wrapper, then restart the "
                    "daemon — `sbxloop doctor` checks it"
                )
            else:
                hint = (
                    f"the long-lived sandbox the daemon makes its {forge} calls from "
                    "could not be created, so polling and delivery cannot run; the sandbox "
                    "backend, its image and the host's disk are what to check — "
                    "`sbxloop doctor`"
                )
            # With its traceback: the report groups by where it failed, and
            # the poll that logs the DaemonError wrapping this one is then
            # the same failure seen twice, not a second report (#1168).
            log.error(
                "github_sandbox.provision_failed",
                sandbox=self.name,
                duration_s=round(time.monotonic() - started, 1),
                error=str(exc),
                hint=hint,
                exc_info=True,
            )
            raise DaemonError(f"cannot provision the daemon {forge} sandbox: {exc}") from exc
        self._sandbox, self._client = sandbox, clients[0]
        log.info(
            "github_sandbox.ready",
            sandbox=self.name,
            duration_s=round(time.monotonic() - started, 1),
        )
        self._clear_previous_forges()
        return self.backend(clients[0])

    def _ensure(self, install: Callable[[Sandbox, str], None]) -> Sandbox:
        """Provision under the current name, once more under the next
        generation when the backend refused the create itself.

        The field failure: a crashed backend came back without the box but
        with its volume, and refused the name on every create for hours
        while the inventory showed nothing to remove. A create refused for
        any reason but a sign-in failure costs one more attempt under a
        fresh name; a failure later in provisioning (secrets, the worker
        install) is not the name's fault and is not retried here.
        """
        try:
            return self.provisioner.ensure_github_only(
                self.name, self.workspace, post_create=install, repo=self.repo
            )
        except ProvisionError as exc:
            cause = exc.__cause__
            refused_create = isinstance(cause, SbxError) and "create" in cause.argv[:4]
            if not refused_create or caused_by_sbx_auth(exc):
                raise
            refused = self.name
            self._give_up_on(refused, exc, reason="create_refused")
            self.name = self._free_name()
            log.warning(
                "github_sandbox.create_retried",
                sandbox=self.name,
                refused=refused,
                error=str(exc),
            )
            return self.provisioner.ensure_github_only(
                self.name, self.workspace, post_create=install, repo=self.repo
            )

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
