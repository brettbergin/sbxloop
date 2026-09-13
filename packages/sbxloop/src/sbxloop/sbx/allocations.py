"""Host receipts for reuse of VMs created with explicit resource limits.

The guest holds only a nonce, never the authoritative allocation. A missing
receipt (including a pre-cutover VM), changed limits, or a replaced guest
refuses reuse before provisioning mutates it. These are creation receipts,
not measurements of the backend's enforcement.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from sbxloop.errors import ProvisionError, SbxError
from sbxloop.paths import SbxloopHome
from sbxloop.resources import SandboxResources
from sbxloop.sbx.sandbox import SBXLOOP_DIR, Sandbox

ALLOCATION_ID = f"{SBXLOOP_DIR}/allocation-id"


class AllocationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nonce: str
    resources: SandboxResources


def _path(home: SbxloopHome, sandbox: Sandbox) -> Path:
    identity = json.dumps([sandbox.cli.app_name or "", sandbox.name])
    key = hashlib.sha256(identity.encode()).hexdigest()
    return home.sandbox_allocations / f"{key}.json"


def record_allocation(home: SbxloopHome, sandbox: Sandbox, resources: SandboxResources) -> None:
    record = AllocationRecord(nonce=secrets.token_hex(16), resources=resources)
    sandbox.mkdirs(SBXLOOP_DIR)
    sandbox.write_text(ALLOCATION_ID, record.nonce)
    path = _path(home, sandbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(record.model_dump_json(), encoding="utf-8")
    temporary.replace(path)


def require_allocation(home: SbxloopHome, sandbox: Sandbox, resources: SandboxResources) -> None:
    try:
        record = AllocationRecord.model_validate_json(_path(home, sandbox).read_text("utf-8"))
        if (
            record.resources.model_fields_set == {"cpus", "memory"}
            and record.resources == resources
            and sandbox.read_text(ALLOCATION_ID) == record.nonce
        ):
            return
    except (OSError, ValueError, SbxError):
        pass
    raise ProvisionError(
        f"sandbox {sandbox.name} needs recreation with {resources.cpus} CPUs and "
        f"{resources.memory} memory: its allocation receipt is missing, differs, "
        "or could not be verified. The existing VM is preserved. Stop the run/daemon, "
        "save any in-VM work and session history with `sbx cp`, then remove this "
        "sandbox with `sbx rm` and resume/restart to recreate it with the configured limits."
    )
