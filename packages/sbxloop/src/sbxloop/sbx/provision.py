"""Provision the per-run sandbox pair: create, network policy, secrets, dirs.

The credential split is enforced here:

- **agent sandbox** gets only ``COPILOT_GITHUB_TOKEN`` (read from the host
  environment), injected via ``sbx secret set-custom`` bound to the
  token-exchange host — the value never enters the VM under the default
  ``proxy`` strategy.
- **github sandbox** gets only ``GH_TOKEN`` via sbx's built-in ``github``
  secret service (scoped to github.com hosts). It is provisioned only when
  the GitHub integration is configured (``[github].repo``); otherwise runs
  have no GitHub capability and GH_TOKEN is not required.

The ``plain-env`` fallback strategy writes tokens to ``~/.sbxloop/env.sh``
inside the sandbox (weaker: the value is visible in the VM) for environments
where the experimental ``set-custom`` proxy rewriting is unavailable.
"""

from __future__ import annotations

import logging
import os
import secrets
import shlex
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from sbxloop.config import Config
from sbxloop.errors import ProvisionError, SbxError
from sbxloop.events import EventBus
from sbxloop.policy import (
    PROMPT_ADVERTISED_DOMAINS,
    baseline_allows,
    toolchain_install_domains,
)
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.conformance import (
    PROBE_SECRET_ENV_VISIBILITY,
    PROBE_WORKSPACE_MOUNT,
    record_field_verdict,
)
from sbxloop.sbx.models import SandboxRole, SandboxSpec, SecretSpec
from sbxloop.sbx.pair import SandboxPair
from sbxloop.sbx.sandbox import ENV_FILE, EVENTS_DIR, JOBS_DIR, RESULTS_DIR, WORK_DIR, Sandbox
from sbxloop.sbx.secretstate import (
    COPILOT_TOKEN_ENV,
    COPILOT_TOKEN_HOST,
    custom_rm_candidates,
    service_rm_candidates,
    set_secret_replacing,
)

logger = logging.getLogger(__name__)

GH_TOKEN_ENVS = ("GH_TOKEN", "GITHUB_TOKEN")

# Hosts the agent sandbox must be able to reach (doctor checks these).
AGENT_TOKEN_HOSTS = ("api.githubcopilot.com", "api.github.com")

AGENT_ALLOW_DOMAINS = (
    "api.githubcopilot.com",
    "*.githubcopilot.com",
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",  # copilot CLI runtime downloads
    "raw.githubusercontent.com",
)
GITHUB_ALLOW_DOMAINS = (
    "api.github.com",
    "github.com",
    "uploads.github.com",
    "objects.githubusercontent.com",
)

# Candidate roots for the in-VM workspace mount search. Where sbx mounts the
# host workspace inside the microVM is undocumented; probe, never assume.
MOUNT_SEARCH_ROOTS = ("/workspace", "/home/agent", "/mnt", "/host", "/root")
MOUNT_SEARCH_MAXDEPTH = 4

PostCreate = Callable[[Sandbox, SandboxRole], None]


def sandbox_name(run_id: str, role: SandboxRole) -> str:
    return f"sbxloop-{run_id}-{role}"


