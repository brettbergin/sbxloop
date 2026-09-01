"""Build a prebaked sandbox template carrying sbxloop's runtime prerequisites.

The worker install ladder (venv → apt self-heal → user-site pip) plus the
Copilot runtime download are deterministic for a given worker version, yet
run identically on every provision. ``sbxloop bake`` runs them ONCE in a
scratch sandbox and persists the result with ``sbx template save``; with
``[sandbox] template`` pointing at the saved ref, provisioning verifies the
baked worker with fast probes instead of reinstalling it, and falls back to
the ladder when the template is stale.

Two manifests record what was baked:

- **in-VM** (``~/.sbxloop/bake.json``, travels inside the template): read by
  provisioning to verify the baked worker version before trusting it.
- **host** (``<state_dir>/bake.json``): read by ``sbxloop doctor`` to flag a
  stale template (host upgraded since the bake) without booting a microVM.

Templates carry *software only* — the scratch sandbox gets no secrets, and
network policy is applied per-sandbox at provision time as always.
"""

from __future__ import annotations

import json
import secrets
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

import sbxloop
from sbxloop import toolchains
from sbxloop.config import Config
from sbxloop.errors import BakeError, SbxError, SbxloopError
from sbxloop.log import get_logger
from sbxloop.policy import PROMPT_ADVERTISED_DOMAINS, baseline_allows
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxSpec
from sbxloop.sbx.provision import AGENT_ALLOW_DOMAINS
from sbxloop.sbx.sandbox import BAKE_MANIFEST, SBXLOOP_DIR, Sandbox
from sbxloop.worker.client import WorkerClient

log = get_logger(__name__)

DEFAULT_TEMPLATE_REF = "sbxloop-baked:latest"

Progress = Callable[[str], None]


class BakeRecord(BaseModel):
    """What a bake produced; persisted host-side for doctor's drift check."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    worker_version: str
    python: str
    runtime_cached: bool
    baked_at: float
    # Whether git was on PATH when the template was saved (#252). Optional so
    # records written before the field existed still load; None reads as
    # "not recorded" in doctor rather than as a failure.
    git: bool | None = None


def bake_record_path(config: Config) -> Path:
    return config.state_dir / "bake.json"


def load_bake_record(config: Config) -> BakeRecord | None:
    """The last bake's host-side record, or None (missing/unreadable)."""
    path = bake_record_path(config)
    if not path.is_file():
        return None
    try:
        return BakeRecord.model_validate_json(path.read_text())
    except (OSError, ValueError):
        return None


def bake_template(
    cli: SbxCLI,
    config: Config,
    *,
    ref: str = DEFAULT_TEMPLATE_REF,
    base_template: str | None = None,
    cache_runtime: bool = True,
    keep: bool = False,
    name: str | None = None,
    progress: Progress | None = None,
) -> BakeRecord:
    """Run the full worker install once and persist it as a template.

    Builds from sbx's default base template unless ``base_template`` is
    given — deliberately NOT from ``[sandbox].template``, so re-baking
    always starts clean instead of layering onto a stale image. The scratch
    sandbox is removed afterwards (``keep`` retains it for debugging).
    """
    report = progress or (lambda _message: None)
    name = name or f"sbxloop-bake-{secrets.token_hex(4)}"
    runtime_cached = False
    git_present = False
    with tempfile.TemporaryDirectory(prefix="sbxloop-bake-") as scratch:
        spec = SandboxSpec(
            name=name,
            role="agent",
            workspace=Path(scratch),
            template=base_template,
            # Same allows a run's agent sandbox gets, so the wheel deps, the
            # dev-tools apt ensure, and the Copilot runtime download all
            # resolve during the bake.
            policy_allows=[
                *AGENT_ALLOW_DOMAINS,
                *baseline_allows(PROMPT_ADVERTISED_DOMAINS, config.policy.deny),
                *config.sandbox.extra_allow_domains,
            ],
        )
        report(f"creating scratch sandbox {name}")
        sandbox: Sandbox | None = None
        try:
            cli.create(spec)
            sandbox = Sandbox(cli, name)
            cli.policy_allow(*spec.policy_allows, sandbox=name)

            report("installing the worker (full install ladder)")
            client = WorkerClient(sandbox)
            client.install(extras=config.agent.backend, ensure_dev_tools=True)

            if cache_runtime and config.agent.backend == "copilot":
                report("pre-caching the Copilot runtime")
                runtime_cached = _cache_copilot_runtime(sandbox, client.python)

            # The dev-tools ensure above installs git best-effort; record
            # what actually landed so doctor can say whether runs will pay
            # an apt top-up on every provision.
            git_present = sandbox.exec(["sh", "-c", toolchains.GIT.probe]).ok

            manifest = {
                "worker_version": sbxloop.__version__,
                "python": client.python,
                "runtime_cached": runtime_cached,
                "baked_at": time.time(),
            }
            # The user-site install fallback never creates ~/.sbxloop.
            sandbox.mkdirs(SBXLOOP_DIR)
            sandbox.write_text(BAKE_MANIFEST, json.dumps(manifest))

            report(f"saving template {ref}")
            cli.template_save(name, ref)
        except SbxloopError as exc:
            raise BakeError(f"bake failed: {exc}") from exc
        finally:
            if sandbox is not None and not keep:
                try:
                    sandbox.rm()
                except SbxError:
                    log.warning("bake.sandbox_remove_failed", sandbox=name, exc_info=True)

    record = BakeRecord(
        ref=ref,
        worker_version=sbxloop.__version__,
        python=client.python,
        runtime_cached=runtime_cached,
        baked_at=time.time(),
        git=git_present,
    )
    path = bake_record_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2) + "\n")
    return record


def _cache_copilot_runtime(sandbox: Sandbox, python: str) -> bool:
    """Best-effort ``python -m copilot download-runtime`` so first sessions
    skip the runtime download. Never fatal: the SDK downloads on demand."""
    try:
        result = sandbox.exec([python, "-m", "copilot", "download-runtime"], timeout=600.0)
    except SbxError:
        log.warning("bake.runtime_precache_failed", sandbox=sandbox.name, exc_info=True)
        return False
    if not result.ok:
        combined = "\n".join(p.strip() for p in (result.stderr, result.stdout) if p.strip())
        log.warning(
            "bake.runtime_precache_failed",
            sandbox=sandbox.name,
            rc=result.returncode,
            output=combined[-2000:] or "(no output)",
            hint="sessions will download the runtime on demand",
        )
    return result.ok
