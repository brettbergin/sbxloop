"""Which hosts sbxloop runs on (#596), decided once and said clearly.

Runs, the daemon and the bake boot Docker Sandboxes microVMs through
``sbx``. Docker ships a per-user MSI for native Windows as well as Linux
and macOS builds. WSL2 remains another path for Windows hosts.
"""

from __future__ import annotations

import platform
from typing import NamedTuple

WSL2_GUIDE = (
    "install a Linux distribution under WSL2, turn on Docker Desktop's WSL integration "
    "for it, and install sbxloop inside that distribution (the install script and "
    "`sbxloop init` run there unchanged)"
)


class HostSupport(NamedTuple):
    """The host as sbxloop sees it: supported or not, and why."""

    system: str  # "Linux", "Darwin", "Windows", "WSL", …
    supported: bool
    detail: str

    @property
    def refusal(self) -> str:
        """The line a sandbox-needing command prints before exiting."""
        return f"sbxloop cannot run sandboxes on this host ({self.system}): {self.detail}"


def host_support(system: str | None = None, release: str | None = None) -> HostSupport:
    """Judge the host from ``platform.system()`` / ``platform.release()``
    (injectable for tests). WSL2 is a Linux kernel whose release names
    Microsoft; native Windows uses Docker's MSI."""
    system = system if system is not None else platform.system()
    release = release if release is not None else platform.release()
    if system == "Windows":
        return HostSupport(
            "Windows",
            True,
            "native Windows — Docker Sandboxes requires Windows 11 x64 and the "
            "Windows Hypervisor Platform; run `sbxloop doctor` for backend readiness",
        )
    if system == "Linux" and "microsoft" in release.lower():
        return HostSupport(
            "WSL",
            True,
            "a Linux distribution under WSL2 — supported; Docker Desktop's WSL integration "
            "must be on for this distribution so `sbx` can reach the Docker engine",
        )
    if system in ("Linux", "Darwin"):
        return HostSupport(system, True, f"{system} — supported")
    return HostSupport(
        system or "unknown",
        True,
        f"{system or 'an unknown OS'} — untested; sbxloop targets Linux, macOS and Windows",
    )


__all__ = ["WSL2_GUIDE", "HostSupport", "host_support"]
