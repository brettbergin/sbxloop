"""Explicit updates of the installation owned by ``sbxloop init``.

The daemon's PyPI lookup is reused, but an unavailable or invalid answer
here is an error. Only the running home's venv can be changed: a checkout,
pipx tool or externally managed environment belongs to its own installer.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from importlib import metadata
from pathlib import Path

from packaging.version import InvalidVersion, Version

import sbxloop
from sbxloop.daemon import versions
from sbxloop.errors import SbxloopError
from sbxloop.homeinit import INSTALL_EXTRAS
from sbxloop.paths import SbxloopHome

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
VERIFY_SCRIPT = (
    "import json, sbxloop, sbxloop_worker; "
    "print(json.dumps([sbxloop.__version__, sbxloop_worker.__version__]))"
)


class UpdateError(SbxloopError):
    """The update could not be completed or verified."""


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 - fixed argv, validated version, never a shell
        list(argv),
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=600,
    )


def _version(raw: str | None, source: str) -> Version:
    if not raw or raw == versions.UNBUILT:
        raise UpdateError(f"could not determine {source} sbxloop version")
    try:
        return Version(raw)
    except InvalidVersion as exc:
        raise UpdateError(f"invalid {source} sbxloop version: {raw!r}") from exc


def _require_home_install(home: SbxloopHome) -> None:
    venv = home.venv.resolve()
    if Path(sys.prefix).resolve() != venv or not Path(sbxloop.__file__).resolve().is_relative_to(
        venv
    ):
        raise UpdateError(
            "automatic updates require the home's own installation; "
            f"invoke {home.launcher} update. For a checkout, pipx, uv tool or another "
            "managed install, use its installer, or run `sbxloop init` to create a home"
        )
    if home.read_record() is None:
        raise UpdateError(f"missing or invalid {home.record}; repair the home with `sbxloop init`")
    for path in (home.uv, home.venv_python):
        if not path.is_file():
            raise UpdateError(f"missing {path}; repair the home with `sbxloop init`")


def _install_spec(latest: Version) -> list[str]:
    extras = INSTALL_EXTRAS.split(",")
    # init supplies both chat bridges. Retain the optional host SDK too when
    # present, so resolving the new release also honours its SDK constraint.
    try:
        metadata.version("github-copilot-sdk")
    except metadata.PackageNotFoundError:
        pass
    else:
        extras.append("copilot")
    return [f"sbxloop[{','.join(extras)}]=={latest}", f"sbxloop-worker=={latest}"]


def update_home(
    home: SbxloopHome,
    *,
    check: bool = False,
    dry_run: bool = False,
    say: Callable[[str], None],
    run: Runner | None = None,
) -> None:
    """Check PyPI, then install and verify a newer release in this home.

    ``--check`` works from any installation; ``--dry-run`` validates the
    destination and prints the same argv an update would execute. Neither
    runs an installer. Running daemons are left for the operator to restart.
    """
    current = _version(sbxloop.__version__, "installed")
    say(f"Installed sbxloop: {current}")
    newest = versions.fetch_latest("sbxloop")
    if newest is None:
        raise UpdateError("could not check PyPI for the latest sbxloop release; try again later")
    latest = _version(newest, "PyPI")
    if latest.is_prerelease or latest.local is not None:
        raise UpdateError(f"PyPI did not report a stable release: {latest}")
    say(f"Latest sbxloop on PyPI: {latest}")
    if current.is_devrelease or current.local is not None:
        message = "development build; use the checkout's installer to update it"
        if check:
            say(message)
            return
        raise UpdateError(message)
    if current >= latest:
        say(
            "Already up to date."
            if current == latest
            else "Installed version is newer; keeping it."
        )
        return
    say(f"Update available: {current} -> {latest}")
    if check:
        return
    _require_home_install(home)
    argv = [
        str(home.uv),
        "--no-config",
        "pip",
        "install",
        "--python",
        str(home.venv_python),
        *_install_spec(latest),
    ]
    if dry_run:
        say(f"Would run: {shlex.join(argv)}")
        return
    run = run or _run
    say(f"Updating {home.venv} to sbxloop {latest}...")
    step = "installation"
    try:
        run(argv)
        step = "verification"
        # Fresh imports from the target interpreter: this process still has
        # the old modules loaded, and its cwd/PYTHONPATH must not mask them.
        result = run([str(home.venv_python), "-I", "-c", VERIFY_SCRIPT])
    except subprocess.CalledProcessError as exc:
        raise UpdateError(
            f"{step} failed (exit {exc.returncode}): {(exc.stderr or exc.stdout or '').strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"{step} timed out; check the installation before retrying") from exc
    except OSError as exc:
        raise UpdateError(f"{step} could not run: {exc}") from exc
    try:
        installed = json.loads(result.stdout)
    except ValueError as exc:
        raise UpdateError("could not verify the installed host and worker versions") from exc
    if installed != [str(latest), str(latest)]:
        raise UpdateError(
            f"verification failed: expected both packages at {latest}, got {installed!r}"
        )
    try:
        home.write_record(sbxloop_version=str(latest), created_by="sbxloop update")
    except OSError as exc:
        raise UpdateError(f"packages updated, but could not update {home.record}: {exc}") from exc
    say(f"Updated sbxloop and sbxloop-worker to {latest}.")
    say("Restart any running daemon when idle to use the new version; run `sbxloop doctor`.")
