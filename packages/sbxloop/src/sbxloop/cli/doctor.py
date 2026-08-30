"""sbxloop doctor: host readiness checks plus the sbx conformance suite.

Readiness checks (binary, login, tokens, ...) say whether this host can run
sbxloop at all. The conformance section reruns the probe catalog from
:mod:`sbxloop.sbx.conformance` — every field-learned assumption about sbx
semantics — and warns loudly when an sbx upgrade flips a verdict a code path
depends on. Cheap probes run every time; ``--deep`` boots a scratch sandbox
for the full suite and refreshes the version-keyed verdict cache.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

import sbxloop
from sbxloop.config import Config, RepoConfig, load_config, load_config_with_sources
from sbxloop.engine.store import StateStore
from sbxloop.errors import SbxError, SbxNotFoundError
from sbxloop.sbx.bake import load_bake_record
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.conformance import ConformanceReport, run_conformance
from sbxloop.sbx.provision import AGENT_TOKEN_HOSTS, gh_credential_status
from sbxloop.sbx.prune import count_orphans
from sbxloop.sbx.secretstate import COPILOT_TOKEN_ENV
from sbxloop.worker.wheel import resolve_worker_wheel
from sbxloop_worker.backends.copilot import (
    SDK_PERMISSION_KINDS,
    installed_sdk_permission_kinds,
)

if TYPE_CHECKING:
    from sbxloop.daemon.github import DaemonGithub

TESTED_SBX_SERIES = "0.38"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hard: bool = True


ProgressFn = Callable[[str], None]


def _sdk_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("github-copilot-sdk")
    except PackageNotFoundError:
        return "unknown version"


RepoProbeFn = Callable[["RepoConfig"], "RepoProbe"]


class RepoProbeUnavailable(Exception):
    """The probe could not ask GitHub at all (no github sandbox).

    Distinct from a probe answering "not reachable": the repository is
    unverified, which is the pre-probe behaviour (a soft row), not a
    verdict against the repository.
    """


@dataclass
class RepoProbe:
    """What an out-of-process probe learned about one repository.

    ``reachable`` is the repository lookup (404/permission denied → False);
    ``missing_permissions`` lists the scopes the token lacks; ``creatable``
    is only meaningful for a repo configured with ``create_repo``.
    """

    reachable: bool
    detail: str = ""
    missing_permissions: tuple[str, ...] = ()
    creatable: bool | None = None


def _repo_token_status(entry: RepoConfig, env: dict[str, str]) -> tuple[bool, str]:
    """Whether this repository has a usable credential, and which — a PAT
    (its own ``token_env`` or the daemon-wide GH_TOKEN) or the GitHub App
    installation (#568)."""
    status = gh_credential_status(env, token_env=entry.token_env)
    return status.ok, status.detail


def daemon_repo_health(
    config: Config, sources: dict[str, str], env: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Per-repository polling health the running daemon persisted (#516),
    by repository, from the daemon's own state db — read only when that
    file exists, so doctor never creates one."""
    from sbxloop.daemon.paths import resolve_state_dir
    from sbxloop.daemon.sources import REPO_HEALTH_KEY
    from sbxloop.daemon.store import DaemonStore

    try:
        state_dir = resolve_state_dir(
            config, sources, cwd=Path.cwd(), env=env, home=Path.home()
        ).path
    except Exception:
        return {}
    db = state_dir / "state.db"
    if not db.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        store = DaemonStore(db)
        try:
            for key, value in store.values_with_prefix(REPO_HEALTH_KEY).items():
                try:
                    data = json.loads(value)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    out[key[len(REPO_HEALTH_KEY) :].casefold()] = data
        finally:
            store.close()
    except Exception:  # a store doctor cannot read is its own row elsewhere
        return {}
    return out


def repo_checks(
    config: Config,
    env: dict[str, str],
    *,
    probe: RepoProbeFn | None = None,
    health: dict[str, dict[str, Any]] | None = None,
) -> list[Check]:
    """One :class:`Check` per configured repository.

    Each repository is verified on its own — credentials, base branch,
    ``create_repo`` intent and, when ``probe`` is supplied (the host never
    holds the PAT itself, so reachability is probed through the github
    sandbox), reachability and token permissions. A repository that fails,
    or whose probe raises, yields exactly one failing row and never masks
    the verdict of the others.
    """
    rows: list[Check] = []
    for entry in config.github.repo_list():
        effective = config.github.effective_repo(entry.repo) or entry
        name = f"github repo {entry.repo}"
        if not entry.enabled:
            rows.append(
                Check(name, True, "disabled in sbxloop.toml — not polled, not run", hard=False)
            )
            continue
        token_ok, token_detail = _repo_token_status(entry, env)
        base = effective.deliver_base or "(repo default)"
        notes = [token_detail, f"base {base}"]
        state = (health or {}).get(entry.repo.casefold())
        if state and state.get("suspended"):
            rows.append(
                Check(
                    name,
                    True,
                    "; ".join(
                        [
                            *notes,
                            f"SUSPENDED from polling by the daemon: {state.get('reason')} "
                            f"— `sbxloop daemon ctl resume-repo {entry.repo}` once fixed",
                        ]
                    ),
                    hard=False,
                )
            )
            continue
        if state and state.get("next_poll"):
            notes.append(f"backing off after {state.get('failures')} poll failure(s)")
        if effective.create_repo:
            notes.append("create_repo on")
        if not token_ok:
            rows.append(Check(name, False, "; ".join(notes)))
            continue
        if probe is None:
            notes.append(
                "reachability unverified from the host (checked in the github sandbox — "
                "`sbxloop doctor --probe` boots one to ask)"
            )
            rows.append(Check(name, True, "; ".join(notes), hard=False))
            continue
        try:
            result = probe(effective)
        except RepoProbeUnavailable as exc:
            notes.append(f"reachability unverified: {exc}")
            rows.append(Check(name, True, "; ".join(notes), hard=False))
            continue
        except Exception as exc:  # one repo's failure must not hide the rest
            rows.append(Check(name, False, "; ".join([*notes, f"probe failed: {exc}"])))
            continue
        if not result.reachable:
            if effective.create_repo and result.creatable:
                notes.append("missing but create_repo is on — it will be created")
                rows.append(Check(name, True, "; ".join(notes), hard=False))
                continue
            notes.append(result.detail or "not reachable with this token")
            rows.append(Check(name, False, "; ".join(notes)))
            continue
        if result.missing_permissions:
            notes.append(f"token missing {', '.join(result.missing_permissions)}")
            rows.append(Check(name, False, "; ".join(notes)))
            continue
        notes.append(result.detail or "reachable, token has the required permissions")
        rows.append(Check(name, True, "; ".join(notes)))
    return rows


@dataclass
class WorkspaceOriginMismatch:
    """An enabled repo whose effective workspace belongs to another repo."""

    repo: str
    path: Path
    origin_repo: str

    @property
    def message(self) -> str:
        return (
            f"workspace {self.path} is a checkout of {self.origin_repo}, not {self.repo} — "
            f"runs for {self.repo} would be built from another repository's tree; "
            f"move [sandbox] workspace into the matching [[github.repos]] entry, or set "
            f'workspace = "..." on the {self.repo} entry'
        )


def workspace_origin_mismatches(config: Config) -> list[WorkspaceOriginMismatch]:
    """Every enabled repo whose workspace origin names a different repository.

    The workspace checked is the resolved one
    (:meth:`Config.workspace_for_repo`) and, where resolution declined to
    hand one back, the raw configured ``[sandbox] workspace``: declining is
    what keeps a run out of the wrong tree, but the operator still has a
    misconfiguration to fix and must be told about it here rather than at
    dispatch.
    """
    from sbxloop import hostgit

    mismatches: list[WorkspaceOriginMismatch] = []
    for entry in config.github.enabled_repos():
        path = config.workspace_for_repo(entry.repo)
        if path is None:
            if entry.workspace is not None:
                path = entry.workspace.expanduser()
            elif config.sandbox.workspace is not None:
                path = config.sandbox.workspace.expanduser()
            else:
                continue
        origin = hostgit.normalise_repo_url(hostgit.origin_url(path))
        if origin is None:
            # Not a git checkout, or an origin we cannot read as owner/name:
            # nothing to contradict, so nothing to fail on here.
            continue
        expected = hostgit.normalise_repo_url(entry.repo)
        if expected is not None and origin != expected:
            mismatches.append(WorkspaceOriginMismatch(entry.repo, path, origin))
    return mismatches


def workspace_origin_checks(config: Config) -> list[Check]:
    """One hard-failing :class:`Check` per origin mismatch."""
    return [
        Check(f"workspace for {m.repo}", False, m.message)
        for m in workspace_origin_mismatches(config)
    ]


REQUIRED_REPO_PERMISSIONS = ("issues", "contents", "pull_requests")


def _missing_permissions(data: dict[str, object]) -> tuple[str, ...]:
    """Permissions the token lacks, from a ``GET /repos/{repo}`` payload.

    GitHub reports the *authenticated* token's effective access on the
    repository as ``permissions: {admin, maintain, push, triage, pull}``.
    sbxloop needs to read the tree, open pull requests and drive issue
    labels/comments — all of which are the ``push`` (write) level; anything
    less can only read.
    """
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        return ()  # not reported (some app tokens) — do not invent a failure
    if perms.get("admin") or perms.get("maintain") or perms.get("push"):
        return ()
    return tuple(f"{kind}:write" for kind in REQUIRED_REPO_PERMISSIONS)


def credential_key(entry: RepoConfig) -> str:
    """Which credential a repository's github box would be provisioned with:
    its own ``token_env``, or ``""`` for the daemon-wide token. Repositories
    sharing a key can share one sandbox (#515)."""
    return entry.token_env or ""


def sandbox_repo_probe(
    config: Config,
    cli: SbxCLI,
    *,
    boxes: dict[str, DaemonGithub] | None = None,
) -> RepoProbeFn:
    """A probe that asks GitHub itself, from a github-ops sandbox.

    The host deliberately never holds the PAT, so reachability and token
    permissions cannot be checked here: a short-lived github-only sandbox
    does the ``repo.get``. One sandbox **per distinct credential**, not per
    repository (#515): the common deployment has every repository on the
    daemon-wide token, and a doctor that booted a microVM per repository
    scaled its wall clock with the repo count for nothing. ``boxes``
    collects the sandboxes, keyed by credential, so the caller can tear
    them down. A credential whose sandbox could not be provisioned answers
    "unverified" for every repository on it, once — never re-provisioned.
    """
    from sbxloop.daemon.github import DaemonGithub
    from sbxloop.events import EventBus

    opened = boxes if boxes is not None else {}
    unavailable: dict[str, str] = {}

    def probe(entry: RepoConfig) -> RepoProbe:
        key = credential_key(entry)
        if key in unavailable:
            raise RepoProbeUnavailable(unavailable[key])
        box = opened.get(key)
        if box is None:
            box = DaemonGithub(
                config,
                cli,
                EventBus(),
                worker_python=config.worker_python,
                name=f"sbxloop-doctor-{key or 'default'}".lower()[:60],
                # Credentials are scoped by repository; the first repository
                # on this credential names it, and every other one shares it.
                repo=entry.repo,
            )
            opened[key] = box
        try:
            ops = box.ops()
        except Exception as exc:  # no sandbox: unverified, not a verdict
            unavailable[key] = f"no github sandbox ({_clean(str(exc), 120)})"
            raise RepoProbeUnavailable(unavailable[key]) from exc
        data = ops.repo_lookup(entry.repo)
        if data is None:
            creatable: bool | None = None
            if entry.create_repo:
                # Only the create path cares; asked as a question so a "no"
                # is data rather than a failed job.
                creatable = True
            return RepoProbe(
                reachable=False, detail="not found with this token", creatable=creatable
            )
        missing = _missing_permissions(data)
        return RepoProbe(
            reachable=True,
            detail="reachable, token has the required permissions" if not missing else "reachable",
            missing_permissions=missing,
        )

    return probe


def collect_checks(
    env: dict[str, str],
    cli: SbxCLI | None = None,
    progress: ProgressFn | None = None,
    probe_repo: RepoProbeFn | None = None,
) -> list[Check]:
    config, sources = load_config_with_sources(env=env)
    cli = cli or SbxCLI(app_name=config.app_name or None)
    checks: list[Check] = []
    report = progress or (lambda _message: None)

    # sbx binary + version. The very first sbx invocation may trigger
    # Docker's interactive browser login if this sbx application state has
    # never authenticated - say so instead of appearing hung.
    report("checking sbx binary (Docker may open a browser window for authentication on first use)")
    try:
        version = cli.version()
    except SbxNotFoundError:
        checks.append(
            Check(
                "sbx binary",
                False,
                "sbx not found on PATH — install Docker Sandboxes: "
                "https://docs.docker.com/ai/sandboxes/",
            )
        )
        version = None
    else:
        detail = f"found sbx {version or '(unknown version)'}"
        if version and not version.startswith(TESTED_SBX_SERIES):
            detail += f" (sbxloop is tested against {TESTED_SBX_SERIES}.x; parsing may drift)"
        checks.append(Check("sbx binary", True, detail))

    if version is not None:
        # login / daemon reachable
        report("checking sbx login")
        logged_in = False
        try:
            cli.ls()
            logged_in = True
            checks.append(Check("sbx login", True, "sbx ls succeeded"))
        except SbxError as exc:
            login_cmd = (
                f"sbx --app-name {config.app_name} login" if config.app_name else "sbx login"
            )
            checks.append(Check("sbx login", False, f"sbx ls failed ({exc}); run `{login_cmd}`"))

        # orphaned sandboxes (leaked pairs from crashed/killed runs)
        if logged_in:
            report("checking for orphaned sandboxes")
            try:
                orphans = count_orphans(cli, StateStore(config.state_dir / "state.db"))
            except (SbxError, OSError, sqlite3.Error):
                orphans = None
            if orphans is not None:
                checks.append(
                    Check(
                        "orphaned sandboxes",
                        orphans == 0,
                        "none found"
                        if orphans == 0
                        else f"{orphans} orphan candidate(s) — run `sbxloop sandbox prune`",
                        hard=False,
                    )
                )

        # network policy reachable for the copilot hosts
        for host in AGENT_TOKEN_HOSTS:
            report(f"checking network policy for {host}")
            try:
                allowed = cli.policy_check(host)
                detail = (
                    "reachable"
                    if allowed
                    else (
                        "blocked — run `sbx policy init balanced` and/or "
                        f"`sbx policy allow network {host}`"
                    )
                )
            except SbxError as exc:
                # The check itself broke; that is not a policy verdict.
                allowed = False
                detail = f"policy check errored (not a policy answer): {exc}"
            checks.append(
                Check(
                    f"policy: {host}",
                    allowed,
                    detail,
                    hard=False,  # per-sandbox allows are applied at provision time
                )
            )

        # Host-side resource stats: sbx 0.35.x has no stats command, so
        # resource telemetry samples in-VM on the worker heartbeat. This is
        # the field-check for the day sbx grows one (issue #54) — purely
        # informational either way.
        report("checking for host-side sandbox stats")
        stats_available = False
        try:
            stats_available = cli.run("stats", "--help", check=False).returncode == 0
        except SbxError:
            stats_available = False
        checks.append(
            Check(
                "sandbox stats",
                True,
                "host-side `sbx stats` available — sbxloop still samples in-VM; "
                "consider filing an issue to prefer host-side stats"
                if stats_available
                else "no host-side `sbx stats`; resource telemetry samples in-VM "
                "on the worker heartbeat",
                hard=False,
            )
        )

    # prebaked template freshness: a stale template never breaks a run
    # (provisioning falls back to the install ladder), so these are warns.
    template = config.sandbox.template
    if template:
        record = load_bake_record(config)
        if record is not None and record.ref == template:
            fresh = record.worker_version == sbxloop.__version__
            checks.append(
                Check(
                    "sandbox template",
                    fresh,
                    f"{template} baked with worker {record.worker_version}"
                    if fresh
                    else f"{template} is stale: baked worker {record.worker_version}, host "
                    f"is {sbxloop.__version__} — run `sbxloop bake` (runs fall back to "
                    "the install ladder meanwhile)",
                    hard=False,
                )
            )
            # git is baseline agent tooling (#252): a template without it
            # costs an apt top-up on every provision, and loses git outright
            # wherever apt is unreachable — worth a row, never a hard fail.
            if record.git is None:
                git_detail = (
                    "not recorded by this bake (older sbxloop) — provisioning probes and "
                    "installs git per run; re-run `sbxloop bake` to carry it in the template"
                )
            elif record.git:
                git_detail = "git on PATH in the baked template"
            else:
                git_detail = (
                    "git missing from the baked template — provisioning tries an apt "
                    "install per run; check the apt mirrors are reachable and re-run "
                    "`sbxloop bake`"
                )
            checks.append(Check("git in template", record.git is not False, git_detail, hard=False))
        else:
            checks.append(
                Check(
                    "sandbox template",
                    True,
                    f"{template} was not baked on this host; provisioning verifies it and "
                    "falls back to the install ladder if needed (`sbxloop bake` builds one)",
                    hard=False,
                )
            )
        if version is not None:
            report("checking sbx template list")
            repo = template.split(":", 1)[0]
            try:
                listed = repo in cli.template_ls()
            except SbxError:
                listed = False
            checks.append(
                Check(
                    "template available",
                    listed,
                    "listed by `sbx template ls`"
                    if listed
                    else f"{repo} not in `sbx template ls` — run `sbxloop bake` "
                    "(or pull/load the template) before running",
                    hard=False,
                )
            )

    # tokens
    checks.append(
        Check(
            COPILOT_TOKEN_ENV,
            bool(env.get(COPILOT_TOKEN_ENV)),
            "set"
            if env.get(COPILOT_TOKEN_ENV)
            else 'not set — create a fine-grained PAT with the "Copilot Requests" '
            f"permission and export {COPILOT_TOKEN_ENV}",
        )
    )
    # A github credential matters only when the GitHub integration is
    # configured; an unconfigured integration is a valid (GitHub-less)
    # setup, not a failure. A PAT or GitHub App credentials both satisfy it;
    # both at once, or a partial App set, is a named failure (#568).
    if config.github.enabled:
        cred = gh_credential_status(env)
        configured = ", ".join(r.repo for r in config.github.repo_list()) or str(config.github.repo)
        checks.append(
            Check(
                "github credentials",
                cred.ok,
                f"{cred.detail} (github integration: {configured})"
                if cred.ok
                else f"{cred.detail} — github repositories {configured} are "
                "configured: create a fine-grained PAT (issues:write, "
                "contents:read, ...) and export GH_TOKEN, or set GITHUB_APP_ID, "
                "GITHUB_APP_INSTALLATION_ID and GITHUB_APP_PRIVATE_KEY[_PATH]",
            )
        )
        if cred.mode == "app" and cred.ok:
            has_openssl = shutil.which("openssl") is not None
            checks.append(
                Check(
                    "openssl (github app auth)",
                    has_openssl,
                    "found on PATH — App JWTs are signed with the host openssl"
                    if has_openssl
                    else "not found on PATH — GitHub App auth cannot sign its "
                    "JWTs; install openssl or switch to a PAT",
                )
            )
        # One row per configured repository: a repo whose credentials or
        # probe fail must not hide the verdict for the others.
        report("checking configured repositories")
        checks.extend(
            repo_checks(
                config, env, probe=probe_repo, health=daemon_repo_health(config, sources, env)
            )
        )
        # A workspace that is a checkout of another repository would build a
        # repo's runs from the wrong tree (#526): refuse before dispatch.
        checks.extend(workspace_origin_checks(config))
    else:
        checks.append(
            Check(
                "github integration",
                True,
                'not configured — GitHub features disabled (set [github] repo = "owner/repo" '
                "in sbxloop.toml to enable)",
                hard=False,
            )
        )

    # copilot SDK permission-kind vocabulary: the worker's read-only critic
    # barrier is an allowlist over these kinds and fails closed on drift, so
    # a vocabulary change never grants write access — but it can silently
    # cost the critic a read capability. Surface drift here on SDK bumps
    # instead of as degraded reviews in the field.
    sdk_kinds = installed_sdk_permission_kinds()
    if sdk_kinds is None:
        checks.append(
            Check(
                "copilot sdk permission kinds",
                True,
                "github-copilot-sdk not installed on this host — vocabulary "
                "unverifiable here (checked where the SDK runs, e.g. e2e); the "
                "read-only barrier fails closed on unknown kinds regardless",
                hard=False,
            )
        )
    elif sdk_kinds == SDK_PERMISSION_KINDS:
        checks.append(
            Check(
                "copilot sdk permission kinds",
                True,
                f"installed SDK ({_sdk_version()}) matches the verified "
                f"vocabulary ({len(sdk_kinds)} kinds)",
            )
        )
    else:
        added = ", ".join(sorted(sdk_kinds - SDK_PERMISSION_KINDS)) or "none"
        removed = ", ".join(sorted(SDK_PERMISSION_KINDS - sdk_kinds)) or "none"
        checks.append(
            Check(
                "copilot sdk permission kinds",
                False,
                f"installed SDK ({_sdk_version()}) drifted from the verified "
                f"vocabulary — new kinds: {added}; missing kinds: {removed}. "
                "New kinds are denied in read-only critic sessions (fails "
                "closed); update READ_ONLY_ALLOWED_KINDS/SDK_PERMISSION_KINDS "
                "in sbxloop_worker.backends.copilot after re-verifying",
                hard=False,
            )
        )

    # daemon's chat bridge (only when a backend is configured)
    backend = config.chat_backend
    if backend is not None:
        problems: list[str] = []
        settings = config.chat_settings
        assert settings is not None
        if backend == "discord":
            try:
                import discord as _discordpy  # noqa: F401
            except ImportError:
                problems.append("discord.py missing (pip install 'sbxloop[discord]')")
            if not env.get("DISCORD_BOT_TOKEN"):
                problems.append("DISCORD_BOT_TOKEN not set")
            ready = "extra installed, token present"
        else:
            try:
                import slack_sdk as _slack_sdk  # noqa: F401
            except ImportError:
                problems.append("slack_sdk missing (pip install 'sbxloop[slack]')")
            for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
                if not env.get(name):
                    problems.append(f"{name} not set")
            ready = "extra installed, tokens present"
        checks.append(
            Check(
                f"chat bridge ({backend})",
                not problems,
                f"channel {settings.channel_ref}: " + ("; ".join(problems) if problems else ready),
                hard=False,
            )
        )
        # ...and its concierge: an agent session on the daemon host's behalf,
        # so the agent token must be here (the run pair needs it too, but a
        # chat-only operator may not have noticed).
        if config.concierge.enabled:
            has_token = bool(env.get("COPILOT_GITHUB_TOKEN"))
            checks.append(
                Check(
                    "chat concierge",
                    has_token,
                    f"model {config.concierge.model or config.model}, "
                    f"{config.concierge.timeout_s:.0f}s per message: "
                    + (
                        "COPILOT_GITHUB_TOKEN present"
                        if has_token
                        else "COPILOT_GITHUB_TOKEN not set (mentions will fail)"
                    ),
                    hard=False,
                )
            )

    # worker wheel
    wheel = resolve_worker_wheel()
    checks.append(
        Check(
            "worker wheel",
            True,
            f"resolved: {wheel.name}" if wheel else "no local wheel; will install from PyPI",
            hard=False,
        )
    )

    # state dir writable
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        probe = config.state_dir / ".doctor-probe"
        probe.write_text("ok")
        probe.unlink()
        checks.append(Check("state dir", True, str(config.state_dir.resolve())))
    except OSError as exc:
        checks.append(Check("state dir", False, f"not writable: {exc}"))

    legacy = _legacy_state_dir(config.state_dir, sources)
    if legacy is not None:
        checks.append(
            Check(
                "legacy state dir",
                False,
                f"{legacy} exists but state_dir is unconfigured, so runs, "
                f"status and logs now use {config.state_dir}; set "
                'state_dir = ".sbxloop" in sbxloop.toml to keep project-scoped '
                "state, or move it to the new location",
                hard=False,
            )
        )

    return checks


def _legacy_state_dir(state_dir: Path, sources: dict[str, str]) -> Path | None:
    """A ``./.sbxloop`` left by the former relative default that
    an unconfigured run would now silently ignore. Only reported when the
    default is in effect and points elsewhere: an explicit relative
    ``state_dir = ".sbxloop"`` is the supported project-scoped opt-in."""
    if sources.get("state_dir") != "default":
        return None
    candidate = Path.cwd() / ".sbxloop"
    if not candidate.is_dir() or candidate.resolve() == state_dir.resolve():
        return None
    return candidate


def _clean(detail: str, limit: int = 300) -> str:
    """Collapse whitespace/newlines so multi-line sbx stderr can't shatter
    the table layout; long tails are elided."""
    flat = " ".join(detail.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "\u2026"


def _age(checked_at: float | None) -> str:
    if checked_at is None:
        return ""
    delta = max(0.0, time.time() - checked_at)
    if delta < 3600:
        return f"{delta / 60:.0f}m ago"
    if delta < 86400:
        return f"{delta / 3600:.0f}h ago"
    return f"{delta / 86400:.0f}d ago"


def render_conformance(console: Console, report: ConformanceReport) -> None:
    table = Table(title=f"sbx conformance \u2014 sbx {report.version or '(unknown version)'}")
    table.add_column("probe", no_wrap=True)
    table.add_column("verdict", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("detail", overflow="fold")
    for outcome in report.outcomes:
        if outcome.source == "unprobed":
            status = "[dim]unprobed[/]"
            detail = "needs a live sandbox \u2014 run `sbxloop doctor --deep`"
        elif outcome.is_error:
            status = "[yellow]error[/]"
            detail = outcome.detail
        elif outcome.drifts:
            status = "[bold red]DRIFT[/]"
            detail = outcome.detail
        else:
            status = "[green]ok[/]"
            detail = outcome.detail
        if outcome.source == "cache":
            status += f" [dim](cached {_age(outcome.checked_at)})[/]"
        elif outcome.source == "provision":
            status += f" [dim](field {_age(outcome.checked_at)})[/]"
        table.add_row(outcome.probe.id, _clean(outcome.verdict, 60), status, _clean(detail))
    console.print(table)

    for outcome in report.drifted:
        for drift in outcome.drifts:
            console.print(
                f"[bold red]sbx drift[/] [bold]{outcome.probe.id}[/] = "
                f"{outcome.verdict!r}: {drift}",
                highlight=False,
            )
    if report.deep_run_hint:
        console.print(f"[bold yellow]{report.deep_run_hint}[/]", highlight=False)


def run_doctor(
    console: Console,
    env: dict[str, str] | None = None,
    *,
    deep: bool = False,
    fail_on_drift: bool = False,
    probe: bool = False,
) -> bool:
    """Run the host checks and the conformance suite; return readiness.

    ``probe`` asks GitHub itself about each configured repository from a
    github-ops sandbox — one microVM per distinct credential (#515). Off
    by default: the cheap doctor is what the deploy health step and a
    curious operator reach for, and it must stay instant; ``--deep``
    implies it, since that already boots a sandbox.

    ``fail_on_drift`` turns the conformance verdicts into a gate: any drift,
    probe error, unprobed seam, or a suite that could not run makes the
    result False. Interactive doctor keeps drift as a loud warning (the
    dependent code paths probe-don't-assume at runtime, so runs may still
    work); CI lanes whose whole job is catching sbx drift ahead of a field
    failure pass the flag (#226).
    """
    import os

    env = dict(os.environ) if env is None else env
    config = load_config(env=env)
    cli = SbxCLI(app_name=config.app_name or None)

    def progress(message: str) -> None:
        console.print(f"[dim]\u2026 {message}[/dim]", highlight=False)

    # Reachability and token permissions are answered by GitHub, and the
    # host has no token — so the probe runs in a github-ops sandbox, one per
    # distinct credential. Only when asked (or under --deep): the default
    # doctor provisions nothing. Torn down again before the table is printed.
    boxes: dict[str, DaemonGithub] = {}
    probe_repo = (
        sandbox_repo_probe(config, cli, boxes=boxes)
        if config.github.enabled and (probe or deep)
        else None
    )
    try:
        checks = collect_checks(env, cli=cli, progress=progress, probe_repo=probe_repo)
    finally:
        for box in boxes.values():
            box.close()
    table = Table(title="sbxloop doctor")
    table.add_column("check", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("detail", overflow="fold")
    for check in checks:
        status = (
            "[green]ok[/]" if check.ok else ("[red]FAIL[/]" if check.hard else "[yellow]warn[/]")
        )
        table.add_row(check.name, status, _clean(check.detail))
    console.print(table)
    ready = all(check.ok or not check.hard for check in checks)

    sbx_present = any(check.name == "sbx binary" and check.ok for check in checks)
    if not sbx_present:
        console.print("[dim]sbx conformance skipped: no usable sbx binary[/]", highlight=False)
        return ready and not fail_on_drift
    try:
        report = run_conformance(
            cli,
            config.state_dir,
            deep=deep,
            template=config.sandbox.template,
            progress=progress,
        )
    except SbxError as exc:
        console.print(f"[yellow]sbx conformance suite failed to run:[/] {_clean(str(exc))}")
        return ready and not fail_on_drift
    render_conformance(console, report)
    # By default drift is a loud warning, not a failure: the dependent code
    # paths all probe-don't-assume at runtime, so runs may still work \u2014 but
    # the verdict snapshot above is exactly what a bug report should include.
    if fail_on_drift and report.unverified:
        console.print(
            f"[bold red]sbx conformance gate failed:[/] {len(report.unverified)} probe(s) "
            f"drifted, errored, or are unprobed under sbx {report.version or '(unknown)'} "
            "(--fail-on-drift)",
            highlight=False,
        )
        return False
    return ready
