"""``sbxloop init``: the home laid out, installed into and wired, without a
network or a shell — every command and download goes through a fake."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sbxloop.cli.app import app
from sbxloop.homeinit import (
    RUNNER_UNIT,
    SBX_VERSION,
    UNIT_NAMES,
    HomeInit,
    InitError,
    InitOptions,
    _render_unit_line,
    _render_word,
    path_hint,
    render_unit,
    sbx_asset_name_matches,
    template,
)
from sbxloop.paths import SbxloopHome

runner = CliRunner()


class FakeRun:
    """Records argv; answers success unless told to fail. Side effects a
    real command would have (uv creating the venv, Docker's installer
    laying sbx out) are simulated so later steps see them — including the
    installed executable answering ``sbx version`` with the version whose
    installer wrote it, which is how a leftover binary gives itself away."""

    def __init__(self, home: SbxloopHome, sbx_version: str = SBX_VERSION) -> None:
        self.home = home
        self.sbx_version = sbx_version
        self.calls: list[list[str]] = []
        self.fail: dict[str, int] = {}

    def __call__(self, argv: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        key = " ".join(argv[:3])
        for prefix, code in self.fail.items():
            if key.startswith(prefix):
                raise subprocess.CalledProcessError(code, argv, output="", stderr="boom")
        if argv[1:2] == ["venv"]:
            self.home.venv_bin.mkdir(parents=True, exist_ok=True)
            self.home.venv_python.write_text("#!python\n")
        if argv[0].endswith("install.sh"):
            # Docker's installer, honouring PREFIX from the environment; it
            # needs mkfs.ext4 on PATH, which Debian keeps under /usr/sbin.
            assert os.environ["PATH"].startswith("/usr/sbin:/sbin:")
            self.install_sbx(Path(os.environ["PREFIX"]), self.sbx_version)
        if argv[0] == str(self.home.sbx_binary) and argv[1:] == ["version"]:
            # The real CLI reports the build that is on disk, not the one
            # that was asked for.
            return subprocess.CompletedProcess(argv, 0, Path(argv[0]).read_text(), "")
        if argv[0] == "sh" and argv[1].endswith("uv-install.sh"):
            self.home.uv.write_text("#!uv\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    @staticmethod
    def install_sbx(prefix: Path, version: str) -> None:
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        binary = prefix / "bin" / "sbx"
        binary.write_text(f"#!sbx\nsbx version {version}\n")
        binary.chmod(0o755)


class RecordingRun:
    """``FakeRun`` plus what each command saw of uv's directory settings.
    A real uv reads them from its environment; a fake one can only report
    the environment it was handed."""

    KEYS = ("UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "UV_INSTALL_DIR")

    def __init__(self, inner: FakeRun) -> None:
        self.inner = inner
        self.seen: list[tuple[list[str], dict[str, str | None]]] = []

    def __call__(self, argv: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in argv]
        self.seen.append((argv, {key: os.environ.get(key) for key in self.KEYS}))
        return self.inner(argv)

    def env_of(self, *words: str) -> dict[str, str | None]:
        """The environment of the one uv command whose arguments start with
        *words* — ``python install``, ``venv``, ``pip install``."""
        found = [env for argv, env in self.seen if argv[1 : 1 + len(words)] == list(words)]
        assert len(found) == 1, f"{words}: {[argv for argv, _ in self.seen]}"
        return found[0]

    def env_of_bootstrap(self) -> dict[str, str | None]:
        """The environment Astral's installer script ran under."""
        found = [env for argv, env in self.seen if argv[0] == "sh"]
        assert len(found) == 1, f"bootstrap: {[argv for argv, _ in self.seen]}"
        return found[0]


class FakeFetch:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url: str, target: Path) -> None:
        self.urls.append(url)
        if "releases/tags/" in url:
            target.write_text(
                json.dumps(
                    {
                        "assets": [
                            {
                                "name": "sbx-0.38.0-darwin-arm64.tar.gz",
                                "browser_download_url": "u/mac",
                            },
                            {
                                "name": "sbx-0.38.0-linux-amd64.tar.gz",
                                "browser_download_url": "u/linux",
                            },
                            {
                                "name": "sbx-0.38.0-linux-arm64.tar.gz",
                                "browser_download_url": "u/arm",
                            },
                            {"name": "checksums.txt", "browser_download_url": "u/sums"},
                        ]
                    }
                )
            )
        elif url.startswith("u/"):
            with tarfile.open(target, "w:gz") as tf:
                script = target.parent / "install.sh"
                script.write_text("#!/bin/sh\n")
                tf.add(script, arcname="docker-sbx/install.sh")
        else:
            target.write_text("#!/bin/sh\n")  # the uv installer


def installer_fails(
    run: FakeRun, *, stderr: str, code: int = 1, wrote: bool = True
) -> Callable[[Any], subprocess.CompletedProcess[str]]:
    """A runner whose ``install.sh`` fails — after laying the binaries down
    (``wrote``, the shape of a step that needs root) or before touching
    anything. Every other command still runs."""

    def failing(argv: Any) -> subprocess.CompletedProcess[str]:
        if str(argv[0]).endswith("install.sh"):
            if wrote:
                run(argv)
            raise subprocess.CalledProcessError(code, [str(a) for a in argv], stderr=stderr)
        return run(argv)

    return failing


