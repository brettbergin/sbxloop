"""``sbxloop init``: lay the home out, install into it, wire the host.

One idempotent command builds everything a host needs under the home
(:mod:`sbxloop.paths`):

1. the directory tree, with ``config/`` private;
2. the launchers ``bin/sbxloop`` and ``bin/sbx`` — bound to the home they
   live in, exporting no secrets;
3. the interpreter: ``uv`` in ``bin/``, a uv-managed CPython under
   ``python/``, and ``venv/`` with ``sbxloop[discord,slack]`` and the
   worker pinned to this exact version (skipped when init already runs
   from that venv);
4. Docker's ``sbx``, installed by its own installer with the home as
   ``PREFIX``, pinned to the series sbxloop is tested against and recorded
   in ``sbx/VERSION`` only once the installed executable reports it;
5. ``config/sbxloop.toml`` from the packaged template and
   ``config/secrets.env`` (0600) from the packaged example — never
   overwritten unless asked;
6. with ``--systemd``, the user units rendered into ``systemd/`` with the
   home's absolute paths and enabled through ``systemctl --user`` (never
   started: starting is the operator's call, or the deploy's);
7. ``home.json``, so ``sbxloop doctor`` can tell an initialised home from a
   directory that merely exists.

Every external command goes through one ``run`` callable and every
download through one ``fetch`` callable, so the whole sequence is unit
tested without a network or a shell. ``--dry-run`` prints the plan and
touches nothing.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import sbxloop
from sbxloop.errors import SbxloopError
from sbxloop.log import get_logger
from sbxloop.paths import SbxloopHome
from sbxloop.sbx.parse import parse_version

log = get_logger(__name__)

#: The sbx series sbxloop is tested against (doctor's TESTED_SBX_SERIES is
#: the major.minor of this). Upgrading sbx is an explicit operator step —
#: `sbxloop init --sbx-version X` — never something a deploy does.
SBX_VERSION = "0.38.0"
SBX_RELEASES_API = "https://api.github.com/repos/docker/sbx-releases/releases/tags/{tag}"
UV_INSTALLER_URL = "https://astral.sh/uv/install.sh"
PYTHON_SERIES = "3.13"
INSTALL_EXTRAS = "discord,slack"
#: The units `--systemd` renders and enables; the runner unit is opt-in.
UNIT_NAMES: tuple[str, ...] = ("sbxloop-daemon.service", "sbx-sandboxd.service")
RUNNER_UNIT = "github-runner.service"
USER_AGENT = f"sbxloop/{sbxloop.__version__}"
#: The never-built fallback both packages report without git metadata.
UNBUILT = "0.0.0"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
Fetcher = Callable[[str, Path], None]
Printer = Callable[[str], None]


class InitError(SbxloopError):
    """``sbxloop init`` could not complete a step; the message names it."""


@dataclass(frozen=True)
class InitOptions:
    systemd: bool = False
    runner_dir: Path | None = None
    sbx: bool = True
    sbx_version: str = SBX_VERSION
    #: The sbxloop version to install into the venv; None means this one.
    version: str | None = None
    #: A directory of wheels to install from (a deploy that fetched the
    #: release assets), on top of the index for the dependencies.
    wheels: Path | None = None
    force: bool = False
    dry_run: bool = False
    preset: str | None = None
    created_by: str = "sbxloop init"


@dataclass
class InitReport:
    home: SbxloopHome
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 - fixed argv lists built here, never a shell
        list(argv), check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )


def default_fetcher(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:  # nosec B310 - https only
        target.write_bytes(response.read())


def template(name: str) -> str:
    return resources.files("sbxloop.data").joinpath("home", name).read_text(encoding="utf-8")


def render_unit(name: str, home: SbxloopHome, *, runner_dir: Path | None = None) -> str:
    text = template(name).replace("@HOME@", str(home.root))
    if runner_dir is not None:
        text = text.replace("@RUNNER@", str(runner_dir))
    return text


def _apparmor_only_failure(detail: str) -> bool:
    """Whether an installer failure is the one outcome an unprivileged run is
    expected to hit: the AppArmor profile it drops into ``/etc`` is root's to
    write, and it is the installer's last step. The detail has to say so —
    a failure that named nothing, or named anything else, is not this one."""
    lowered = detail.lower()
    return "apparmor" in lowered


def sbx_asset_name_matches(name: str, *, system: str, machine: str) -> bool:
    """Whether a docker/sbx-releases asset is the tarball for this host."""
    if not name.endswith(".tar.gz"):
        return False
    lowered = name.lower()
    os_ok = system.lower() in lowered
    arch = machine.lower()
    if arch in ("x86_64", "amd64"):
        arch_ok = "amd64" in lowered or "x86_64" in lowered
    elif arch in ("aarch64", "arm64"):
        arch_ok = "arm64" in lowered or "aarch64" in lowered
    else:
        arch_ok = arch in lowered
    if os_ok and not arch_ok and system.lower() == "darwin":
        # docker/sbx-releases ships one macOS tarball with no architecture in
        # its name (DockerSandboxes-darwin.tar.gz): the darwin build is
        # arm64-only, so a name that says darwin and nothing else is it.
        arch_ok = arch in ("arm64", "aarch64") and not any(
            marker in lowered for marker in ("amd64", "x86_64")
        )
    return os_ok and arch_ok


class HomeInit:
    """The steps of ``sbxloop init``, each idempotent."""

    def __init__(
        self,
        home: SbxloopHome,
        options: InitOptions,
        *,
        env: Mapping[str, str] | None = None,
        run: Runner = default_runner,
        fetch: Fetcher = default_fetcher,
        system: str | None = None,
        machine: str | None = None,
        sys_prefix: Path | None = None,
        say: Printer | None = None,
        user_units: Path | None = None,
    ) -> None:
        self.home = home
        self.options = options
        self.env = dict(os.environ if env is None else env)
        self.run = run
        self.fetch = fetch
        self.system = system or platform.system()
        self.machine = machine or platform.machine()
        self.sys_prefix = (sys_prefix or Path(sys.prefix)).resolve()
        self.say = say or (lambda _line: None)
        home_dir = self.env.get("HOME") or str(Path.home())
        self.user_units = user_units or Path(home_dir) / ".config" / "systemd" / "user"
        self.report = InitReport(home)

    # -- the plan ---------------------------------------------------------------

    def plan(self) -> list[tuple[str, str]]:
        home = self.home
        steps: list[tuple[str, str]] = [
            ("tree", f"create the layout under {home.root}"),
            ("launchers", f"write {home.launcher} and {home.sbx_launcher}"),
        ]
        if self._venv_is_current():
            steps.append(("venv", f"keep {home.venv} (init runs from it)"))
        else:
            steps.append(
                (
                    "venv",
                    f"install uv, CPython {PYTHON_SERIES} and sbxloop=={self.version} "
                    f"into {home.venv}",
                )
            )
        if self.options.sbx:
            installed = self._installed_sbx_version()
            if installed == self.options.sbx_version:
                steps.append(("sbx", f"keep sbx {installed} at {home.sbx_binary}"))
            else:
                steps.append(
                    ("sbx", f"install sbx {self.options.sbx_version} under {home.sbx_prefix}")
                )
        steps.append(("config", f"write {home.config_toml} and {home.secrets_env} unless present"))
        if self.options.systemd:
            steps.append(("systemd", f"render {', '.join(self.unit_names)} and enable them"))
        steps.append(("record", f"stamp {home.record}"))
        return steps

    @property
    def version(self) -> str:
        return self.options.version or sbxloop.__version__

    @property
    def unit_names(self) -> tuple[str, ...]:
        return (*UNIT_NAMES, RUNNER_UNIT) if self.options.runner_dir else UNIT_NAMES

    # -- running it -------------------------------------------------------------

    def execute(self) -> InitReport:
        if self.options.dry_run:
            for step, what in self.plan():
                self.say(f"would {step}: {what}")
            return self.report
        self._tree()
        self._launchers()
        self._venv()
        if self.options.sbx:
            self._sbx()
        self._config()
        if self.options.systemd:
            self._systemd()
        self._record()
        return self.report

    def _tree(self) -> None:
        self.home.ensure_tree()
        self.report.done.append("tree")

    def _launchers(self) -> None:
        for path, name in (
            (self.home.launcher, "sbxloop.launcher.sh"),
            (self.home.sbx_launcher, "sbx.launcher.sh"),
        ):
            path.write_text(template(name))
            path.chmod(0o755)
        self.report.done.append("launchers")

    # -- interpreter --------------------------------------------------------------

    def _venv_is_current(self) -> bool:
        """Init runs from the home's own venv and no other version was
        asked for: the interpreter step has nothing to do."""
        venv = self.home.venv
        wanted = self.options.version
        return (
            venv.exists()
            and venv.resolve() == self.sys_prefix
            and (wanted is None or wanted == sbxloop.__version__)
        )

    def _venv(self) -> None:
        if self._venv_is_current():
            self.report.skipped.append("venv (init runs from it)")
            return
        if self.version == UNBUILT and self.options.wheels is None:
            raise InitError(
                "this sbxloop reports version 0.0.0 (a checkout without git metadata); "
                "pass --version X.Y.Z to say which release to install, or --wheels DIR"
            )
        uv = self._ensure_uv()
        self.run([str(uv), "python", "install", PYTHON_SERIES])
        if not self.home.venv_python.exists():
            self.run([str(uv), "venv", "--python", PYTHON_SERIES, str(self.home.venv)])
        spec = [f"sbxloop[{INSTALL_EXTRAS}]=={self.version}", f"sbxloop-worker=={self.version}"]
        argv = [str(uv), "pip", "install", "--python", str(self.home.venv_python)]
        if self.options.wheels is not None:
            argv += ["--find-links", str(self.options.wheels)]
        self.run([*argv, *spec])
        self.report.done.append(f"venv (sbxloop {self.version})")

    def _uv_env(self) -> dict[str, str]:
        return {
            **self.env,
            "UV_INSTALL_DIR": str(self.home.bin),
            "UV_NO_MODIFY_PATH": "1",
            "UV_CACHE_DIR": str(self.home.cache / "uv"),
            "UV_PYTHON_INSTALL_DIR": str(self.home.python),
        }

    def _ensure_uv(self) -> Path:
        """``bin/uv``: the home's own, downloaded with Astral's installer into
        ``bin/`` when missing (UV_INSTALL_DIR, no PATH edits). A ``uv`` already
        on PATH is used only to bootstrap, never relied on afterwards."""
        uv = self.home.uv
        if uv.exists():
            return uv
        on_path = shutil.which("uv")
        if on_path:
            shutil.copy2(on_path, uv)
            uv.chmod(0o755)
            self.report.notes.append(f"copied uv from {on_path}")
            return uv
        with tempfile.TemporaryDirectory(dir=self.home.tmp) as scratch:
            script = Path(scratch) / "uv-install.sh"
            self.fetch(UV_INSTALLER_URL, script)
            self._run_env(["sh", str(script)], self._uv_env())
        if not uv.exists():
            raise InitError(f"the uv installer did not leave {uv} behind")
        return uv

    def _run_env(
        self, argv: Sequence[str], env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        """Run with an explicit environment through the same seam as ``run``:
        the values ride on the process environment for the call's duration
        so a fake runner sees the same argv a real one would."""
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            return self.run(argv)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    # -- sbx ----------------------------------------------------------------------

    def _installed_sbx_version(self) -> str | None:
        if not self.home.sbx_binary.exists():
            return None
        try:
            return self.home.sbx_version_file.read_text().strip() or None
        except OSError:
            return None

    def _sbx(self) -> None:
        wanted = self.options.sbx_version
        if self._installed_sbx_version() == wanted:
            self.report.skipped.append(f"sbx {wanted} (installed)")
            return
        tarball_url = self._sbx_asset_url(wanted)
        with tempfile.TemporaryDirectory(dir=self.home.tmp) as scratch:
            tarball = Path(scratch) / "sbx.tar.gz"
            self.fetch(tarball_url, tarball)
            unpacked = Path(scratch) / "docker-sbx"
            unpacked.mkdir()
            with tarfile.open(tarball) as tf:
                tf.extractall(unpacked, filter="data")
            installer = self._find_installer(unpacked)
            # Docker's installer refuses outright when mkfs.ext4 is not on
            # PATH — and Debian keeps /usr/sbin off a non-root PATH, which is
            # how the field host's first migration stopped here. It then
            # copies the binaries and tries to drop an AppArmor profile into
            # /etc — root's business, and the only step that can fail for an
            # unprivileged user; the binaries are already in place by then.
            env = {
                **self.env,
                "PREFIX": str(self.home.sbx_prefix),
                "PATH": "/usr/sbin:/sbin:" + self.env.get("PATH", "/usr/bin:/bin"),
            }
            partial: str | None = None
            try:
                self._run_env([str(installer)], env)
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or "").strip() or "no output"
                if not _apparmor_only_failure(detail):
                    # Anything else is a failed install, whatever is lying
                    # around under the prefix: an executable left by an
                    # earlier release is not proof this one landed.
                    self._forget_sbx(wanted)
                    raise InitError(
                        f"sbx install failed (exit {exc.returncode}): {detail}"
                    ) from exc
                partial = (
                    "sbx binaries installed; its AppArmor profile was not (that step needs "
                    f"root), so the sandbox backend cannot start yet: sudo PREFIX="
                    f"{self.home.sbx_prefix} {installer.name} from the release tarball"
                )
            self._verify_sbx(wanted, env)
        self.home.sbx_version_file.write_text(wanted + "\n")
        if partial is None:
            self.report.done.append(f"sbx {wanted}")
        else:
            self.report.notes.append(partial)
            self.report.done.append(f"sbx {wanted} (AppArmor profile pending)")

    def _verify_sbx(self, wanted: str, env: Mapping[str, str]) -> None:
        """What the prefix must hold before ``sbx/VERSION`` may claim *wanted*:
        an executable, and that executable reporting the version asked for.
        Anything this cannot establish — a missing component, an executable
        that will not answer, output with no version in it — is a failed
        install, never a marker."""
        problem = self._sbx_problem(wanted, env)
        if problem is None:
            return
        self._forget_sbx(wanted)
        raise InitError(problem)

    def _sbx_problem(self, wanted: str, env: Mapping[str, str]) -> str | None:
        binary = self.home.sbx_binary
        if not binary.is_file() or not os.access(binary, os.X_OK):
            return f"sbx {wanted} install left no executable at {binary}"
        try:
            result = self._run_env([str(binary), "version"], env)
        except (subprocess.CalledProcessError, OSError) as exc:
            return f"sbx {wanted} install left {binary}, but `sbx version` failed: {exc}"
        output = f"{result.stdout or ''}\n{result.stderr or ''}".strip()
        reported = parse_version(output)
        if reported is None:
            return (
                f"sbx {wanted} install left {binary}, but `sbx version` reported no version: "
                f"{output or 'no output'}"
            )
        if reported != wanted.lstrip("v"):
            return (
                f"sbx {wanted} install left {binary} reporting {reported}: the install did not "
                "replace the executable"
            )
        return None

    def _forget_sbx(self, wanted: str) -> None:
        """Drop ``sbx/VERSION`` after a failed install: whatever is under the
        prefix now, no later init may skip the repair as done."""
        stale = self._installed_sbx_version()
        self.home.sbx_version_file.unlink(missing_ok=True)
        if stale is not None and stale != wanted:
            self.report.notes.append(
                f"the failed sbx {wanted} install cleared the marker for sbx {stale}; "
                "the next init reinstalls"
            )

    @staticmethod
    def _find_installer(unpacked: Path) -> Path:
        candidates = sorted(unpacked.rglob("install.sh"))
        if not candidates:
            raise InitError("the sbx tarball carries no install.sh")
        return candidates[0]

    def _sbx_asset_url(self, version: str) -> str:
        tag = version if version.startswith("v") else f"v{version}"
        with tempfile.TemporaryDirectory(dir=self.home.tmp) as scratch:
            listing = Path(scratch) / "release.json"
            self.fetch(SBX_RELEASES_API.format(tag=tag), listing)
            data = json.loads(listing.read_text())
        assets = [
            a["browser_download_url"]
            for a in data.get("assets", [])
            if sbx_asset_name_matches(a.get("name", ""), system=self.system, machine=self.machine)
        ]
        if not assets:
            raise InitError(
                f"no sbx {tag} asset for {self.system}/{self.machine} in docker/sbx-releases"
            )
        return str(assets[0])

    # -- config -------------------------------------------------------------------

    def _config(self) -> None:
        from sbxloop.data import render_config_template, secrets_env_template

        wrote: list[str] = []
        if self.options.force or not self.home.config_toml.exists():
            self.home.config_toml.write_text(render_config_template(self.options.preset))
            wrote.append(self.home.config_toml.name)
        if not self.home.secrets_env.exists():
            self.home.secrets_env.touch(mode=0o600)
            self.home.secrets_env.write_text(secrets_env_template())
            wrote.append(self.home.secrets_env.name)
        self.home.secrets_env.chmod(0o600)
        if wrote:
            self.report.done.append("config (" + ", ".join(wrote) + ")")
        else:
            self.report.skipped.append("config (present)")

    # -- systemd ------------------------------------------------------------------

    def _systemd(self) -> None:
        if self.system != "Linux":
            self.report.notes.append("systemd units skipped: not Linux")
            return
        self.user_units.mkdir(parents=True, exist_ok=True)
        for name in self.unit_names:
            rendered = render_unit(name, self.home, runner_dir=self.options.runner_dir)
            self.home.unit(name).write_text(rendered)
            link = self.user_units / name
            if link.exists() and not link.is_symlink():
                backup = self.home.backups / "units"
                backup.mkdir(parents=True, exist_ok=True)
                shutil.move(str(link), str(backup / name))
                self.report.notes.append(f"moved the previous {name} to {backup / name}")
            elif link.is_symlink():
                link.unlink()
        self.run(["systemctl", "--user", "daemon-reload"])
        self.run(
            ["systemctl", "--user", "enable", *(str(self.home.unit(n)) for n in self.unit_names)]
        )
        user = self.env.get("USER") or self.env.get("LOGNAME") or ""
        if user:
            try:
                self.run(["loginctl", "enable-linger", user])
            except (OSError, subprocess.CalledProcessError) as exc:
                self.report.notes.append(f"loginctl enable-linger {user} failed: {exc}")
        self.report.done.append("systemd (" + ", ".join(self.unit_names) + ")")

    # -- record -------------------------------------------------------------------

    def _record(self) -> None:
        self.home.write_record(sbxloop_version=self.version, created_by=self.options.created_by)
        self.report.done.append("record")


def path_hint(home: SbxloopHome, env: Mapping[str, str]) -> str | None:
    """What to tell the operator when ``bin/`` is not on PATH."""
    entries = env.get("PATH", "").split(os.pathsep)
    if str(home.bin) in entries:
        return None
    return f'export PATH="{home.bin}:$PATH"'


__all__ = [
    "INSTALL_EXTRAS",
    "PYTHON_SERIES",
    "RUNNER_UNIT",
    "SBX_VERSION",
    "UNIT_NAMES",
    "HomeInit",
    "InitError",
    "InitOptions",
    "InitReport",
    "path_hint",
    "render_unit",
    "sbx_asset_name_matches",
    "template",
]
