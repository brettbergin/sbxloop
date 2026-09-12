"""Private files on the host, under the platform's own access control.

``config/secrets.env`` and ``config/github-app.pem`` hold the credentials a
run is authorised with, so only the operator's own account may read them.
How that is expressed is the host's business, and it is not the same
everywhere:

- **POSIX** — a mode. The file is created and checked as ``0600``; anything
  else is a one-line remedy (``chmod 600``) the operator can act on.
- **Windows** — not a mode. ``os.chmod`` there toggles the read-only
  attribute and nothing else, so a file chmod'ed ``0600`` still reports
  ``0666``: a mode check can never pass, and the ``chmod 600`` it suggests
  can never clear it. Privacy is a discretionary ACL instead, set and read
  through ``icacls``.

Both halves of the contract matter. A checker that could not read a file's
access control says so and reports the file as *not known private* — never
"fine", which would quietly call every Windows secrets file safe. And a
setter that could not restrict a file raises, so the caller does not go on
to write a token into a world-readable file believing it protected.

The Windows branch is exercised in tests through the ``runner`` seam on
every platform; its behaviour against a real ``icacls`` is **field-unverified**.
"""

from __future__ import annotations

import getpass
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple, Protocol

from sbxloop.errors import SbxloopError

#: The private-file mode on a POSIX host: the owner, and nobody else.
PRIVATE_FILE_MODE = 0o600

#: Principals a private file may still name on Windows besides its owner.
#: Both are the machine's own administrative identities: they can read any
#: file on the host regardless of its ACL, so requiring their absence would
#: fail every file without protecting anything.
WINDOWS_ALLOWED_PRINCIPALS = ("NT AUTHORITY\\SYSTEM", "BUILTIN\\ADMINISTRATORS")


class PrivacyError(SbxloopError):
    """A file could not be restricted to this user."""


class Runner(Protocol):
    """The ``icacls`` seam: a test drives the Windows branch on any host."""

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]: ...


class Privacy(NamedTuple):
    """What a host says about one file's access control.

    ``private`` is deliberately three-valued: True (only this user reaches
    it), False (someone else does), and None — *could not tell*, which is a
    failure to report, not a pass to grant.
    """

    private: bool | None
    #: What was found, for the row that reports it ("mode 0600", …).
    detail: str
    #: What the operator can do about it, empty when there is nothing to do.
    remedy: str

    @property
    def ok(self) -> bool:
        return self.private is True


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 - fixed argv, no shell
        list(argv), capture_output=True, text=True, check=False
    )


def _account() -> str:
    """This process's account, as ``icacls`` spells principals: the domain
    (or machine) and the user name."""
    user = getpass.getuser()
    domain = os.environ.get("USERDOMAIN", "").strip()
    return f"{domain}\\{user}" if domain else user


def _icacls_principals(output: str, path: Path) -> list[str]:
    """The principals named in a plain ``icacls <path>`` listing.

    The first line carries the path before the first ACE; every later ACE
    is indented. The trailing "Successfully processed N files" summary and
    any blank line are not ACEs.
    """
    principals: list[str] = []
    prefix = str(path)
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("successfully processed"):
            continue
        if line.startswith(prefix):
            line = line[len(prefix) :].strip()
        if not line:
            continue
        principal, sep, _rights = line.partition(":")
        if not sep:
            continue
        principals.append(principal.strip())
    return principals


def _is_allowed(principal: str, account: str) -> bool:
    """Whether one ACE's principal is this user or a machine identity.

    A user is named either bare or domain-qualified depending on how the ACE
    was written, so the comparison is on the account name with any domain
    dropped from both sides.
    """
    folded = principal.upper()
    if folded in WINDOWS_ALLOWED_PRINCIPALS:
        return True
    return folded.rpartition("\\")[2] == account.upper().rpartition("\\")[2]


def _windows_privacy(path: Path, runner: Runner) -> Privacy:
    grant = f'icacls {path} /inheritance:r /grant:r "{_account()}":F'
    try:
        result = runner(["icacls", str(path)])
    except OSError as exc:
        return Privacy(None, f"its access control could not be read ({exc})", grant)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        said = detail[0] if detail else f"icacls exited {result.returncode}"
        return Privacy(None, f"its access control could not be read ({said})", grant)
    account = _account()
    principals = _icacls_principals(result.stdout, path)
    if not principals:
        return Privacy(None, "its access control listed no entries", grant)
    others = sorted({p for p in principals if not _is_allowed(p, account)})
    if others:
        return Privacy(False, f"is also readable by {', '.join(others)}", grant)
    return Privacy(True, f"is restricted to {account}", "")


def _posix_privacy(path: Path) -> Privacy:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        return Privacy(None, f"its mode could not be read ({exc})", f"chmod 600 {path}")
    if mode == PRIVATE_FILE_MODE:
        return Privacy(True, f"mode {mode:04o}", "")
    return Privacy(False, f"mode {mode:04o}", f"chmod 600 {path}")


def privacy(path: Path, *, os_name: str = os.name, runner: Runner | None = None) -> Privacy:
    """Whether ``path`` is readable only by this user, and how it is spelled.

    Never raises: the caller is a diagnostic row, and a host that could not
    answer is itself the finding (``private is None``).
    """
    if os_name == "nt":
        return _windows_privacy(path, runner or _run)
    return _posix_privacy(path)


def make_private(path: Path, *, os_name: str = os.name, runner: Runner | None = None) -> None:
    """Restrict ``path`` to this user with the host's own access control.

    Raises :class:`PrivacyError` when it could not — a caller about to write
    a credential must not carry on believing the file protected.
    """
    if os_name != "nt":
        path.chmod(PRIVATE_FILE_MODE)
        return
    account = _account()
    argv = ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:F"]
    try:
        result = (runner or _run)(argv)
    except OSError as exc:
        raise PrivacyError(f"could not restrict {path} to {account}: {exc}") from exc
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        said = output or f"icacls exited {result.returncode}"
        raise PrivacyError(f"could not restrict {path} to {account}: {said}")


def create_private(path: Path, *, os_name: str = os.name, runner: Runner | None = None) -> None:
    """Create ``path`` if it is missing and restrict it, in that order.

    On POSIX the mode rides the creation so there is no window in which the
    file exists world-readable; Windows has no such argument, so the file is
    created empty (no credential in it yet) and restricted immediately.
    """
    if not path.exists():
        if os_name == "nt":
            path.touch()
        else:
            path.touch(mode=PRIVATE_FILE_MODE)
    make_private(path, os_name=os_name, runner=runner)


__all__ = [
    "PRIVATE_FILE_MODE",
    "WINDOWS_ALLOWED_PRINCIPALS",
    "Privacy",
    "PrivacyError",
    "Runner",
    "create_private",
    "make_private",
    "privacy",
]