def make(
    tmp_path: Path, **overrides: Any
) -> tuple[SbxloopHome, HomeInit, FakeRun, FakeFetch, list[str]]:
    home = SbxloopHome(tmp_path / "home")
    options = InitOptions(**{"version": "1.2.3", **overrides})
    run, fetch, said = FakeRun(home, options.sbx_version), FakeFetch(), []
    init = HomeInit(
        home,
        options,
        env={"HOME": str(tmp_path), "USER": "bergs", "PATH": "/usr/bin"},
        run=run,
        fetch=fetch,
        system="Linux",
        machine="x86_64",
        sys_prefix=tmp_path / "elsewhere-venv",
        say=said.append,
        user_units=tmp_path / "units",
    )
    return home, init, run, fetch, said


class TestWindowsHost:
    """#899: `sbxloop init` on a native Windows host writes entry points
    that host can run, and promises no sandbox runtime it does not have."""

    @pytest.fixture(autouse=True)
    def icacls(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        """There is no `icacls` on the runner; record what init asks it."""
        calls: list[list[str]] = []

        def fake_run(argv: Any) -> subprocess.CompletedProcess[str]:
            calls.append([str(a) for a in argv])
            return subprocess.CompletedProcess([str(a) for a in argv], 0, "", "")

        monkeypatch.setattr("sbxloop.hostfiles._run", fake_run)
        return calls

    def test_the_launcher_is_a_cmd_and_there_is_no_sbx_wrapper(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / "home", os_name="nt")
        options = InitOptions(version="1.2.3", sbx=False, systemd=False)
        init = HomeInit(
            home,
            options,
            env={"USERPROFILE": str(tmp_path), "PATH": ""},
            run=FakeRun(home, options.sbx_version),
            fetch=FakeFetch(),
            system="Windows",
            machine="AMD64",
            sys_prefix=tmp_path / "elsewhere-venv",
            say=[].append,
            user_units=tmp_path / "units",
        )
        init.execute()
        assert home.launcher.name == "sbxloop.cmd" and home.launcher.is_file()
        assert r"venv\Scripts\sbxloop.exe" in home.launcher.read_text()
        assert not home.sbx_launcher.exists()
        assert any("no sbx wrapper" in note for note in init.report.notes)

    def test_init_sbx_is_refused_before_anything_is_downloaded(self, tmp_path: Path) -> None:
        """Fail closed on unsupported work: the sbx assets are a POSIX
        tarball around an install.sh, so the step is refused by name rather
        than attempted and failed part-way through."""
        home = SbxloopHome(tmp_path / "home", os_name="nt")
        options = InitOptions(version="1.2.3", sbx=True, systemd=False)
        fetch = FakeFetch()
        init = HomeInit(
            home,
            options,
            env={"USERPROFILE": str(tmp_path), "PATH": ""},
            run=FakeRun(home, options.sbx_version),
            fetch=fetch,
            system="Windows",
            machine="AMD64",
            sys_prefix=tmp_path / "elsewhere-venv",
            say=[].append,
            user_units=tmp_path / "units",
        )
        with pytest.raises(InitError, match="no native Windows build"):
            init.execute()
        assert not any("sbx-releases" in url or url.startswith("u/") for url in fetch.urls)
        assert "--no-sbx" in init.SBX_UNSUPPORTED
        (step,) = [what for name, what in init.plan() if name == "sbx"]
        assert "no native Windows build" in step

    def test_the_secrets_file_is_restricted_not_chmodded(
        self, tmp_path: Path, icacls: list[list[str]]
    ) -> None:
        """A mode is not privacy on Windows, so init reaches for the ACL
        instead — and a failure there raises rather than leaving the file
        open (:mod:`sbxloop.hostfiles`)."""
        home = SbxloopHome(tmp_path / "home", os_name="nt")
        options = InitOptions(version="1.2.3", sbx=False, systemd=False)
        HomeInit(
            home,
            options,
            env={"USERPROFILE": str(tmp_path), "PATH": ""},
            run=FakeRun(home, options.sbx_version),
            fetch=FakeFetch(),
            system="Windows",
            machine="AMD64",
            sys_prefix=tmp_path / "elsewhere-venv",
            say=[].append,
            user_units=tmp_path / "units",
        ).execute()
        assert home.secrets_env.is_file()
        granted = [c for c in icacls if c[0] == "icacls"]
        assert granted and all("/inheritance:r" in c for c in granted)
        assert all(str(home.secrets_env) in c for c in granted)


class TestLayout:
    def test_fresh_host_gets_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)  # no uv on PATH: fetch it
        home, init, run, fetch, _ = make(tmp_path, systemd=True)
        report = init.execute()
        assert home.missing_directories() == []
        assert home.launcher.stat().st_mode & 0o111 and home.sbx_launcher.stat().st_mode & 0o111
        launcher = home.launcher.read_text()
        assert 'exec "$home/venv/bin/sbxloop" "$@"' in launcher
        # nothing sourced, nothing exported: the launcher carries no secrets
        assert "set -a" not in launcher and "source" not in launcher
        assert not any(line.lstrip().startswith(". ") for line in launcher.splitlines())
        # the interpreter: uv fetched into bin/, python installed, venv made, sbxloop pinned
        assert fetch.urls[0].startswith("https://astral.sh/uv/")
        uv = str(home.uv)
        assert [uv, "python", "install", "3.13"] in run.calls
        assert [uv, "venv", "--python", "3.13", str(home.venv)] in run.calls
        pip = next(c for c in run.calls if c[1:3] == ["pip", "install"])
        assert pip[-2:] == ["sbxloop[discord,slack]==1.2.3", "sbxloop-worker==1.2.3"]
        assert "--python" in pip and str(home.venv_python) in pip
        # sbx: the pinned release for this platform, through Docker's installer with PREFIX=home
        assert any("releases/tags/v0.38.0" in u for u in fetch.urls)
        assert "u/linux" in fetch.urls
        assert home.sbx_binary.exists() and home.sbx_version_file.read_text().strip() == SBX_VERSION
        # config written once, secrets private
        assert home.config_toml.exists()
        assert home.secrets_env.stat().st_mode & 0o777 == 0o600
        assert "COPILOT_GITHUB_TOKEN" in home.secrets_env.read_text()
        # units rendered with the home's absolute paths and enabled, never started
        for name in UNIT_NAMES:
            text = home.unit(name).read_text()
            assert str(home.root) in text and "@HOME@" not in text
        assert ["systemctl", "--user", "daemon-reload"] in run.calls
        enable = next(c for c in run.calls if c[:3] == ["systemctl", "--user", "enable"])
        assert set(enable[3:]) == {str(home.unit(n)) for n in UNIT_NAMES}
        assert ["loginctl", "enable-linger", "bergs"] in run.calls
        assert not any("start" in c for c in run.calls)
        # stamped
        record = home.read_record()
        assert record is not None and record.sbxloop_version == "1.2.3"
        assert record.created_by == "sbxloop init"
        assert "record" in report.done and "launchers" in report.done

    def test_second_run_keeps_what_is_there(self, tmp_path: Path) -> None:
        home, init, run, fetch, _ = make(tmp_path)
        init.execute()
        home.config_toml.write_text("model = 'mine'\n")
        home.secrets_env.write_text("GH_TOKEN=x\n")
        again = HomeInit(
            home,
            InitOptions(),  # this version, from this venv
            env={"HOME": str(tmp_path), "PATH": ""},
            run=run,
            fetch=fetch,
            system="Linux",
            machine="x86_64",
            sys_prefix=home.venv,  # init now runs from the home's venv
            user_units=tmp_path / "units",
        )
        before = len(run.calls)
        report = again.execute()
        assert home.config_toml.read_text() == "model = 'mine'\n"
        assert home.secrets_env.read_text() == "GH_TOKEN=x\n"
        assert any(s.startswith("venv") for s in report.skipped)
        assert any(s.startswith("sbx 0.38.0") for s in report.skipped)
        assert any(s.startswith("config") for s in report.skipped)
        assert len(run.calls) == before  # nothing to run: no venv, no sbx, no systemd

    def test_force_rewrites_the_config_but_never_the_secrets(self, tmp_path: Path) -> None:
        home, init, *_ = make(tmp_path)
        init.execute()
        home.config_toml.write_text("model = 'mine'\n")
        home.secrets_env.write_text("GH_TOKEN=x\n")
        _home2, init2, *_ = make(tmp_path, force=True)
        init2.execute()
        assert 'model = "auto"' in home.config_toml.read_text()
        assert home.secrets_env.read_text() == "GH_TOKEN=x\n"

    def test_dry_run_touches_nothing(self, tmp_path: Path) -> None:
        home, init, run, fetch, said = make(tmp_path, dry_run=True, systemd=True)
        init.execute()
        assert not home.root.exists()
        assert run.calls == [] and fetch.urls == []
        assert any("would venv" in line and "1.2.3" in line for line in said)
        assert any("would sbx" in line and SBX_VERSION in line for line in said)
        assert any("would systemd" in line for line in said)

    def test_new_version_reinstalls_and_new_sbx_version_reinstalls(self, tmp_path: Path) -> None:
        home, init, *_ = make(tmp_path)
        init.execute()
        _, upgrade, run, fetch, _ = make(tmp_path, version="1.2.4", sbx_version="0.39.0")
        upgrade.execute()
        pip = next(c for c in run.calls if c[1:3] == ["pip", "install"])
        assert "sbxloop[discord,slack]==1.2.4" in pip
        assert any("tags/v0.39.0" in u for u in fetch.urls)
        assert home.sbx_version_file.read_text().strip() == "0.39.0"
        assert home.read_record().sbxloop_version == "1.2.4"  # type: ignore[union-attr]

    def test_wheels_directory_feeds_the_install(self, tmp_path: Path) -> None:
        wheels = tmp_path / "dist"
        wheels.mkdir()
        _, init, run, _, _ = make(tmp_path, wheels=wheels)
        init.execute()
        pip = next(c for c in run.calls if c[1:3] == ["pip", "install"])
        assert "--find-links" in pip and str(wheels) in pip

    def test_unbuilt_version_is_refused_without_a_pin(self, tmp_path: Path) -> None:
        _, init, *_ = make(tmp_path, version=None)
        init.options = InitOptions(version=None)  # type: ignore[misc]
        import sbxloop

        if sbxloop.__version__ != "0.0.0":
            pytest.skip("a built checkout knows its version")
        with pytest.raises(InitError, match="--version"):
            init.execute()

    def test_sbx_can_be_left_out(self, tmp_path: Path) -> None:
        home, init, _run, fetch, _ = make(tmp_path, sbx=False)
        init.execute()
        assert not home.sbx_prefix.exists()
        assert not any("sbx-releases" in u for u in fetch.urls)

    def test_sbx_installer_failing_after_the_binary_landed_is_a_note(self, tmp_path: Path) -> None:
        """Docker's installer copies the binaries, then tries /etc/apparmor.d —
        root's business; the unprivileged run still leaves the executable it
        was asked for, and the note says the backend is not ready."""
        home, init, run, _, _ = make(tmp_path)
        init.run = installer_fails(run, stderr="apparmor: permission denied")  # type: ignore[assignment]
        report = init.execute()
        assert home.sbx_binary.exists()
        assert home.sbx_version_file.read_text().strip() == SBX_VERSION
        note = next(n for n in report.notes if "AppArmor" in n)
        assert "cannot start yet" in note and str(home.sbx_prefix) in note

    def test_sbx_installer_failing_outright_is_an_error(self, tmp_path: Path) -> None:
        _, init, run, _, _ = make(tmp_path)
        run.fail["sbx"] = 1  # no such prefix; the install.sh call fails before writing
        init.run = installer_fails(run, stderr="mkfs.ext4 not found", code=2)  # type: ignore[assignment]
        with pytest.raises(InitError, match=r"mkfs\.ext4"):
            init.execute()


