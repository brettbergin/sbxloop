"""Host preparation, kept apart from installation (#898).

Installing sbxloop is the operator's own business: a home under
``$SBXLOOP_HOME``, an interpreter, the packages, Docker's ``sbx`` — every
byte of it lands in a directory the invoking account already owns, and
nothing in it needs an administrator. *Preparing the host* is a different
job, and some of it is not the operator's to do. Booting a microVM needs
the kernel's virtualisation device and an account allowed to open it;
formatting the sandbox's block devices needs ``mkfs.ext4``; keeping the
daemon alive unattended needs a per-user service manager that is reachable
and an account whose session lingers past logout.

This module answers one question per capability — *can this account do
this, on this host, right now* — from probes that change nothing: no group
added, no sandbox booted, no service started, no privilege elevated. A
capability that cannot be decided answers :data:`UNKNOWN` rather than
reporting a readiness it did not observe, because the one thing worse than
an unprepared host is a report that calls it ready: ``sbxloop init`` must
never record unattended persistence it could not confirm.

Access is *checked*, never inferred. Membership in a group named ``kvm`` is
the remedy Docker documents, not proof of anything: a group added in this
shell is not in this process's credentials until the next login, and a host
may hand the device out by ACL or under another group entirely. So the
probe puts the question to the kernel instead of reading a group list.

Every check is scoped to the platform and the mode it belongs to. The
virtualisation device and the filesystem tools are Linux concerns; the
service-manager and lingering checks are asked only where user units were
actually requested. Off Linux, and in a ``--no-systemd`` installation, they
answer :data:`NOT_APPLICABLE` so they cannot block a supported mode.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: The capability is there and this account can use it.
READY = "ready"
#: It is not on this host at all.
MISSING = "missing"
#: It is here, but not for this account.
DENIED = "denied"
#: The probe could not decide — never to be read as readiness.
UNKNOWN = "unknown"
#: It is not a prerequisite for this platform or this mode.
NOT_APPLICABLE = "n/a"

#: Where distributions keep the filesystem tools the sandbox backend needs.
#: Debian and its derivatives leave these off a non-root ``PATH``, so the
#: lookup adds them — exactly as ``sbxloop init`` does when it hands over to
#: Docker's installer, which refuses to run without ``mkfs.ext4``.
SBIN_PATH: tuple[str, ...] = ("/usr/sbin", "/sbin")

#: The kernel's virtualisation device: a sandbox is a microVM, and without
#: an openable one nothing boots here.
KVM_DEVICE = Path("/dev/kvm")

MKFS = "mkfs.ext4"

#: ``systemctl --user is-system-running`` states that all mean the manager
#: answered. Only ``running`` exits 0; a degraded or still-starting manager
#: is reachable, which is what is being asked. ``offline`` and ``unknown``
#: are deliberately absent: those are the answers of a manager that is not
#: there.
REACHABLE_STATES = frozenset(
    {"running", "degraded", "starting", "initializing", "maintenance", "stopping"}
)

_KVM_ABSENT = (
    "an administrator enables hardware virtualisation for this machine — in its firmware, "
    "or as nested virtualisation on a hosted VM — and loads the kvm module; until then no "
    "sandbox can boot on this host"
)
_KVM_DENIED = (
    "an administrator grants this account access to the device; Docker's Linux setup "
    "documents adding the account to the `kvm` group, and a group added now reaches this "
    "process only at the next login, so log out and back in before rechecking"
)
_MKFS_ABSENT = (
    "an administrator installs the distribution's e2fsprogs package (apt-get install "
    "e2fsprogs, dnf install e2fsprogs, apk add e2fsprogs); the sandbox backend formats its "
    "block devices with it and Docker's sbx installer refuses to run without it"
)
_NO_SYSTEMD = (
    "this host manages services another way: install without user units (`sbxloop init "
    "--no-systemd`) and start `sbxloop daemon` under whatever supervisor it does use"
)
_UNREACHABLE_MANAGER = (
    "log in as this account on the console or over ssh so its systemd user manager and "
    "$XDG_RUNTIME_DIR exist — `su` alone does not start one — or install without user "
    "units (`sbxloop init --no-systemd`)"
)
_LINGER_OFF = (
    "`loginctl enable-linger {user}` keeps this account's services running after logout; "
    "where polkit does not let the account enable its own, an administrator runs it"
)

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
Which = Callable[..., "str | None"]
Access = Callable[[Path, int], bool]
Exists = Callable[[Path], bool]


def _exists(path: Path) -> bool:
    return path.exists()


@dataclass(frozen=True)
class Capability:
    """One host capability as this account finds it.

    ``detail`` is what was observed; ``remedy`` is the next action, and
    ``admin`` says whether that action is an administrator's. Nothing here
    performs the remedy — naming it is the whole job.
    """

    name: str
    status: str
    detail: str
    remedy: str = ""
    admin: bool = False

    @property
    def ready(self) -> bool:
        """Observed working. ``UNKNOWN`` is never ready: a probe that could
        not decide has not established anything."""
        return self.status == READY

    @property
    def applicable(self) -> bool:
        return self.status != NOT_APPLICABLE

    @property
    def message(self) -> str:
        """The row an operator reads: what is wrong, then what fixes it and
        who has to do it."""
        if not self.remedy:
            return self.detail
        who = "needs an administrator: " if self.admin else ""
        return f"{self.detail} — {who}{self.remedy}"


def probe_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Ask a command and keep its answer, whatever it is.

    Unlike the runner ``sbxloop init`` drives its steps with, a probe's
    non-zero exit is data, not a failure: ``systemctl --user
    is-system-running`` exits 1 on a perfectly reachable degraded manager.
    """
    return subprocess.run(  # nosec B603 - fixed argv lists built here, never a shell
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


def _one_line(text: str, limit: int = 200) -> str:
    collapsed = " ".join(text.split())
    return collapsed[: limit - 1] + "…" if len(collapsed) > limit else collapsed


class HostPrep:
    """The host's capabilities, probed through seams a test can replace.

    Every external observation — the device, ``PATH``, the two systemd
    commands — goes through one of ``access``, ``which`` or ``run``, so the
    whole matrix is exercised without a machine that has (or lacks) any of
    it.
    """

    def __init__(
        self,
        *,
        system: str | None = None,
        env: Mapping[str, str] | None = None,
        run: Runner = probe_runner,
        which: Which = shutil.which,
        access: Access = os.access,
        exists: Exists = _exists,
        kvm_device: Path = KVM_DEVICE,
    ) -> None:
        self.system = system or platform.system()
        self.env = dict(os.environ if env is None else env)
        self.run = run
        self.which = which
        self.access = access
        self.exists = exists
        self.kvm_device = kvm_device

    # -- the capabilities ---------------------------------------------------------

    def kvm(self) -> Capability:
        """The virtualisation device, and whether this account may open it.

        Both halves matter and fail differently: a machine without nested
        virtualisation has no device to grant, while a machine that has one
        commonly hands it out to a group the operator is not in yet.
        """
        if not self._linux:
            return self._elsewhere("kvm", f"the kernel virtualisation device {self.kvm_device}")
        device = self.kvm_device
        try:
            present = self.exists(device)
        except OSError as exc:
            return Capability("kvm", UNKNOWN, f"{device} could not be inspected: {exc}")
        if not present:
            return Capability("kvm", MISSING, f"{device} does not exist", _KVM_ABSENT, admin=True)
        if not self.access(device, os.R_OK | os.W_OK):
            return Capability(
                "kvm",
                DENIED,
                f"{device} exists but this account cannot open it for reading and writing",
                _KVM_DENIED,
                admin=True,
            )
        return Capability("kvm", READY, f"{device} is open to this account")

    def filesystem_tools(self) -> Capability:
        """``mkfs.ext4``, looked up the way the sbx installer will see it."""
        if not self._linux:
            return self._elsewhere(MKFS, f"the sandbox backend's block-device tool {MKFS}")
        found = self.which(MKFS, path=self._tool_path)
        if found is None:
            return Capability(
                MKFS,
                MISSING,
                f"{MKFS} is on neither this account's PATH nor {', '.join(SBIN_PATH)}",
                _MKFS_ABSENT,
                admin=True,
            )
        if self.which(MKFS, path=self.env.get("PATH", "")) is None:
            return Capability(
                MKFS,
                READY,
                f"{found} — off this account's PATH, so sbxloop adds "
                f"{', '.join(SBIN_PATH)} when it runs the sbx installer",
            )
        return Capability(MKFS, READY, str(found))

    def user_manager(self) -> Capability:
        """Whether ``systemctl --user`` reaches this account's own manager.

        Asked before user units are written, not after: a session without
        one (a bare ``su``, a container with no ``$XDG_RUNTIME_DIR``) takes
        every ``systemctl --user`` call with a bus error, and units enabled
        into a manager that was never reached are not a service.
        """
        if not self._linux:
            return self._elsewhere("user service manager", "a per-user systemd manager")
        systemctl = self.which("systemctl", path=self.env.get("PATH"))
        if systemctl is None:
            return Capability(
                "user service manager",
                MISSING,
                "systemctl is not on PATH: this host does not run systemd",
                _NO_SYSTEMD,
            )
        answer = self._ask([systemctl, "--user", "is-system-running"])
        if answer is None:
            return Capability(
                "user service manager",
                UNKNOWN,
                f"`{systemctl} --user is-system-running` could not be run",
                _UNREACHABLE_MANAGER,
            )
        state = _one_line(answer.stdout) or _one_line(answer.stderr)
        if answer.returncode == 0 or state in REACHABLE_STATES:
            return Capability("user service manager", READY, f"reachable ({state or 'running'})")
        return Capability(
            "user service manager",
            DENIED,
            f"`systemctl --user` did not reach this account's manager: "
            f"{state or f'exit {answer.returncode}'}",
            _UNREACHABLE_MANAGER,
        )

    def lingering(self, user: str) -> Capability:
        """Whether *user*'s services survive logout — read, never assumed.

        ``loginctl enable-linger`` can be refused by polkit and can be
        reported as done on a host where lingering still reads off, so the
        state is read back from ``loginctl`` itself. What cannot be read is
        ``UNKNOWN``: an unattended deployment may not be told its daemon
        persists on the strength of a command that merely exited 0.
        """
        if not self._linux:
            return self._elsewhere("unattended persistence", "systemd user lingering")
        if not user:
            return Capability(
                "unattended persistence",
                UNKNOWN,
                "this session names no account ($USER and $LOGNAME are both unset), "
                "so lingering could not be read",
                _LINGER_OFF.format(user="<the service account>"),
            )
        loginctl = self.which("loginctl", path=self.env.get("PATH"))
        if loginctl is None:
            return Capability(
                "unattended persistence",
                UNKNOWN,
                "loginctl is not on PATH, so lingering could not be read",
                _LINGER_OFF.format(user=user),
            )
        answer = self._ask([loginctl, "show-user", user, "--property=Linger"])
        if answer is None or answer.returncode != 0:
            detail = _one_line(answer.stderr or answer.stdout) if answer else "the command failed"
            return Capability(
                "unattended persistence",
                UNKNOWN,
                f"`loginctl show-user {user}` did not answer: {detail or 'no output'}",
                _LINGER_OFF.format(user=user),
            )
        value = _one_line(answer.stdout).partition("=")[2].strip().lower()
        if value == "yes":
            return Capability("unattended persistence", READY, f"lingering is on for {user}")
        if value == "no":
            return Capability(
                "unattended persistence",
                MISSING,
                f"lingering is off for {user}: this account's services stop at logout",
                _LINGER_OFF.format(user=user),
            )
        return Capability(
            "unattended persistence",
            UNKNOWN,
            f"`loginctl show-user {user} --property=Linger` answered "
            f"{_one_line(answer.stdout) or 'nothing'}",
            _LINGER_OFF.format(user=user),
        )

    # -- the sets a caller wants ---------------------------------------------------

    def sandbox_capabilities(self) -> list[Capability]:
        """What booting a sandbox on this host needs, whoever installed it."""
        return [self.kvm(), self.filesystem_tools()]

    def service_capabilities(self, user: str) -> list[Capability]:
        """What an *unattended* install needs on top — asked only where user
        units were requested, so a ``--no-systemd`` host is never judged
        against them."""
        return [self.user_manager(), self.lingering(user)]

    def login_name(self) -> str:
        return self.env.get("USER") or self.env.get("LOGNAME") or ""

    # -- seams ---------------------------------------------------------------------

    @property
    def _linux(self) -> bool:
        return self.system == "Linux"

    @property
    def _tool_path(self) -> str:
        """``PATH`` as Docker's installer will see it under ``sbxloop init``."""
        return os.pathsep.join([*SBIN_PATH, self.env.get("PATH", os.defpath)])

    def _elsewhere(self, name: str, what: str) -> Capability:
        return Capability(
            name,
            NOT_APPLICABLE,
            f"not checked: {what} is a Linux prerequisite and this host is {self.system}",
        )

    def _ask(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return self.run(argv)
        except (OSError, subprocess.SubprocessError):
            return None


__all__ = [
    "DENIED",
    "KVM_DEVICE",
    "MISSING",
    "MKFS",
    "NOT_APPLICABLE",
    "REACHABLE_STATES",
    "READY",
    "SBIN_PATH",
    "UNKNOWN",
    "Capability",
    "HostPrep",
    "probe_runner",
]
