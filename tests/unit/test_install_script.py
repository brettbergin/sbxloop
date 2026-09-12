"""The bootstrap script's host preflight (#893).

`scripts/install.sh` used to check only `curl` and `tar`, download an
interpreter and every package, and then hand over to `sbxloop init` — which
imports sbxloop, which imports GitPython, which resolves the git executable
at *import* time. On a host without a usable git that turned a missing
prerequisite into an `ImportError` traceback after the install had already
mutated the home.

Every test here runs the real script through an absolute `/bin/sh` against a
synthetic host: its own `HOME` and `SBXLOOP_HOME` under `tmp_path`, and a
`PATH` holding only what the script needs. Whether this runner happens to
have git installed never reaches the script, in either direction.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "scripts" / "install.sh"

# The externals the script reaches for that a fake cannot stand in for; every
# other name on the runner's PATH stays out of the synthetic host.
BORROWED = ("mkdir", "uname")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="a POSIX shell bootstrap; Windows installs through WSL2"
)


@dataclass(frozen=True)
class Host:
    """A synthetic host: its PATH, its home, and the log every fake writes to."""

    root: Path
    home: Path
    path: Path
    log: Path

    @property
    def invocations(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    @property
    def downloads(self) -> list[str]:
        """Everything the fakes recorded bar the preflight's own `git --version`
        — what a host that fails the preflight must never have run."""
        return [line for line in self.invocations if not line.startswith("git ")]

    def uname(self, system: str) -> None:
        """Say this host is *system*, so the Linux-only host-preparation
        branch can be exercised on any runner."""
        target = self.path / "uname"
        target.unlink(missing_ok=True)
        target.write_text(f'#!/bin/sh\nprintf "%s\\n" "{system}"\n')
        target.chmod(0o755)

    def fake(self, name: str, *, exit_code: int = 0, on_path: bool = True) -> Path:
        """An executable that records its argv and exits ``exit_code``."""
        target = (self.path if on_path else self.root / "elsewhere") / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> "{self.log}"\nexit {exit_code}\n'
        )
        target.chmod(0o755)
        return target

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(INSTALL)],
            env={
                "PATH": str(self.path),
                "HOME": str(self.root / "home"),
                "SBXLOOP_HOME": str(self.home),
                **env,
            },
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


@pytest.fixture
def host(tmp_path: Path) -> Host:
    """A host that satisfies every declared prerequisite except git."""
    root = tmp_path / "host"
    path = root / "path"
    path.mkdir(parents=True)
    (root / "home").mkdir()
    host = Host(root=root, home=root / "sbxloop", path=path, log=root / "invocations.log")
    for name in BORROWED:
        real = shutil.which(name)
        assert real is not None, f"this runner has no {name}"
        (path / name).symlink_to(real)
    host.fake("curl")
    host.fake("tar")
    return host


@pytest.fixture
def installable(host: Host) -> Host:
    """…and the pieces the script would otherwise download, pre-placed, so a
    passing preflight runs the bootstrap through to `sbxloop init`."""
    host.fake("git")
    for name, relative in (("uv", "bin/uv"), ("sbxloop", "venv/bin/sbxloop")):
        landed = host.home / relative
        landed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(host.fake(name, on_path=False), landed)
    (host.home / "venv/bin/python").symlink_to(host.home / "venv/bin/sbxloop")
    return host


def test_missing_git_stops_before_the_first_download(host: Host) -> None:
    result = host.run()

    assert result.returncode == 2
    assert "git is required" in result.stdout
    assert host.downloads == []
    assert not (host.home / "bin").exists(), "the home was mutated before the preflight"


def test_missing_git_names_what_to_install(host: Host) -> None:
    """The advice is the host's own, and the message stands on its own: no
    traceback, nothing imported, no privileged command run on the operator's
    behalf."""
    result = host.run()

    expected = {"darwin": "xcode-select --install"}.get(sys.platform, "apt-get install git")
    assert expected in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert "sudo" not in result.stdout


def test_present_but_unusable_git_stops_too(host: Host) -> None:
    host.fake("git", exit_code=127)

    result = host.run()

    assert result.returncode == 2
    assert "not usable" in result.stdout
    assert host.downloads == []
    assert not (host.home / "bin").exists()


def test_a_usable_git_bootstraps(installable: Host) -> None:
    result = installable.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "is required" not in result.stdout
    assert "sbxloop init --systemd" in "\n".join(installable.invocations)


def test_an_explicitly_configured_git_satisfies_the_preflight(installable: Host) -> None:
    """GitPython reads $GIT_PYTHON_GIT_EXECUTABLE ahead of $PATH, so a working
    one is a working host — the preflight checks it instead of PATH."""
    (installable.path / "git").unlink()
    elsewhere = installable.fake("git", on_path=False)

    result = installable.run(GIT_PYTHON_GIT_EXECUTABLE=str(elsewhere))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "sbxloop init --systemd" in "\n".join(installable.invocations)


def test_an_explicitly_configured_git_that_is_missing_fails(installable: Host) -> None:
    """…and the same variable pointed at nothing fails the host, even with a
    usable git on PATH: that is the executable sbxloop would try to use."""
    result = installable.run(GIT_PYTHON_GIT_EXECUTABLE=str(installable.root / "no-such-git"))

    assert result.returncode == 2
    assert "GIT_PYTHON_GIT_EXECUTABLE" in result.stdout
    assert installable.downloads == []


def test_git_is_a_declared_prerequisite() -> None:
    """The script's own header is the operator-facing list; docs/README repeat it."""
    header = INSTALL.read_text().split("set -eu", 1)[0]
    assert "git" in header

    quickstart = (ROOT / "docs" / "user-guide.md").read_text()
    assert "curl, tar, git" in quickstart
    assert "curl, tar, git" in (ROOT / "README.md").read_text()