class TestSbxInstall:
    """A recorded sbx version has to mean an sbx that was installed: an
    executable left behind by an earlier release is not proof of one."""

    def test_a_failed_upgrade_never_advances_the_marker(self, tmp_path: Path) -> None:
        """The installer refuses before touching anything (Debian keeps
        mkfs.ext4 off a non-root PATH); the previous release's executable is
        still there, and it is not the release that was asked for."""
        home, first, *_ = make(tmp_path)
        first.execute()
        assert home.sbx_version_file.read_text().strip() == SBX_VERSION

        _, upgrade, run, _, _ = make(tmp_path, sbx_version="0.39.0")
        upgrade.run = installer_fails(run, stderr="mkfs.ext4 not found", code=2, wrote=False)  # type: ignore[assignment]
        with pytest.raises(InitError, match=r"exit 2.*mkfs\.ext4"):
            upgrade.execute()
        assert home.sbx_binary.exists()  # the old one, untouched
        assert not home.sbx_version_file.exists()  # nothing claims an install

    def test_an_unrelated_failure_after_the_binary_landed_is_an_error(self, tmp_path: Path) -> None:
        """A copy that got as far as the executable and then broke is a
        half-installed sbx, not the AppArmor step."""
        home, init, run, _, _ = make(tmp_path)
        init.run = installer_fails(run, stderr="cp: cannot stat 'sandboxd': no such file")  # type: ignore[assignment]
        with pytest.raises(InitError, match="cannot stat"):
            init.execute()
        assert not home.sbx_version_file.exists()

    def test_an_installer_that_left_the_old_executable_is_an_error(self, tmp_path: Path) -> None:
        """Exit 0 is not enough either: what the prefix reports has to be the
        release that was asked for."""
        home, first, *_ = make(tmp_path)
        first.execute()
        _, upgrade, run, _, _ = make(tmp_path, sbx_version="0.39.0")
        run.sbx_version = SBX_VERSION  # the installer copied nothing new
        with pytest.raises(InitError, match=f"reporting {SBX_VERSION}"):
            upgrade.execute()
        assert not home.sbx_version_file.exists()

    def test_an_executable_that_reports_nothing_is_an_error(self, tmp_path: Path) -> None:
        home, init, run, _, _ = make(tmp_path)
        original = run.__call__

        def mute(argv: Any) -> subprocess.CompletedProcess[str]:
            result = original(argv)
            if list(argv)[1:] == ["version"]:
                return subprocess.CompletedProcess([str(a) for a in argv], 0, "", "")
            return result

        init.run = mute  # type: ignore[assignment]
        with pytest.raises(InitError, match="reported no version"):
            init.execute()
        assert not home.sbx_version_file.exists()

    def test_a_failed_install_is_retried_by_the_next_init(self, tmp_path: Path) -> None:
        home, init, run, _, _ = make(tmp_path)
        init.run = installer_fails(run, stderr="cp: cannot stat 'sandboxd'", wrote=False)  # type: ignore[assignment]
        with pytest.raises(InitError):
            init.execute()

        _, retry, run2, _, _ = make(tmp_path)
        report = retry.execute()
        assert [c for c in run2.calls if c[0].endswith("install.sh")]  # not skipped
        assert home.sbx_version_file.read_text().strip() == SBX_VERSION
        assert any(d.startswith(f"sbx {SBX_VERSION}") for d in report.done)

    def test_no_asset_for_this_platform_is_an_error(self, tmp_path: Path) -> None:
        _, init, *_ = make(tmp_path)
        init.machine = "riscv64"
        with pytest.raises(InitError, match="riscv64"):
            init.execute()

    def test_systemd_is_skipped_off_linux(self, tmp_path: Path) -> None:
        _home, init, run, _, _ = make(tmp_path, systemd=True, sbx=False)
        init.system = "Darwin"
        report = init.execute()
        assert not any(c[0] == "systemctl" for c in run.calls)
        assert any("not Linux" in n for n in report.notes)

    def test_existing_unit_files_are_moved_aside_for_the_links(self, tmp_path: Path) -> None:
        home, init, _run, _, _ = make(tmp_path, systemd=True)
        units = tmp_path / "units"
        units.mkdir()
        (units / "sbxloop-daemon.service").write_text("[Unit]\nDescription=old\n")
        report = init.execute()
        moved = home.backups / "units" / "sbxloop-daemon.service"
        assert moved.read_text() == "[Unit]\nDescription=old\n"
        assert not (units / "sbxloop-daemon.service").exists()  # the link is systemctl's job
        assert any("moved the previous sbxloop-daemon.service" in n for n in report.notes)

    def test_runner_unit_is_rendered_on_request(self, tmp_path: Path) -> None:
        home, init, run, _, _ = make(tmp_path, systemd=True, runner_dir=tmp_path / "actions-runner")
        init.execute()
        text = home.unit("github-runner.service").read_text()
        assert f"WorkingDirectory={tmp_path / 'actions-runner'}" in text
        enable = next(c for c in run.calls if c[:3] == ["systemctl", "--user", "enable"])
        assert str(home.unit("github-runner.service")) in enable

    def test_runner_unit_carries_the_home_it_was_installed_under(self, tmp_path: Path) -> None:
        """#895: a job on this runner deploys to the installation that is
        there, not to whatever `$HOME/.sbxloop` would be."""
        home, init, _run, _, _ = make(
            tmp_path, systemd=True, runner_dir=tmp_path / "actions-runner"
        )
        init.execute()
        text = home.unit("github-runner.service").read_text()
        assert f"Environment=SBXLOOP_HOME={home.root}" in text

    def test_uv_on_path_is_copied_into_the_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_uv = tmp_path / "uv"
        fake_uv.write_text("#!uv-on-path\n")
        fake_uv.chmod(0o755)
        monkeypatch.setattr("shutil.which", lambda name: str(fake_uv) if name == "uv" else None)
        home, init, _run, fetch, _ = make(tmp_path)
        report = init.execute()
        assert home.uv.read_text() == "#!uv-on-path\n"
        assert not any("astral.sh" in u for u in fetch.urls)
        assert any("copied uv" in n for n in report.notes)


