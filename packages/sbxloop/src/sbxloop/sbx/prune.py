"""Orphaned-sandbox classification for ``sbxloop sandbox prune`` and doctor.

The in-process cleanup registry only protects against failures inside a live
sbxloop process; a host crash, OOM-kill, or ``kill -9`` leaves the run's
sandbox pair running indefinitely. This module cross-references ``sbx ls``
against the state DB to find such leaks safely.

Honesty caveat: the state DB is per working copy, but sandboxes live on the
sbx machine, which may serve several working copies. A sandbox "unknown to
this state DB" may simply belong to another checkout's runs — verdicts say
so explicitly, and nothing is ever removed without ``--force``.
"""

from __future__ import annotations

import contextlib
import re
import time

from pydantic import BaseModel, ConfigDict

from sbxloop.engine.store import StateStore
from sbxloop.errors import SbxError, StateError
from sbxloop.ids import is_run_id
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxInfo, SandboxRole
from sbxloop.sbx.secretstate import COPILOT_TOKEN_ENV, COPILOT_TOKEN_HOST

# A run in one of these states has already had (or never needed) its
# teardown: any of its sandboxes still present are leaked. "failed" is safe
# to include even though it is resumable — resume re-provisions a fresh pair.
TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})

# Age a run must be inactive before its sandboxes count as orphaned. Guards
# against racing a run that another terminal just started or is mid-phase.
DEFAULT_MIN_AGE_S = 3600.0

_NAME_RE = re.compile(r"^sbxloop-(?P<run>[^-]+)-(?P<role>agent|github)$")


class SandboxVerdict(BaseModel):
    """One sandbox's classification: what we know and whether it is prunable."""

    model_config = ConfigDict(extra="forbid")

    name: str
    run_id: str | None = None  # parsed from the name; None → unrecognized
    role: str | None = None
    run_state: str | None = None  # None → unknown to this state DB
    kept_reason: str | None = None
    age_s: float | None = None  # since last DB activity; None → no signal
    orphan: bool = False
    reason: str


def format_age(age_s: float | None) -> str:
    if age_s is None:
        return "?"
    if age_s < 3600:
        return f"{age_s / 60:.0f}m"
    if age_s < 48 * 3600:
        return f"{age_s / 3600:.1f}h"
    return f"{age_s / 86400:.1f}d"


def classify_sandboxes(
    infos: list[SandboxInfo],
    store: StateStore,
    *,
    min_age_s: float = DEFAULT_MIN_AGE_S,
    include_kept: bool = False,
    now: float | None = None,
) -> list[SandboxVerdict]:
    """Classify every ``sbxloop-*`` sandbox against the state DB.

    Non-sbxloop sandboxes are never considered. Names that carry the prefix
    but do not match the ``sbxloop-<run>-<role>`` scheme (future taxonomies:
    warm-pool standby, etc.) are reported but never marked orphaned.
    """
    now = time.time() if now is None else now
    verdicts: list[SandboxVerdict] = []
    for info in infos:
        if not info.name.startswith("sbxloop-"):
            continue
        verdicts.append(
            _classify_one(info.name, store, min_age_s=min_age_s, include_kept=include_kept, now=now)
        )
    return verdicts