class TestHostPreparation:
    """The sandbox backend's Linux prerequisites (#898).

    These are not the script's own: it can install a whole home without
    them, and the remedies — enabling virtualisation, granting the device,
    installing a package — are an administrator's. So they are reported
    early and never performed, and `sbxloop init` is what refuses the one
    step that cannot work without them.
    """

    def test_the_header_separates_installation_from_host_preparation(self) -> None:
        header = INSTALL.read_text().split("set -eu", 1)[0]

        assert "installation prerequisites" in header
        assert "host preparation" in header
        assert "/dev/kvm" in header and "e2fsprogs" in header
        assert "elevates a privilege" in header

    def test_a_linux_host_is_told_what_it_is_missing_without_being_stopped(
        self, installable: Host
    ) -> None:
        installable.uname("Linux")

        result = installable.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert "sbxloop init --systemd" in "\n".join(installable.invocations)
        # Whichever this runner is, the advisory and the host agree.
        assert ("/dev/kvm does not exist" in result.stderr) is not Path("/dev/kvm").exists()

    def test_a_missing_device_names_an_administrator_task_never_a_command_to_run(
        self, installable: Host
    ) -> None:
        installable.uname("Linux")

        result = installable.run()

        for line in result.stderr.splitlines():
            if "host preparation needed" not in line:
                continue
            assert "an administrator" in line
            assert "sudo" not in line and "usermod" not in line

    def test_nothing_linux_specific_is_said_on_a_host_it_does_not_apply_to(
        self, installable: Host
    ) -> None:
        """macOS brings its own virtualisation and has neither /dev/kvm nor
        mkfs.ext4; a prerequisite check that fired there would fault a
        supported host."""
        installable.uname("Darwin")

        result = installable.run()

        assert result.returncode == 0, result.stdout + result.stderr
        assert "host preparation needed" not in result.stderr
        assert "sbxloop init --systemd" in "\n".join(installable.invocations)


@pytest.mark.slow
def test_importing_sbxloop_without_a_usable_git_is_an_importerror(tmp_path: Path) -> None:
    """Why the preflight exists, reproduced in an isolated interpreter: an
    unreachable git executable — simulated through GitPython's own variable,
    never by touching the runner's installation — and the import chain behind
    `sbxloop init` fails with no mention of a prerequisite."""
    result = subprocess.run(
        [sys.executable, "-c", "import sbxloop"],
        env={**os.environ, "GIT_PYTHON_GIT_EXECUTABLE": str(tmp_path / "no-such-git")},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert result.returncode != 0
    assert "Bad git executable" in result.stderr