class TestTemplates:
    def test_units_carry_no_placeholder_and_no_home_relative_paths(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / "h")
        for name in UNIT_NAMES:
            text = render_unit(name, home)
            assert "@HOME@" not in text and "%h" not in text
            assert f"{home.root}/bin/" in text
        assert "Environment=SBXLOOP_HOME=" + str(home.root) in render_unit(
            "sbxloop-daemon.service", home
        )
        assert "WorkingDirectory=" + str(home.root) in render_unit("sbxloop-daemon.service", home)
        # #953: a clean stop or restart exits 143 (SIGTERM) and is not a failure
        daemon_unit = render_unit("sbxloop-daemon.service", home)
        assert "SuccessExitStatus=143" in daemon_unit and "Restart=always" in daemon_unit

    def test_launchers_bind_to_their_own_home(self) -> None:
        for name in ("sbxloop.launcher.sh", "sbx.launcher.sh"):
            text = template(name)
            assert text.startswith("#!/bin/sh\n")
            assert 'export SBXLOOP_HOME="$home"' in text
            assert "/usr/sbin:/sbin" in text  # mkfs.ext4 for sandboxd's block driver
            assert "DBUS_SESSION_BUS_ADDRESS" in text
        assert 'exec "$home/sbx/bin/sbx" "$@"' in template("sbx.launcher.sh")

    def test_the_windows_launcher_binds_to_its_own_home_too(self) -> None:
        text = template("sbxloop.launcher.cmd")
        assert 'set "SBXLOOP_HOME=%~dp0.."' in text
        assert r"venv\Scripts\sbxloop.exe" in text
        # the same trust boundary as the POSIX one: the launcher carries no
        # secrets and reads none — sbxloop reads config\\secrets.env itself.
        code = [ln for ln in text.splitlines() if not ln.strip().lower().startswith("rem")]
        assert not any("secrets" in ln for ln in code)

    @pytest.mark.parametrize(
        ("name", "system", "machine", "ok"),
        [
            ("sbx-0.38.0-linux-amd64.tar.gz", "Linux", "x86_64", True),
            ("sbx-0.38.0-linux-x86_64.tar.gz", "Linux", "amd64", True),
            ("sbx-0.38.0-linux-arm64.tar.gz", "Linux", "aarch64", True),
            ("sbx-0.38.0-darwin-arm64.tar.gz", "Darwin", "arm64", True),
            # the real names on docker/sbx-releases (field-verified v0.38.0)
            ("DockerSandboxes-linux-amd64.tar.gz", "Linux", "x86_64", True),
            ("DockerSandboxes-darwin.tar.gz", "Darwin", "arm64", True),
            ("DockerSandboxes-darwin.tar.gz", "Darwin", "x86_64", False),
            ("DockerSandboxes-darwin.dmg", "Darwin", "arm64", False),
            ("DockerSandboxes-linux-amd64-ubuntu2404.deb", "Linux", "x86_64", False),
            ("sbx-0.38.0-darwin-arm64.tar.gz", "Linux", "x86_64", False),
            ("sbx-0.38.0-linux-amd64.deb", "Linux", "x86_64", False),
            ("checksums.txt", "Linux", "x86_64", False),
        ],
    )
    def test_asset_selection(self, name: str, system: str, machine: str, ok: bool) -> None:
        assert sbx_asset_name_matches(name, system=system, machine=machine) is ok

    def test_path_hint(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path)
        assert path_hint(home, {"PATH": f"/usr/bin:{home.bin}"}) is None
        assert path_hint(home, {"PATH": "/usr/bin"}) == f'export PATH="{home.bin}:$PATH"'
        windows = SbxloopHome(tmp_path / "h", os_name="nt")
        assert path_hint(windows, {"PATH": "C:\\Windows"}).startswith("setx PATH")


