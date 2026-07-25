"""The sbx conformance suite: field-learned sbx assumptions as runnable probes.

Every load-bearing assumption about sbx semantics that sbxloop discovered in
the field (0.1.1 through 0.1.9 and the artifacts work) is encoded here as a
probe with a machine-checkable verdict. Verdicts are cached in the state dir
keyed by ``sbx version`` output, so ``sbxloop doctor`` can warn loudly when
an sbx upgrade flips an answer a code path depends on — instead of that
drift surfacing as a confusing field failure.

Three probe tiers:

- **cheap** probes talk to the sbx CLI only (help text, version, ls) and run
  on every ``doctor``.
- **sandbox** probes need a live microVM; ``doctor --deep`` boots one shared
  scratch sandbox for all of them. Their last verdicts are served from the
  version-keyed cache on non-deep runs.
- provisioning re-runs a subset in the field (secret visibility, mount
  discovery) and refreshes the cache for free via
  :func:`record_field_verdict`.

This module is also the canonical home of the sbx error-shape knowledge the
rest of the codebase depends on (secret exists-markers, the conflicting-scope
regex): provisioning imports them from here.
"""

from __future__ import annotations

import json
import re
import secrets as _secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from sbxloop.errors import SbxError, SbxNotFoundError, SdxloopError
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxSpec
from sbxloop.sbx.parse import _CELL_SPLIT, parse_version
from sbxloop.sbx.sandbox import Sandbox

# -- sbx error shapes (single source of truth; provisioning imports these) --

# Substrings marking an "already exists" refusal from `sbx secret set*`.
SECRET_EXISTS_MARKERS = ("exist", "already")
# sbx names the owner of a conflicting secret, e.g.
#   ERROR: custom secret env "X" already exists in scope NAME with placeholder ...
SECRET_SCOPE_RE = re.compile(r'in scope "?([A-Za-z0-9._-]+)"?')


def parse_secret_conflict_scope(stderr: str) -> str | None:
    """The scope owning a conflicting secret, per sbx's error message
    (None when unparseable)."""
    match = SECRET_SCOPE_RE.search(stderr)
    return match.group(1) if match else None


# -- probe ids (importable so provisioning hooks can't typo them) -----------

PROBE_CLI_SURFACE = "cli-surface"
PROBE_VERSION_FORMAT = "version-format"
PROBE_LS_COLUMNS = "ls-columns"
PROBE_EXEC_ERROR_CHANNEL = "exec-error-channel"
PROBE_CP_DIR_SEMANTICS = "cp-dir-semantics"
PROBE_WORKSPACE_MOUNT = "workspace-mount"
PROBE_PYTHON3_VENV = "python3-venv"
PROBE_SECRET_ENV_VISIBILITY = "secret-env-visibility"
PROBE_SECRET_EXISTS_ERROR = "secret-exists-error"

VERDICT_ERROR = "error"
VERDICT_UNPROBED = "unprobed"

ProbeTier = Literal["cheap", "sandbox"]
VerdictSource = Literal["probe", "cache", "provision"]


@dataclass
class ProbeContext:
    """What a probe gets to work with."""

    cli: SbxCLI
    sandbox: Sandbox | None = None  # the shared scratch sandbox (deep runs)
    workspace: Path | None = None  # host dir the scratch sandbox was created on


@dataclass(frozen=True)
class Probe:
    id: str
    summary: str
    tier: ProbeTier
    # The verdict the current sbxloop codebase is built against, or None for
    # informational probes whose either answer is handled.
    expected: str | None
    # The dependent behavior, named in drift alarms.
    depends: str
    run: Callable[[ProbeContext], tuple[str, str]]  # -> (verdict, detail)


class ProbeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str
    detail: str = ""
    checked_at: float
    source: VerdictSource = "probe"


# -- probe implementations ---------------------------------------------------

_CORE_SUBCOMMANDS = ("create", "exec", "cp", "ls", "stop", "rm", "policy", "secret", "version")


def _probe_cli_surface(ctx: ProbeContext) -> tuple[str, str]:
    result = ctx.cli.run("--help", check=False)
    text = f"{result.stdout}\n{result.stderr}"
    missing = [cmd for cmd in _CORE_SUBCOMMANDS if cmd not in text]
    if missing:
        return f"missing({','.join(missing)})", "subcommands absent from `sbx --help`"
    return "complete", "all subcommands sbxloop invokes are advertised"


def _probe_version_format(ctx: ProbeContext) -> tuple[str, str]:
    raw = ctx.cli.run("version", check=False).stdout
    version = parse_version(raw)
    if version is None:
        return "unparseable", f"no semver in {raw.strip()!r}"
    return "semver", f"parsed {version}"


