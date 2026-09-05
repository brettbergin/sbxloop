"""sbxloop doctor: host readiness checks plus the sbx conformance suite.

Readiness checks (binary, login, tokens, ...) say whether this host can run
sbxloop at all. The conformance section reruns the probe catalog from
:mod:`sbxloop.sbx.conformance` — every field-learned assumption about sbx
semantics — and warns loudly when an sbx upgrade flips a verdict a code path
depends on. Cheap probes run every time; ``--deep`` boots a scratch sandbox
for the full suite and refreshes the version-keyed verdict cache.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

import sbxloop
from sbxloop import toolchains
from sbxloop.backends import backend_for
from sbxloop.config import Config, MergeMethod, RepoConfig, load_config, load_config_with_sources
from sbxloop.engine.landing import allowed_merge_methods, resolve_merge_method
from sbxloop.engine.store import StateStore
from sbxloop.errors import GithubOpsError, SbxError, SbxNotFoundError
from sbxloop.gh.labels import lifecycle_specs, missing_labels
from sbxloop.gh.ops import GithubOps
from sbxloop.gh.permissions import (
    NEEDS,
    Need,
    missing_from_app,
    missing_from_scopes,
    split_required,
)
from sbxloop.gh.protection import read_base_requirements
from sbxloop.sbx.bake import load_bake_record
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.conformance import ConformanceReport, run_conformance
from sbxloop.sbx.provision import gh_credential_status
from sbxloop.sbx.prune import count_orphans
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


@dataclass(frozen=True)
class RepoCi:
    """What the repository's own Actions look like (#696): how many
    workflows are active, and the latest run on the delivery base."""

    workflows: int
    base: str = ""
    latest: str | None = None


@dataclass
class RepoProbe:
    """What an out-of-process probe learned about one repository.

    ``reachable`` is the repository lookup (404/permission denied → False);
    ``missing_permissions`` names each permission the token lacks that a
    run needs, with the feature that first needs it (#696) —
    ``optional_permissions`` the ones a repository may never need;
    ``creatable`` is only meaningful for a repo configured with
    ``create_repo``.
    """

    reachable: bool
    detail: str = ""
    missing_permissions: tuple[str, ...] = ()
    creatable: bool | None = None
    optional_permissions: tuple[str, ...] = ()
    # Where the permissions verdict came from: the classic PAT's scopes,
    # the App installation's grant, or a fine-grained PAT asked endpoint by
    # endpoint. Empty = not checked.
    permissions_source: str = ""
    # The repository's Actions, for the CI row. None = could not be listed.
    ci: RepoCi | None = None
    # The delivery base's rules the loop cannot satisfy (#673) — each one
    # 405s every loop merge. Empty = none known; None = unverifiable.
    base_blockers: tuple[str, ...] | None = None
    # The sources of the base's rules this token could not read (#674):
    # "protection" (classic, admin-only) and/or "rulesets".
    base_unread: tuple[str, ...] = ()
    # The merge methods the repository allows, in the loop's order of
    # preference (#620). None = the payload did not say.
    merge_methods: tuple[MergeMethod, ...] | None = None
    # The lifecycle (and follow-up) labels the repository does not carry
    # (#630) — `sbxloop init-repo` creates them. None = not listed.
    missing_labels: tuple[str, ...] | None = None
    # Whether the repository has Issues enabled (#631): the daemon polls
    # issues for work, and follow-ups are filed as issues. None = unknown.
    issues_enabled: bool | None = None


def _repo_token_status(entry: RepoConfig, env: dict[str, str]) -> tuple[bool, str]:
    """Whether this repository has a usable credential, and which — a PAT
    (its own ``token_env`` or the daemon-wide GH_TOKEN) or the GitHub App
    installation (#568)."""
    status = gh_credential_status(env, token_env=entry.token_env)
    return status.ok, status.detail


def _login_name() -> str:
    """Who runs the console by default; a uid where no login name resolves
    (a container running as an arbitrary user), never an exception."""
    try:
        return getpass.getuser()
    except (OSError, KeyError, ImportError):
        return str(os.getuid()) if hasattr(os, "getuid") else "operator"


def _daemon_state_path(config: Config, sources: dict[str, str], env: dict[str, str]) -> Path:
    """Where `sbxloop daemon` keeps its state — the same rule the daemon
    and `sbxloop tui` apply — falling back to the configured state dir."""
    from sbxloop.daemon.paths import resolve_state_dir

    try:
        return resolve_state_dir(config, sources, cwd=Path.cwd(), env=env, home=Path.home()).path
    except Exception:
        return config.state_dir


def daemon_repo_health(
    config: Config, sources: dict[str, str], env: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Per-repository polling health the running daemon persisted (#516),
    by repository, from the daemon's own state db — read only when that
    file exists, so doctor never creates one."""
    from sbxloop.daemon.sources import REPO_HEALTH_KEY
    from sbxloop.daemon.store import DaemonStore

    db = _daemon_state_path(config, sources, env) / "state.db"
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
            notes.append(
                "token missing "
                + "; ".join(result.missing_permissions)
                + f" — see docs/permissions.md{_source_note(result)}"
            )
            rows.append(Check(name, False, "; ".join(notes)))
            continue
        notes.append(result.detail or "reachable, token has the required permissions")
        merge_note, merge_row = _merge_method_status(name, config, result.merge_methods)
        notes.append(merge_note)
        rows.append(Check(name, True, "; ".join(notes)))
        if merge_row is not None:
            rows.append(merge_row)
        if result.optional_permissions:
            rows.append(
                Check(
                    f"{name} workflows",
                    False,
                    "token lacks "
                    + "; ".join(result.optional_permissions)
                    + f" — a run whose changes stay out of .github/workflows is unaffected; "
                    f"see docs/permissions.md{_source_note(result)}",
                    hard=False,
                )
            )
        if result.issues_enabled is False:
            rows.append(
                Check(
                    f"{name} issues",
                    False,
                    "Issues are disabled on this repository — the daemon polls issues "
                    "for work, so nothing can be queued here, and a run's follow-ups "
                    "are listed on its pull request instead of filed",
                    hard=False,
                )
            )
        if result.missing_labels:
            rows.append(
                Check(
                    f"{name} labels",
                    False,
                    "missing "
                    + ", ".join(f"`{label}`" for label in result.missing_labels)
                    + f" — `sbxloop init-repo {entry.repo}` creates them (with colors "
                    "and descriptions); GitHub attaches an unknown label name without "
                    "creating it, so the loop's states would show as bare text",
                    hard=False,
                )
            )
        if result.base_blockers:
            rows.append(
                Check(
                    f"{name} branch protection",
                    False,
                    "the delivery base has rules the loop cannot satisfy, so every merge "
                    "is refused (HTTP 405) and runs end blocked:\n"
                    + "\n".join(f"- {reason}" for reason in result.base_blockers),
                    hard=False,
                )
            )
        if result.base_unread:
            rows.append(Check(f"{name} required checks", True, _unread_note(result.base_unread)))
        if result.ci is not None:
            rows.append(_ci_row(name, result.ci))
    return rows


def _source_note(result: RepoProbe) -> str:
    return f" (per {result.permissions_source})" if result.permissions_source else ""


def _ci_row(name: str, ci: RepoCi) -> Check:
    """The repository's CI as the loop will meet it (#696): the CI stage
    waits for the check runs on the delivered head, so a repository with
    no workflow of its own has nothing to wait for unless another app
    reports checks."""
    if ci.workflows == 0:
        return Check(
            f"{name} ci",
            True,
            "no active Actions workflows — the CI stage has nothing of the repository's "
            "own to wait for and passes on the delivered head; check runs another app "
            "reports (a status check installed on the repository) still count",
            hard=False,
        )
    latest = (
        f"; latest run on {ci.base}: {ci.latest}" if ci.latest else f"; no run yet on {ci.base}"
    )
    return Check(f"{name} ci", True, f"{ci.workflows} active Actions workflow(s){latest}")


def _unread_note(unread: tuple[str, ...]) -> str:
    """What the loop does about a base whose rules this token cannot read
    (#674): the required checks come from each pull request's own rollup
    — what GitHub itself holds the merge for — so a bot with write but not
    admin still gates only on what the base requires."""
    if "protection" in unread:
        what = "classic branch protection is not readable with this token (GitHub needs admin)"
        if "rulesets" in unread:
            what += " and the rulesets could not be read either"
    else:
        what = "the base's rulesets could not be read"
    return (
        f"{what}; the required checks will be read from each pull request's own rollup, "
        "and a rule other than a check the base enforces shows up only as a blocked run"
    )


def _merge_method_status(
    name: str, config: Config, allowed: tuple[MergeMethod, ...] | None
) -> tuple[str, Check | None]:
    """How the loop will merge on this repository (#620): a note for the
    repo's row — what ``auto`` resolves to — and, when the configured
    method is one the repository refuses, a failing row of its own, so
    the operator hears it here rather than from a blocked run."""
    configured = config.landing.merge_method
    if allowed is None:
        return f"merge method {configured} (repository merge settings not reported)", None
    method, why = resolve_merge_method(configured, allowed)
    if method is None:
        return f"merge method {configured} not allowed", Check(
            f"{name} merge method", False, why, hard=False
        )
    if configured == "auto":
        return f"merge method auto → {method}", None
    return f"merge method {method}", None


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


def host_lfs_check(config: Config) -> Check:
    """Whether the host can populate a Git LFS checkout (#693).

    A repository whose ``.gitattributes`` routes files through
    ``filter=lfs`` provisions from the host's git-lfs; without it the run
    fails closed at clone time. Soft: repositories that never use LFS are
    unaffected, and ``[sandbox] clone_lfs = false`` opts out explicitly.
    """
    from sbxloop import hostgit

    version = hostgit.lfs_version()
    if version is not None:
        return Check("host git-lfs", True, f"found {version}", hard=False)
    if not config.sandbox.clone_lfs:
        return Check(
            "host git-lfs",
            True,
            "not on PATH; [sandbox] clone_lfs = false, so LFS-tracked files stay pointer files",
            hard=False,
        )
    return Check(
        "host git-lfs",
        False,
        "not on PATH — a repository whose .gitattributes uses filter=lfs fails to provision "
        "until git-lfs is installed (apt install git-lfs / brew install git-lfs); set "
        "[sandbox] clone_lfs = false to run with pointer files instead",
        hard=False,
    )


def _missing_from_push_bit(data: dict[str, object]) -> tuple[Need, ...]:
    """The write needs a token lacks, from a ``GET /repos/{repo}`` payload.

    GitHub reports the *authenticated* token's effective access on the
    repository as ``permissions: {admin, maintain, push, triage, pull}``.
    Every write a run does — delivering, the pull request, the labels — is
    the ``push`` (write) level; anything less can only read. This is the
    coarse answer for a credential that names its permissions no other way
    (a fine-grained PAT).
    """
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        return ()  # not reported (some app tokens) — do not invent a failure
    if perms.get("admin") or perms.get("maintain") or perms.get("push"):
        return ()
    if not perms.get("pull"):
        # Self-contradictory: the request that fetched this payload WAS a
        # read, so booleans denying even `pull` describe some other
        # identity, not the authenticated token. GitHub App installation
        # tokens answer exactly this way (all five False, field-verified
        # 2026-08-31) while holding full write — their capabilities live on
        # the installation, not the user-centric repo payload. Do not
        # invent a failure the first daemon write would disprove.
        return ()
    return tuple(n for n in NEEDS if n.level == "write" and n.required)


# The read a fine-grained PAT is asked to prove each permission with
# (#696): GitHub answers 401/403 when the permission is not on the token,
# and anything else — 200, an empty list, 404 on an empty repository, 422
# — means the permission is there. ``{repo}`` and ``{base}`` are filled in;
# a probe naming ``{base}`` is skipped when the repository has no base yet.
_READ_PROBES: tuple[tuple[str, str], ...] = (
    ("contents", "/repos/{repo}/commits?per_page=1&sha={base}"),
    ("issues", "/repos/{repo}/issues?per_page=1"),
    ("pull_requests", "/repos/{repo}/pulls?per_page=1"),
    ("checks", "/repos/{repo}/commits/{base}/check-runs?per_page=1"),
    ("actions", "/repos/{repo}/actions/runs?per_page=1"),
)


def _missing_from_probes(ops: GithubOps, repo: str, base: str) -> tuple[Need, ...]:
    """The needs a fine-grained PAT fails a read for. A permission the
    token lacks entirely fails its read; a read-only grant on a write need
    is the push bit's business (:func:`_missing_from_push_bit`)."""
    by_permission = {n.permission: n for n in NEEDS}
    missing: list[Need] = []
    for permission, template in _READ_PROBES:
        if "{base}" in template and not base:
            continue
        try:
            ops.raw("GET", template.format(repo=repo, base=base))
        except GithubOpsError as exc:
            if exc.http_status in (401, 403):
                missing.append(by_permission[permission])
    return tuple(missing)


def _credential_needs(
    ops: GithubOps,
    app_permissions: Mapping[str, str] | None,
    repo: str,
    base: str,
    data: dict[str, Any],
) -> tuple[tuple[Need, ...], tuple[Need, ...], str]:
    """``(required, optional, source)`` — what the credential lacks of
    :data:`NEEDS`, judged from whichever source describes it (#696): the
    App installation's grant, a classic PAT's scopes, or — a fine-grained
    PAT, which reports neither — the push bit plus one read per permission.
    """
    if app_permissions is not None:
        # The installation's grant is the whole story: it is not a user.
        missing = missing_from_app(app_permissions)
        source = "the App installation's permissions"
    else:
        # A PAT is capped twice: by what the token was granted, and by what
        # its user may do on this repository (the payload's push bit).
        scopes = ops.token_scopes()
        if scopes is not None:
            found = (
                *missing_from_scopes(scopes, private=bool(data.get("private"))),
                *_missing_from_push_bit(data),
            )
            source = f"the classic PAT's scopes {', '.join(scopes) or '(none)'}"
        else:
            found = (*_missing_from_push_bit(data), *_missing_from_probes(ops, repo, base))
            source = "a fine-grained PAT, asked endpoint by endpoint; workflows:write unverifiable"
        lacking = {n.permission for n in found}
        missing = tuple(n for n in NEEDS if n.permission in lacking)
    required, optional = split_required(missing)
    return required, optional, source


def _ci_summary(ops: GithubOps, repo: str, base: str) -> RepoCi | None:
    """The repository's active Actions workflows and its latest run on
    ``base`` (#696); None when they could not be listed (actions:read
    missing is reported as a permission, not here)."""
    try:
        listing = ops.raw("GET", f"/repos/{repo}/actions/workflows?per_page=100")
    except GithubOpsError:
        return None
    workflows = listing.get("workflows") if isinstance(listing, dict) else None
    if not isinstance(workflows, list):
        return None
    active = sum(1 for w in workflows if isinstance(w, dict) and w.get("state") == "active")
    latest: str | None = None
    if active and base:
        try:
            runs = ops.raw("GET", f"/repos/{repo}/actions/runs?branch={base}&per_page=1")
        except GithubOpsError:
            runs = None
        listed = runs.get("workflow_runs") if isinstance(runs, dict) else None
        if isinstance(listed, list) and listed and isinstance(listed[0], dict):
            run = listed[0]
            outcome = str(run.get("conclusion") or run.get("status") or "unknown")
            latest = f"{run.get('name') or 'workflow'} {outcome}"
    return RepoCi(workflows=active, base=base, latest=latest)


def _base_blockers(
    ops: GithubOps, repo: str, base: str, config: Config, *, can_sign: bool
) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    """The rules of ``base`` the loop cannot satisfy (#673).

    A required review is the classic one — the loop cannot approve its own
    pull request — and a code-owner review, approval of the last push,
    signed commits or a required deployment refuse a merge the same way
    (HTTP 405, the run ends blocked); a merge queue is not one — the loop
    enqueues (#676). Read by
    :func:`sbxloop.gh.protection.read_base_requirements` — the same reading
    the landing gate uses for required checks (#611) — and judged by the
    same :func:`sbxloop.engine.landing.base_blockers` the run would report.
    ``None`` when GitHub would not say (a token without admin on classic
    protection, say): unverifiable is not a verdict. The second element
    names the sources that could not be read (#674).
    """
    from sbxloop.engine.landing import base_blockers

    requirements = read_base_requirements(ops, repo, base)
    if requirements.source == "unknown" and not requirements.blockers():
        return None, requirements.unread
    return base_blockers(requirements, config.landing, can_sign=can_sign), requirements.unread


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
        base = entry.deliver_base or str(data.get("default_branch") or "")
        required, optional, source = _credential_needs(
            ops, box.provisioner.gh_app_permissions(entry.repo), entry.repo, base, data
        )
        has_issues = data.get("has_issues")
        blockers, unread = (
            _base_blockers(
                ops,
                entry.repo,
                base,
                config,
                # A GitHub App's API commits arrive signed by GitHub.
                can_sign=box.provisioner.gh_bot_login(entry.repo) is not None,
            )
            if base
            else (None, ())
        )
        return RepoProbe(
            reachable=True,
            detail=(
                f"reachable, token has the required permissions (per {source})"
                if not required
                else "reachable"
            ),
            missing_permissions=tuple(n.describe() for n in required),
            optional_permissions=tuple(n.describe() for n in optional),
            permissions_source=source,
            ci=_ci_summary(ops, entry.repo, base),
            base_blockers=blockers,
            base_unread=unread,
            merge_methods=allowed_merge_methods(data),
            missing_labels=_missing_repo_labels(ops, config, entry),
            issues_enabled=has_issues if isinstance(has_issues, bool) else None,
        )

    return probe


def _missing_repo_labels(
    ops: GithubOps, config: Config, entry: RepoConfig
) -> tuple[str, ...] | None:
    """The sbxloop labels ``entry`` does not carry (#630); None when the
    list could not be read (a token without issue read, a 5xx) — that is
    "unknown", not "all present"."""
    specs = lifecycle_specs(config.labels_for(entry.repo), config.landing.followup_label)
    try:
        return tuple(missing_labels(ops, entry.repo, specs))
    except GithubOpsError:
        return None


def registry_credential_checks(config: Config, env: dict[str, str]) -> list[Check]:
    """One row when a `[[registries]]` entry (or a repository's override)
    has an `auth_env` (#680): every name must be set in the daemon's
    environment, or provisioning fails by name before a sandbox boots —
    this row says so before the first run does. The names go to the
    service sandbox, which fetches the dependencies; the agent's sandbox
    never sees them (#766). Values are never shown."""
    scopes: list[tuple[str, list[str]]] = [
        # (no brackets: the detail is rich markup on the way to the table)
        ("registries", [r.auth_env for r in config.registries if r.auth_env]),
    ]
    scopes.extend(
        (f"{entry.repo} registries", [r.auth_env for r in entry.registries if r.auth_env])
        for entry in config.github.repos
        if entry.registries is not None
    )
    names = sorted({name for _scope, listed in scopes for name in listed})
    if not names:
        return []
    unset = [name for name in names if not env.get(name)]
    if not unset:
        return [Check("registry credentials", True, f"set: {', '.join(names)} (service sandbox)")]
    where = "; ".join(
        f"{scope}: {', '.join(n for n in listed if n in unset)}"
        for scope, listed in scopes
        if any(n in unset for n in listed)
    )
    return [
        Check(
            "registry credentials",
            False,
            f"not set in the daemon's environment — {where}; export them where the "
            "daemon reads its secrets, or drop the auth_env (runs that need them "
            "fail at provisioning)",
        )
    ]


def credentials_checks(config: Config, env: dict[str, str]) -> list[Check]:
    """One row when `[[credentials]]` declares anything (#765): every
    entry's `env` must be set in the daemon's environment, or a run
    granted it fails at provisioning by name — this row says so before
    the first run does. Values are never shown; the hosts are, since
    they are what the service sandbox may reach."""
    if not config.credentials:
        return []
    unset = [c for c in config.credentials if not env.get(c.env)]
    listed = ", ".join(f"{c.name} → {c.host} ({c.env})" for c in config.credentials)
    if not unset:
        return [Check("service credentials", True, f"set: {listed}")]
    missing = ", ".join(f"{c.name} ({c.env})" for c in unset)
    return [
        Check(
            "service credentials",
            False,
            f"not set in the daemon's environment — {missing}; export them where the "
            "daemon reads its secrets, or drop the entries (a run granted one fails "
            "at provisioning)",
        )
    ]


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

        # network policy reachable for the chosen agent backend's hosts
        for host in backend_for(config).token_hosts:
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
            # The template is baked for one language set; a run resolving
            # more tops it up per provision (#615). Config only — the
            # workspace a run detects from (#624) is not known here.
            wanted = [tc.name for tc in toolchains.resolve(config.sandbox.effective_languages)]
            if record.languages is None:
                languages_ok, languages_detail = (
                    True,
                    "not recorded by this bake (older sbxloop) — runs probe and top up the "
                    "configured languages per provision; re-run `sbxloop bake` to carry them",
                )
            else:
                lacking = [name for name in wanted if name not in record.languages]
                languages_ok = not lacking
                languages_detail = (
                    f"baked with {', '.join(record.languages) or 'no language toolchains'}"
                    + (
                        f"; `[sandbox] languages` also wants {', '.join(lacking)} — every run "
                        "provisions it on top of the template; re-run `sbxloop bake`"
                        if lacking
                        else " — covers `[sandbox] languages`"
                    )
                )
            checks.append(
                Check("languages in template", languages_ok, languages_detail, hard=False)
            )
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

    # tokens — the agent credential row follows [agent] backend (#533),
    # read off the backend descriptor (#617)
    agent = backend_for(config)
    checks.append(
        Check(
            agent.doctor_check_name,
            agent.has_token(env),
            "set" if agent.has_token(env) else agent.missing_token_detail,
        )
    )
    checks.extend(registry_credential_checks(config, env))
    checks.extend(credentials_checks(config, env))
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
                "configured: create a fine-grained PAT with the permissions in "
                "docs/permissions.md and export GH_TOKEN, or set GITHUB_APP_ID, "
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
    # instead of as degraded reviews in the field. Only meaningful where
    # that SDK runs the agent: another backend gets no row (#617).
    sdk_kinds = installed_sdk_permission_kinds() if agent.name == "copilot" else None
    if agent.name != "copilot":
        pass
    elif sdk_kinds is None:
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
    # The operator console's local bridge is always on: its mailbox lives in
    # the daemon's state.db and the console attaches as the login user.
    tui_state = _daemon_state_path(config, sources, env)
    operator = config.tui.operator_id or _login_name()
    checks.append(
        Check(
            "operator console",
            True,
            f"`sbxloop tui` attaches to {tui_state}/state.db as {operator}; "
            f"unit {config.tui.daemon_unit}",
            hard=False,
        )
    )
    # ...and the concierge: an agent session on the daemon host's behalf,
    # so the agent token must be here (the run pair needs it too, but a
    # chat-only operator may not have noticed). *Which* token follows
    # [agent] backend, exactly like the credential row above (#533): the
    # concierge box authenticates with ANTHROPIC_API_KEY under the claude
    # backend, so naming COPILOT_GITHUB_TOKEN here warned that "mentions
    # will fail" on a host where nothing was wrong. It answers the console
    # too, so the row is not gated on a chat backend.
    if config.concierge.enabled:
        token_env = agent.token_env
        has_token = agent.has_token(env)
        checks.append(
            Check(
                "chat concierge",
                has_token,
                f"model {config.concierge.model or config.model}, "
                f"{config.concierge.timeout_s:.0f}s per message: "
                + (
                    f"{token_env} present"
                    if has_token
                    else f"{token_env} not set (mentions will fail)"
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

    checks.append(host_lfs_check(config))

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


@dataclass
class DoctorReport:
    """What one doctor pass found: the host checks and, when sbx was usable,
    the conformance suite — the data behind ``sbxloop doctor``'s tables and
    the console's Doctor screen."""

    checks: list[Check]
    conformance: ConformanceReport | None
    #: Why there is no conformance report (sbx missing, the suite failed).
    conformance_note: str | None
    checked_at: float
    deep: bool
    probe: bool

    @property
    def ready(self) -> bool:
        return all(check.ok or not check.hard for check in self.checks)

    @property
    def drifted(self) -> bool:
        return self.conformance is not None and bool(self.conformance.unverified)


def doctor_report(
    env: dict[str, str] | None = None,
    *,
    deep: bool = False,
    probe: bool = False,
    progress: ProgressFn | None = None,
    on_checks: Callable[[list[Check]], None] | None = None,
) -> DoctorReport:
    """Run the host checks and the conformance suite; the report, not the
    rendering. ``probe`` asks GitHub itself about each configured repository
    from a github-ops sandbox — one microVM per distinct credential (#515);
    ``deep`` implies it, since that already boots a sandbox. ``on_checks``
    hears the host checks as soon as they are in — before a deep run boots
    its sandbox, so a failing check is never withheld for minutes."""
    import os

    env = dict(os.environ) if env is None else env
    config = load_config(env=env)
    cli = SbxCLI(app_name=config.app_name or None)
    report = progress or (lambda _message: None)

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
        checks = collect_checks(env, cli=cli, progress=report, probe_repo=probe_repo)
    finally:
        for box in boxes.values():
            box.close()
    if on_checks is not None:
        on_checks(checks)
    conformance: ConformanceReport | None = None
    note: str | None = None
    sbx_present = any(check.name == "sbx binary" and check.ok for check in checks)
    if not sbx_present:
        note = "sbx conformance skipped: no usable sbx binary"
    else:
        try:
            conformance = run_conformance(
                cli,
                config.state_dir,
                deep=deep,
                template=config.sandbox.template,
                progress=report,
            )
        except SbxError as exc:
            note = f"sbx conformance suite failed to run: {_clean(str(exc))}"
    return DoctorReport(checks, conformance, note, time.time(), deep, probe)


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

    def progress(message: str) -> None:
        console.print(f"[dim]\u2026 {message}[/dim]", highlight=False)

    def show_checks(checks: list[Check]) -> None:
        table = Table(title="sbxloop doctor")
        table.add_column("check", no_wrap=True)
        table.add_column("status", no_wrap=True)
        table.add_column("detail", overflow="fold")
        for check in checks:
            status = (
                "[green]ok[/]"
                if check.ok
                else ("[red]FAIL[/]" if check.hard else "[yellow]warn[/]")
            )
            table.add_row(check.name, status, _clean(check.detail))
        console.print(table)

    report = doctor_report(env, deep=deep, probe=probe, progress=progress, on_checks=show_checks)
    ready = report.ready

    if report.conformance is None:
        if report.conformance_note and report.conformance_note.startswith(
            "sbx conformance skipped"
        ):
            console.print(f"[dim]{report.conformance_note}[/]", highlight=False)
        elif report.conformance_note:
            console.print(f"[yellow]{report.conformance_note}[/]", highlight=False)
        return ready and not fail_on_drift
    render_conformance(console, report.conformance)
    # By default drift is a loud warning, not a failure: the dependent code
    # paths all probe-don't-assume at runtime, so runs may still work \u2014 but
    # the verdict snapshot above is exactly what a bug report should include.
    if fail_on_drift and report.conformance.unverified:
        unverified = len(report.conformance.unverified)
        version = report.conformance.version or "(unknown)"
        console.print(
            f"[bold red]sbx conformance gate failed:[/] {unverified} probe(s) "
            f"drifted, errored, or are unprobed under sbx {version} (--fail-on-drift)",
            highlight=False,
        )
        return False
    return ready
