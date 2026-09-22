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
from collections.abc import Collection

from pydantic import BaseModel, ConfigDict

from sbxloop.backends import BACKENDS
from sbxloop.config import VCS_KINDS, Config
from sbxloop.engine.model import TERMINAL_RUN_STATES
from sbxloop.engine.store import StateStore
from sbxloop.errors import SbxError, StateError
from sbxloop.ids import is_run_id
from sbxloop.paths import SbxloopHome
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import SandboxInfo, SandboxRole
from sbxloop.sbx.naming import instance_id, is_managed_name

# A run in a terminal state has already had (or never needed) its teardown:
# any of its sandboxes still present are leaked. The resumable terminal
# states ("failed", "blocked", "cancelled") are safe to include — resume
# re-provisions a fresh pair.

# Age a run must be inactive before its sandboxes count as orphaned. Guards
# against racing a run that another terminal just started or is mid-phase.
DEFAULT_MIN_AGE_S = 3600.0

_VCS_NAME_KINDS = "|".join(VCS_KINDS)
_NAME_RE = re.compile(rf"^sbxloop-(?P<run>[^-]+)-(?P<role>agent|service|{_VCS_NAME_KINDS})$")
_NEW_NAME_RE = re.compile(
    rf"^sbxl-(?P<instance>[0-9a-f]{{8}})-(?P<run>[^-]+)-run-"
    rf"(?P<role>agent|credential-service|vcs-(?:{_VCS_NAME_KINDS}))$"
)

# Sandboxes the daemon owns for its whole lifetime (not tied to a run):
# the VCS-ops box and the concierge box. Never pruned here — the daemon
# manages them; `sbxloop sandbox rm` removes them explicitly.
DAEMON_OWNED_PREFIXES = (
    *(f"sbxloop-daemon-{kind}-" for kind in VCS_KINDS),
    "sbxloop-concierge-",
)


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
    warm_run_ids: Collection[str] = (),
    home: SbxloopHome | None = None,
) -> list[SandboxVerdict]:
    """Classify sbxloop sandboxes against the state DB and home identity.

    Other sandboxes are never considered. Unrecognized names with a managed
    prefix are reported but never marked orphaned. ``warm_run_ids`` are the daemon's warm sets
    (#47): sandboxes standing by under a run id no run has taken yet, which
    the state DB cannot know about and prune must leave alone.
    """
    now = time.time() if now is None else now
    verdicts: list[SandboxVerdict] = []
    for info in infos:
        if not is_managed_name(info.name):
            continue
        verdicts.append(
            _classify_one(
                info.name,
                store,
                min_age_s=min_age_s,
                include_kept=include_kept,
                now=now,
                warm_run_ids=warm_run_ids,
                home=home,
            )
        )
    return verdicts


def _classify_one(
    name: str,
    store: StateStore,
    *,
    min_age_s: float,
    include_kept: bool,
    now: float,
    warm_run_ids: Collection[str] = (),
    home: SbxloopHome | None = None,
) -> SandboxVerdict:
    if name.startswith(DAEMON_OWNED_PREFIXES) or re.match(r"^sbxl-[0-9a-f]{8}-daemon-", name):
        return SandboxVerdict(
            name=name,
            reason="daemon-owned sandbox (VCS-ops / concierge); not touched — "
            "`sbxloop sandbox rm` removes it explicitly",
        )
    new_match = _NEW_NAME_RE.match(name)
    if new_match is not None and (home is None or new_match.group("instance") != instance_id(home)):
        return SandboxVerdict(name=name, reason="belongs to another or unknown sbxloop home")
    match = new_match or _NAME_RE.match(name)
    if match is None or not is_run_id(match.group("run")):
        return SandboxVerdict(
            name=name,
            reason="unrecognized sbxloop naming scheme; not touched",
        )
    run_id, suffix = match.group("run"), match.group("role")
    role = (
        "github"
        if suffix in VCS_KINDS or suffix.startswith("vcs-")
        else "service"
        if suffix == "credential-service"
        else suffix
    )
    if run_id in warm_run_ids:
        return SandboxVerdict(
            name=name,
            run_id=run_id,
            role=role,
            reason="warm sandbox set standing by for the next run (`[daemon] warm_pairs`); "
            "not touched — the daemon retires it itself",
        )

    try:
        run = store.get_run(run_id)
    except StateError:
        return SandboxVerdict(
            name=name,
            run_id=run_id,
            role=role,
            orphan=new_match is not None,
            reason=(
                "owned by this home but unknown to its state DB"
                if new_match is not None
                else "unknown to this state DB (may belong to another working copy); not pruned"
            ),
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


def remove_run_sandbox_secrets(cli: SbxCLI, name: str, role: SandboxRole, config: Config) -> None:
    """Best-effort: unregister the secrets provisioning bound to ``name``.

    ``sbx rm`` removes the microVM but not the secret registrations keyed
    by its name. Left behind, they poison the next provision under the same
    name: replace-on-exists cannot replace, keeps the stale entry, and the
    agent sandbox ends up with the proxy sentinel instead of a usable token
    (field failure rgn9ccjam — a daemon recovery resumed a killed run and
    the Copilot SDK got 401). Agent sandboxes carry the configured
    backend's custom secret — and since the backend may have changed since
    the sandbox was provisioned, every backend's registration is tried
    (#617), each as it binds under ``config``; github sandboxes the
    built-in ``github`` service secret; a service sandbox (#765) registers
    nothing — its credentials never touch the sbx proxy — so there is
    nothing to remove.
    """
    if role == "service":
        return
    if role == "github":
        with contextlib.suppress(SbxError):
            cli.secret_rm(service="github", sandbox=name)
        return
    for backend in BACKENDS:
        try:
            env, host = backend.secret(config)
        except ValueError:
            # No endpoint to bind under this config (the openai backend
            # while another is selected): provisioning under it could not
            # have registered anything, so there is nothing to name.
            continue
        with contextlib.suppress(SbxError):
            cli.secret_rm(host=host, env=env, sandbox=name)


def remove_run_sandbox(cli: SbxCLI, name: str, role: SandboxRole, config: Config) -> None:
    """Remove a run's sandbox AND its registered secrets — the pair that a
    dead process leaves behind and a re-provision under the same name
    trips over. Sandbox removal errors propagate; secret removal is
    best-effort (registration syntax is not a stable sbx API)."""
    remove_sandbox(cli, name)
    remove_run_sandbox_secrets(cli, name, role, config)


def count_orphans(
    cli: SbxCLI,
    store: StateStore,
    warm_run_ids: Collection[str] = (),
    *,
    home: SbxloopHome | None = None,
) -> int:
    """Orphan-candidate count with default thresholds (doctor's view)."""
    verdicts = classify_sandboxes(cli.ls(), store, warm_run_ids=warm_run_ids, home=home)
    return sum(1 for v in verdicts if v.orphan)