def _probe_ls_columns(ctx: ProbeContext) -> tuple[str, str]:
    stdout = ctx.cli.run("ls").stdout
    lines = [line for line in stdout.splitlines() if line.strip()]
    header = lines[0].strip() if lines else ""
    have = {cell.lower() for cell in _CELL_SPLIT.split(header)} if header else set()
    missing = [col for col in ("name", "agent", "status", "workspace") if col not in have]
    if missing:
        return f"drifted({','.join(missing)})", f"header: {header!r}"
    return "expected-columns", f"header: {header!r}"


def _probe_exec_error_channel(ctx: ProbeContext) -> tuple[str, str]:
    assert ctx.sandbox is not None
    try:
        result = ctx.sandbox.exec(["sbxloop-conformance-no-such-binary"])
    except SbxNotFoundError:
        # The missing-binary text hit stderr hard enough to trip the CLI
        # wrapper's not-found markers — that IS the stderr channel.
        return "stderr", "error text on stderr (tripped not-found detection)"
    if result.ok:
        return "no-error", "executing a nonexistent binary reported success"
    out, err = bool(result.stdout.strip()), bool(result.stderr.strip())
    if out and err:
        return "both", "error text on both stdout and stderr"
    if out:
        return "stdout", "error text on stdout only"
    if err:
        return "stderr", "error text on stderr only"
    return "silent", f"rc={result.returncode} with no output on either stream"


_CP_PROBE_DST = "/home/agent/.sbxloop-conformance-cp"


def _probe_cp_dir_semantics(ctx: ProbeContext) -> tuple[str, str]:
    assert ctx.sandbox is not None
    with tempfile.TemporaryDirectory(prefix="sbxloop-conformance-") as tmp:
        src = Path(tmp) / "payload"
        (src / "sub").mkdir(parents=True)
        (src / "sub" / "marker.txt").write_text("conformance")
        # cp via the raw CLI: pathlib would normalize away the trailing "/."
        # that this probe exists to exercise.
        ctx.cli.cp(f"{src}/.", f"{ctx.sandbox.name}:{_CP_PROBE_DST}")
    if ctx.sandbox.exec(["test", "-f", f"{_CP_PROBE_DST}/sub/marker.txt"]).ok:
        return "contents-into-dst", "`cp <dir>/. box:dst` lands contents in dst"
    if ctx.sandbox.exec(["test", "-f", f"{_CP_PROBE_DST}/payload/sub/marker.txt"]).ok:
        return "nests-source-dir", "`cp <dir>/. box:dst` nests the source dir under dst"
    return "unknown", "copied tree not found at either candidate layout"


def _probe_workspace_mount(ctx: ProbeContext) -> tuple[str, str]:
    assert ctx.sandbox is not None and ctx.workspace is not None
    from sbxloop.sbx.provision import MOUNT_SEARCH_MAXDEPTH, MOUNT_SEARCH_ROOTS

    marker = f".sbxloop-conformance-{_secrets.token_hex(8)}"
    (ctx.workspace / marker).write_text("")
    try:
        command = (
            f"find -L {' '.join(MOUNT_SEARCH_ROOTS)} "
            f"-maxdepth {MOUNT_SEARCH_MAXDEPTH} -name {marker} -print 2>/dev/null | head -1"
        )
        result = ctx.sandbox.exec(["sh", "-c", command])
        hit = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    finally:
        (ctx.workspace / marker).unlink(missing_ok=True)
    if hit.endswith(f"/{marker}"):
        return "discoverable", f"workspace mounted at {hit[: -len(f'/{marker}')] or '/'}"
    return "not-found", f"marker not found under {', '.join(MOUNT_SEARCH_ROOTS)}"


def _probe_python3_venv(ctx: ProbeContext) -> tuple[str, str]:
    assert ctx.sandbox is not None
    result = ctx.sandbox.exec(["python3", "-c", "import venv, ensurepip"])
    if result.ok:
        return "available", "python3 -m venv should work first try"
    return "missing", "venv/ensurepip not importable; the install ladder's apt rung is needed"


_VIS_PROBE_ENV = "SBXLOOP_CONFORMANCE_VIS"
_DUP_PROBE_ENV = "SBXLOOP_CONFORMANCE_DUP"
_PROBE_SECRET_HOST = "example.com"


