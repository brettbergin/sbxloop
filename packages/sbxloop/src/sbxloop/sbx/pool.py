"""Warm sandbox pool: pre-provisioned standby pairs reused across runs.

Provisioning dominates run startup (two ``sbx create`` calls, policy, secret
registration, and the worker install ladder). ``sbxloop warmup`` pays that
cost ahead of time: it provisions standby pairs, installs the workers, and
records them in the state DB. ``sbxloop run`` then claims a warm pair when
one matches; otherwise cold provisioning proceeds unchanged.

Reuse is keyed by a *provision fingerprint* — a digest over every input that
shapes a pair (sbxloop/worker version, template, app-name, network allows,
secret strategy, github enablement, explicit workspace). Any mismatch means
the pair is simply not a candidate; stale pairs age out via a TTL and are
discarded, never reused across fingerprint changes.

Claiming re-verifies the pair (liveness + worker version probe), re-applies
secrets from the current host environment (registrations may be stale after
token rotation or a sandbox stop/start cycle), and resets run-scoped state
(jobs/results/events dirs, the agent work dir, and the pool-owned host
workspace) so consecutive runs cannot leak artifacts into each other. A pair
that fails any of these checks is discarded and the claim falls through to
the next candidate — reuse is an optimization, never a correctness risk.

Naming: pooled sandboxes are ``sbxloop-pool-<pool_id>-<role>`` (via the
normal ``sandbox_name`` with a ``pool-``-prefixed scope), so ``sbx ls``
parsing, ``sbxloop sandbox``, and future orphan-GC can classify them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
import shutil
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

import sbxloop
from sbxloop.config import Config
from sbxloop.errors import SbxError
from sbxloop.events import EventBus, HostEventTypes
from sbxloop.ids import new_pool_id
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxRole
from sbxloop.sbx.pair import SandboxPair, cleanup_registry
from sbxloop.sbx.provision import Provisioner
from sbxloop.sbx.sandbox import EVENTS_DIR, JOBS_DIR, RESULTS_DIR, Sandbox
from sbxloop.worker.client import WorkerClient

if TYPE_CHECKING:
    from sbxloop.engine.store import StateStore

logger = logging.getLogger(__name__)

# Pool pairs occupy the run-id slot of the sandbox naming scheme with a
# "pool-" scope, yielding names like sbxloop-pool-p7k2m9qp3-agent.
POOL_SCOPE_PREFIX = "pool-"


def pool_scope(pool_id: str) -> str:
    return f"{POOL_SCOPE_PREFIX}{pool_id}"


class PoolPairRecord(BaseModel):
    """One standby pair as persisted in the state DB."""

    model_config = ConfigDict(extra="forbid")

    pool_id: str
    fingerprint: str
    agent_name: str
    github_name: str | None = None
    workspace: Path
    agent_workdir: str
    mounted: bool
    worker_python: str
    created_at: float
    expires_at: float


def provision_fingerprint(config: Config) -> str:
    """Digest of every input that shapes a provisioned pair.

    A warm pair is reusable only when the claiming run's fingerprint equals
    the one it was provisioned under. The worker wheel version moves in
    lockstep with the sbxloop version, so ``version`` covers both; the
    built-in allow-domain constants are code, also covered by ``version``.
    """
    payload = {
        "version": sbxloop.__version__,
        "app_name": config.app_name,
        "template": config.sandbox.template or "",
        "extra_allow_domains": sorted(config.sandbox.extra_allow_domains),
        "secret_strategy": config.secret_strategy,
        "github": config.github.enabled,
        "install_workers": config.install_workers,
        "workspace": str(config.sandbox.workspace) if config.sandbox.workspace else "",
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


class _ReviveError(Exception):
    """A warm pair failed a claim-time check; discard it and move on."""


class WarmPool:
    def __init__(
        self,
        cli: SbxCLI,
        config: Config,
        store: StateStore,
        bus: EventBus | None = None,
        *,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cli = cli
        self.config = config
        self.store = store
        self.bus = bus or EventBus()
        self.clock = clock
        self.provisioner = Provisioner(cli, config, self.bus, env=env)

    # -- warmup --------------------------------------------------------------

    def warmup(self, count: int = 1) -> list[PoolPairRecord]:
        """Provision ``count`` standby pairs and record them in the state DB."""
        self.prune()
        fingerprint = provision_fingerprint(self.config)
        return [self._provision_one(fingerprint) for _ in range(count)]

    def _provision_one(self, fingerprint: str) -> PoolPairRecord:
        pool_id = new_pool_id()
        scope = pool_scope(pool_id)
        workspace = self.config.sandbox.workspace or self._pool_workspace(pool_id)
        pair = self.provisioner.ensure_pair(scope, workspace)
        # Warm pairs must outlive this process, but a failure (or Ctrl-C)
        # before the DB row commits must not leak microVMs.
        cleanup_registry.register(pair)
        try:
            worker_python = self._install_workers(pair)
            assert pair.workspace is not None
            now = self.clock()
            record = PoolPairRecord(
                pool_id=pool_id,
                fingerprint=fingerprint,
                agent_name=pair.agent.name,
                github_name=pair.github.name if pair.github is not None else None,
                workspace=pair.workspace,
                agent_workdir=pair.agent_workdir,
                mounted=pair.mounted,
                worker_python=worker_python,
                created_at=now,
                expires_at=now + self.config.pool.ttl_s,
            )
            self.store.add_pool_pair(record)
        except BaseException:
            pair.cleanup()
            raise
        cleanup_registry.unregister(pair)
        self.bus.emit(
            HostEventTypes.SANDBOX_POOL_READY,
            scope,
            pool_id=pool_id,
            fingerprint=fingerprint,
            agent=record.agent_name,
            github=record.github_name,
            expires_at=record.expires_at,
        )
        return record

    def _pool_workspace(self, pool_id: str) -> Path:
        return self.config.state_dir / "pool" / pool_id / "workspace"

    def _install_workers(self, pair: SandboxPair) -> str:
        """Run the worker install ladder now, so claims skip it entirely."""
        if not self.config.install_workers:
            return self.config.worker_python
        agent = WorkerClient(
            pair.agent,
            self.bus,
            transport=self.config.worker_transport,
            python=self.config.worker_python,
        )
        agent.install(extras="copilot", ensure_dev_tools=True)
        if pair.github is not None:
            WorkerClient(
                pair.github,
                self.bus,
                transport=self.config.worker_transport,
                python=self.config.worker_python,
            ).install(extras="")
        # install() rewrites .python when the venv ladder fell back to the
        # system interpreter; the claim-time WorkerClient must reuse it.
        return agent.python

    # -- claim ---------------------------------------------------------------

    def claim(self, run_id: str) -> SandboxPair | None:
        """Claim a matching warm pair for ``run_id``, or None to go cold.

        Consumes candidates oldest-first; each is revived (probed, secrets
        refreshed, run-scoped state reset) and any failure discards it and
        tries the next. Returns None when no candidate survives.
        """
        self.prune()
        fingerprint = provision_fingerprint(self.config)
        now = self.clock()
        candidates = [
            r
            for r in self.store.list_pool_pairs()
            if r.fingerprint == fingerprint and r.expires_at > now
        ]
        if not candidates:
            return None
        # Fail fast on missing host tokens BEFORE consuming a standby: the
        # claim must fail exactly as loudly as cold provisioning would, but
        # without burning a pooled pair on the way out.
        tokens: dict[SandboxRole, str] = {"agent": self.provisioner.copilot_token()}
        if self.config.github.enabled:
            tokens["github"] = self.provisioner.gh_token()
        while True:
            record = self.store.take_pool_pair(fingerprint, now=self.clock())
            if record is None:
                return None
            try:
                pair = self._revive(record, run_id, tokens)
            except _ReviveError as exc:
                logger.warning("discarding warm pair %s: %s", record.pool_id, exc)
                self._discard(record, reason=str(exc))
                continue
            self.bus.emit(
                HostEventTypes.SANDBOX_POOL_CLAIM,
                run_id,
                pool_id=record.pool_id,
                agent=record.agent_name,
                github=record.github_name,
            )
            return pair

    def _revive(
        self, record: PoolPairRecord, run_id: str, tokens: Mapping[SandboxRole, str]
    ) -> SandboxPair:
        agent = Sandbox(self.cli, record.agent_name)
        github = Sandbox(self.cli, record.github_name) if record.github_name else None
        try:
            for sandbox in (agent, github):
                if sandbox is not None:
                    self._probe(sandbox, record.worker_python)
            self._refresh_secrets(record, run_id, tokens, agent, github)
            self._reset(record, agent, github)
        except SbxError as exc:
            raise _ReviveError(f"sbx failure during revive: {exc}") from exc
        return SandboxPair(
            run_id,
            agent=agent,
            github=github,
            keep=self.config.keep_sandboxes,
            workspace=record.workspace,
            agent_workdir=record.agent_workdir,
            mounted=record.mounted,
            preinstalled=True,
            worker_python=record.worker_python,
        )

    def _probe(self, sandbox: Sandbox, worker_python: str) -> None:
        """Liveness + lockstep check; raises _ReviveError on any mismatch."""
        if not self.config.install_workers:
            if not sandbox.exec(["true"]).ok:
                raise _ReviveError(f"{sandbox.name} is not executable")
            return
        result = sandbox.exec(
            [worker_python, "-c", "import sbxloop_worker; print(sbxloop_worker.__version__)"]
        )
        installed = result.stdout.strip()
        if not result.ok or installed != sbxloop.__version__:
            raise _ReviveError(
                f"{sandbox.name} worker probe failed "
                f"(rc={result.returncode}, version={installed!r})"
            )

    def _refresh_secrets(
        self,
        record: PoolPairRecord,
        run_id: str,
        tokens: Mapping[SandboxRole, str],
        agent: Sandbox,
        github: Sandbox | None,
    ) -> None:
        """Re-apply secrets from the current host env, then re-verify.

        Whether registrations survive sandbox stop/start cycles is not a
        documented sbx guarantee, and the host tokens may have rotated since
        warmup — re-applying (via the existing replace-on-exists flow) plus
        the cheap visibility probe makes every claim self-verifying.
        """
        agent_spec, github_spec = self.provisioner.build_specs(
            pool_scope(record.pool_id), record.workspace
        )
        pairs: list[tuple[Sandbox | None, SandboxRole]] = [(agent, "agent"), (github, "github")]
        for sandbox, role in pairs:
            if sandbox is None:
                continue
            spec = agent_spec if role == "agent" else github_spec
            self.provisioner.refresh_secrets(run_id, spec, sandbox, tokens[role])

    def _reset(self, record: PoolPairRecord, agent: Sandbox, github: Sandbox | None) -> None:
        """Hygiene between runs: no artifact or job state may carry over."""
        dirs = " ".join(shlex.quote(d) for d in (JOBS_DIR, RESULTS_DIR, EVENTS_DIR))
        reset_cmd = f'for d in {dirs}; do rm -rf "$d" && mkdir -p "$d"; done'
        for sandbox in (agent, github):
            if sandbox is None:
                continue
            if not sandbox.exec(["sh", "-c", reset_cmd]).ok:
                raise _ReviveError(f"failed to reset run dirs in {sandbox.name}")
        if not record.mounted:
            workdir = shlex.quote(record.agent_workdir)
            if not agent.exec(["sh", "-c", f"rm -rf {workdir} && mkdir -p {workdir}"]).ok:
                raise _ReviveError(f"failed to reset work dir in {agent.name}")
        # The pool-owned host workspace is cleared host-side (for mounted
        # pairs this also empties the in-VM work dir through the mount). An
        # explicit [sandbox].workspace is the user's own directory — cold
        # runs share it as-is, so reuse must not wipe it either.
        if self.config.sandbox.workspace is None:
            try:
                record.workspace.mkdir(parents=True, exist_ok=True)
                for child in record.workspace.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            except OSError as exc:
                raise _ReviveError(f"failed to reset pool workspace: {exc}") from exc

    # -- discard / prune -----------------------------------------------------

    def _discard(self, record: PoolPairRecord, *, reason: str) -> None:
        self._remove_sandboxes(record)
        self.bus.emit(
            HostEventTypes.SANDBOX_POOL_DISCARD,
            pool_scope(record.pool_id),
            pool_id=record.pool_id,
            reason=reason,
        )

    def _remove_sandboxes(self, record: PoolPairRecord) -> None:
        for name in (record.agent_name, record.github_name):
            if not name:
                continue
            try:
                self.cli.rm(name)
            except SbxError:
                logger.warning("failed to remove pooled sandbox %s", name, exc_info=True)

    def prune(self) -> list[PoolPairRecord]:
        """Discard expired pairs; returns what was pruned."""
        pruned: list[PoolPairRecord] = []
        for record in self.store.expired_pool_pairs(now=self.clock()):
            # Only the process that wins the row delete tears the pair down.
            if self.store.remove_pool_pair(record.pool_id):
                self._remove_sandboxes(record)
                pruned.append(record)
        return pruned
