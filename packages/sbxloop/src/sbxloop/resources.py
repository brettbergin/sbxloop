"""Explicit VM allocations, shared by configuration and sandbox creation."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

ResourcePurpose = Literal["agent", "concierge", "github", "service"]
CpuCount = Annotated[int, Field(strict=True, gt=0)]


def _memory(value: str) -> str:
    """Accept whole MiB/GiB and canonicalize equivalent allocations."""
    if not re.fullmatch(r"[1-9][0-9]*[mg]", value, re.IGNORECASE):
        raise ValueError("memory must be a positive whole size in MiB or GiB, e.g. '2048m' or '2g'")
    mb = int(value[:-1]) * (1024 if value[-1].lower() == "g" else 1)
    return f"{mb // 1024}g" if mb % 1024 == 0 else f"{mb}m"


MemoryLimit = Annotated[str, AfterValidator(_memory)]


class SandboxResources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cpus: CpuCount = 6
    memory: MemoryLimit = "12g"