def _cleanup_probe_secret(cli: SbxCLI, env: str, sandbox: str) -> None:
    for scope in (sandbox, None):
        if cli.secret_rm(host=_PROBE_SECRET_HOST, env=env, sandbox=scope):
            return
        if cli.secret_rm(env=env, sandbox=scope):
            return


def _probe_secret_env_visibility(ctx: ProbeContext) -> tuple[str, str]:
    assert ctx.sandbox is not None
    name = ctx.sandbox.name
    ctx.cli.secret_set_custom(
        host=_PROBE_SECRET_HOST, env=_VIS_PROBE_ENV, value="conformance", sandbox=name
    )
    try:
        result = ctx.sandbox.exec(["sh", "-lc", f'test -n "${{{_VIS_PROBE_ENV}}}"'])
    finally:
        _cleanup_probe_secret(ctx.cli, _VIS_PROBE_ENV, name)
    if result.ok:
        return "visible-under-exec", "custom secret env visible to `sbx exec` login shells"
    return "invisible-under-exec", "custom secret env NOT visible to `sbx exec` processes"


def _probe_secret_exists_error(ctx: ProbeContext) -> tuple[str, str]:
    assert ctx.sandbox is not None
    name = ctx.sandbox.name
    set_dup = partial(
        ctx.cli.secret_set_custom,
        host=_PROBE_SECRET_HOST,
        env=_DUP_PROBE_ENV,
        value="conformance",
        sandbox=name,
    )
    set_dup()
    try:
        try:
            set_dup()
        except SbxError as exc:
            stderr = exc.stderr
            if not any(m in stderr.lower() for m in SECRET_EXISTS_MARKERS):
                return (
                    "unrecognized-error",
                    f"duplicate set failed without exists-marker: {stderr!r}",
                )
            scope = parse_secret_conflict_scope(stderr)
            if scope is None:
                return "unparseable-scope", f"exists-error names no scope: {stderr!r}"
            return "parseable-scope", f"exists-error names owning scope {scope!r}"
        return "overwrite-allowed", "sbx accepted setting the same custom secret env twice"
    finally:
        _cleanup_probe_secret(ctx.cli, _DUP_PROBE_ENV, name)


CATALOG: tuple[Probe, ...] = (
    Probe(
        id=PROBE_CLI_SURFACE,
        summary="`sbx --help` advertises every subcommand sbxloop invokes",
        tier="cheap",
        expected="complete",
        depends="the entire SbxCLI wrapper (create/exec/cp/ls/stop/rm/policy/secret/version)",
        run=_probe_cli_surface,
    ),
    Probe(
        id=PROBE_VERSION_FORMAT,
        summary="`sbx version` output contains a parseable semver",
        tier="cheap",
        expected="semver",
        depends="version-keyed conformance caching and doctor's tested-series warning",
        run=_probe_version_format,
    ),
    Probe(
        id=PROBE_LS_COLUMNS,
        summary="`sbx ls` header carries the NAME/AGENT/STATUS/WORKSPACE columns",
        tier="cheap",
        expected="expected-columns",
        depends="parse_ls (sandbox listing, `sandbox rm --all`, pair cleanup)",
        run=_probe_ls_columns,
    ),
    Probe(
        id=PROBE_EXEC_ERROR_CHANNEL,
        summary="which stream `sbx exec` uses for in-sandbox launch errors",
        tier="sandbox",
        expected="stdout",
        depends="worker error surfacing joins stdout+stderr because sbx reports some "
        "in-sandbox errors on stdout (stderr alone can be empty exactly when it matters)",
        run=_probe_exec_error_channel,
    ),
    Probe(
        id=PROBE_CP_DIR_SEMANTICS,
        summary="`sbx cp <dir>/. box:dst` copies directory contents into dst",
        tier="sandbox",
        expected="contents-into-dst",
        depends="artifact harvest stages the in-VM workdir with a trailing `/.` and "
        "expects docker-style contents-into-dst copies",
        run=_probe_cp_dir_semantics,
    ),
    Probe(
        id=PROBE_WORKSPACE_MOUNT,
        summary="the host workspace is discoverable inside the VM (nonce-marker search)",
        tier="sandbox",
        expected="discoverable",
        depends="mount discovery: live workspace artifacts vs the harvest fallback "
        "(a not-found verdict silently downgrades every run to harvest mode)",
        run=_probe_workspace_mount,
    ),
    Probe(
        id=PROBE_PYTHON3_VENV,
        summary="whether the sandbox template ships python3-venv/ensurepip",
        tier="sandbox",
        expected=None,  # the install ladder handles both answers
        depends="the worker install ladder (venv -> apt python3-venv -> user-site) exists "
        "because the default template lacks python3-venv",
        run=_probe_python3_venv,
    ),
    Probe(
        id=PROBE_SECRET_ENV_VISIBILITY,
        summary="whether sbx proxy secret injection reaches `sbx exec` processes",
        tier="sandbox",
        expected="invisible-under-exec",
        depends="provisioning's plain-env auto-heal; a visible verdict means the proxy "
        "path may now work under exec and the in-VM env-file fallback may be unnecessary",
        run=_probe_secret_env_visibility,
    ),
    Probe(
        id=PROBE_SECRET_EXISTS_ERROR,
        summary="duplicate custom-secret sets fail with a parseable owning scope",
        tier="sandbox",
        expected="parseable-scope",
        depends="the replace-on-exists secret flow parses the owning scope out of sbx's "
        "stderr to remove stale secrets from previous runs",
        run=_probe_secret_exists_error,
    ),
)


