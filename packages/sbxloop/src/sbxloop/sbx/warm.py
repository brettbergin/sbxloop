"""Warm sandbox sets (#47): a run's sandboxes provisioned before the run exists.

Field (db, 2026-09-19): a run spent 57 to 94 seconds between dispatch and
its first model call, all of it booting microVMs and installing the worker
into them, and every run paid it again. A warm set is that work done ahead
of time: the daemon provisions a run's sandboxes under a run id nobody has
used yet, installs the workers, and parks them. When the next run is
dispatched it takes that run id, so provisioning finds its sandboxes already
in the inventory (the same reuse path a provider recovery takes) and skips
the create and the install ladder. Nothing about a run's names, paths or
cleanup changes: the run owns its set from the moment it claims it and
removes it at the end as it always has.

What can be warm is what does not depend on the run: the boxes, their
allocation, the baseline egress policy, the worker and the configured
toolchains. What cannot is applied at claim time by the ordinary provision
pass: the run's workspace (a clone cut into the directory the warm agent box
already mounts, or a workload's data directory), the repository's extra
allows, and the credentials, which travel per job and are never at rest.

A set is keyed by a fingerprint of everything that shaped it (sbxloop
version, template, backend, toolchains, resources, forge, secret strategy).
A set whose fingerprint no longer matches the running configuration, whose
sandboxes are gone, or that is older than ``[daemon] warm_ttl_s`` is removed
rather than handed to a run. The registry is a JSON file under the daemon's
state directory, so a set survives a restart and the daemon adopts it at
boot. ``sbxloop sandbox prune`` reads the same registry and leaves warm
sandboxes alone.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

import sbxloop
from sbxloop.config import Config
from sbxloop.errors import SbxError, SbxloopError
from sbxloop.events import EventBus
from sbxloop.ids import new_run_id
from sbxloop.log import get_logger
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxRole
from sbxloop.sbx.provision import Provisioner
from sbxloop.sbx.prune import remove_run_sandbox_secrets, remove_sandbox
from sbxloop.sbx.sandbox import VENV_PYTHON as DEFAULT_PYTHON, Sandbox
from sbxloop.worker.client import WorkerClient

log = get_logger(__name__)

WARM_REGISTRY = "warm-sets.json"
# How often the warmer looks at the pool when nothing is happening.
WARM_INTERVAL_S = 20.0
# A claimed set whose run never appeared (the dispatch failed before the
# run row existed) is swept after this long.
CLAIM_GRACE_S = 3600.0


class WarmSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    fingerprint: str
    created_at: float
    # The sandbox names the set holds, agent first.
    names: list[str] = Field(default_factory=list)
    state: Literal["ready", "claimed"] = "ready"
    claimed_at: float | None = None

    def role_of(self, name: str) -> SandboxRole:
        return "agent" if name.endswith("-agent") else "github"


class WarmRegistry:
    """The JSON file the sets live in; every write is atomic."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[WarmSet]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        sets: list[WarmSet] = []
        for entry in raw.get("sets", []) if isinstance(raw, dict) else []:
            try:
                sets.append(WarmSet.model_validate(entry))
            except ValueError:
                continue
        return sets

    def save(self, sets: list[WarmSet]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"sets": [s.model_dump(mode="json") for s in sets]}, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def run_ids(self) -> set[str]:
        return {s.run_id for s in self.load()}


def registry_for(config: Config) -> WarmRegistry:
    return WarmRegistry(config.paths.daemon / WARM_REGISTRY)


def warm_fingerprint(config: Config, *, sbx_version: str | None = None) -> str:
    """What shaped a warm set; a set from another shape is never handed out."""
    repo = config.primary_repo
    parts = {
        "sbxloop": sbxloop.__version__,
        "sbx": sbx_version,
        "template": config.sandbox.template,
        "backend": config.agent.backend,
        "languages": sorted(config.sandbox.effective_languages),
        "apt_packages": sorted(config.apt_packages_for(repo)),
        "agent": config.sandbox_resources_for("agent", repo).model_dump(mode="json"),
        "github": config.sandbox_resources_for("github").model_dump(mode="json"),
        "github_enabled": config.vcs.enabled,
        "forge": config.vcs_kind_for(repo),
        "secret_strategy": config.secret_strategy,
        "transport": config.worker_transport,
    }
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode())
    return digest.hexdigest()[:16]


