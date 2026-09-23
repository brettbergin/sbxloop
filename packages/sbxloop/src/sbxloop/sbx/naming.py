"""Names of sbxloop-managed sandboxes, including names from older releases.

The resolved home path identifies an installation sharing an sbx app state.
Keep the digest stable with the daemon's previous naming scheme; names are
opaque identifiers to sbx, but their final components explain their purpose.
"""

from __future__ import annotations

import hashlib

from sbxloop.config import VcsKind
from sbxloop.paths import SbxloopHome
from sbxloop.sbx.models import SandboxRole


def instance_id(home: SbxloopHome) -> str:
    return hashlib.sha256(str(home.root.resolve()).encode()).hexdigest()[:8]


def instance_prefix(home: SbxloopHome) -> str:
    return f"sbxl-{instance_id(home)}"


def run_name(
    home: SbxloopHome, run_id: str, role: SandboxRole, *, vcs_kind: VcsKind = "github"
) -> str:
    purpose = (
        "run-agent"
        if role == "agent"
        else "run-credential-service"
        if role == "service"
        else f"run-vcs-{vcs_kind}"
    )
    return f"{instance_prefix(home)}-{run_id}-{purpose}"


def legacy_run_names(
    run_id: str, role: SandboxRole, *, vcs_kind: VcsKind = "github"
) -> tuple[str, ...]:
    suffix = vcs_kind if role == "github" else role
    current = f"sbxloop-{run_id}-{suffix}"
    if role == "github" and vcs_kind != "github":
        return (current, f"sbxloop-{run_id}-github")
    return (current,)


def run_name_candidates(
    home: SbxloopHome, run_id: str, role: SandboxRole, *, vcs_kind: VcsKind = "github"
) -> tuple[str, ...]:
    return (
        run_name(home, run_id, role, vcs_kind=vcs_kind),
        *legacy_run_names(run_id, role, vcs_kind=vcs_kind),
    )


def daemon_vcs_name(home: SbxloopHome, kind: VcsKind = "github") -> str:
    return f"{instance_prefix(home)}-daemon-vcs-{kind}"


def legacy_daemon_vcs_name(home: SbxloopHome, kind: VcsKind = "github") -> str:
    return f"sbxloop-daemon-{kind}-{instance_id(home)}"


def concierge_name(home: SbxloopHome) -> str:
    return f"{instance_prefix(home)}-daemon-chat-concierge"


def legacy_concierge_name(home: SbxloopHome) -> str:
    return f"sbxloop-concierge-{instance_id(home)}"


def is_managed_name(name: str) -> bool:
    return name.startswith(("sbxl-", "sbxloop-"))