# -- version-keyed verdict cache ---------------------------------------------


def _conformance_dir(state_dir: Path) -> Path:
    return state_dir / "conformance"


def _version_slug(version: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", version or "unknown")


def cache_path(state_dir: Path, version: str | None) -> Path:
    return _conformance_dir(state_dir) / f"sbx-{_version_slug(version)}.json"


def load_verdicts(state_dir: Path, version: str | None) -> dict[str, ProbeRecord]:
    path = cache_path(state_dir, version)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    records: dict[str, ProbeRecord] = {}
    for probe_id, raw in data.get("records", {}).items():
        try:
            records[probe_id] = ProbeRecord.model_validate(raw)
        except ValueError:
            continue
    return records


def save_verdicts(state_dir: Path, version: str | None, records: dict[str, ProbeRecord]) -> None:
    """Merge ``records`` into the cache file for ``version``."""
    merged = load_verdicts(state_dir, version)
    merged.update(records)
    path = cache_path(state_dir, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sbx_version": version,
        "records": {pid: rec.model_dump() for pid, rec in sorted(merged.items())},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def record_field_verdict(
    state_dir: Path,
    version: str | None,
    probe_id: str,
    verdict: str,
    detail: str = "",
) -> None:
    """Refresh one cached verdict from a field observation (provisioning).

    Best-effort by contract: a cache write must never fail the caller.
    """
    try:
        record = ProbeRecord(
            verdict=verdict, detail=detail, checked_at=time.time(), source="provision"
        )
        save_verdicts(state_dir, version, {probe_id: record})
    except OSError:
        pass


def cached_versions(state_dir: Path) -> list[str]:
    """Cached sbx versions, most recently written first."""
    directory = _conformance_dir(state_dir)
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("sbx-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    versions: list[str] = []
    for path in files:
        try:
            version = json.loads(path.read_text()).get("sbx_version")
        except (OSError, ValueError):
            continue
        if isinstance(version, str) and version not in versions:
            versions.append(version)
    return versions


# -- suite runner ------------------------------------------------------------


@dataclass
class ProbeOutcome:
    probe: Probe
    verdict: str
    detail: str = ""
    source: VerdictSource | Literal["unprobed"] = "probe"
    checked_at: float | None = None
    drifts: list[str] = field(default_factory=list)

    @property
    def is_error(self) -> bool:
        return self.verdict == VERDICT_ERROR

    @property
    def matches_expected(self) -> bool:
        return self.probe.expected is None or self.verdict == self.probe.expected


@dataclass
class ConformanceReport:
    version: str | None
    deep: bool
    outcomes: list[ProbeOutcome]
    previous_version: str | None = None
    # Set when this sbx version has never been deep-probed: the loud nudge.
    deep_run_hint: str | None = None

    @property
    def drifted(self) -> list[ProbeOutcome]:
        return [o for o in self.outcomes if o.drifts]


ProgressFn = Callable[[str], None]


def _run_probe(probe: Probe, ctx: ProbeContext) -> ProbeOutcome:
    try:
        verdict, detail = probe.run(ctx)
    except (SdxloopError, OSError) as exc:
        return ProbeOutcome(probe, VERDICT_ERROR, detail=str(exc), checked_at=time.time())
    return ProbeOutcome(probe, verdict, detail=detail, checked_at=time.time())


def _apply_drift(
    outcome: ProbeOutcome,
    previous_version: str | None,
    previous: dict[str, ProbeRecord],
) -> None:
    """Attach drift alarms: expected-verdict mismatches and cross-version flips."""
    if outcome.is_error or outcome.source == "unprobed":
        return
    probe = outcome.probe
    if probe.expected is not None and outcome.verdict != probe.expected:
        outcome.drifts.append(f"this sbxloop build depends on {probe.expected!r}: {probe.depends}")
    prior = previous.get(probe.id)
    if (
        previous_version is not None
        and prior is not None
        and prior.verdict not in (VERDICT_ERROR, outcome.verdict)
    ):
        outcome.drifts.append(
            f"changed from {prior.verdict!r} under sbx {previous_version} — {probe.depends}"
        )


def _scratch_sandbox(cli: SbxCLI, state_dir: Path, template: str | None) -> tuple[Sandbox, Path]:
    nonce = _secrets.token_hex(4)
    # Resolve like provisioning does: sbx mounts the workspace by path, and a
    # relative state dir would hand it a cwd-dependent reference.
    workspace = _conformance_dir(state_dir) / f"scratch-{nonce}"
    workspace.mkdir(parents=True, exist_ok=True)
    workspace = workspace.resolve()
    spec = SandboxSpec(
        name=f"sbxloop-doctor-{nonce}", role="agent", workspace=workspace, template=template
    )
    cli.create(spec)
    return Sandbox(cli, spec.name), workspace


def run_conformance(
    cli: SbxCLI,
    state_dir: Path,
    *,
    deep: bool = False,
    template: str | None = None,
    progress: ProgressFn | None = None,
) -> ConformanceReport:
    """Run the probe catalog and reconcile with the version-keyed cache.

    Default (non-deep) runs execute the cheap CLI probes and serve sandbox
    probes from the current version's cache. ``deep=True`` boots one scratch
    sandbox (removed afterwards, even on failure) and runs everything live.
    Probed verdicts are written back to the cache.
    """
    report_progress = progress or (lambda _m: None)
    version = cli.version()
    cached = load_verdicts(state_dir, version)
    previous_version = next((v for v in cached_versions(state_dir) if v != version), None)
    previous = load_verdicts(state_dir, previous_version) if previous_version else {}

    outcomes: list[ProbeOutcome] = []
    fresh: dict[str, ProbeRecord] = {}

    def note(outcome: ProbeOutcome) -> None:
        outcomes.append(outcome)
        if outcome.source == "probe" and not outcome.is_error:
            assert outcome.checked_at is not None
            fresh[outcome.probe.id] = ProbeRecord(
                verdict=outcome.verdict, detail=outcome.detail, checked_at=outcome.checked_at
            )

    for probe in (p for p in CATALOG if p.tier == "cheap"):
        report_progress(f"probing {probe.id}")
        note(_run_probe(probe, ProbeContext(cli)))

    sandbox_probes = [p for p in CATALOG if p.tier == "sandbox"]
    if deep:
        report_progress("creating scratch sandbox for deep probes (first boot can be slow)")
        sandbox, workspace = _scratch_sandbox(cli, state_dir, template)
        try:
            ctx = ProbeContext(cli, sandbox=sandbox, workspace=workspace)
            for probe in sandbox_probes:
                report_progress(f"probing {probe.id}")
                note(_run_probe(probe, ctx))
        finally:
            try:
                sandbox.rm()
            except SbxError:
                report_progress(f"could not remove scratch sandbox {sandbox.name}")
            shutil.rmtree(workspace, ignore_errors=True)
    else:
        for probe in sandbox_probes:
            record = cached.get(probe.id)
            if record is None:
                note(ProbeOutcome(probe, VERDICT_UNPROBED, source="unprobed"))
            else:
                # Field-recorded verdicts (provisioning) keep their provenance;
                # everything else served from disk renders as "cache".
                source: Literal["provision", "cache"] = (
                    "provision" if record.source == "provision" else "cache"
                )
                note(
                    ProbeOutcome(
                        probe,
                        record.verdict,
                        detail=record.detail,
                        source=source,
                        checked_at=record.checked_at,
                    )
                )

    for outcome in outcomes:
        _apply_drift(outcome, previous_version, previous)

    if fresh:
        try:
            save_verdicts(state_dir, version, fresh)
        except OSError:
            report_progress("could not write the conformance cache")

    deep_run_hint = None
    if not deep and any(o.source == "unprobed" for o in outcomes):
        seen = f" (last deep-probed sbx version: {previous_version})" if previous_version else ""
        deep_run_hint = (
            f"sbx {version or '(unknown)'} has unprobed sandbox behaviors{seen} — "
            "run `sbxloop doctor --deep` to validate them against this sbx build"
        )

    return ConformanceReport(
        version=version,
        deep=deep,
        outcomes=outcomes,
        previous_version=previous_version,
        deep_run_hint=deep_run_hint,
    )
