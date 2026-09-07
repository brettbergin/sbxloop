"""Which hosts sbxloop runs on (#596), decided once and said clearly.

The sandbox layer is the hard constraint: runs, the daemon and the bake
all boot Docker Sandboxes microVMs through ``sbx``, which Docker ships for
macOS and Linux. On Windows the supported path is WSL2 — a Linux
distribution with Docker Desktop's WSL integration on — where sbxloop is
just a Linux install. Native Windows is refused by name at the commands
that need a sandbox, rather than failing part-way through provisioning
with a path or shell error; the read-only commands (``doctor``, ``logs``,
``config``) still answer so the refusal can be diagnosed from the host.
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
    Microsoft; native Windows is the one unsupported host."""
    system = system if system is not None else platform.system()
    release = release if release is not None else platform.release()
    if system == "Windows":
        return HostSupport(
            "Windows",
            False,
            f"the sandbox runtime (Docker Sandboxes, `sbx`) has no native Windows build; "
            f"{WSL2_GUIDE}",
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
        f"{system or 'an unknown OS'} — untested; sbxloop is developed on Linux and macOS",
    )


__all__ = ["WSL2_GUIDE", "HostSupport", "host_support"]
