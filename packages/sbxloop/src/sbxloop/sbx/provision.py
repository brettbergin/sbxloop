"""Provision the per-run sandbox pair: create, network policy, secrets, dirs.

The credential split is enforced here:

- **agent sandbox** gets only ``COPILOT_GITHUB_TOKEN`` (read from the host
  environment), injected via ``sbx secret set-custom`` bound to the
  token-exchange host — the value never enters the VM under the default
  ``proxy`` strategy.
- **github sandbox** gets only ``GH_TOKEN`` via sbx's built-in ``github``
  secret service (scoped to github.com hosts).

The ``plain-env`` fallback strategy writes tokens to ``~/.sbxloop/env.sh``
inside the sandbox (weaker: the value is visible in the VM) for environments
where the experimental ``set-custom`` proxy rewriting is unavailable.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shlex
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

from sbxloop.config import Config
from sbxloop.errors import ProvisionError, SbxError
from sbxloop.events import EventBus
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxRole, SandboxSpec, SecretSpec
from sbxloop.sbx.pair import SandboxPair
from sbxloop.sbx.sandbox import ENV_FILE, EVENTS_DIR, JOBS_DIR, RESULTS_DIR, WORK_DIR, Sandbox

logger = logging.getLogger(__name__)

COPILOT_TOKEN_ENV = "COPILOT_GITHUB_TOKEN"
GH_TOKEN_ENVS = ("GH_TOKEN", "GITHUB_TOKEN")

# The PAT is exchanged for a Copilot API token at api.github.com; the
# exchanged token lives in SDK process memory, so the copilot API hosts only
# need network allows - never an env rewrite. One env var also cannot be
# registered twice: sbx keys custom secrets by env name, so binding the same
# env to two hosts fails with "already exists".
COPILOT_TOKEN_HOST = "api.github.com"

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

    # -- spec construction -------------------------------------------------

    def build_specs(self, run_id: str, workspace: Path) -> tuple[SandboxSpec, SandboxSpec]:
        extra = tuple(self.config.sandbox.extra_allow_domains)
        template = self.config.sandbox.template
        agent = SandboxSpec(
            name=sandbox_name(run_id, "agent"),
            role="agent",
            workspace=workspace,
            template=template,
            policy_allows=[*AGENT_ALLOW_DOMAINS, *extra],
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

        # Fail fast on missing tokens before creating any microVM.
        tokens: dict[SandboxRole, str] = {
            "agent": self.copilot_token(),
            "github": self.gh_token(),
        }

        agent_spec, github_spec = self.build_specs(run_id, workspace)
        created: list[Sandbox] = []
        try:
            sandboxes: dict[SandboxRole, Sandbox] = {}
            for spec in (agent_spec, github_spec):
                self.bus.emit("sandbox.provision_start", run_id, name=spec.name, role=spec.role)
                self.cli.create(spec)
                sandbox = Sandbox(self.cli, spec.name)
                created.append(sandbox)
                self._apply_policy(spec)
                self._apply_secrets(spec, sandbox, tokens[spec.role])
                self._verify_secret_env(run_id, spec, sandbox, tokens[spec.role])
                sandbox.mkdirs(JOBS_DIR, RESULTS_DIR, EVENTS_DIR)
                if self.post_create is not None:
                    self.post_create(sandbox, spec.role)
                sandboxes[spec.role] = sandbox
                self.bus.emit("sandbox.ready", run_id, name=spec.name, role=spec.role)
            agent_workdir = self._discover_mount(run_id, sandboxes["agent"], workspace)
            mounted = agent_workdir is not None
            if agent_workdir is None:
                agent_workdir = WORK_DIR
                sandboxes["agent"].mkdirs(agent_workdir)
            return SandboxPair(
                run_id,
                agent=sandboxes["agent"],
                github=sandboxes["github"],
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
            if isinstance(exc, ProvisionError):
                raise
            raise ProvisionError(f"provisioning run {run_id} failed: {exc}") from exc

    def _apply_policy(self, spec: SandboxSpec) -> None:
        for domain in spec.policy_allows:
            self.cli.policy_allow(domain, sandbox=spec.name)

    def _apply_secrets(self, spec: SandboxSpec, sandbox: Sandbox, token: str) -> None:
        if self.config.secret_strategy == "plain-env":
            self._apply_plain_env(spec, sandbox, token)
            return
        for secret in spec.secrets:
            if secret.kind == "service":
                assert secret.service is not None
                service = secret.service
                self._set_secret_replacing(
                    f"service {service} ({spec.name})",
                    set_fn=partial(self.cli.secret_set, service, sandbox=spec.name, token=token),
                    rm_candidates=partial(self._service_rm_candidates, service, spec.name),
                )
            else:
                assert secret.host is not None and secret.env is not None
                self._set_secret_replacing(
                    f"custom {secret.env}@{secret.host} ({spec.name})",
                    set_fn=partial(
                        self.cli.secret_set_custom,
                        host=secret.host,
                        env=secret.env,
                        value=token,
                        sandbox=spec.name,
                    ),
                    rm_candidates=partial(
                        self._custom_rm_candidates, secret.host, secret.env, spec.name
                    ),
                )

    _SECRET_EXISTS_MARKERS = ("exist", "already")
    # sbx reports the owner of a conflicting secret, e.g.
    #   ERROR: custom secret env "X" already exists in scope NAME with placeholder ...
    _SCOPE_RE = re.compile(r'in scope "?([A-Za-z0-9._-]+)"?')

    @classmethod
    def _parsed_scope(cls, stderr: str) -> str | None:
        """The scope owning the conflicting secret, per sbx's error message.

        Returns None when unparseable; the literal scopes "global"/"-g" map
        to None-as-global in secret_rm terms via the callers below.
        """
        match = cls._SCOPE_RE.search(stderr)
        return match.group(1) if match else None

    def _service_rm_candidates(
        self, service: str, sandbox: str, stderr: str
    ) -> list[Callable[[], bool]]:
        scopes: list[str | None] = []
        parsed = self._parsed_scope(stderr)
        if parsed:
            scopes.append(None if parsed in ("global", "-g") else parsed)
        scopes += [sandbox, None]
        seen: list[str | None] = []
        candidates: list[Callable[[], bool]] = []
        for scope in scopes:
            if scope in seen:
                continue
            seen.append(scope)
            candidates.append(partial(self.cli.secret_rm, service=service, sandbox=scope))
        return candidates

    def _custom_rm_candidates(
        self, host: str, env: str, sandbox: str, stderr: str
    ) -> list[Callable[[], bool]]:
        scopes: list[str | None] = []
        parsed = self._parsed_scope(stderr)
        if parsed:
            scopes.append(None if parsed in ("global", "-g") else parsed)
        scopes += [sandbox, None]
        seen: list[str | None] = []
        candidates: list[Callable[[], bool]] = []
        for scope in scopes:
            if scope in seen:
                continue
            seen.append(scope)
            # env+host first, then env-only: sbx keys custom secrets by env
            # name, so the conflicting entry may carry a different host.
            candidates.append(partial(self.cli.secret_rm, host=host, env=env, sandbox=scope))
            candidates.append(partial(self.cli.secret_rm, env=env, sandbox=scope))
        return candidates

    def _set_secret_replacing(
        self,
        describe: str,
        *,
        set_fn: Callable[[], None],
        rm_candidates: Callable[[str], list[Callable[[], bool]]],
    ) -> None:
        """Set a secret, replacing a leftover one from a previous run.

        sbx refuses to overwrite an existing secret and keys custom secrets
        by env name, with the conflicting entry possibly owned by another
        scope (a previous run's sandbox). On an exists-error we parse the
        owning scope out of sbx's stderr and try removal candidates from
        most to least specific, retrying the set after each successful
        removal. An exists-conflict NEVER fails provisioning: if nothing
        can be replaced, the existing value is kept with a warning (it may
        be stale if the token was rotated). Non-exists errors raise.

        Only sbx's stderr is matched for exists-markers: the full exception
        string embeds argv, and arbitrary paths can contain words like
        "exists" (a pytest tmp dir did exactly that).
        """
        try:
            set_fn()
            return
        except SbxError as exc:
            if not any(m in exc.stderr.lower() for m in self._SECRET_EXISTS_MARKERS):
                raise
            stderr = exc.stderr
        for rm_fn in rm_candidates(stderr):
            if not rm_fn():
                continue
            try:
                set_fn()
                return
            except SbxError as exc:
                if not any(m in exc.stderr.lower() for m in self._SECRET_EXISTS_MARKERS):
                    raise
        logger.warning(
            "secret %s already exists and could not be replaced; keeping the "
            "existing value (it may be stale if the token was rotated)",
            describe,
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
        """
        if self.config.secret_strategy != "proxy":
            # plain-env: the worker loads the env file itself; a shell
            # visibility check can never pass and would only produce noise.
            return
        env_name = COPILOT_TOKEN_ENV if spec.role == "agent" else "GH_TOKEN"
        result = sandbox.exec(["sh", "-lc", f'test -n "${{{env_name}}}"'])
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
        """
        marker = f".sbxloop-mount-{secrets.token_hex(8)}"
        try:
            (workspace / marker).write_text("")
        except OSError:
            logger.warning("mount discovery: cannot write marker into %s", workspace)
            return None
        try:
            command = (
                f"find -L {' '.join(MOUNT_SEARCH_ROOTS)} "
                f"-maxdepth {MOUNT_SEARCH_MAXDEPTH} -name {marker} -print 2>/dev/null"
                " | head -1"
            )
            result = sandbox.exec(["sh", "-c", command])
            hit = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        except SbxError:
            logger.warning("mount discovery failed for %s", sandbox.name, exc_info=True)
            hit = ""
        finally:
            (workspace / marker).unlink(missing_ok=True)
        if not hit.endswith(f"/{marker}"):
            self.bus.emit(
                "sandbox.workspace_mount",
                run_id,
                name=sandbox.name,
                mounted=False,
                message="workspace mount not found in VM; artifacts will be harvested",
            )
            return None
        mount_dir = hit[: -len(f"/{marker}")] or "/"
        self.bus.emit(
            "sandbox.workspace_mount",
            run_id,
            name=sandbox.name,
            mounted=True,
            path=mount_dir,
        )
        return mount_dir

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
