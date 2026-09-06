"""``sbxloop init``: the home laid out, installed into and wired, without a
network or a shell — every command and download goes through a fake."""

from __future__ import annotations

import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sbxloop.cli.app import app
from sbxloop.homeinit import (
    SBX_VERSION,
    UNIT_NAMES,
    HomeInit,
    InitError,
    InitOptions,
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
    laying sbx out) are simulated so later steps see them."""

    def __init__(self, home: SbxloopHome) -> None:
        self.home = home
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
            (self.home.venv / "bin").mkdir(parents=True, exist_ok=True)
            self.home.venv_python.write_text("#!python\n")
        if argv[0].endswith("install.sh"):
            # Docker's installer, honouring PREFIX from the environment.
            prefix = Path(os.environ["PREFIX"])
            (prefix / "bin").mkdir(parents=True, exist_ok=True)
            (prefix / "bin" / "sbx").write_text("#!sbx\n")
        if argv[0] == "sh" and argv[1].endswith("uv-install.sh"):
            self.home.uv.write_text("#!uv\n")
        return subprocess.CompletedProcess(argv, 0, "", "")


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


def make(
    tmp_path: Path, **overrides: Any
) -> tuple[SbxloopHome, HomeInit, FakeRun, FakeFetch, list[str]]:
    home = SbxloopHome(tmp_path / "home")
    run, fetch, said = FakeRun(home), FakeFetch(), []
    options = InitOptions(**{"version": "1.2.3", **overrides})
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
        root's business; the unprivileged run still leaves a working sbx."""
        home, init, run, _, _ = make(tmp_path)

        original = run.__call__

        def flaky(argv: Any) -> subprocess.CompletedProcess[str]:
            result = original(argv)
            if str(argv[0]).endswith("install.sh"):
                raise subprocess.CalledProcessError(
                    1, list(argv), stderr="apparmor: permission denied"
                )
            return result

        init.run = flaky  # type: ignore[assignment]
        report = init.execute()
        assert home.sbx_binary.exists()
        assert any("AppArmor" in n for n in report.notes)

    def test_sbx_installer_failing_outright_is_an_error(self, tmp_path: Path) -> None:
        _, init, run, _, _ = make(tmp_path)
        run.fail["sbx"] = 1  # no such prefix; the install.sh call fails before writing
        original = run.__call__

        def failing(argv: Any) -> subprocess.CompletedProcess[str]:
            if str(argv[0]).endswith("install.sh"):
                raise subprocess.CalledProcessError(2, list(argv), stderr="mkfs.ext4 not found")
            return original(argv)

        init.run = failing  # type: ignore[assignment]
        with pytest.raises(InitError, match=r"mkfs\.ext4"):
            init.execute()

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

    def test_launchers_bind_to_their_own_home(self) -> None:
        for name in ("sbxloop.launcher.sh", "sbx.launcher.sh"):
            text = template(name)
            assert text.startswith("#!/bin/sh\n")
            assert 'export SBXLOOP_HOME="$home"' in text
            assert "/usr/sbin:/sbin" in text  # mkfs.ext4 for sandboxd's block driver
            assert "DBUS_SESSION_BUS_ADDRESS" in text
        assert 'exec "$home/sbx/bin/sbx" "$@"' in template("sbx.launcher.sh")

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
