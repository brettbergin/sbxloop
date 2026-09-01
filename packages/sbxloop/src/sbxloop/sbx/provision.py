"""Provision the per-run sandbox pair: create, network policy, secrets, dirs.

The credential split is enforced here:

- **agent sandbox** gets only ``COPILOT_GITHUB_TOKEN`` (read from the host
  environment), injected via ``sbx secret set-custom`` bound to the
  token-exchange host — the value never enters the VM under the default
  ``proxy`` strategy.
- **github sandbox** gets only ``GH_TOKEN`` — either the operator's PAT
  (via sbx's built-in ``github`` secret service, as before), or a
  host-minted GitHub App installation token (#568; see
  :mod:`sbxloop.gh.appauth`). It is provisioned only when the GitHub
  integration is configured (``[github].repo``); otherwise runs have no
  GitHub capability and no GitHub credential is required.

When the proxy cannot deliver, non-proxy delivery has two tiers, chosen by
the ``exec-stdin-env`` field probe (#592, verdict cached per sbx version):
**per-job stdin** — WorkerClient pipes each job's exports into the launch
shell's stdin via :meth:`Provisioner.job_env`, so the value transits worker
process memory only, never the sandbox filesystem — and, when this sbx does
not pass exec stdin through, the ``~/.sbxloop/env.sh`` file inside the
sandbox (weaker: the value is at rest in the VM). The ``plain-env``
strategy selects non-proxy delivery explicitly, for environments where the
experimental ``set-custom`` proxy rewriting is unavailable. Under the
default ``proxy`` strategy non-proxy delivery is also chosen directly — no
doomed registration, no probe, no per-run warning — when either

- the conformance cache already knows this sbx version leaves proxy secrets
  invisible (or sentinel-shaped) under ``exec`` — the field-verified sbx
  behavior since 0.35. An unknown/new sbx version still probes once, so the
  "sbx fixed exec injection" upside is never lost; or
- the github sandbox authenticates as a GitHub App installation: its token
  rotates ~hourly and the host must rewrite the env file on refresh anyway,
  so registering each short-lived token with sbx would be pure ceremony.
"""

from __future__ import annotations

import os
import secrets
import shlex
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal, NamedTuple

from sbxloop import hostgit
from sbxloop.config import Config, RepoConfig
from sbxloop.errors import ProvisionError, SbxError
from sbxloop.events import EventBus
from sbxloop.gh.appauth import (
    APP_ID_ENV,
    APP_INSTALLATION_ID_ENV,
    APP_KEY_ENV,
    APP_KEY_PATH_ENV,
    AppTokenSource,
    app_credentials,
)
from sbxloop.ids import branch_name
from sbxloop.log import get_logger
from sbxloop.policy import PROMPT_ADVERTISED_DOMAINS, baseline_allows
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.conformance import (
    PROBE_EXEC_STDIN_ENV,
    PROBE_SECRET_ENV_VISIBILITY,
    PROBE_WORKSPACE_MOUNT,
    VERDICT_STDIN_DELIVERS,
    VERDICT_STDIN_NO_DELIVERY,
    load_verdicts,
    record_field_verdict,
    stdin_env_probe,
)
from sbxloop.sbx.models import SandboxRole, SandboxSpec, SecretSpec
from sbxloop.sbx.pair import SandboxPair
from sbxloop.sbx.sandbox import (
    ENV_FILE,
    EVENTS_DIR,
    JOBS_DIR,
    RESULTS_DIR,
    TOOLS_DIR,
    WORK_DIR,
    Sandbox,
)
from sbxloop.sbx.secretstate import (
    ANTHROPIC_TOKEN_ENV,
    ANTHROPIC_TOKEN_HOST,
    COPILOT_TOKEN_ENV,
    COPILOT_TOKEN_HOST,
    custom_rm_candidates,
    service_rm_candidates,
    set_secret_replacing,
)
from sbxloop_worker.secrets import shell_sentinel_case, shell_token_case

log = get_logger(__name__)

GH_TOKEN_ENVS = ("GH_TOKEN", "GITHUB_TOKEN")

# Hosts the agent sandbox must be able to reach (doctor checks these),
# per agent backend (#533).
AGENT_TOKEN_HOSTS = ("api.githubcopilot.com", "api.github.com")
CLAUDE_TOKEN_HOSTS = (ANTHROPIC_TOKEN_HOST,)

