"""Cheap in-sandbox resource sampling for the heartbeat cadence.

Everything here reads local kernel state (statvfs, /proc, loadavg) — no
subprocesses — so a sample is unobservable in run wall-clock. Fields that
cannot be read on the current platform are simply omitted; consumers treat
every field as optional.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

MEMINFO_PATH = Path("/proc/meminfo")

# Severity ordering for escalation edges (ok -> warn -> abort).
LEVEL_SEVERITY = {"ok": 0, "warn": 1, "abort": 2}


def sample_resources(path: str = ".") -> dict[str, Any]:
    """One resource sample: disk usage of ``path``'s filesystem (the worker
    runs with cwd = the run workspace), memory from /proc/meminfo, and the
    1-minute load average."""
    sample: dict[str, Any] = {}
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total > 0:
            sample["disk_total_bytes"] = total
            sample["disk_free_bytes"] = free
            sample["disk_used_pct"] = round(100.0 * (1.0 - free / total), 1)
    except OSError:
        pass
    try:
        fields: dict[str, int] = {}
        for line in MEMINFO_PATH.read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                fields[key] = int(rest.split()[0])  # kB
        total_kb = fields.get("MemTotal", 0)
        available_kb = fields.get("MemAvailable")
        if total_kb > 0 and available_kb is not None:
            sample["mem_total_kb"] = total_kb
            sample["mem_available_kb"] = available_kb
            sample["mem_used_pct"] = round(100.0 * (1.0 - available_kb / total_kb), 1)
    except (OSError, ValueError, IndexError):
        pass
    with contextlib.suppress(OSError):
        sample["load1"] = round(os.getloadavg()[0], 2)
    return sample


def classify_level(
    sample: dict[str, Any],
    *,
    disk_warn: float = 0.0,
    disk_abort: float = 0.0,
    mem_warn: float = 0.0,
    mem_abort: float = 0.0,
) -> str:
    """Guardrail level for a sample: "ok", "warn", or "abort".

    A threshold of 0 (the worker default when the host passes none) disables
    that guardrail. Memory abort is opt-in on the host side (#253) because
    a parallel test run spikes MemAvailable transiently; the worker just
    classifies whatever thresholds it is handed.
    """
    disk = sample.get("disk_used_pct")
    mem = sample.get("mem_used_pct")
    if isinstance(disk, (int, float)) and disk_abort > 0 and disk >= disk_abort:
        return "abort"
    if isinstance(mem, (int, float)) and mem_abort > 0 and mem >= mem_abort:
        return "abort"
    if isinstance(disk, (int, float)) and disk_warn > 0 and disk >= disk_warn:
        return "warn"
    if isinstance(mem, (int, float)) and mem_warn > 0 and mem >= mem_warn:
        return "warn"
    return "ok"