def expand_specifiers(value: str) -> str:
    """systemd's specifier expansion, as far as a rendered unit uses it: the
    only specifier a path may produce is ``%%``, one literal percent. A bare
    ``%`` left on a line would expand to something else entirely."""
    out: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "%":
            assert value[index + 1 : index + 2] == "%", f"unescaped specifier in {value!r}"
            out.append("%")
            index += 2
        else:
            out.append(value[index])
            index += 1
    return "".join(out)


def parse_words(value: str) -> list[str]:
    """systemd's own word splitting of a command line or an ``Environment=``
    value: whitespace separates words, quotes group them, a backslash outside
    single quotes escapes what follows, and specifiers expand per word.

    This is deliberately a reader, not a mirror of the writer: it says what
    systemd will hand the service, so a test can assert the value rather than
    the punctuation around it.
    """
    words: list[str] = []
    word: list[str] = []
    quote: str | None = None
    started = False
    index = 0
    while index < len(value):
        char = value[index]
        index += 1
        if quote is None and char.isspace():
            if started:
                words.append("".join(word))
                word, started = [], False
            continue
        started = True
        if char == "\\" and quote != "'":
            word.append(value[index])
            index += 1
        elif quote is None and char in "\"'":
            quote = char
        elif char == quote:
            quote = None
        else:
            word.append(char)
    assert quote is None, f"unterminated quote in {value!r}"
    if started:
        words.append("".join(word))
    return [expand_specifiers(w) for w in words]