AGENT_ALLOW_DOMAINS = (
    "api.githubcopilot.com",
    "*.githubcopilot.com",
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",  # copilot CLI runtime downloads
    # GitHub release-asset downloads (`github.com/<org>/<repo>/releases/download/…`)
    # answer with a redirect here; the Python toolchain's uv tarball and its
    # uv-managed interpreter both come that way at provision time (#250).
    "release-assets.githubusercontent.com",
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

# Secret-visibility probe exit code for "set, but not a credential" —
# sbx's proxy sentinel. 0 is a usable token, 1 is unset/empty.
_SENTINEL_EXIT = 3

# Shadow-probe exit code: the exec environment already carries a
# credential-shaped github token that would win over the env file (#576).
_SHADOW_EXIT = 3

# Cached probe verdicts that mean the sbx proxy cannot feed exec'd workers:
# under the ``proxy`` strategy, provisioning on an sbx version already known
# to behave this way goes straight to the in-VM env file instead of
# re-living the register→probe→downgrade dance (and its per-run warning).
_PROXY_BROKEN_VERDICTS = ("invisible-under-exec", "sentinel-under-exec")

# Why a sandbox's credentials are delivered via the in-VM env file rather
# than the sbx secret proxy. ``None`` means the proxy path is attempted.
EnvFileReason = Literal["strategy", "cached", "app"]


@dataclass(frozen=True)
class GhPat:
    """A personal access token, as configured today."""

    value: str

    def token(self) -> str:
        return self.value


@dataclass(frozen=True)
class GhApp:
    """A GitHub App installation: short-lived tokens minted on the host."""

    source: AppTokenSource

    def token(self) -> str:
        return self.source.current()


GhCredential = GhPat | GhApp


class GhCredentialStatus(NamedTuple):
    """A describable answer to "which github credential is configured?" —
    the advisory twin of :meth:`Provisioner.gh_credential` for `doctor`,
    which must report rather than raise. Keep the two in step."""

    ok: bool
    detail: str
    mode: Literal["pat", "app", "none"]


def gh_credential_status(
    env: Mapping[str, str], *, token_env: str | None = None
) -> GhCredentialStatus:
    """Describe the credential :meth:`Provisioner.gh_credential` would pick.

    ``token_env`` is a repository's own ``[[github.repos]] token_env``,
    which — as in resolution — is an explicit per-repo PAT choice and wins
    over the ambient PAT/App decision.
    """
    if token_env:
        if env.get(token_env):
            return GhCredentialStatus(True, f"token from {token_env}", "pat")
        return GhCredentialStatus(False, f"token_env {token_env} is not set on the host", "pat")
    try:
        app = app_credentials(env)
    except ProvisionError as exc:
        return GhCredentialStatus(False, str(exc), "app")
    pat_name = next((name for name in GH_TOKEN_ENVS if env.get(name)), None)
    if app is not None and pat_name:
        return GhCredentialStatus(
            False,
            f"both {pat_name} and GitHub App credentials ({APP_ID_ENV}, …) are set — "
            "unset the PAT to run as the App installation, or the GITHUB_APP_* "
            "variables to keep the PAT",
            "app",
        )
    if app is not None:
        return GhCredentialStatus(
            True,
            f"GitHub App installation {app.installation_id} (app {app.app_id})",
            "app",
        )
    if pat_name:
        return GhCredentialStatus(True, f"token from {pat_name}", "pat")
    return GhCredentialStatus(
        False,
        f"none of {'/'.join(GH_TOKEN_ENVS)} are set and no GitHub App credentials are configured",
        "none",
    )


def mount_search_roots(workspace: Path | None) -> tuple[str, ...]:
    """The in-VM roots to search for the workspace mount, most likely first.

    sbx 0.38 mounts the host workspace at its own host absolute path inside
    the VM (identity mount, field-verified on 0.38.0), so the workspace path
    itself is probed first; the 0.35-era candidate roots remain as fallback.
    """
    if workspace is None:
        return MOUNT_SEARCH_ROOTS
    return (str(workspace), *MOUNT_SEARCH_ROOTS)


def mount_probe_command(workspace: Path | None, marker: str) -> str:
    """One bounded in-VM marker search over the candidate mount roots.

    Roots are existence-filtered before the ``find`` and the command always
    exits 0 on a clean answer: on sbx 0.38 most candidate roots don't exist
    in the VM, and a bare ``find`` over missing roots exits nonzero — which
    would misclassify every clean "not mounted" answer as a broken probe
    (the probe="answered" vs probe="error" split, #63).
    """
    roots = " ".join(shlex.quote(root) for root in mount_search_roots(workspace))
    return (
        f'set --; for r in {roots}; do [ -e "$r" ] && set -- "$@" "$r"; done; '
        f'[ $# -eq 0 ] || find -L "$@" -maxdepth {MOUNT_SEARCH_MAXDEPTH} '
        f"-name {marker} -print 2>/dev/null | head -1; :"
    )


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
        # One shared token source per provisioner: provisioning and the
        # refresh hook must see the same cached installation token.
        self._app_source: AppTokenSource | None = None

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
            log.debug("conformance.record_failed", probe=probe_id, verdict=verdict, exc_info=True)

    # -- spec construction -------------------------------------------------

    def build_specs(
        self, run_id: str, workspace: Path, repo: str | None = None
    ) -> tuple[SandboxSpec, SandboxSpec]:
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
            policy_allows=[
                *AGENT_ALLOW_DOMAINS,
                *self._agent_extra_allows(),
                *baseline_allows(PROMPT_ADVERTISED_DOMAINS, self.config.policy.deny),
                *extra,
            ],
            secrets=[self._agent_secret_spec()],
            persistent_env=self.agent_persistent_env(),
        )
        github = SandboxSpec(
            name=sandbox_name(run_id, "github"),
            role="github",
            workspace=workspace,
            template=template,
            policy_allows=[*GITHUB_ALLOW_DOMAINS, *extra],
            secrets=[SecretSpec(kind="service", service="github")],
            persistent_env=self.github_repo_env(repo),
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

    # -- the agent backend's credential (#533) -----------------------------

    def agent_backend(self) -> str:
        """Which SDK the agent sandbox runs (``[agent] backend``)."""
        return self.config.agent.backend

    def agent_token_env(self) -> str:
        """The env var carrying the agent sandbox's credential."""
        return ANTHROPIC_TOKEN_ENV if self.agent_backend() == "claude" else COPILOT_TOKEN_ENV

    def agent_token(self) -> str:
        """The chosen agent backend's credential, read from the host env."""
        if self.agent_backend() != "claude":
            return self.copilot_token()
        token = self.env.get(ANTHROPIC_TOKEN_ENV, "")
        if not token:
            raise ProvisionError(
                f'{ANTHROPIC_TOKEN_ENV} is not set on the host but [agent] backend = "claude". '
                "Create an Anthropic API key and export it, or switch back to "
                'backend = "copilot".'
            )
        return token

    def agent_token_hosts(self) -> tuple[str, ...]:
        """Hosts the agent sandbox's credential path needs (doctor rows)."""
        return CLAUDE_TOKEN_HOSTS if self.agent_backend() == "claude" else AGENT_TOKEN_HOSTS

    def agent_persistent_env(self) -> dict[str, str]:
        """Non-secret env the agent sandbox needs for the chosen backend.

        The worker resolves its backend from ``SBXLOOP_WORKER_BACKEND``
        (default copilot), so only the claude backend needs the selector
        delivered. ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`` keeps the
        Claude Code CLI hermetic — no telemetry or auto-update calls to
        hosts the balanced network policy would only refuse.
        """
        if self.agent_backend() != "claude":
            return {}
        return {
            "SBXLOOP_WORKER_BACKEND": "claude",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }

    def _agent_secret_spec(self) -> SecretSpec:
        if self.agent_backend() == "claude":
            return SecretSpec(kind="custom", host=ANTHROPIC_TOKEN_HOST, env=ANTHROPIC_TOKEN_ENV)
        return SecretSpec(kind="custom", host=COPILOT_TOKEN_HOST, env=COPILOT_TOKEN_ENV)

    def _agent_extra_allows(self) -> tuple[str, ...]:
        return (ANTHROPIC_TOKEN_HOST,) if self.agent_backend() == "claude" else ()

    def gh_credential(self, repo: str | None = None) -> GhCredential:
        """The credential the github sandbox authenticates with, scoped to
        ``repo`` — a PAT (as today) or a GitHub App installation (#568).

        Resolution order:

        - a repository's own ``[[github.repos]] token_env`` is an explicit
          per-repo choice and always wins (a PAT, as before);
        - otherwise the ambient credential set decides: GH_TOKEN /
          GITHUB_TOKEN → PAT mode; GITHUB_APP_ID +
          GITHUB_APP_INSTALLATION_ID + GITHUB_APP_PRIVATE_KEY[_PATH] →
          App mode (the host mints short-lived installation tokens);
        - both ambient sets, or neither, fail loudly here — before any
          microVM exists — naming what to fix, instead of an obscure
          401/403 later.
        """
        entry = self._repo_entry(repo)
        if entry is not None and entry.token_env:
            token = self.env.get(entry.token_env, "")
            if token:
                return GhPat(token)
            raise ProvisionError(
                f"{entry.token_env} (the token_env of {entry.repo}) is not set on the host."
            )
        app = app_credentials(self.env)
        pat = next((self.env.get(name, "") for name in GH_TOKEN_ENVS if self.env.get(name)), "")
        if app is not None and pat:
            raise ProvisionError(
                "both a PAT (GH_TOKEN/GITHUB_TOKEN) and GitHub App credentials "
                f"({APP_ID_ENV}, {APP_INSTALLATION_ID_ENV}, …) are set — sbxloop cannot "
                "choose for you: unset the PAT to run as the App installation, or unset "
                "the GITHUB_APP_* variables to keep the PAT"
            )
        if app is not None:
            if self._app_source is None:
                self._app_source = AppTokenSource(app)
            return GhApp(self._app_source)
        if pat:
            return GhPat(pat)
        raise ProvisionError(
            f"none of {', '.join(GH_TOKEN_ENVS)} are set on the host and no GitHub App "
            "credentials are configured. Either create a fine-grained PAT with the "
            "repository permissions sbxloop should act with (e.g. issues:write, "
            "contents:read) and export GH_TOKEN, or install a GitHub App on the "
            f"repository and set {APP_ID_ENV}, {APP_INSTALLATION_ID_ENV} and "
            f"{APP_KEY_PATH_ENV} (or {APP_KEY_ENV})."
        )

    def gh_token(self, repo: str | None = None) -> str:
        """The token value the github sandbox gets right now.

        See :meth:`gh_credential`; in App mode this mints (or reuses) an
        installation token.
        """
        return self.gh_credential(repo).token()

    def gh_bot_login(self, repo: str | None = None) -> str | None:
        """The login GitHub attributes the github sandbox's writes to, when
        the credential alone can answer.

        App mode answers ``<slug>[bot]`` (one cached ``GET /app`` per
        process — the shared :class:`AppTokenSource` holds it); PAT mode
        answers ``None``: a PAT's login comes from ``GET /user``, which the
        engine already asks. Best-effort by design — a misconfigured
        credential raises at provisioning time, not here.
        """
        try:
            cred = self.gh_credential(repo)
        except ProvisionError:
            return None
        if isinstance(cred, GhApp):
            return cred.source.bot_login()
        return None

    def _repo_entry(self, repo: str | None) -> RepoConfig | None:
        """The configured entry a sandbox is scoped to.

        With no selector this resolves only when the choice is unambiguous —
        the sole enabled repository, which is exactly what a run's narrowed
        config holds. A daemon-wide sandbox with several repositories
        configured must not silently inherit the first repo's token or
        remote: it falls back to the daemon-wide credentials instead.
        """
        if repo is not None:
            return self.config.github.effective_repo(repo)
        return self.config.github.default_repo()

    def github_repo_env(self, repo: str | None = None) -> dict[str, str]:
        """Remote configuration for the github-ops sandbox.

        The sandbox is told which repository it acts on (``GH_REPO``, which
        gh itself honours) so ops that omit an explicit repo still target the
        run's repository rather than a globally configured one. No credential
        is carried here — the token is registered as a secret.
        """
        entry = self._repo_entry(repo)
        if entry is None:
            return {}
        return {"GH_REPO": entry.repo, "SBXLOOP_GITHUB_REPO": entry.repo}

    # -- provisioning ------------------------------------------------------

    def ensure_pair(
        self, run_id: str, workspace: Path | None = None, repo: str | None = None
    ) -> SandboxPair:
        if workspace is not None:
            # An explicit workspace is authoritative: it is either the
            # resume pin from the runs table (which must be reused in place
            # so the relocation check holds and agent work survives) or an
            # embedder's deliberate choice. Isolation applies only to
            # config-sourced workspaces on fresh runs.
            workspace = workspace.resolve()
        else:
            workspace = self._resolve_workspace(run_id, repo)
        workspace.mkdir(parents=True, exist_ok=True)
        return self._provision_pair(run_id, workspace, repo)

    def _run_repo(self, repo: str | None) -> str | None:
        """The ``owner/name`` this run acts on, or None when there is none."""
        entry = self.config.github.find_repo(repo)
        if entry is not None:
            return entry.repo
        if repo:
            return repo
        return self.config.github.repo

    def _resolve_workspace(self, run_id: str, repo: str | None = None) -> Path:
        """Where this fresh run works: the per-run dir, *this repo's*
        configured workspace, or — when that workspace is a git checkout — a
        per-run clone of it, so runs never disturb the checkout's branch
        setup.

        The clone source is resolved per repository
        (:meth:`Config.workspace_for_repo`), never from a daemon-wide path:
        with several repositories configured, one ``[sandbox] workspace``
        would otherwise build every repo's runs from whichever repository
        that checkout happens to be (#526).
        """
        clone_dir = (self.config.state_dir / "runs" / run_id / "workspace").resolve()
        mode = self.config.sandbox.workspace_isolation
        run_repo = self._run_repo(repo)
        source = self.config.workspace_for_repo(run_repo)
        if source is None:
            configured = self.config.sandbox.workspace is not None or any(
                entry.workspace is not None for entry in self.config.github.repos
            )
            if configured and run_repo is not None:
                # A workspace is configured somewhere, but none of it belongs
                # to this repository. Its tree must come from its own remote
                # or not at all — never from another repo's checkout.
                return self._clone_repo_remote(run_id, run_repo, clone_dir)
            if mode == "clone":
                raise ProvisionError(
                    "workspace_isolation = 'clone' requires [sandbox] workspace "
                    "to point at a git checkout"
                )
            return clone_dir
        source = source.resolve()
        self._assert_origin_matches(source, run_repo)
        if mode == "in-place" or source == clone_dir:
            return source
        git = hostgit.find_git()
        if git is None:
            if mode == "clone":
                raise ProvisionError("workspace_isolation = 'clone' but no git binary is on PATH")
            log.debug("workspace.in_place", path=str(source), reason="no git binary on PATH")
            return source
        root = hostgit.repo_toplevel(source)
        if root is None:
            if mode == "clone":
                raise ProvisionError(
                    f"workspace_isolation = 'clone' but {source} is not a git repository"
                )
            return source
        if root != source:
            raise ProvisionError(
                f"workspace {source} is inside a git checkout (root {root}) but is "
                "not its root; clone isolation cannot isolate a subtree — point "
                "[sandbox] workspace at the repo root, or set "
                "workspace_isolation = 'in-place' to keep mutating it directly"
            )
        if hostgit.head_commit(source) is None:
            raise ProvisionError(
                f"workspace {source} is a git repository with no commits; commit "
                "something first, or set workspace_isolation = 'in-place'"
            )
        # The tool's own state directory is run state, not user content:
        # any sbxloop command run from inside the checkout drops a relative
        # ".sbxloop" there, and that must not trip the isolation refusal.
        # Both the configured state-dir name and the default are ignored —
        # a different invocation with default config may have dropped the
        # default name even when this run's state_dir points elsewhere.
        ignore = {self.config.state_dir.name, ".sbxloop"}
        dirty = hostgit.is_dirty(source, ignore=sorted(ignore))
        if dirty and mode == "auto":
            raise ProvisionError(
                f"workspace {source} has uncommitted changes; sbxloop isolates "
                "runs in a clone of committed HEAD, which would silently exclude "
                "them. Commit or stash them, or set [sandbox] "
                "workspace_isolation = 'clone' to run from HEAD anyway, or "
                "'in-place' to run directly in the checkout"
            )
        return self._clone_workspace(run_id, source, clone_dir, dirty=dirty)

    def _assert_origin_matches(self, source: Path, repo: str | None) -> None:
        """Refuse a source checkout whose ``origin`` names another repository.

        Belt and braces behind the daemon-start/``doctor`` preflight: a run
        must never be built from a tree belonging to a different repo, no
        matter how the workspace was configured or resolved (#526).

        With several repositories configured this fails *closed*: an origin
        that cannot be shown to belong to this repo (no ``origin`` remote at
        all, a non-GitHub or nested-path URL, an ssh alias, a local path
        remote) is refused too. "Nothing to contradict it" is not evidence of
        ownership, and no preflight covers that case. With a single repo the
        unknown case stays permissive — that is the unchanged single-repo
        deployment, where there is no other tree to confuse it with.
        """
        if repo is None:
            return
        origin = hostgit.origin_url(source)
        matches = hostgit.origin_matches_repo(source, repo)
        if matches is True:
            return
        if matches is None and not self.config.github.multi_repo:
            return
        if matches is None:
            actual = origin or "no origin remote"
            detail = f"workspace {source} has no origin that can be shown to be {repo} ({actual})"
        else:
            named = hostgit.normalise_repo_url(origin) or origin
            detail = f"workspace {source} is a checkout of {named}"
        if not (source / ".git").exists() and hostgit.repo_toplevel(source) is None:
            # Not a git checkout at all: there is no origin to check and the
            # isolation logic handles it (in-place / clone refusal).
            return
        raise ProvisionError(
            f"{detail}, but this run is for {repo}; refusing to build {repo} "
            "from a tree that is not demonstrably its own. Point that "
            "repository's [[github.repos]] entry at its own workspace (or "
            "move [sandbox] workspace into the matching entry)"
        )

    def _clone_repo_remote(self, run_id: str, repo: str, clone_dir: Path) -> Path:
        """Clone the run's repository from its remote into the run dir.

        The explicit no-workspace path: this repository has no host checkout,
        so its tree comes from its own remote. Only credential-free (public)
        remotes can succeed — the host holds no git credential by design
        (#46) — and a failure is raised with that reason rather than falling
        back to another repository's tree or to an empty directory.
        """
        if (clone_dir / ".git").exists():
            self.bus.emit(
                "sandbox.workspace_clone",
                run_id,
                source=repo,
                target=str(clone_dir),
                commit=hostgit.head_commit(clone_dir),
                branch=branch_name(run_id),
                dirty=False,
                reused=True,
                message=f"reusing existing run clone at {clone_dir}",
            )
            return clone_dir
        if hostgit.find_git() is None:
            raise ProvisionError(
                f"no workspace is configured for {repo} and no git binary is on "
                "PATH to clone it from its remote"
            )
        url = f"https://github.com/{repo}"
        branch = self.config.sandbox.continue_branch or branch_name(run_id)
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            sha = hostgit.clone_from_remote(
                url, clone_dir, branch, existing=bool(self.config.sandbox.continue_branch)
            )
        except ProvisionError as exc:
            raise ProvisionError(
                f"no workspace is configured for {repo}, and cloning it from "
                f"{url} failed: {exc}. The host holds no git credential, so this "
                "path only works for a public repository; configure a workspace "
                "for this repository in its [[github.repos]] entry (see #46)"
            ) from exc
        self.bus.emit(
            "sandbox.workspace_clone",
            run_id,
            source=url,
            target=str(clone_dir),
            commit=sha,
            branch=branch,
            dirty=False,
            reused=False,
            message=f"cloned {url} at {sha[:12]} onto branch {branch}",
        )
        return clone_dir

    def _clone_workspace(self, run_id: str, source: Path, clone_dir: Path, *, dirty: bool) -> Path:
        branch = branch_name(run_id)
        if (clone_dir / ".git").exists():
            # A run that crashed after cloning but before the workspace pin
            # landed re-enters this path on resume; never re-clone over
            # whatever work is already in the clone.
            self.bus.emit(
                "sandbox.workspace_clone",
                run_id,
                source=str(source),
                target=str(clone_dir),
                commit=hostgit.head_commit(clone_dir),
                branch=branch,
                dirty=False,
                reused=True,
                message=f"reusing existing run clone at {clone_dir}",
            )
            return clone_dir
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        existing = self.config.sandbox.continue_branch
        if existing:
            # A fix round continues its own pull request: start from what
            # that branch actually has, so the delivery updates the PR
            # instead of replacing it with work rebuilt from the default
            # branch.
            branch = existing
            sha = hostgit.clone_existing_branch(source, clone_dir, branch)
            message = f"cloned {source} at {sha[:12]} continuing branch {branch}"
        else:
            sha = hostgit.clone_for_run(source, clone_dir, branch)
            message = f"cloned {source} at {sha[:12]} onto branch {branch}"
        if dirty:
            message += " — source tree has uncommitted changes; they are NOT in the run workspace"
            log.warning("workspace.dirty", path=str(source), detail=message)
        self.bus.emit(
            "sandbox.workspace_clone",
            run_id,
            source=str(source),
            target=str(clone_dir),
            commit=sha,
            branch=branch,
            dirty=dirty,
            reused=False,
            message=message,
        )
        return clone_dir

    def _provision_pair(self, run_id: str, workspace: Path, repo: str | None = None) -> SandboxPair:
        # The github sandbox (and its token requirement) exists only when the
        # GitHub integration is configured; without [github].repo a run has
        # no GitHub capability at all — and one less microVM to boot.
        github_enabled = self.config.github.enabled

        # Fail fast on missing credentials before creating any microVM. In
        # App mode this mints the first installation token here.
        gh_cred = self.gh_credential(repo) if github_enabled else None
        tokens: dict[SandboxRole, str] = {"agent": self.agent_token()}
        if gh_cred is not None:
            tokens["github"] = gh_cred.token()
        # Decided once, before the parallel threads: a probe verdict recorded
        # by one thread must not flip the other thread's delivery mid-flight.
        env_file_reasons: dict[SandboxRole, EnvFileReason | None] = {
            "agent": self._env_file_reason("agent", None),
            "github": self._env_file_reason("github", gh_cred),
        }

        agent_spec, github_spec = self.build_specs(run_id, workspace, repo)
        specs = (agent_spec, github_spec) if github_enabled else (agent_spec,)
        created: list[Sandbox] = []
        registered_secret_rms: list[Callable[[], bool]] = []
        # Guards the two rollback lists: the sandboxes provision on parallel
        # threads, and a failure must still see everything the OTHER thread
        # created so rollback stays complete.
        rollback_lock = threading.Lock()

        def provision_one(spec: SandboxSpec) -> Sandbox:
            started = time.monotonic()
            log.info(
                "sandbox.provision_start",
                run=run_id,
                sandbox=spec.name,
                role=spec.role,
                template=spec.template,
            )
            self.bus.emit("sandbox.provision_start", run_id, name=spec.name, role=spec.role)
            if env_file_reasons[spec.role] is not None:
                # sbx stamps *registered* secrets into the VM at create;
                # purge leftovers parked at this name first (#576).
                self._purge_stale_registrations(spec)
            self.cli.create(spec)
            log.debug(
                "sandbox.created",
                run=run_id,
                sandbox=spec.name,
                duration_s=round(time.monotonic() - started, 1),
            )
            sandbox = Sandbox(self.cli, spec.name)
            with rollback_lock:
                created.append(sandbox)
            self._apply_policy(spec)
            reason = env_file_reasons[spec.role]
            if reason is not None:
                self._apply_env_file_only(run_id, spec, sandbox, tokens[spec.role], reason)
            else:
                rms = self._apply_secrets(spec, sandbox, tokens[spec.role])
                with rollback_lock:
                    registered_secret_rms.extend(rms)
                self._apply_persistent_env(spec, sandbox)
                # After the env write: the fallback rewrites the whole file
                # (it folds persistent_env in itself), so it must have the
                # last word.
                self._verify_secret_env(run_id, spec, sandbox, tokens[spec.role])
            sandbox.mkdirs(JOBS_DIR, RESULTS_DIR, EVENTS_DIR, TOOLS_DIR)
            if self.post_create is not None:
                self.post_create(sandbox, spec.role)
            self.bus.emit("sandbox.ready", run_id, name=spec.name, role=spec.role)
            log.info(
                "sandbox.ready",
                run=run_id,
                sandbox=spec.name,
                role=spec.role,
                duration_s=round(time.monotonic() - started, 1),
            )
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
                            # Only the first is raised; the others would
                            # otherwise vanish, and "the github one failed
                            # too" is exactly what a field debug needs.
                            log.warning(
                                "sandbox.provision_failed",
                                run=run_id,
                                sandbox=spec.name,
                                role=spec.role,
                                error=str(exc),
                                exc_info=len(errors) > 0,
                            )
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
            log.warning(
                "sandbox.rollback",
                run=run_id,
                sandboxes=[sb.name for sb in created],
                secrets=len(registered_secret_rms),
                error=str(exc),
            )
            for sandbox in created:
                try:
                    sandbox.rm()
                except SbxError:
                    log.warning(
                        "sandbox.rollback_remove_failed",
                        run=run_id,
                        sandbox=sandbox.name,
                        exc_info=True,
                    )
            # Symmetric with sandbox removal: best-effort unregister the
            # secrets THIS attempt registered. Left behind, they would be
            # owned by a now-deleted sandbox scope, and the next run's
            # replace-on-exists recovery would depend on scope-parsing
            # heuristics instead of starting clean.
            for rm in registered_secret_rms:
                try:
                    if not rm():
                        log.warning("sandbox.rollback_secret_refused", run=run_id)
                except SbxError:
                    log.warning("sandbox.rollback_secret_failed", run=run_id, exc_info=True)
            if isinstance(exc, ProvisionError):
                raise
            raise ProvisionError(f"provisioning run {run_id} failed: {exc}") from exc

    def github_only_spec(self, name: str, workspace: Path, repo: str | None = None) -> SandboxSpec:
        """A github-role spec that is not tied to a run — the daemon's
        long-lived polling/ops sandbox. Mirrors the pair's github spec."""
        return SandboxSpec(
            name=name,
            role="github",
            workspace=workspace,
            template=self.config.sandbox.template,
            policy_allows=[*GITHUB_ALLOW_DOMAINS, *self.config.sandbox.extra_allow_domains],
            secrets=[SecretSpec(kind="service", service="github")],
            persistent_env=self.github_repo_env(repo),
        )

    def agent_only_spec(self, name: str, workspace: Path) -> SandboxSpec:
        """An agent-role spec that is not tied to a run — the daemon's
        long-lived concierge sandbox (Copilot token, no GH_TOKEN). Mirrors
        the pair's agent spec, including the prompt-advertised baseline."""
        return SandboxSpec(
            name=name,
            role="agent",
            workspace=workspace,
            template=self.config.sandbox.template,
            policy_allows=[
                *AGENT_ALLOW_DOMAINS,
                *self._agent_extra_allows(),
                *baseline_allows(PROMPT_ADVERTISED_DOMAINS, self.config.policy.deny),
                *self.config.sandbox.extra_allow_domains,
            ],
            secrets=[self._agent_secret_spec()],
            persistent_env=self.agent_persistent_env(),
        )

    def ensure_github_only(
        self,
        name: str,
        workspace: Path,
        *,
        post_create: PostCreate | None = None,
        run_id: str | None = None,
        repo: str | None = None,
    ) -> Sandbox:
        """Provision one github-role sandbox (GH_TOKEN only) outside a run's
        pair.

        The daemon polls issues and drives label/comment lifecycle from
        here, keeping the credential split intact: the host still never
        holds the PAT; ``sbxloop deliver`` re-delivers a finished run from
        one too (#223) and passes ``run_id`` so the provisioning events land
        in that run's log rather than under a daemon label. Same
        fail-fast/rollback discipline as the pair — token check before any
        microVM, sandbox + registered secrets removed on failure.
        ``post_create`` (falling back to the instance-level hook) runs
        *inside* that try, so a caller's worker install failing rolls the
        sandbox and its secrets back too.
        """
        cred = self.gh_credential(repo)
        spec = self.github_only_spec(name, workspace, repo)
        return self._ensure_single(
            spec,
            cred.token(),
            reason=self._env_file_reason("github", cred),
            post_create=post_create,
            run_id=run_id,
        )

    def ensure_agent_only(
        self,
        name: str,
        workspace: Path,
        *,
        post_create: PostCreate | None = None,
        run_id: str | None = None,
    ) -> Sandbox:
        """Provision one agent-role sandbox (Copilot token only) outside a
        run's pair — the daemon's concierge box. Same discipline as
        :meth:`ensure_github_only`."""
        token = self.agent_token()
        spec = self.agent_only_spec(name, workspace)
        return self._ensure_single(
            spec,
            token,
            reason=self._env_file_reason("agent", None),
            post_create=post_create,
            run_id=run_id,
        )

    def _ensure_single(
        self,
        spec: SandboxSpec,
        token: str,
        *,
        reason: EnvFileReason | None,
        post_create: PostCreate | None,
        run_id: str | None,
    ) -> Sandbox:
        name = spec.name
        spec.workspace.mkdir(parents=True, exist_ok=True)
        label = run_id or f"daemon:{name}"
        created: Sandbox | None = None
        registered_secret_rms: list[Callable[[], bool]] = []
        try:
            self.bus.emit("sandbox.provision_start", label, name=spec.name, role=spec.role)
            if reason is not None:
                # Stable-named boxes (the daemon's, doctor's) are exactly the
                # ones that accumulate stale registrations (#576).
                self._purge_stale_registrations(spec)
            self.cli.create(spec)
            created = Sandbox(self.cli, spec.name)
            self._apply_policy(spec)
            if reason is not None:
                self._apply_env_file_only(label, spec, created, token, reason)
            else:
                registered_secret_rms.extend(self._apply_secrets(spec, created, token))
                self._apply_persistent_env(spec, created)
                self._verify_secret_env(label, spec, created, token)
            created.mkdirs(JOBS_DIR, RESULTS_DIR, EVENTS_DIR, TOOLS_DIR)
            hook = post_create or self.post_create
            if hook is not None:
                hook(created, spec.role)
            self.bus.emit("sandbox.ready", label, name=spec.name, role=spec.role)
            return created
        except Exception as exc:
            log.warning(
                "sandbox.rollback",
                sandbox=name,
                secrets=len(registered_secret_rms),
                error=str(exc),
            )
            if created is not None:
                try:
                    created.rm()
                except SbxError:
                    log.warning("sandbox.rollback_remove_failed", sandbox=name, exc_info=True)
            for rm in registered_secret_rms:
                try:
                    if not rm():
                        log.warning("sandbox.rollback_secret_refused", sandbox=name)
                except SbxError:
                    log.warning("sandbox.rollback_secret_failed", sandbox=name, exc_info=True)
            if isinstance(exc, ProvisionError):
                raise
            raise ProvisionError(f"provisioning {name} failed: {exc}") from exc

    def _apply_policy(self, spec: SandboxSpec) -> None:
        self.cli.policy_allow(*spec.policy_allows, sandbox=spec.name)

    def _apply_secrets(
        self, spec: SandboxSpec, sandbox: Sandbox, token: str
    ) -> list[Callable[[], bool]]:
        """Register the spec's secrets with sbx (the proxy path), returning
        one rollback (rm) callable per registration this attempt actually
        created — so a provisioning failure can unregister them symmetric
        with sandbox removal, instead of leaving entries owned by a scope
        that no longer exists. Callers choose the in-VM env file instead
        via :meth:`_apply_env_file_only` (see :meth:`_env_file_reason`)."""
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

    def _env_file_reason(
        self, role: SandboxRole, gh_cred: GhCredential | None
    ) -> EnvFileReason | None:
        """Why ``role``'s credentials go to the in-VM env file (``None`` →
        try the sbx secret proxy)."""
        if self.config.secret_strategy == "plain-env":  # nosec B105 - strategy label
            return "strategy"
        if role == "github" and isinstance(gh_cred, GhApp):
            # Installation tokens rotate ~hourly and every refresh rewrites
            # the env file; registering each short-lived value with sbx
            # would be ceremony with no security upside.
            return "app"
        if self._proxy_cached_broken():
            return "cached"
        return None

    def _proxy_cached_broken(self) -> bool:
        """Whether the conformance cache already says this sbx version's
        proxy secrets never reach exec'd workers — in which case the
        register→probe→downgrade dance (and its per-run warning) is skipped
        and the non-proxy delivery is used directly. Unknown or unreadable
        answers count as "not known broken", so a new sbx version is probed
        exactly as before and the cache re-learns per version."""
        verdict = self._cached_verdict(PROBE_SECRET_ENV_VISIBILITY)
        return verdict is not None and verdict in _PROXY_BROKEN_VERDICTS

    def _cached_verdict(self, probe_id: str) -> str | None:
        """The conformance cache's verdict for this sbx version, or None.

        Read fresh on every call (only the sbx version lookup is memoized):
        a long-lived daemon whose first provision recorded a verdict
        benefits on its next re-provision, not its next restart."""
        try:
            with self._probe_lock:
                if not self._sbx_version_known:
                    self._sbx_version = self.cli.version()
                    self._sbx_version_known = True
                version = self._sbx_version
        except SbxError:
            return None  # undecided, not cached: probe as before
        record = load_verdicts(self.config.state_dir, version).get(probe_id)
        return record.verdict if record is not None else None

    def _stdin_env_supported(self, sandbox: Sandbox) -> bool:
        """Whether per-job stdin env delivery works on this sbx (#592).

        Answered from the version-keyed conformance cache when it can be;
        otherwise one field probe against ``sandbox`` exercises the exact
        launch shape WorkerClient uses and records the verdict. An sbx-level
        error is retried once and then answers False *without* recording —
        an infra blip must never cache "no stdin" (or worse, leave a version
        marked capable that never was), it just costs this provision the
        env-file path.
        """
        cached = self._cached_verdict(PROBE_EXEC_STDIN_ENV)
        if cached == VERDICT_STDIN_DELIVERS:
            return True
        if cached == VERDICT_STDIN_NO_DELIVERY:
            return False
        nonce = secrets.token_hex(8)
        cmd, payload, marker = stdin_env_probe(nonce)
        error = ""
        for _attempt in range(2):
            try:
                result = sandbox.exec(cmd, stdin=payload, timeout=60.0)
            except SbxError as exc:
                error = str(exc)
                continue
            break
        else:
            log.warning(
                "sandbox.stdin_env_probe_error",
                sandbox=sandbox.name,
                error=error,
                action="using the in-VM env file for this provision",
            )
            return False
        delivered = result.ok and marker in result.stdout
        self._record_probe(
            PROBE_EXEC_STDIN_ENV,
            VERDICT_STDIN_DELIVERS if delivered else VERDICT_STDIN_NO_DELIVERY,
            f"observed while provisioning {sandbox.name}",
        )
        return delivered

    def _apply_env_file_only(
        self,
        run_id: str,
        spec: SandboxSpec,
        sandbox: Sandbox,
        token: str,
        reason: EnvFileReason,
    ) -> None:
        """Deliver credentials without the sbx secret proxy, saying why.

        Delivery itself is decided by :meth:`_deliver_env` (#592): per-job
        stdin when this sbx version passes exec stdin through (nothing at
        rest in the VM), the 0600 in-VM env file otherwise. ``strategy``
        (explicit plain-env config) stays silent, exactly as before.
        ``cached`` emits the same ``sandbox.secret_env_fallback`` event the
        probe-driven downgrade emits — the semantic is identical, the
        decision just came from the conformance cache — but calmly (info
        log, ``cached=True``) rather than as a per-run warning. ``app``
        announces the App identity once per sandbox.
        """
        delivery = self._deliver_env(spec, sandbox, token)
        via_file = delivery == "env-file"
        env_name = self.agent_token_env() if spec.role == "agent" else "GH_TOKEN"
        how = (
            "using the in-VM env file directly"
            if via_file
            else "delivering it per job over the worker launch's stdin (nothing at rest in the VM)"
        )
        if reason == "cached":
            message = (
                f"{env_name}: sbx proxy secrets are invisible under exec on this sbx "
                f'version (cached conformance verdict) — {how} (secret_strategy="plain-env" '
                "silences this)"
            )
            log.info(
                "sandbox.secret_env_fallback",
                run=run_id,
                sandbox=spec.name,
                env=env_name,
                cached=True,
                delivery=delivery,
                detail=message,
            )
            self.bus.emit(
                "sandbox.secret_env_fallback",
                run_id,
                name=spec.name,
                env=env_name,
                cached=True,
                delivery=delivery,
                message=message,
            )
        elif reason == "app":
            refresh_home = "in the in-VM env file" if via_file else "on the host, per job"
            message = (
                "github sandbox authenticates as a GitHub App installation; the host "
                f"mints short-lived installation tokens and refreshes them {refresh_home}"
            )
            log.info("sandbox.github_app_auth", run=run_id, sandbox=spec.name, detail=message)
            self.bus.emit(
                "sandbox.github_app_auth",
                run_id,
                name=spec.name,
                delivery=delivery,
                message=message,
            )
        if via_file:
            # Only file delivery can lose to a stamped exec environment; a
            # per-job eval runs after the login shell's profile and wins.
            self._verify_env_file_unshadowed(run_id, spec, sandbox)

    def _deliver_env(self, spec: SandboxSpec, sandbox: Sandbox, token: str) -> str:
        """Choose and apply non-proxy credential delivery; returns the mode.

        ``"stdin"``: this sbx version passes exec stdin through to the in-VM
        launch shell (field-probed, verdict cached per version), so nothing
        is written — every job's exports are piped fresh by WorkerClient
        (see :meth:`job_env`) and the credential never touches the sandbox
        filesystem. ``"env-file"``: today's 0600 in-VM env file, unchanged.
        """
        if self._stdin_env_supported(sandbox):
            return "stdin"
        self._apply_plain_env(spec, sandbox, token)
        return "env-file"

    def _purge_stale_registrations(self, spec: SandboxSpec) -> None:
        """Remove secret registrations parked at this sandbox's name (#576).

        Env-file delivery registers nothing of its own — but ``sbx rm``
        leaves sandbox-scoped registrations behind (rgn9ccjam), sbx stamps
        whatever *is* registered into the VM at create, and on sbx builds
        that stamp exec environments the leftover's shape-mimicking sentinel
        shadows the env file: the worker keeps what looks like a real token
        and the egress proxy rewrites it on the way out with the stale
        value. Field failure (db, 2026-08-30): after the GitHub App
        cutover, daemon ops were still attributed to the retired PAT held
        by a ``github`` service registration at the daemon box's stable
        name. Runs *before* ``create`` so the box never boots stamped;
        "nothing to remove" is the common case and returns quietly. An
        sbx-level removal failure propagates into a ProvisionError —
        provisioning a box that might act as the wrong identity is worse
        than not provisioning it.
        """
        for secret in spec.secrets:
            if secret.kind == "service":
                assert secret.service is not None
                removed = self.cli.secret_rm(service=secret.service, sandbox=spec.name)
            else:
                assert secret.host is not None and secret.env is not None
                removed = self._rm_custom(host=secret.host, env=secret.env, scope=spec.name)
            if removed:
                log.info(
                    "sandbox.stale_registration_purged",
                    sandbox=spec.name,
                    kind=secret.kind,
                    service=secret.service,
                    env=secret.env,
                )

    def _verify_env_file_unshadowed(self, run_id: str, spec: SandboxSpec, sandbox: Sandbox) -> None:
        """No foreign credential may outrank the env file on a github box.

        After the purge nothing should be stamping GH_TOKEN/GITHUB_TOKEN
        into exec environments at all — but a registration the purge cannot
        touch (a *global*-scope ``github`` service secret, or an sbx build
        that stamps from somewhere new) would still shadow the env file:
        the worker keeps any credential-shaped value it finds (a
        shape-mimicking ``gho_…`` proxy sentinel included) and the egress
        proxy rewrites it with the stale secret. That failure mode is
        silent — ops succeed, as the wrong identity — so it must be caught
        here, loudly, at provision time (#576).

        The value is classified in the shell and never read back. A plain
        ``sbx-cs-…`` sentinel is fine (the worker overrides those); a clean
        empty answer is the expected case. This is a guard behind the
        purge, not the primary defense: a probe that cannot get a clean
        answer logs and proceeds rather than failing provisioning.
        """
        if spec.role != "github":
            return
        probe = [
            "sh",
            "-lc",
            'for v in "$GH_TOKEN" "$GITHUB_TOKEN"; do [ -n "$v" ] || continue; '
            # a proxy placeholder (either shape) is not a shadow: the worker
            # overrides sentinels, so the env file still wins.
            f'case "$v" in {shell_sentinel_case()}) : ;; '
            f"{shell_token_case()}) exit {_SHADOW_EXIT} ;; esac; done; exit 0",
        ]
        error = ""
        for _attempt in range(2):
            try:
                result = sandbox.exec(probe)
            except SbxError as exc:
                error = str(exc)
                continue
            if result.returncode in (0, _SHADOW_EXIT):
                break
            error = f"probe exited {result.returncode}: {result.stderr.strip()}"
        else:
            log.warning("sandbox.shadow_probe_error", run=run_id, sandbox=spec.name, error=error)
            return
        if result.returncode == 0:
            return
        message = (
            "exec environment already carries a credential-shaped GH_TOKEN/GITHUB_TOKEN "
            "that would shadow the in-VM env file — a stale sbx secret registration "
            "(possibly global scope) still stamps this box. Remove it "
            f"(`sbx secret rm github --sandbox {spec.name} -f`, and check `sbx secret ls` "
            "for a global github entry) and retry; refusing to run as the wrong identity"
        )
        self.bus.emit("sandbox.credential_shadowed", run_id, name=spec.name, message=message)
        raise ProvisionError(f"{spec.name}: {message}")

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

        There is a third answer, and missing it cost a live outage
        (2026-08-21): sbx can export its proxy *sentinel* (``sbx-cs-...``)
        into the exec environment. That is non-empty, so a `test -n` probe
        called it visible and skipped the fallback - but no GitHub client
        can authenticate with a sentinel, so the agent got a token-shaped
        hole. The probe therefore asks what the consumer asks, whether the
        value looks like a credential, and treats a sentinel exactly like an
        absent one.

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
        env_name = self.agent_token_env() if spec.role == "agent" else "GH_TOKEN"
        # 0 = a usable credential, 1 = unset/empty, SENTINEL_EXIT = set but
        # not a credential (sbx's proxy placeholder). Classifying in the shell
        # keeps the value inside the VM - it is never read back out here.
        probe = [
            "sh",
            "-lc",
            f'v="${{{env_name}}}"; [ -n "$v" ] || exit 1; '
            # sentinel shapes FIRST: the service-secret mimic matches the
            # token prefixes too and must never classify as usable.
            f'case "$v" in {shell_sentinel_case()}) exit {_SENTINEL_EXIT} ;; '
            f"{shell_token_case()}) exit 0 ;; *) exit {_SENTINEL_EXIT} ;; esac",
        ]
        clean = (0, 1, _SENTINEL_EXIT)
        error = ""
        for _attempt in range(2):
            try:
                result = sandbox.exec(probe)
            except SbxError as exc:
                error = str(exc)
                continue
            if result.returncode in clean:
                break
            error = (
                f"probe exited {result.returncode} (expected one of {clean}): "
                f"{result.stderr.strip()}"
            )
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
        sentinel = result.returncode == _SENTINEL_EXIT
        if result.ok:
            verdict = "visible-under-exec"
        elif sentinel:
            verdict = "sentinel-under-exec"
        else:
            verdict = "invisible-under-exec"
        self._record_probe(
            PROBE_SECRET_ENV_VISIBILITY,
            verdict,
            f"observed while provisioning {spec.name} ({env_name})",
        )
        if result.ok:
            return
        # Auto-heal: fall back to non-proxy delivery for this sandbox —
        # per-job stdin when this sbx passes it through (the value transits
        # worker process memory only), the plain-env file otherwise (the
        # value becomes visible inside the microVM, which the agent already
        # controls). Egress remains bounded by the network policy either way.
        seen = (
            "sbx proxy secret is a sentinel under exec, which no GitHub client accepts"
            if sentinel
            else "sbx proxy secret invisible to exec"
        )
        delivery = self._deliver_env(spec, sandbox, token)
        how = (
            "using in-VM env file"
            if delivery == "env-file"
            else "delivering per job over the worker launch's stdin"
        )
        message = f'{env_name}: {seen} — {how} (secret_strategy="plain-env" silences this)'
        # A security-relevant downgrade, not routine progress.
        log.warning(
            "sandbox.secret_env_fallback",
            run=run_id,
            sandbox=spec.name,
            env=env_name,
            delivery=delivery,
            detail=message,
        )
        self.bus.emit(
            "sandbox.secret_env_fallback",
            run_id,
            name=spec.name,
            env=env_name,
            delivery=delivery,
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
            log.warning("mount.marker_write_failed", run=run_id, workspace=str(workspace))
            return None
        probe_error = ""
        try:
            command = mount_probe_command(workspace, marker)
            result = sandbox.exec(["sh", "-c", command])
            hit = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
            if not result.ok:
                # find's own errors are discarded by the pipeline, so a
                # nonzero exit means the probe itself broke — not "no mount".
                probe_error = f"probe exited {result.returncode}: {result.stderr.strip()}"
        except SbxError as exc:
            log.warning("mount.probe_failed", run=run_id, sandbox=sandbox.name, exc_info=True)
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

    def _apply_persistent_env(self, spec: SandboxSpec, sandbox: Sandbox) -> None:
        """Write the spec's non-secret environment into the VM's env file.

        Under ``plain-env`` the token writer already emitted these exports,
        so this is the ``proxy`` (default) strategy's path: the repository a
        github-ops sandbox is scoped to has to reach the worker, and the env
        file is what the worker loads at startup. Nothing secret goes here.
        """
        if not spec.persistent_env or self.config.secret_strategy == "plain-env":  # nosec B105
            return
        lines = "".join(
            f"export {key}={shlex.quote(value)}\n"
            for key, value in sorted(spec.persistent_env.items())
        )
        sandbox.exec(["mkdir", "-p", ENV_FILE.rsplit("/", 1)[0]])
        sandbox.write_text(ENV_FILE, lines)
        sandbox.exec(["chmod", "600", ENV_FILE])

    def _write_env_file(self, sandbox: Sandbox, exports: Mapping[str, str]) -> None:
        """(Re)write the in-VM env file the worker loads at startup."""
        lines = "".join(
            f"export {key}={shlex.quote(value)}\n" for key, value in sorted(exports.items())
        )
        sandbox.exec(["mkdir", "-p", ENV_FILE.rsplit("/", 1)[0]])
        sandbox.write_text(ENV_FILE, lines)
        sandbox.exec(["chmod", "600", ENV_FILE])

    def _apply_plain_env(self, spec: SandboxSpec, sandbox: Sandbox, token: str) -> None:
        """Weaker fallback: write tokens/env into ~/.sbxloop/env.sh in the VM."""
        exports: dict[str, str] = dict(spec.persistent_env)
        if spec.role == "agent":
            exports[self.agent_token_env()] = token
        else:
            exports["GH_TOKEN"] = token
            exports["GITHUB_TOKEN"] = token
        self._write_env_file(sandbox, exports)

    def gh_refresher(self, sandbox: Sandbox, repo: str | None = None) -> Callable[[], None] | None:
        """A hook keeping a live github sandbox's credential fresh (App mode).

        ``None`` for PAT credentials — nothing to refresh, zero per-job
        overhead — and under per-job stdin delivery (#592): there the token
        never lives in the VM at all, and :meth:`job_env`'s provider calls
        ``cred.token()`` per job, which re-mints inside the refresh margin
        by itself. For a GitHub App on the env-file path, the returned
        callable is what WorkerClient invokes before each job: when the
        cached installation token is inside its refresh margin it re-mints
        and rewrites the in-VM env file, so the job's worker process
        (spawned per job; loads the env file at startup) authenticates with
        a live token. This is what lets a run — or the daemon's long-lived
        polling sandbox — outlive the ~1 hour installation token lifetime
        (#568).
        """
        cred = self.gh_credential(repo)
        if not isinstance(cred, GhApp):
            return None
        if self._cached_verdict(PROBE_EXEC_STDIN_ENV) == VERDICT_STDIN_DELIVERS:
            return None
        persistent = self.github_repo_env(repo)

        def refresh() -> None:
            if not cred.source.refresh_due():
                return
            token = cred.source.current()
            self._write_env_file(sandbox, {**persistent, "GH_TOKEN": token, "GITHUB_TOKEN": token})
            log.info("github.app_token_refreshed", sandbox=sandbox.name)

        return refresh

    def job_env(
        self,
        role: SandboxRole,
        repo: str | None = None,
        *,
        sandbox: Sandbox | None = None,
    ) -> Callable[[], dict[str, str]] | None:
        """A per-job env provider for stdin delivery, or ``None`` (#592).

        ``None`` means this sandbox's credentials arrive another way — the
        sbx secret proxy, or the in-VM env file — and WorkerClient launches
        jobs exactly as before. A provider is returned only when both hold:

        - the sandbox's credentials do not go through the sbx proxy (the
          same :meth:`_env_file_reason` decision provisioning made), and
        - this sbx version passes exec stdin through to the launch shell
          (the ``exec-stdin-env`` verdict, normally cached by the provision
          that just ran; ``sandbox`` lets a caller reusing a long-lived box
          across a wiped cache re-probe instead of silently losing auth).

        The provider computes exports fresh per job — ``cred.token()``
        re-mints an App installation token inside its refresh margin — so
        rotating credentials need nothing rewritten anywhere.
        """
        gh_cred = self.gh_credential(repo) if role == "github" else None
        if self._env_file_reason(role, gh_cred) is None:
            return None
        supported = (
            self._stdin_env_supported(sandbox)
            if sandbox is not None
            else self._cached_verdict(PROBE_EXEC_STDIN_ENV) == VERDICT_STDIN_DELIVERS
        )
        if not supported:
            return None
        if role == "agent":
            return lambda: {
                **self.agent_persistent_env(),
                self.agent_token_env(): self.agent_token(),
            }
        cred = gh_cred
        assert cred is not None
        persistent = self.github_repo_env(repo)

        def provide() -> dict[str, str]:
            token = cred.token()
            return {**persistent, "GH_TOKEN": token, "GITHUB_TOKEN": token}

        return provide
