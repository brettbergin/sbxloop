"""sbxloop doctor: host readiness checks with remediation hints."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

import sbxloop
from sbxloop.config import load_config
from sbxloop.errors import SbxError, SbxNotFoundError
from sbxloop.sbx.bake import load_bake_record
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import (
    AGENT_TOKEN_HOSTS,
    COPILOT_TOKEN_ENV,
    GH_TOKEN_ENVS,
)
from sbxloop.worker.wheel import resolve_worker_wheel

TESTED_SBX_SERIES = "0.35"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hard: bool = True


ProgressFn = Callable[[str], None]


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
        try:
            cli.ls()
            checks.append(Check("sbx login", True, "sbx ls succeeded"))
        except SbxError as exc:
            login_cmd = (
                f"sbx --app-name {config.app_name} login" if config.app_name else "sbx login"
            )
            checks.append(Check("sbx login", False, f"sbx ls failed ({exc}); run `{login_cmd}`"))

        # network policy reachable for the copilot hosts
        for host in AGENT_TOKEN_HOSTS:
            report(f"checking network policy for {host}")
            allowed = False
            try:
                allowed = cli.policy_check(host)
            except SbxError:
                allowed = False
            checks.append(
                Check(
                    f"policy: {host}",
                    allowed,
                    "reachable"
                    if allowed
                    else (
                        "blocked — run `sbx policy init balanced` and/or "
                        f"`sbx policy allow network {host}`"
                    ),
                    hard=False,  # per-sandbox allows are applied at provision time
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


def run_doctor(console: Console, env: dict[str, str] | None = None) -> bool:
    import os

    checks = collect_checks(
        dict(os.environ) if env is None else env,
        progress=lambda message: console.print(f"[dim]\u2026 {message}[/dim]", highlight=False),
    )
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
    return all(check.ok or not check.hard for check in checks)
