"""Custom-secret registration state, shared by provisioning and `sbxloop secrets`.

Field-verified sbx behavior this module encodes (2026-07-23):

- sbx keys custom secrets by ENV VAR NAME — one registration per env var,
  whatever the scope. Registering the same env again fails with
  ``custom secret env "X" already exists in scope <scope> with placeholder
  <p>``; the error names the owning scope, which is parseable for targeted
  removal.
- sbx refuses to overwrite an existing secret; replacement is rm + set.
- Removal syntax for custom secrets is not a stable documented API, so
  removals are best-effort ladders from most- to least-specific scope.
- ``sbx secret ls``-style enumeration is UNVERIFIED across sbx builds:
  listing is attempted and parsed tolerantly, with the exists-error
  collision probe as the authoritative fallback.

Provisioning's replace-on-exists collision recovery and the `sbxloop secrets`
command group both call into here, so the field-hardened logic has exactly
one implementation.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import partial
from pathlib import Path
from secrets import token_hex
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sbxloop.backends import (
    ANTHROPIC_TOKEN_ENV,
    ANTHROPIC_TOKEN_HOST,
    COPILOT_TOKEN_ENV,
    COPILOT_TOKEN_HOST,
    backend_for,
)
from sbxloop.config import Config
from sbxloop.errors import SbxError, SecretStateError
from sbxloop.log import get_logger
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxSpec

log = get_logger(__name__)

# The agent credentials' env names and binding hosts live on the backend
# descriptor (#617); re-exported here because this is where every
# secret-handling module has always imported them from.
__all__ = [
    "ANTHROPIC_TOKEN_ENV",
    "ANTHROPIC_TOKEN_HOST",
    "COPILOT_TOKEN_ENV",
    "COPILOT_TOKEN_HOST",
]

# Every sbxloop sandbox (and therefore every sandbox-scoped registration
# sbxloop creates) is named with this prefix.
SANDBOX_SCOPE_PREFIX = "sbxloop-"

SECRET_EXISTS_MARKERS = ("exist", "already")
# sbx reports the owner of a conflicting secret, e.g.
#   ERROR: custom secret env "X" already exists in scope NAME with placeholder ...
_SCOPE_RE = re.compile(r'in scope "?([A-Za-z0-9._-]+)"?')
# Spellings sbx uses for the global scope in errors and listings.
_GLOBAL_SCOPE_NAMES = ("global", "-g")

_DOMAIN_RE = re.compile(r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}")

Source = Literal["ls", "probe"]

RmCandidates = Callable[[str], list[Callable[[], bool]]]


def tracked_custom_secrets(config: Config) -> list[tuple[str, str]]:
    """The (env, canonical host) custom secrets provisioning registers.

    Exactly the configured agent backend's credential (#617): the Copilot
    token by default, the Anthropic key under ``[agent] backend =
    "claude"``. Config declares no additional custom secrets (the github
    sandbox uses sbx's built-in ``github`` service secret, which is never
    managed here).
    """
    return [backend_for(config).secret]


def parsed_scope(stderr: str) -> str | None:
    """The scope owning the conflicting secret, per sbx's error message.

    Returns None when unparseable; the literal global spellings
    ("global"/"-g") map to None-as-global in secret_rm terms via the
    candidate builders below.
    """
    match = _SCOPE_RE.search(stderr)
    return match.group(1) if match else None


def service_rm_candidates(
    cli: SbxCLI, service: str, sandbox: str | None, stderr: str
) -> list[Callable[[], bool]]:
    scopes: list[str | None] = []
    parsed = parsed_scope(stderr)
    if parsed:
        scopes.append(None if parsed in _GLOBAL_SCOPE_NAMES else parsed)
    scopes += [sandbox, None]
    seen: list[str | None] = []
    candidates: list[Callable[[], bool]] = []
    for scope in scopes:
        if scope in seen:
            continue
        seen.append(scope)
        candidates.append(partial(cli.secret_rm, service=service, sandbox=scope))
    return candidates


def custom_rm_candidates(
    cli: SbxCLI, host: str, env: str, sandbox: str | None, stderr: str
) -> list[Callable[[], bool]]:
    scopes: list[str | None] = []
    parsed = parsed_scope(stderr)
    if parsed:
        scopes.append(None if parsed in _GLOBAL_SCOPE_NAMES else parsed)
    scopes += [sandbox, None]
    seen: list[str | None] = []
    candidates: list[Callable[[], bool]] = []
    for scope in scopes:
        if scope in seen:
            continue
        seen.append(scope)
        # env+host first, then env-only: sbx keys custom secrets by env
        # name, so the conflicting entry may carry a different host.
        candidates.append(partial(cli.secret_rm, host=host, env=env, sandbox=scope))
        candidates.append(partial(cli.secret_rm, env=env, sandbox=scope))
    return candidates


def set_secret_replacing(
    describe: str,
    *,
    set_fn: Callable[[], None],
    rm_candidates: RmCandidates,
    strict: bool = False,
) -> bool:
    """Set a secret, replacing a leftover one from a previous run.

    sbx refuses to overwrite an existing secret and keys custom secrets
    by env name, with the conflicting entry possibly owned by another
    scope (a previous run's sandbox). On an exists-error we parse the
    owning scope out of sbx's stderr and try removal candidates from
    most to least specific, retrying the set after each successful
    removal. Returns True when the set succeeded (fresh or replaced).

    By default an exists-conflict NEVER fails: if nothing can be replaced,
    the existing value is kept with a warning (it may be stale if the
    token was rotated) and False is returned — provisioning must not die
    on a leftover registration. Under ``strict`` (rotation, where keeping
    a stale value defeats the point) an irreplaceable secret raises
    SecretStateError instead. Non-exists errors always raise.

    Only sbx's stderr is matched for exists-markers: the full exception
    string embeds argv, and arbitrary paths can contain words like
    "exists" (a pytest tmp dir did exactly that).
    """
    try:
        set_fn()
        return True
    except SbxError as exc:
        if not any(m in exc.stderr.lower() for m in SECRET_EXISTS_MARKERS):
            raise
        stderr = exc.stderr
    for rm_fn in rm_candidates(stderr):
        if not rm_fn():
            continue
        try:
            set_fn()
            return True
        except SbxError as exc:
            if not any(m in exc.stderr.lower() for m in SECRET_EXISTS_MARKERS):
                raise
    if strict:
        raise SecretStateError(
            f"secret {describe} already exists and could not be replaced "
            f"(sbx said: {' '.join(stderr.split())})"
        )
    log.warning(
        "secret.not_replaced",
        secret=describe,
        detail="already exists and could not be replaced; keeping the existing value",
        hint="it may be stale if the token was rotated",
    )
    return False


# -- inspection --------------------------------------------------------------


class CustomSecretState(BaseModel):
    """What sbx currently has registered for one custom-secret env var."""

    model_config = ConfigDict(extra="forbid")

    env: str
    # None: undetermined (listing unsupported and probing disabled/failed).
    exists: bool | None = None
    # "global", a sandbox-scope name, or None when the owner is unknown.
    scope: str | None = None
    # Host bindings, when a listing revealed them (a probe cannot).
    hosts: list[str] = Field(default_factory=list)
    source: Source | None = None
    detail: str = ""


def parse_secret_ls_entry(raw: str, env: str) -> tuple[str | None, list[str]] | None:
    """Best-effort (scope, hosts) for ``env`` from ``sbx secret ls`` output.

    The listing format is unverified across sbx builds, so parsing is
    token-based on whichever line names the env var: domain-shaped tokens
    are host bindings, a ``sbxloop-*`` token is the owning sandbox scope,
    and a global spelling maps to "global". Returns None when no line
    mentions the env (which is NOT proof of absence — the build's listing
    may simply omit custom secrets; callers corroborate with the probe).
    """
    for line in raw.splitlines():
        if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(env)}(?![A-Za-z0-9_])", line):
            continue
        tokens = line.replace(",", " ").split()
        hosts = [t for t in tokens if _DOMAIN_RE.fullmatch(t)]
        scope = next((t for t in tokens if t.startswith(SANDBOX_SCOPE_PREFIX)), None)
        if scope is None and any(t.lower() in _GLOBAL_SCOPE_NAMES for t in tokens):
            scope = "global"
        return scope, hosts
    return None


def probe_custom_secret(cli: SbxCLI, env: str, *, host: str) -> CustomSecretState:
    """Detect a registration via the field-verified exists-error.

    Attempts a global set-custom with a sentinel value: an exists-error
    proves the registration (and names its owner scope); success proves
    absence, and the sentinel is removed again immediately. A leftover
    sentinel (removal rejected mid-probe) is harmless — provisioning
    replaces on collision and never trusts a pre-existing value — but is
    logged loudly.
    """
    sentinel = f"sbxloop-probe-{token_hex(4)}"
    try:
        cli.secret_set_custom(host=host, env=env, value=sentinel, sandbox=None)
    except SbxError as exc:
        if any(m in exc.stderr.lower() for m in SECRET_EXISTS_MARKERS):
            scope = parsed_scope(exc.stderr)
            if scope in _GLOBAL_SCOPE_NAMES:
                scope = "global"
            return CustomSecretState(
                env=env,
                exists=True,
                scope=scope,
                source="probe",
                detail=exc.stderr.strip(),
            )
        return CustomSecretState(env=env, exists=None, detail=str(exc))
    removed = cli.secret_rm(host=host, env=env, sandbox=None) or cli.secret_rm(
        env=env, sandbox=None
    )
    if not removed:
        log.warning(
            "secret.probe_sentinel_left",
            env=env,
            hint="sbx rejected removing the transient sentinel; the next provisioning replaces it",
        )
    return CustomSecretState(env=env, exists=False, source="probe")


def inspect_custom_secret(
    cli: SbxCLI, env: str, *, host: str, probe: bool = True
) -> CustomSecretState:
    """Current registration state for ``env``: listing first, probe fallback."""
    listing = cli.secret_ls()
    if listing.ok:
        entry = parse_secret_ls_entry(listing.stdout, env)
        if entry is not None:
            scope, hosts = entry
            return CustomSecretState(env=env, exists=True, scope=scope, hosts=hosts, source="ls")
        # No line names the env. That usually means "not registered", but a
        # build whose listing omits custom secrets would look identical, so
        # the collision probe settles it when allowed.
    if not probe:
        return CustomSecretState(
            env=env,
            exists=None,
            detail="not in `sbx secret ls` output and probing disabled (--no-probe)",
        )
    return probe_custom_secret(cli, env, host=host)


# -- assessment --------------------------------------------------------------


class Assessment(BaseModel):
    """One tracked secret's registration state, judged against what current
    provisioning would register — mismatches are pre-collision warnings."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "warn", "unknown"]
    note: str
    # Safe for `secrets clean` to remove: definitely sbxloop-owned and no
    # longer what provisioning would produce.
    stale: bool = False
    # sbxloop-owned at all (stale or not) — what `clean --all` may remove.
    owned: bool = False


def assess(
    state: CustomSecretState, *, canonical_host: str, live_sandboxes: set[str]
) -> Assessment:
    if state.exists is False:
        return Assessment(status="ok", note="not registered — the next run registers it fresh")
    if state.exists is None:
        return Assessment(status="unknown", note=state.detail or "state undetermined")
    host_mismatch = bool(state.hosts) and canonical_host not in state.hosts
    if state.scope is not None and state.scope.startswith(SANDBOX_SCOPE_PREFIX):
        if state.scope in live_sandboxes:
            return Assessment(
                status="warn" if host_mismatch else "ok",
                note=(
                    f"owned by live sandbox {state.scope}"
                    + (
                        f" but bound to {', '.join(state.hosts)} (expected {canonical_host})"
                        if host_mismatch
                        else ""
                    )
                ),
                stale=host_mismatch,
                owned=True,
            )
        return Assessment(
            status="warn",
            note=(
                f"stale: owner scope {state.scope} no longer exists — the next run "
                "will hit (and recover from) a set-custom collision"
            ),
            stale=True,
            owned=True,
        )
    if state.scope == "global":
        if host_mismatch:
            return Assessment(
                status="warn",
                note=(
                    f"global registration bound to {', '.join(state.hosts)}, expected "
                    f"{canonical_host} — likely left by an older sbxloop version"
                ),
                stale=True,
                owned=True,
            )
        return Assessment(
            status="ok",
            note="global registration with the canonical host binding",
            owned=True,
        )
    if state.scope is None:
        return Assessment(
            status="warn",
            note="registered but the owner scope could not be determined — "
            "inspect with `sbx secret ls` or remove manually",
        )
    return Assessment(
        status="warn",
        note=(
            f"owned by foreign scope {state.scope!r} — not sbxloop's to remove; the next "
            "run will attempt collision recovery against it. Remove it yourself with "
            "`sbx secret rm` if it is yours."
        ),
    )


def removal_ladder(cli: SbxCLI, state: CustomSecretState, *, host: str) -> list[Callable[[], bool]]:
    """rm candidates for a known registration, most- to least-specific.

    ``state.detail`` may carry the probe's exists-error, whose parsed scope
    then leads the ladder exactly as in provisioning's collision recovery.
    """
    scope = None if state.scope in (None, "global") else state.scope
    return custom_rm_candidates(cli, host, state.env, scope, state.detail)


# -- rotation ----------------------------------------------------------------


def replace_registration(cli: SbxCLI, *, env: str, host: str, token: str) -> None:
    """Atomically replace ``env``'s registration at global scope: the rm +
    set-custom dance with the canonical host binding, failing loudly (unlike
    provisioning's keep-on-conflict) when the old entry cannot be removed."""
    set_secret_replacing(
        f"custom {env}@{host} (global)",
        set_fn=partial(cli.secret_set_custom, host=host, env=env, value=token, sandbox=None),
        rm_candidates=partial(custom_rm_candidates, cli, host, env, None),
        strict=True,
    )


def verify_secret_visibility(
    cli: SbxCLI, *, env: str, workspace: Path, template: str | None = None
) -> bool | None:
    """Boot a throwaway sandbox and check whether the proxy-injected env is
    visible to exec'd processes (the same probe provisioning runs per-run).

    Returns True (proxy strategy will hold), False (runs will fall back to
    the in-VM plain-env file), or None when the check could not run. The
    sandbox is always removed.
    """
    name = f"sbxloop-secretcheck-{token_hex(4)}"
    spec = SandboxSpec(name=name, role="agent", workspace=workspace, template=template)
    try:
        cli.create(spec)
    except SbxError:
        log.warning("secret.visibility_check_create_failed", sandbox=name, exc_info=True)
        return None
    try:
        result = cli.exec(name, ["sh", "-lc", f'test -n "${{{env}}}"'])
        return result.ok
    except SbxError:
        log.warning("secret.visibility_check_failed", sandbox=name, env=env, exc_info=True)
        return None
    finally:
        try:
            cli.rm(name)
        except SbxError:
            log.warning("secret.visibility_check_remove_failed", sandbox=name, exc_info=True)


# -- what the CLI and the console both show -----------------------------------------


def secrets_context(config: Config, cli: SbxCLI | None = None) -> tuple[SbxCLI, set[str]]:
    """The sbx handle and the live sbxloop sandbox scopes every secrets
    command judges registrations against."""
    cli = cli or SbxCLI(app_name=config.app_name or None)
    live = {i.name for i in cli.ls() if i.name.startswith(SANDBOX_SCOPE_PREFIX)}
    return cli, live


class SecretRow(BaseModel):
    """One tracked secret as ``sbxloop secrets list`` shows it."""

    model_config = ConfigDict(extra="forbid")

    env: str
    host: str
    state: CustomSecretState
    judgement: Assessment

    @property
    def expected(self) -> str:
        return f"custom @ {self.host} (per-run scope)"

    @property
    def actual(self) -> str:
        state = self.state
        if state.exists:
            actual = f"scope {state.scope or '(unknown)'}"
            if state.hosts:
                actual += f" @ {', '.join(state.hosts)}"
            return actual
        if state.exists is None:
            return "(undetermined)"
        return "not registered"


def secret_rows(
    config: Config, cli: SbxCLI, live: set[str], *, probe: bool = True
) -> list[SecretRow]:
    rows: list[SecretRow] = []
    for env, host in tracked_custom_secrets(config):
        state = inspect_custom_secret(cli, env, host=host, probe=probe)
        judgement = assess(state, canonical_host=host, live_sandboxes=live)
        rows.append(SecretRow(env=env, host=host, state=state, judgement=judgement))
    return rows


class CleanOutcome(BaseModel):
    """What ``secrets clean`` did (or, dry, would do) for one secret."""

    model_config = ConfigDict(extra="forbid")

    env: str
    message: str
    #: A removal happened (apply) or would (dry run).
    removed: bool = False
    #: sbx rejected every removal.
    failed: bool = False


def clean_secrets(
    config: Config, cli: SbxCLI, live: set[str], *, apply: bool, all_: bool
) -> list[CleanOutcome]:
    """Remove stale sbxloop-owned registrations (every owned one with
    ``all_``); dry unless ``apply``. Never a foreign scope, never the
    built-in ``github`` service secret."""
    outcomes: list[CleanOutcome] = []
    for env, host in tracked_custom_secrets(config):
        state = inspect_custom_secret(cli, env, host=host)
        judgement = assess(state, canonical_host=host, live_sandboxes=live)
        if not (judgement.stale or (all_ and judgement.owned)):
            outcomes.append(CleanOutcome(env=env, message=f"nothing to clean ({judgement.note})"))
            continue
        where = f"scope {state.scope or '(unknown)'}"
        if not apply:
            outcomes.append(
                CleanOutcome(
                    env=env,
                    message=f"would remove the registration in {where} — {judgement.note}",
                    removed=True,
                )
            )
            continue
        if any(rm() for rm in removal_ladder(cli, state, host=host)):
            outcomes.append(
                CleanOutcome(env=env, message=f"removed the registration in {where}", removed=True)
            )
        else:
            outcomes.append(
                CleanOutcome(
                    env=env, message=f"sbx rejected every removal for {where}", failed=True
                )
            )
    return outcomes