class Warmer:
    """Keeps ``[daemon] warm_pairs`` sets ready and hands them to dispatch."""

    def __init__(
        self,
        config: Config,
        cli: SbxCLI,
        *,
        worker_python: str | None = None,
        install_workers: bool = True,
        env: Mapping[str, str] | None = None,
        registry: WarmRegistry | None = None,
        clock: Callable[[], float] = time.time,
        interval_s: float = WARM_INTERVAL_S,
    ) -> None:
        self.config = config
        self.cli = cli
        self.worker_python = worker_python
        self.install_workers = install_workers
        self.env = env
        self.registry = registry or registry_for(config)
        self.clock = clock
        self.interval_s = interval_s
        self.target = config.daemon.warm_pairs
        self.ttl_s = config.daemon.warm_ttl_s
        self._lock = threading.Lock()
        self._fingerprint: str | None = None

    # -- the pool ----------------------------------------------------------

    def fingerprint(self) -> str:
        if self._fingerprint is None:
            version: str | None = None
            with contextlib.suppress(SbxloopError):
                version = self.cli.version()
            self._fingerprint = warm_fingerprint(self.config, sbx_version=version)
        return self._fingerprint

    def sets(self) -> list[WarmSet]:
        with self._lock:
            return self.registry.load()

    def ready(self) -> list[WarmSet]:
        current = self.fingerprint()
        return [s for s in self.sets() if s.state == "ready" and s.fingerprint == current]

    def claim(self) -> str | None:
        """The oldest ready set's run id, now claimed; None when there is none."""
        current = self.fingerprint()
        with self._lock:
            sets = self.registry.load()
            candidates = [s for s in sets if s.state == "ready" and s.fingerprint == current]
            if not candidates:
                return None
            chosen = min(candidates, key=lambda s: s.created_at)
            chosen.state = "claimed"
            chosen.claimed_at = self.clock()
            self.registry.save(sets)
        log.info("warm.claimed", run=chosen.run_id, sandboxes=chosen.names)
        return chosen.run_id

    def is_claimed(self, run_id: str) -> bool:
        return any(s.run_id == run_id and s.state == "claimed" for s in self.sets())

    # -- filling -------------------------------------------------------------

    def fill_one(self) -> WarmSet | None:
        """Provision one set under a fresh run id and record it."""
        run_id = new_run_id()
        workspace = self.config.paths.run_workspace(run_id).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        provisioner = Provisioner(self.cli, self.config, bus=EventBus(), env=self.env)
        started = time.monotonic()
        log.info("warm.provision_start", run=run_id, target=self.target)
        try:
            languages = provisioner.resolve_languages(workspace, kind="code")
            pair = provisioner._provision_pair(
                run_id,
                workspace,
                self.config.primary_repo,
                languages=languages,
                expects_mount=False,
                kind="code",
            )
        except SbxloopError as exc:
            log.warning("warm.provision_failed", run=run_id, error=str(exc)[:500])
            return None
        github_name = pair.github.name if pair.github is not None else None
        try:
            if self.install_workers:
                self._install(pair.agent.name, github_name, provisioner, languages)
        except SbxloopError as exc:
            log.warning("warm.install_failed", run=run_id, error=str(exc)[:500])
            pair.cleanup()
            return None
        names = [pair.agent.name] + ([pair.github.name] if pair.github is not None else [])
        warm = WarmSet(
            run_id=run_id,
            fingerprint=self.fingerprint(),
            created_at=self.clock(),
            names=names,
        )
        with self._lock:
            sets = self.registry.load()
            sets.append(warm)
            self.registry.save(sets)
        log.info(
            "warm.ready",
            run=run_id,
            sandboxes=names,
            duration_s=round(time.monotonic() - started, 1),
        )
        return warm

    def _install(
        self,
        agent_name: str,
        github_name: str | None,
        provisioner: Provisioner,
        languages: object,
    ) -> None:
        """The engine's own install step (`_install_workers`), so a run that
        claims the set finds exactly what it would have installed."""
        repo = self.config.primary_repo
        prebaked = bool(self.config.sandbox.template)
        python = self.worker_python or DEFAULT_PYTHON
        agent = WorkerClient(
            Sandbox(self.cli, agent_name),
            EventBus(),
            transport=self.config.worker_transport,
            python=python,
            role="agent",
            backend=self.config.agent.backend,
            limits=self.config.limits,
            job_env=provisioner.job_env("agent", repo, sandbox=Sandbox(self.cli, agent_name)),
        )
        try:
            agent.install(
                extras=self.config.agent.backend,
                ensure_dev_tools=True,
                languages=languages.languages,  # type: ignore[attr-defined]
                versions=languages.versions,  # type: ignore[attr-defined]
                expect_prebaked=prebaked,
                apt_packages=self.config.apt_packages_for(repo),
            )
        finally:
            agent.close()
        if github_name is None:
            return
        github = WorkerClient(
            Sandbox(self.cli, github_name),
            EventBus(),
            transport=self.config.worker_transport,
            python=python,
            role="github",
            limits=self.config.limits,
            job_env=provisioner.job_env("github", repo, sandbox=Sandbox(self.cli, github_name)),
        )
        try:
            github.install(extras="", expect_prebaked=prebaked)
        finally:
            github.close()

    # -- upkeep --------------------------------------------------------------

    def reconcile(self) -> None:
        """At boot: adopt the sets whose sandboxes are all still there under
        the running configuration; remove the rest."""
        try:
            listed = {info.name for info in self.cli.ls()}
        except SbxError as exc:
            log.warning("warm.inventory_unreadable", error=str(exc)[:300])
            return
        current = self.fingerprint()
        for warm in self.sets():
            if not all(name in listed for name in warm.names):
                log.info("warm.dropped", run=warm.run_id, reason="sandboxes gone")
                self._remove(warm, listed)
            elif warm.state == "ready" and warm.fingerprint != current:
                log.info("warm.dropped", run=warm.run_id, reason="configuration changed")
                self._remove(warm, listed)

    def expire(self) -> None:
        now = self.clock()
        for warm in self.sets():
            if warm.state == "ready" and now - warm.created_at > self.ttl_s:
                log.info("warm.dropped", run=warm.run_id, reason="expired")
                self._remove(warm)

    def sweep_claimed(self, run_finished: Callable[[str], bool | None]) -> None:
        """Forget claimed sets whose run has finished (the run removed what
        it used; whatever it did not use goes here), or that never became a
        run at all."""
        now = self.clock()
        for warm in self.sets():
            if warm.state != "claimed":
                continue
            finished = run_finished(warm.run_id)
            stale = finished is None and now - (warm.claimed_at or warm.created_at) > CLAIM_GRACE_S
            if finished or stale:
                self._remove(warm)

    def _remove(self, warm: WarmSet, listed: set[str] | None = None) -> None:
        if listed is None:
            try:
                listed = {info.name for info in self.cli.ls()}
            except SbxError:
                listed = set(warm.names)
        for name in warm.names:
            if name not in listed:
                continue
            try:
                remove_sandbox(self.cli, name)
            except SbxError:
                log.warning("warm.remove_failed", run=warm.run_id, sandbox=name, exc_info=True)
            with contextlib.suppress(Exception):
                remove_run_sandbox_secrets(self.cli, name, warm.role_of(name), self.config)
        with self._lock:
            sets = [s for s in self.registry.load() if s.run_id != warm.run_id]
            self.registry.save(sets)

    def run_forever(
        self,
        stop: threading.Event,
        *,
        paused: Callable[[], bool] = lambda: False,
        run_finished: Callable[[str], bool | None] = lambda run_id: None,
    ) -> None:
        """The warmer thread: adopt what is there, then keep the pool full."""
        try:
            self.reconcile()
        except Exception:
            log.warning("warm.reconcile_failed", exc_info=True)
        while not stop.is_set():
            try:
                self.expire()
                self.sweep_claimed(run_finished)
                if not paused() and len(self.ready()) < self.target:
                    self.fill_one()
            except Exception:
                log.warning("warm.cycle_failed", exc_info=True)
            stop.wait(self.interval_s)