class Provisioner:
    def __init__(
        self,
        cli: SbxCLI,
        config: Config,
        bus: EventBus | None = None,
        *,
        env: Mapping[str, str] | None = None,
        post_create: PostCreate | None = None,
    ) -> None:
        self.cli = cli
        self.config = config
        self.bus = bus or EventBus()
        self.env = os.environ if env is None else env
        self.post_create = post_create
        self._sbx_version: str | None = None
        self._sbx_version_known = False
        # Serializes the version lookup and the cache file's read-modify-
        # write: the two sandboxes provision on parallel threads (#127).
        self._probe_lock = threading.Lock()

    def _record_probe(self, probe_id: str, verdict: str, detail: str = "") -> None:
        """Refresh the conformance cache from a field observation.

        Provisioning already performs these checks for its own needs; feeding
        the verdicts into the version-keyed cache keeps `doctor` fresh for
        free. Best-effort: recording must never affect provisioning.
        """
        try:
            with self._probe_lock:
                if not self._sbx_version_known:
                    self._sbx_version = self.cli.version()
                    self._sbx_version_known = True
                record_field_verdict(
                    self.config.state_dir, self._sbx_version, probe_id, verdict, detail
                )
        except Exception:
            logger.debug("conformance verdict recording failed", exc_info=True)

    # -- spec construction -------------------------------------------------

    def build_specs(self, run_id: str, workspace: Path) -> tuple[SandboxSpec, SandboxSpec]:
        extra = tuple(self.config.sandbox.extra_allow_domains)
        template = self.config.sandbox.template
        agent = SandboxSpec(
            name=sandbox_name(run_id, "agent"),
            role="agent",
            workspace=workspace,
            template=template,
            # PROMPT_ADVERTISED_DOMAINS: the prompts promise the language
            # registry baseline and the apt mirrors are reachable, and both
            # the worker's pip install and the dev-tools apt ensure run
            # before any plan-declared egress exists — the promise must not
            # depend on the operator's global sbx preset. Seeded through
            # baseline_allows so [policy] deny still wins over the tier that
            # never asks for a grant.
            #
            # toolchain_install_domains: same ordering problem, narrower
            # scope. The selected languages' installers fetch from vendor
            # hosts during THIS provisioning, before a plan exists to declare
            # them — so they are seeded too, gated on [sandbox] languages so
            # a Python-only run carries none of Node's or Rust's hosts.
            policy_allows=[
                *AGENT_ALLOW_DOMAINS,
                *baseline_allows(
                    (
                        *PROMPT_ADVERTISED_DOMAINS,
                        *toolchain_install_domains(self.config.sandbox.effective_languages),
                    ),
                    self.config.policy.deny,
                ),
                *extra,
            ],
            secrets=[SecretSpec(kind="custom", host=COPILOT_TOKEN_HOST, env=COPILOT_TOKEN_ENV)],
        )
        github = SandboxSpec(
            name=sandbox_name(run_id, "github"),
            role="github",
            workspace=workspace,
            template=template,
            policy_allows=[*GITHUB_ALLOW_DOMAINS, *extra],
            secrets=[SecretSpec(kind="service", service="github")],
        )
        return agent, github

    # -- tokens ------------------------------------------------------------

    def copilot_token(self) -> str:
        token = self.env.get(COPILOT_TOKEN_ENV, "")
        if not token:
            raise ProvisionError(
                f"{COPILOT_TOKEN_ENV} is not set on the host. Create a fine-grained PAT "
                'with the "Copilot Requests" permission and export it.'
            )
        return token

    def gh_token(self) -> str:
        for name in GH_TOKEN_ENVS:
            token = self.env.get(name, "")
            if token:
                return token
        raise ProvisionError(
            f"none of {', '.join(GH_TOKEN_ENVS)} are set on the host. Create a fine-grained "
            "PAT with the repository permissions sbxloop should act with (e.g. issues:write, "
            "contents:read) and export GH_TOKEN."
        )

    # -- provisioning ------------------------------------------------------

    def ensure_pair(self, run_id: str, workspace: Path | None = None) -> SandboxPair:
        workspace = workspace or self.config.sandbox.workspace
        if workspace is None:
            workspace = self.config.state_dir / "runs" / run_id / "workspace"
        workspace = workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)

        # The github sandbox (and its token requirement) exists only when the
        # GitHub integration is configured; without [github].repo a run has
        # no GitHub capability at all — and one less microVM to boot.
        github_enabled = self.config.github.enabled

        # Fail fast on missing tokens before creating any microVM.
        tokens: dict[SandboxRole, str] = {"agent": self.copilot_token()}
        if github_enabled:
            tokens["github"] = self.gh_token()

        agent_spec, github_spec = self.build_specs(run_id, workspace)
        specs = (agent_spec, github_spec) if github_enabled else (agent_spec,)
        created: list[Sandbox] = []
        registered_secret_rms: list[Callable[[], bool]] = []
        # Guards the two rollback lists: the sandboxes provision on parallel
        # threads, and a failure must still see everything the OTHER thread
        # created so rollback stays complete.
        rollback_lock = threading.Lock()

        def provision_one(spec: SandboxSpec) -> Sandbox:
            self.bus.emit("sandbox.provision_start", run_id, name=spec.name, role=spec.role)
            self.cli.create(spec)
            sandbox = Sandbox(self.cli, spec.name)
            with rollback_lock:
                created.append(sandbox)
            self._apply_policy(spec)
            rms = self._apply_secrets(spec, sandbox, tokens[spec.role])
            with rollback_lock:
                registered_secret_rms.extend(rms)
            self._verify_secret_env(run_id, spec, sandbox, tokens[spec.role])
            sandbox.mkdirs(JOBS_DIR, RESULTS_DIR, EVENTS_DIR)
            if self.post_create is not None:
                self.post_create(sandbox, spec.role)
            self.bus.emit("sandbox.ready", run_id, name=spec.name, role=spec.role)
            return sandbox

        try:
            sandboxes: dict[SandboxRole, Sandbox] = {}
            if len(specs) == 1:
                sandboxes[specs[0].role] = provision_one(specs[0])
            else:
                # The pair shares nothing but the host workspace dir, so the
                # two microVMs boot and configure concurrently (#127). Every
                # future is drained before any failure propagates: rollback
                # must never race a thread still mid-provision.
                with ThreadPoolExecutor(
                    max_workers=len(specs), thread_name_prefix="sbxloop-provision"
                ) as pool:
                    futures = [(spec, pool.submit(provision_one, spec)) for spec in specs]
                    errors: list[Exception] = []
                    for spec, future in futures:
                        try:
                            sandboxes[spec.role] = future.result()
                        except Exception as exc:
                            errors.append(exc)
                    if errors:
                        raise errors[0]
            agent_workdir = self._discover_mount(run_id, sandboxes["agent"], workspace)
            mounted = agent_workdir is not None
            if agent_workdir is None:
                agent_workdir = WORK_DIR
                sandboxes["agent"].mkdirs(agent_workdir)
            return SandboxPair(
                run_id,
                agent=sandboxes["agent"],
                github=sandboxes.get("github"),
                keep=self.config.keep_sandboxes,
                workspace=workspace,
                agent_workdir=agent_workdir,
                mounted=mounted,
            )
        except Exception as exc:
            for sandbox in created:
                try:
                    sandbox.rm()
                except SbxError:
                    logger.warning("rollback: failed to remove %s", sandbox.name, exc_info=True)
            # Symmetric with sandbox removal: best-effort unregister the
            # secrets THIS attempt registered. Left behind, they would be
            # owned by a now-deleted sandbox scope, and the next run's
            # replace-on-exists recovery would depend on scope-parsing
            # heuristics instead of starting clean.
            for rm in registered_secret_rms:
                try:
                    if not rm():
                        logger.warning("rollback: sbx rejected removing a registered secret")
                except SbxError:
                    logger.warning("rollback: failed to remove a registered secret", exc_info=True)
            if isinstance(exc, ProvisionError):
                raise
            raise ProvisionError(f"provisioning run {run_id} failed: {exc}") from exc

    def _apply_policy(self, spec: SandboxSpec) -> None:
        self.cli.policy_allow(*spec.policy_allows, sandbox=spec.name)

    def _apply_secrets(
        self, spec: SandboxSpec, sandbox: Sandbox, token: str
    ) -> list[Callable[[], bool]]:
        """Register the spec's secrets, returning one rollback (rm) callable
        per registration this attempt actually created — so a provisioning
        failure can unregister them symmetric with sandbox removal, instead
        of leaving entries owned by a scope that no longer exists."""
        if self.config.secret_strategy == "plain-env":  # nosec B105 - strategy label
            self._apply_plain_env(spec, sandbox, token)
            return []
        rollbacks: list[Callable[[], bool]] = []
        for secret in spec.secrets:
            if secret.kind == "service":
                assert secret.service is not None
                service = secret.service
                registered = set_secret_replacing(
                    f"service {service} ({spec.name})",
                    set_fn=partial(self.cli.secret_set, service, sandbox=spec.name, token=token),
                    rm_candidates=partial(service_rm_candidates, self.cli, service, spec.name),
                )
                if registered:
                    rollbacks.append(
                        partial(self.cli.secret_rm, service=service, sandbox=spec.name)
                    )
            else:
                assert secret.host is not None and secret.env is not None
                registered = set_secret_replacing(
                    f"custom {secret.env}@{secret.host} ({spec.name})",
                    set_fn=partial(
                        self.cli.secret_set_custom,
                        host=secret.host,
                        env=secret.env,
                        value=token,
                        sandbox=spec.name,
                    ),
                    rm_candidates=partial(
                        custom_rm_candidates, self.cli, secret.host, secret.env, spec.name
                    ),
                )
                if registered:
                    rollbacks.append(
                        partial(self._rm_custom, host=secret.host, env=secret.env, scope=spec.name)
                    )
        return rollbacks

    def _rm_custom(self, *, host: str, env: str, scope: str) -> bool:
        # env+host first, then env-only — same ladder shape as collision
        # recovery (sbx keys custom secrets by env name).
        return self.cli.secret_rm(host=host, env=env, sandbox=scope) or self.cli.secret_rm(
            env=env, sandbox=scope
        )

    def _verify_secret_env(
        self, run_id: str, spec: SandboxSpec, sandbox: Sandbox, token: str
    ) -> None:
        """Verify the secret env is visible; auto-heal with plain-env if not.

        Field-confirmed (2026-07-23): sbx's proxy secret injection feeds the
        interactive agent sessions sbx launches, but NOT `sbx exec`
        processes - not even login shells. When the proxy strategy leaves
        the env invisible, provisioning falls back to writing the in-VM env
        file for that sandbox so runs work, with a loud event about the
        security tradeoff. If sbx later injects secrets into exec sessions,
        this check passes and the token stays out of the VM.

        The downgrade is a security decision, so it only ever happens on a
        clean probe answer (`test -n` exiting 0 or 1). An sbx-level failure
        or any other exit code is retried once and then fails provisioning
        loudly (#63): a transient infra blip must never silently select the
        weaker secret strategy.
        """
        if self.config.secret_strategy != "proxy":  # nosec B105 - strategy label
            # plain-env: the worker loads the env file itself; a shell
            # visibility check can never pass and would only produce noise.
            return
        env_name = COPILOT_TOKEN_ENV if spec.role == "agent" else "GH_TOKEN"
        probe = ["sh", "-lc", f'test -n "${{{env_name}}}"']
        error = ""
        for _attempt in range(2):
            try:
                result = sandbox.exec(probe)
            except SbxError as exc:
                error = str(exc)
                continue
            if result.returncode in (0, 1):
                break
            error = f"probe exited {result.returncode} (expected 0 or 1): {result.stderr.strip()}"
        else:
            message = (
                f"{env_name}: secret visibility probe failed twice without a clean answer — "
                "refusing to auto-downgrade to plain-env; retry when sbx is healthy, or set "
                'secret_strategy="plain-env" to choose the in-VM env file explicitly'
            )
            self.bus.emit(
                "sandbox.secret_probe_error",
                run_id,
                name=spec.name,
                env=env_name,
                message=message,
            )
            raise ProvisionError(f"{spec.name}: {message} (last error: {error})")
        # A transient probe error must not clobber the cached knowledge of
        # sbx semantics, so only clean answers are recorded.
        self._record_probe(
            PROBE_SECRET_ENV_VISIBILITY,
            "visible-under-exec" if result.ok else "invisible-under-exec",
            f"observed while provisioning {spec.name} ({env_name})",
        )
        if result.ok:
            return
        # Auto-heal: fall back to the plain-env file for this sandbox. The
        # token value becomes visible inside the microVM (which the agent
        # already controls); egress remains bounded by the network policy.
        message = (
            f"{env_name}: sbx proxy secret invisible to exec — using in-VM env file "
            f'(secret_strategy="plain-env" silences this)'
        )
        logger.info("%s: %s", spec.name, message)
        self._apply_plain_env(spec, sandbox, token)
        self.bus.emit(
            "sandbox.secret_env_fallback",
            run_id,
            name=spec.name,
            env=env_name,
            message=message,
        )

    def _discover_mount(self, run_id: str, sandbox: Sandbox, workspace: Path) -> str | None:
        """Find where sbx mounted the host workspace inside the agent VM.

        Writes a nonce marker file into the host workspace, then runs one
        bounded in-sandbox search for it over candidate roots. Returns the
        in-VM directory containing the marker, or None when discovery fails
        (→ harvest mode; non-fatal, mirroring _verify_secret_env's
        probe-don't-assume pattern). The marker is always removed.

        A failed probe degrades the same way a clean "not mounted" answer
        does, but the two are kept distinguishable (#63): the
        ``sandbox.workspace_mount`` event carries ``probe="error"`` vs
        ``probe="answered"``, and only clean answers refresh the conformance
        cache — so field debugging of harvest-mode runs chases the right
        cause.
        """
        marker = f".sbxloop-mount-{secrets.token_hex(8)}"
        try:
            (workspace / marker).write_text("")
        except OSError:
            logger.warning("mount discovery: cannot write marker into %s", workspace)
            return None
        probe_error = ""
        try:
            command = (
                f"find -L {' '.join(MOUNT_SEARCH_ROOTS)} "
                f"-maxdepth {MOUNT_SEARCH_MAXDEPTH} -name {marker} -print 2>/dev/null"
                " | head -1"
            )
            result = sandbox.exec(["sh", "-c", command])
            hit = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
            if not result.ok:
                # find's own errors are discarded by the pipeline, so a
                # nonzero exit means the probe itself broke — not "no mount".
                probe_error = f"probe exited {result.returncode}: {result.stderr.strip()}"
        except SbxError as exc:
            logger.warning("mount discovery failed for %s", sandbox.name, exc_info=True)
            hit = ""
            probe_error = str(exc)
        finally:
            (workspace / marker).unlink(missing_ok=True)
        if hit.endswith(f"/{marker}"):
            mount_dir = hit[: -len(f"/{marker}")] or "/"
            self._record_probe(
                PROBE_WORKSPACE_MOUNT,
                "discoverable",
                f"workspace mounted at {mount_dir} (observed while provisioning {sandbox.name})",
            )
            self.bus.emit(
                "sandbox.workspace_mount",
                run_id,
                name=sandbox.name,
                mounted=True,
                probe="answered",
                path=mount_dir,
            )
            return mount_dir
        if probe_error:
            # No verdict recorded: an infra failure is not knowledge about
            # sbx mount semantics and must not clobber the cached answer.
            self.bus.emit(
                "sandbox.workspace_mount",
                run_id,
                name=sandbox.name,
                mounted=False,
                probe="error",
                message=f"mount discovery probe failed ({probe_error}); "
                "artifacts will be harvested",
            )
            return None
        self._record_probe(
            PROBE_WORKSPACE_MOUNT,
            "not-found",
            f"observed while provisioning {sandbox.name}",
        )
        self.bus.emit(
            "sandbox.workspace_mount",
            run_id,
            name=sandbox.name,
            mounted=False,
            probe="answered",
            message="workspace mount not found in VM; artifacts will be harvested",
        )
        return None

    def _apply_plain_env(self, spec: SandboxSpec, sandbox: Sandbox, token: str) -> None:
        """Weaker fallback: write tokens/env into ~/.sbxloop/env.sh in the VM."""
        exports: dict[str, str] = dict(spec.persistent_env)
        if spec.role == "agent":
            exports[COPILOT_TOKEN_ENV] = token
        else:
            exports["GH_TOKEN"] = token
            exports["GITHUB_TOKEN"] = token
        lines = "".join(
            f"export {key}={shlex.quote(value)}\n" for key, value in sorted(exports.items())
        )
        sandbox.exec(["mkdir", "-p", ENV_FILE.rsplit("/", 1)[0]])
        sandbox.write_text(ENV_FILE, lines)
        sandbox.exec(["chmod", "600", ENV_FILE])
