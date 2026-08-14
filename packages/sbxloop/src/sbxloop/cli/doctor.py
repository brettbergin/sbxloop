"""sbxloop doctor: host readiness checks plus the sbx conformance suite.

Readiness checks (binary, login, tokens, ...) say whether this host can run
sbxloop at all. The conformance section reruns the probe catalog from
:mod:`sbxloop.sbx.conformance` — every field-learned assumption about sbx
semantics — and warns loudly when an sbx upgrade flips a verdict a code path
depends on. Cheap probes run every time; ``--deep`` boots a scratch sandbox
for the full suite and refreshes the version-keyed verdict cache.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

import sbxloop
from sbxloop.config import load_config
from sbxloop.engine.store import StateStore
from sbxloop.errors import SbxError, SbxNotFoundError
from sbxloop.sbx.bake import load_bake_record
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.conformance import ConformanceReport, run_conformance
from sbxloop.sbx.provision import AGENT_TOKEN_HOSTS, GH_TOKEN_ENVS
from sbxloop.sbx.prune import count_orphans
from sbxloop.sbx.secretstate import COPILOT_TOKEN_ENV
from sbxloop.worker.wheel import resolve_worker_wheel
from sbxloop_worker.backends.copilot import SDK_PERMISSION_KINDS, installed_sdk_permission_kinds

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


def collect_checks(
    env: dict[str, str],
    cli: SbxCLI | None = None,
    progress: ProgressFn | None = None,
) -> list[Check]:
    config = load_config(env=env)
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
    # GH_TOKEN matters only when the GitHub integration is configured; an
    # unconfigured integration is a valid (GitHub-less) setup, not a failure.
    if config.github.enabled:
        gh_set = any(env.get(name) for name in GH_TOKEN_ENVS)
        checks.append(
            Check(
                "/".join(GH_TOKEN_ENVS),
                gh_set,
                f"set (github integration: {config.github.repo})"
                if gh_set
                else f"not set but [github].repo = {config.github.repo!r} is configured — "
                "create a fine-grained PAT (issues:write, contents:read, ...) "
                "and export GH_TOKEN",
            )
        )
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

    return checks


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


def run_doctor(console: Console, env: dict[str, str] | None = None, *, deep: bool = False) -> bool:
    import os

    env = dict(os.environ) if env is None else env
    config = load_config(env=env)
    cli = SbxCLI(app_name=config.app_name or None)

    def progress(message: str) -> None:
        console.print(f"[dim]\u2026 {message}[/dim]", highlight=False)

    checks = collect_checks(env, cli=cli, progress=progress)
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
        return ready
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
        return ready
    render_conformance(console, report)
    # Drift is a loud warning, not a failure: the dependent code paths all
    # probe-don't-assume at runtime, so runs may still work \u2014 but the verdict
    # snapshot above is exactly what a bug report should include.
    return ready