def _classify_one(
    name: str,
    store: StateStore,
    *,
    min_age_s: float,
    include_kept: bool,
    now: float,
) -> SandboxVerdict:
    match = _NAME_RE.match(name)
    if match is None or not is_run_id(match.group("run")):
        return SandboxVerdict(
            name=name,
            reason="unrecognized sbxloop naming scheme; not touched",
        )
    run_id, role = match.group("run"), match.group("role")

    try:
        run = store.get_run(run_id)
    except StateError:
        return SandboxVerdict(
            name=name,
            run_id=run_id,
            role=role,
            orphan=True,
            reason="unknown to this state DB (may belong to another working copy)",
        )

    if run.kept_reason is not None and not include_kept:
        return SandboxVerdict(
            name=name,
            run_id=run_id,
            role=role,
            run_state=run.state,
            kept_reason=run.kept_reason,
            age_s=now - run.updated_at,
            reason=f"kept ({run.kept_reason}); use --include-kept to prune",
        )

    # Liveness = the newest thing the run ever wrote: state transitions bump
    # updated_at, and every bus event (heartbeats included) is persisted.
    last_activity = max(run.updated_at, store.last_event_ts(run_id) or 0.0)
    age_s = now - last_activity
    kept_note = f", kept ({run.kept_reason})" if run.kept_reason is not None else ""

    if run.state in TERMINAL_RUN_STATES:
        if age_s >= min_age_s:
            return SandboxVerdict(
                name=name,
                run_id=run_id,
                role=role,
                run_state=run.state,
                kept_reason=run.kept_reason,
                age_s=age_s,
                orphan=True,
                reason=f"run {run.state} {format_age(age_s)} ago{kept_note}",
            )
        return SandboxVerdict(
            name=name,
            run_id=run_id,
            role=role,
            run_state=run.state,
            kept_reason=run.kept_reason,
            age_s=age_s,
            reason=f"run {run.state} only {format_age(age_s)} ago (younger than --min-age)",
        )

    if age_s >= min_age_s:
        return SandboxVerdict(
            name=name,
            run_id=run_id,
            role=role,
            run_state=run.state,
            kept_reason=run.kept_reason,
            age_s=age_s,
            orphan=True,
            reason=f"run {run.state} but silent for {format_age(age_s)}{kept_note}",
        )
    return SandboxVerdict(
        name=name,
        run_id=run_id,
        role=role,
        run_state=run.state,
        kept_reason=run.kept_reason,
        age_s=age_s,
        reason=f"run {run.state}, active {format_age(age_s)} ago (possibly live)",
    )


def remove_sandbox(cli: SbxCLI, name: str) -> None:
    """Stop (best-effort) then force-remove one sandbox.

    ``stop`` failing is expected for already-stopped sandboxes; ``rm``
    failing propagates so callers can report it.
    """
    with contextlib.suppress(SbxError):
        cli.stop(name)
    cli.rm(name, force=True)


def remove_run_sandbox_secrets(cli: SbxCLI, name: str, role: SandboxRole) -> None:
    """Best-effort: unregister the secrets provisioning bound to ``name``.

    ``sbx rm`` removes the microVM but not the secret registrations keyed
    by its name. Left behind, they poison the next provision under the same
    name: replace-on-exists cannot replace, keeps the stale entry, and the
    agent sandbox ends up with the proxy sentinel instead of a usable token
    (field failure rgn9ccjam — a daemon recovery resumed a killed run and
    the Copilot SDK got 401). Agent sandboxes carry the Copilot custom
    secret; github sandboxes the built-in ``github`` service secret.
    """
    with contextlib.suppress(SbxError):
        if role == "agent":
            cli.secret_rm(host=COPILOT_TOKEN_HOST, env=COPILOT_TOKEN_ENV, sandbox=name)
            cli.secret_rm(env=COPILOT_TOKEN_ENV, sandbox=name)
        else:
            cli.secret_rm(service="github", sandbox=name)


def remove_run_sandbox(cli: SbxCLI, name: str, role: SandboxRole) -> None:
    """Remove a run's sandbox AND its registered secrets — the pair that a
    dead process leaves behind and a re-provision under the same name
    trips over. Sandbox removal errors propagate; secret removal is
    best-effort (registration syntax is not a stable sbx API)."""
    remove_sandbox(cli, name)
    remove_run_sandbox_secrets(cli, name, role)


def count_orphans(cli: SbxCLI, store: StateStore) -> int:
    """Orphan-candidate count with default thresholds (doctor's view)."""
    return sum(1 for v in classify_sandboxes(cli.ls(), store) if v.orphan)