def values_of(unit: str, key: str) -> list[str]:
    return [line.partition("=")[2] for line in unit.splitlines() if line.startswith(f"{key}=")]


def environment_of(unit: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in values_of(unit, "Environment"):
        for word in parse_words(line):
            name, _, value = word.partition("=")
            out[name] = value
    return out


def working_directory_of(unit: str) -> str:
    (value,) = values_of(unit, "WorkingDirectory")
    # A verbatim directive: systemd keeps quotes as part of the path, so a
    # quoted one is a bug however good it looks.
    assert '"' not in value, f"WorkingDirectory= must not be quoted: {value!r}"
    return expand_specifiers(value)


class TestUnitPaths:
    """#894: the home and the runner directory are whatever the operator
    chose. Each systemd directive spells a space, a percent or a dollar its
    own way, and a unit that spells one wrong starts the wrong command — or
    nothing at all."""

    def render_all(self, home: SbxloopHome, runner: Path) -> dict[str, str]:
        return {
            name: render_unit(name, home, runner_dir=runner) for name in (*UNIT_NAMES, RUNNER_UNIT)
        }

    @pytest.mark.parametrize(
        "root",
        [
            "/home/alice/Loop Data",  # the issue's own reproduction
            "/srv/loop 50%",  # a specifier systemd would otherwise expand
            "/srv/$HOME dir",  # no expansion happens in an executable path
        ],
    )
    def test_every_directive_carries_the_whole_path(self, root: str) -> None:
        home = SbxloopHome(Path(root))
        runner = Path(f"{root}/Actions Runner")
        units = self.render_all(home, runner)

        daemon = units["sbxloop-daemon.service"]
        assert parse_words(values_of(daemon, "ExecStart")[0]) == [
            f"{root}/bin/sbxloop",
            "daemon",
        ]
        assert environment_of(daemon) == {"SBXLOOP_HOME": root, "PYTHONUNBUFFERED": "1"}
        assert working_directory_of(daemon) == root

        sandboxd = units["sbx-sandboxd.service"]
        assert parse_words(values_of(sandboxd, "ExecStart")[0]) == [
            f"{root}/bin/sbx",
            "daemon",
            "start",
        ]
        assert parse_words(values_of(sandboxd, "ExecStop")[0]) == [
            f"{root}/bin/sbx",
            "daemon",
            "stop",
        ]
        assert environment_of(sandboxd)["SBXLOOP_HOME"] == root

        runner_unit = units[RUNNER_UNIT]
        assert parse_words(values_of(runner_unit, "ExecStart")[0]) == [f"{runner}/run.sh"]
        assert working_directory_of(runner_unit) == str(runner)

    def test_a_simple_path_renders_exactly_as_before(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / "home")
        runner = tmp_path / "runner"
        for name, unit in self.render_all(home, runner).items():
            plain = (
                template(name).replace("@HOME@", str(home.root)).replace("@RUNNER@", str(runner))
            )
            assert unit == plain, name

    def test_a_path_in_a_comment_reads_as_the_operator_typed_it(self) -> None:
        home = SbxloopHome(Path("/home/alice/Loop Data"))
        unit = render_unit("sbxloop-daemon.service", home)
        assert "# at /home/alice/Loop Data and linked into" in unit

    @pytest.mark.parametrize(
        ("root", "message"),
        [
            ("/home/o'brien/loop", "refuses"),  # quotes are out in an executable
            ('/home/al"ice/loop', "refuses"),
            ("/home/back\\slash/loop", "refuses"),
            ("/home/alice/loop\tdata", "refuses"),  # a tab is a control character
            ("/home/alice/loop\nExecStart=/bin/sh", "control characters"),
            ("/home/alice/loop ", "whitespace"),  # WorkingDirectory= strips it
        ],
    )
    def test_what_systemd_cannot_carry_stops_init_by_name(self, root: str, message: str) -> None:
        home = SbxloopHome(Path(root))
        with pytest.raises(InitError) as caught:
            render_unit("sbxloop-daemon.service", home)
        assert message in str(caught.value) and repr(root) in str(caught.value)

    def test_a_runner_directory_is_checked_the_same_way(self) -> None:
        home = SbxloopHome(Path("/home/alice/loop"))
        with pytest.raises(InitError) as caught:
            render_unit(RUNNER_UNIT, home, runner_dir=Path("/home/o'brien/runner"))
        assert "/home/o'brien/runner" in str(caught.value)

    def test_a_directive_with_no_rule_stops_rather_than_guesses(self) -> None:
        values = {"@HOME@": "/home/alice/Loop Data"}
        with pytest.raises(InitError, match="no systemd quoting rule"):
            _render_unit_line("RuntimeDirectory=@HOME@", values)
        with pytest.raises(InitError, match="not a directive"):
            _render_unit_line("@HOME@", values)
        with pytest.raises(InitError, match="open the executable word"):
            _render_unit_line("ExecStart=-@HOME@/bin/sbxloop", values)

    def test_an_argument_word_keeps_its_dollar_out_of_expansion(self) -> None:
        # No template puts a path in an argument today; the rule that says how
        # it would be spelled is the difference between $x and a variable.
        rendered = _render_word(
            "@HOME@/a b",
            {"@HOME@": "/srv/$x 50%"},
            directive="ExecStart",
            executable=False,
            expand_dollar=True,
        )
        assert rendered == '"/srv/$$x 50%%/a b"'
        assert parse_words(rendered.replace("$$", "$")) == ["/srv/$x 50%/a b"]

    @pytest.mark.slow
    @pytest.mark.skipif(
        shutil.which("systemd-analyze") is None, reason="systemd tooling is not on this host"
    )
    def test_systemd_itself_accepts_the_rendered_units(self, tmp_path: Path) -> None:
        # Dummy executables at the real paths, verified read-only: nothing is
        # installed, started, or enabled on the host running the test.
        home = SbxloopHome(tmp_path / "sbxloop home 50% $x")
        runner = tmp_path / "actions runner"
        for executable in (home.bin / "sbxloop", home.bin / "sbx", runner / "run.sh"):
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
        units = tmp_path / "units"
        units.mkdir()
        for name, text in self.render_all(home, runner).items():
            (units / name).write_text(text)
        proc = subprocess.run(
            ["systemd-analyze", "verify", *(str(units / n) for n in (*UNIT_NAMES, RUNNER_UNIT))],
            capture_output=True,
            text=True,
            check=False,
        )
        if "Failed to initialize manager" in proc.stderr:
            pytest.skip(f"systemd-analyze cannot run here: {proc.stderr.strip()}")
        assert proc.returncode == 0, proc.stderr
        assert "Invalid environment assignment" not in proc.stderr


class TestCli:
    def test_project_writes_the_repository_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--project"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "sbxloop.toml").is_file()
        assert not (tmp_path / ".sbxloop" / "bin" / "sbxloop").exists()

    def test_dry_run_prints_the_plan_for_the_home(self, tmp_path: Path) -> None:
        # HOME is tmp_path (autouse fixture): the home is tmp_path/.sbxloop.
        result = runner.invoke(app, ["init", "--dry-run", "--systemd"])
        assert result.exit_code == 0, result.output
        assert f"sbxloop home: {tmp_path / '.sbxloop'}" in result.output
        assert "would tree" in result.output and "would systemd" in result.output
        assert not (tmp_path / ".sbxloop" / "bin" / "sbxloop").exists()

    def test_unknown_preset_is_refused_before_anything_happens(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["init", "--preset", "huge-repo", "--dry-run"])
        assert result.exit_code == 2, result.output
        assert "unknown preset" in result.output


class TestUvDirectories:
    """Init builds one home, and every uv command it runs must build *that*
    home: the managed CPython under ``python/`` and the cache under
    ``cache/uv``, whatever uv directories the operator's own environment
    names. The launcher exports the same two, so a home whose init wrote
    them elsewhere is a home whose launcher cannot find its interpreter."""

    DECOY = "/somewhere/else"

    def arrange(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: Any
    ) -> tuple[SbxloopHome, HomeInit, RecordingRun, FakeRun]:
        monkeypatch.setattr("shutil.which", lambda _name: None)  # no uv on PATH: fetch it
        home, init, run, _fetch, _said = make(tmp_path, **overrides)
        recorder = RecordingRun(run)
        init.run = recorder
        return home, init, recorder, run

    def wanted(self, home: SbxloopHome) -> dict[str, str]:
        return {
            "UV_PYTHON_INSTALL_DIR": str(home.python),
            "UV_CACHE_DIR": str(home.cache / "uv"),
        }

    def assert_home_scoped(self, home: SbxloopHome, seen: dict[str, str | None]) -> None:
        wanted = self.wanted(home)
        assert {key: seen[key] for key in wanted} == wanted

    def test_every_uv_command_builds_under_the_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in RecordingRun.KEYS:
            monkeypatch.delenv(key, raising=False)
        home, init, recorder, _ = self.arrange(tmp_path, monkeypatch)
        init.execute()
        for words in (("python", "install"), ("venv",), ("pip", "install")):
            self.assert_home_scoped(home, recorder.env_of(*words))
        # and nothing of ours is left behind for whatever runs next
        assert not any(os.environ.get(key) for key in RecordingRun.KEYS)

    def test_inherited_settings_cannot_redirect_the_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in RecordingRun.KEYS:
            monkeypatch.setenv(key, self.DECOY)
        home, init, recorder, _ = self.arrange(tmp_path, monkeypatch)
        init.execute()
        for words in (("python", "install"), ("venv",), ("pip", "install")):
            self.assert_home_scoped(home, recorder.env_of(*words))
        # the operator's own settings are theirs again once init returns
        assert all(os.environ[key] == self.DECOY for key in RecordingRun.KEYS)

    def test_the_bootstrap_routes_agree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", self.DECOY)
        monkeypatch.setenv("UV_CACHE_DIR", self.DECOY)
        # downloaded: the installer script itself runs pointed at the home
        home, init, recorder, _ = self.arrange(tmp_path, monkeypatch)
        init.execute()
        downloaded = recorder.env_of_bootstrap()
        self.assert_home_scoped(home, downloaded)
        assert downloaded["UV_INSTALL_DIR"] == str(home.bin)
        # copied from PATH: no installer to run, and the same directories after
        other = tmp_path / "other"
        fake_uv = tmp_path / "uv-on-path"
        fake_uv.write_text("#!uv\n")
        fake_uv.chmod(0o755)
        monkeypatch.setattr("shutil.which", lambda name: str(fake_uv) if name == "uv" else None)
        copied_home = SbxloopHome(other / "home")
        copied = RecordingRun(FakeRun(copied_home))
        HomeInit(
            copied_home,
            InitOptions(version="1.2.3", sbx=False),
            env={"HOME": str(other), "PATH": "/usr/bin"},
            run=copied,
            fetch=FakeFetch(),
            system="Linux",
            machine="x86_64",
            sys_prefix=other / "elsewhere-venv",
            user_units=other / "units",
        ).execute()
        assert not any(argv[0] == "sh" for argv, _ in copied.seen)  # nothing to bootstrap
        for words in (("python", "install"), ("venv",), ("pip", "install")):
            self.assert_home_scoped(home, recorder.env_of(*words))
            self.assert_home_scoped(copied_home, copied.env_of(*words))

    def test_a_failed_uv_command_restores_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in RecordingRun.KEYS:
            monkeypatch.setenv(key, self.DECOY)
        home, init, _recorder, run = self.arrange(tmp_path, monkeypatch)
        run.fail = {f"{home.uv} pip install": 3}
        with pytest.raises(subprocess.CalledProcessError):
            init.execute()
        assert all(os.environ[key] == self.DECOY for key in RecordingRun.KEYS)

    def test_running_from_the_home_venv_touches_neither(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in RecordingRun.KEYS:
            monkeypatch.setenv(key, self.DECOY)
        home, init, _recorder, run = self.arrange(tmp_path, monkeypatch)
        init.execute()
        again = RecordingRun(run)
        HomeInit(
            home,
            InitOptions(),  # this version, from the home's own venv
            env={"HOME": str(tmp_path), "PATH": ""},
            run=again,
            fetch=FakeFetch(),
            system="Linux",
            machine="x86_64",
            sys_prefix=home.venv,
            user_units=tmp_path / "units",
        ).execute()
        assert again.seen == []  # no uv command at all: nothing to rebuild
        assert all(os.environ[key] == self.DECOY for key in RecordingRun.KEYS)
