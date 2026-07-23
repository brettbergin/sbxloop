"""Provision the per-run sandbox pair: create, network policy, secrets, dirs.

The credential split is enforced here:

- **agent sandbox** gets only ``COPILOT_GITHUB_TOKEN`` (read from the host
  environment), injected via ``sbx secret set-custom`` bound to the Copilot
  API hosts — the value never enters the VM under the default ``proxy``
  strategy.
- **github sandbox** gets only ``GH_TOKEN`` via sbx's built-in ``github``
  secret service (scoped to github.com hosts).

The ``plain-env`` fallback strategy writes tokens to ``~/.sdxloop/env.sh``
inside the sandbox (weaker: the value is visible in the VM) for environments
where the experimental ``set-custom`` proxy rewriting is unavailable.
"""

from __future__ import annotations

import logging
import os
import shlex
from collections.abc import Callable, Mapping
from pathlib import Path

from sdxloop.config import Config
from sdxloop.errors import ProvisionError, SbxError
from sdxloop.events import EventBus
from sdxloop.sbx.cli import SbxCLI
from sdxloop.sbx.models import SandboxRole, SandboxSpec, SecretSpec
from sdxloop.sbx.pair import SandboxPair
from sdxloop.sbx.sandbox import ENV_FILE, EVENTS_DIR, JOBS_DIR, RESULTS_DIR, Sandbox

logger = logging.getLogger(__name__)

COPILOT_TOKEN_ENV = "COPILOT_GITHUB_TOKEN"
GH_TOKEN_ENVS = ("GH_TOKEN", "GITHUB_TOKEN")

# Hosts the Copilot token must reach: token exchange happens against
# api.github.com, model/agent traffic against the copilot API hosts.
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

PostCreate = Callable[[Sandbox, SandboxRole], None]


def sandbox_name(run_id: str, role: SandboxRole) -> str:
    return f"sdxloop-{run_id}-{role}"


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
            secrets=[
                SecretSpec(kind="custom", host=host, env=COPILOT_TOKEN_ENV)
                for host in AGENT_TOKEN_HOSTS
            ],
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
            "PAT with the repository permissions sdxloop should act with (e.g. issues:write, "
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
                sandbox.mkdirs(JOBS_DIR, RESULTS_DIR, EVENTS_DIR)
                if self.post_create is not None:
                    self.post_create(sandbox, spec.role)
                sandboxes[spec.role] = sandbox
                self.bus.emit("sandbox.ready", run_id, name=spec.name, role=spec.role)
            return SandboxPair(
                run_id,
                agent=sandboxes["agent"],
                github=sandboxes["github"],
                keep=self.config.keep_sandboxes,
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
                self.cli.secret_set(secret.service, sandbox=spec.name, token=token)
            else:
                assert secret.host is not None and secret.env is not None
                self.cli.secret_set_custom(
                    host=secret.host,
                    env=secret.env,
                    value=token,
                    sandbox=spec.name,
                )

    def _apply_plain_env(self, spec: SandboxSpec, sandbox: Sandbox, token: str) -> None:
        """Weaker fallback: write tokens/env into ~/.sdxloop/env.sh in the VM."""
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
