"""A Linux host with whatever preparation a test wants it to have (#898).

Every capability :mod:`sbxloop.hostprep` probes is answered here through the
seams the real class already takes — an ``access`` callable, a ``which``
callable and a runner for the two systemd commands — so the matrix runs
against the *real* ``HostPrep`` logic on any runner, whatever that runner's
own ``/dev/kvm``, ``PATH`` or session happen to be. Nothing here touches the
machine: no device is created, no group joined, no service started.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from sbxloop.hostprep import HostPrep

#: The states this fake's ``systemctl --user is-system-running`` can report.
#: "offline" is what a session with no user manager answers.
MANAGER_RUNNING = "running"
MANAGER_DEGRADED = "degraded"
MANAGER_OFFLINE = "offline"


def fake_prep(
    *,
    system: str = "Linux",
    kvm: str = "open",
    mkfs: str | None = "/usr/sbin/mkfs.ext4",
    mkfs_on_path: bool = False,
    systemctl: str | None = "/bin/systemctl",
    loginctl: str | None = "/bin/loginctl",
    manager: str = MANAGER_RUNNING,
    linger: str | None = "yes",
    env: dict[str, str] | None = None,
    device: Path = Path("/dev/kvm"),
) -> HostPrep:
    """A :class:`HostPrep` over a synthetic host.

    ``kvm`` is ``"open"``, ``"denied"`` (the node is there, this account may
    not open it), ``"absent"`` or ``"unreadable"`` (the node's very
    existence cannot be established); ``mkfs`` is where ``mkfs.ext4`` was
    found or ``None``; ``manager`` is what ``systemctl --user
    is-system-running`` says; ``linger`` is ``"yes"``, ``"no"``, ``None``
    (``loginctl`` refuses to answer) or any other string to model an answer
    nothing can be read out of.
    """

    def exists(path: Path) -> bool:
        if kvm == "unreadable":
            raise PermissionError(13, "Permission denied")
        return path == device and kvm != "absent"

    def access(path: Path, _mode: int) -> bool:
        return path == device and kvm == "open"

    def which(name: str, path: str | None = None) -> str | None:
        if name == "mkfs.ext4":
            # `path` is the augmented lookup when sbxloop adds the sbin
            # directories and the bare one when it asks whether this
            # account would find the tool itself.
            augmented = path is not None and "/usr/sbin" in path
            return mkfs if augmented or mkfs_on_path else None
        return {"systemctl": systemctl, "loginctl": loginctl}.get(name)

    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in argv]
        if argv[1:2] == ["--user"]:
            return subprocess.CompletedProcess(
                argv, 0 if manager == MANAGER_RUNNING else 1, manager + "\n", ""
            )
        if argv[1:2] == ["show-user"]:
            if linger is None:
                return subprocess.CompletedProcess(argv, 1, "", "Failed to get user: No such user")
            return subprocess.CompletedProcess(argv, 0, f"Linger={linger}\n", "")
        raise AssertionError(f"unexpected probe: {argv}")

    return HostPrep(
        system=system,
        env=env if env is not None else {"PATH": "/usr/bin", "USER": "bergs"},
        run=run,
        which=which,
        access=access,
        exists=exists,
        kvm_device=device,
    )


__all__ = ["MANAGER_DEGRADED", "MANAGER_OFFLINE", "MANAGER_RUNNING", "fake_prep"]
