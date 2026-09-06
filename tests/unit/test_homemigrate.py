"""``sbxloop init --migrate``: a pre-home installation found, snapshotted,
carried and (with --purge) removed — through fakes, no systemd, no net."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sbxloop.backup import list_backups
from sbxloop.config import load_config
from sbxloop.engine.store import StateStore
from sbxloop.homeinit import HomeInit, InitOptions
from sbxloop.homemigrate import (
    HomeMigration,
    MigrateError,
    MigrateOptions,
    discover,
    migrate_options_for,
)
from sbxloop.paths import SbxloopHome
from tests.unit.test_homeinit import FakeFetch, FakeRun


class SystemdRun(FakeRun):
    """The init fake plus `systemctl is-active`."""

    def __init__(self, home: SbxloopHome, *, active: bool) -> None:
        super().__init__(home)
        self.active = active

    def __call__(self, argv: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(a) for a in argv]
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            self.calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0 if self.active else 3, "active\n" if self.active else "inactive\n", ""
            )
        return super().__call__(argv)


def git_checkout(path: Path, origin: str) -> Path:
    from git import Repo

    path.mkdir(parents=True)
    repo = Repo.init(path)
    (path / "README").write_text("x\n")
    repo.index.add(["README"])
    repo.index.commit("init")
    repo.create_remote("origin", origin)
    return path


def legacy_host(tmp_path: Path, *, active: bool = True) -> dict[str, Any]:
    """The field host before the home: everything the old rules scattered."""
    user_home = tmp_path
    config_dir = user_home / ".config" / "sbxloop"
    config_dir.mkdir(parents=True)
    (config_dir / "secrets.env").write_text(
        "GH_TOKEN=old\nGITHUB_APP_PRIVATE_KEY_PATH=" + str(config_dir / "github-app.pem") + "\n"
    )
    (config_dir / "github-app.pem").write_text("PEM\n")
    (config_dir / "env.sh").write_text("set -a; . secrets.env\n")
    work = user_home / "sbxloop-work"
    work.mkdir()
    checkout = git_checkout(work / "mountain-dew", "https://github.com/acme/mountain-dew.git")
    (work / "sbxloop.toml").write_text(
        'state_dir = "~/elsewhere"\nmodel = "claude-sonnet-5"\n\n[sandbox]\nworkspace = "'
        + str(checkout)
        + '"\n\n[daemon]\nstate_dir = "'
        + str(user_home / ".local" / "state" / "sbxloop" / "sbxloop-work")
        + '"\npoll_interval_s = 60.0\n'
    )
    (work / "sbxloop.toml.bak").write_text("old\n")
    (work / "workload-profile.toml").write_text("orphan\n")
    (work / "entrygraph").mkdir()  # another clone, not ours to touch
    live = user_home / ".local" / "state" / "sbxloop" / "sbxloop-work"
    live.mkdir(parents=True)
    store = StateStore(live / "state.db")
    store.create_run("rlive0001", "the queue")
    store.close()
    (live / "runs" / "rlive0001" / "workspace").mkdir(parents=True)
    # The flat layout at the home's own path — not the initialised home the
    # autouse fixture lays out, which a host from before the home never had.
    stray = SbxloopHome(user_home / ".sbxloop")
    shutil.rmtree(stray.root, ignore_errors=True)
    stray.root.mkdir()
    sqlite3.connect(stray.root / "state.db").execute("CREATE TABLE t (x)").connection.close()
    (stray.root / "conformance").mkdir()
    (stray.root / "conformance" / "sbx-0.38.0.json").write_text("{}")
    (stray.root / "runs" / "rold00001" / "workspace").mkdir(parents=True)
    (user_home / ".sbxloop-venv" / "bin").mkdir(parents=True)
    (user_home / ".local" / "bin").mkdir(parents=True)
    (user_home / ".local" / "bin" / "sbxloop").write_text(
        '. "$HOME/.config/sbxloop/env.sh"\nexec venv\n'
    )
    (user_home / ".local" / "bin" / "sbx").write_text(
        '. "$HOME/.config/sbxloop/env.sh"\nexec /usr/local/bin/sbx\n'
    )
    units = user_home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "sbxloop-daemon.service").write_text(
        "[Service]\nWorkingDirectory=%h/sbxloop-work\nExecStart=%h/.local/bin/sbxloop daemon\n"
    )
    (units / "sbx-sandboxd.service").write_text(
        "[Service]\nExecStart=%h/.local/bin/sbx daemon start\n"
    )
    (user_home / "actions-runner").mkdir()
    (user_home / "actions-runner" / "run.sh").write_text("#!/bin/sh\n")
    env = {"HOME": str(user_home), "USER": "bergs", "PATH": "/usr/bin"}
    run = SystemdRun(stray, active=active)
    return {
        "home": stray,
        "env": env,
        "run": run,
        "units": units,
        "work": work,
        "live": live,
        "checkout": checkout,
        "config_dir": config_dir,
    }


def make_migration(tmp_path: Path, host: dict[str, Any], **opts: Any) -> HomeMigration:
    legacy = discover(
        host["home"],
        host["env"],
        cwd=tmp_path / "somewhere",
        run=host["run"],
        user_units=host["units"],
    )
    init = HomeInit(
        host["home"],
        migrate_options_for(legacy, InitOptions(version="1.2.3")),
        env=host["env"],
        run=host["run"],
        fetch=FakeFetch(),
        system="Linux",
        machine="x86_64",
        sys_prefix=tmp_path / ".sbxloop-venv",
        user_units=host["units"],
    )
    return HomeMigration(host["home"], legacy, MigrateOptions(**opts), init=init, run=host["run"])


class TestDiscover:
    def test_finds_everything_the_old_rules_scattered(self, tmp_path: Path) -> None:
        host = legacy_host(tmp_path)
        legacy = discover(
            host["home"], host["env"], cwd=tmp_path, run=host["run"], user_units=host["units"]
        )
        assert legacy.runner_dir == host["work"]
        assert legacy.config_toml == host["work"] / "sbxloop.toml"
        assert legacy.secrets == host["config_dir"] / "secrets.env"
        assert legacy.pem == host["config_dir"] / "github-app.pem"
        assert legacy.env_sh == host["config_dir"] / "env.sh"
        assert legacy.live_state_db == host["live"] / "state.db"
        assert host["home"].root / "state.db" in legacy.state_db_candidates
        assert legacy.venv == tmp_path / ".sbxloop-venv"
        assert {p.name for p in legacy.launchers} == {"sbxloop", "sbx"}
        assert {p.name for p in legacy.unit_files} == {
            "sbxloop-daemon.service",
            "sbx-sandboxd.service",
        }
        assert legacy.actions_runner == tmp_path / "actions-runner"
        assert legacy.daemon_active is True
        assert {p.name for p in legacy.runner_dir_files} == {
            "sbxloop.toml",
            "sbxloop.toml.bak",
            "workload-profile.toml",
        }
        assert any("runner directory" in line for line in legacy.summary())

    def test_clean_host_finds_nothing(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / ".sbxloop")
        legacy = discover(
            home,
            {"HOME": str(tmp_path)},
            cwd=tmp_path,
            run=FakeRun(home),
            user_units=tmp_path / "u",
        )
        assert legacy.live_state_db is None and legacy.state_db_candidates == []
        assert legacy.runner_dir is None and legacy.venv is None and legacy.launchers == []


class TestMigrate:
    def test_carries_the_daemon_and_removes_nothing_without_purge(self, tmp_path: Path) -> None:
        host = legacy_host(tmp_path)
        home: SbxloopHome = host["home"]
        report = make_migration(tmp_path, host).execute()
        run: SystemdRun = host["run"]
        # stopped first, restarted last
        assert ["systemctl", "--user", "stop", "sbxloop-daemon.service"] in run.calls
        assert run.calls[-1] == ["systemctl", "--user", "start", "sbxloop-daemon.service"]
        assert report.restarted
        # the backup holds every legacy file and every database
        assert report.backup is not None
        legacy_files = sorted(p.name for p in (report.backup.path / "legacy").iterdir())
        assert any("secrets.env" in n for n in legacy_files)
        assert any("sbxloop-daemon.service" in n for n in legacy_files)
        assert sum(n.endswith("state.db") for n in legacy_files) == 2  # live + the stray flat one
        # the live queue is now the home's
        assert StateStore(home.state_db).list_runs()[0].run_id == "rlive0001"
        # config carried: retired keys dropped, the workspace moved under the home
        text = home.config_toml.read_text()
        assert "state_dir" not in text and 'model = "claude-sonnet-5"' in text
        assert f'workspace = "{home.workspace_for("acme/mountain-dew")}"' in text
        assert (home.workspace_for("acme/mountain-dew") / "README").exists()
        assert not host["checkout"].exists()
        # secrets and the App key, with the key's path rewritten
        assert home.secrets_env.stat().st_mode & 0o777 == 0o600
        assert f"GITHUB_APP_PRIVATE_KEY_PATH={home.github_app_pem}" in home.secrets_env.read_text()
        assert home.github_app_pem.read_text() == "PEM\n"
        # the flat layout and the old run directories are gone from the home
        assert not (home.root / "state.db").exists() and not (home.root / "conformance").exists()
        assert not (home.runs / "rold00001").exists()
        # the home is laid out and stamped by init, with the runner unit re-rendered
        assert home.initialised and home.launcher.exists()
        assert home.unit("github-runner.service").exists()
        assert (
            home.backups / "units" / "sbxloop-daemon.service"
        ).exists()  # the old unit file, aside
        # nothing purged: the leftovers are listed
        assert tmp_path.joinpath(".sbxloop-venv").exists()
        assert str(tmp_path / ".sbxloop-venv") in report.left
        assert str(host["config_dir"]) in report.left
        # the carried config loads under the new rules
        config = load_config(cwd=tmp_path / "somewhere", env=host["env"])
        assert config.model == "claude-sonnet-5"
        assert config.sandbox.workspace == home.workspace_for("acme/mountain-dew")

    def test_purge_removes_the_leftovers_but_not_other_peoples_files(self, tmp_path: Path) -> None:
        host = legacy_host(tmp_path, active=False)
        report = make_migration(tmp_path, host, purge=True).execute()
        assert not report.restarted
        assert not (tmp_path / ".sbxloop-venv").exists()
        assert not (tmp_path / ".local" / "bin" / "sbxloop").exists()
        assert not host["config_dir"].exists()
        assert not (tmp_path / ".local" / "state" / "sbxloop").exists()
        assert not (host["work"] / "sbxloop.toml").exists()
        assert not (host["work"] / "workload-profile.toml").exists()
        assert (host["work"] / "entrygraph").exists()  # not ours
        assert any("still holds entrygraph" in n for n in report.notes)
        assert report.left == []
        # everything removed is still in the backup
        assert list_backups(host["home"])[0].label == "migrate"

    def test_keep_runs_leaves_the_homes_run_directories(self, tmp_path: Path) -> None:
        host = legacy_host(tmp_path, active=False)
        make_migration(tmp_path, host, keep_runs=True).execute()
        assert (host["home"].runs / "rold00001").exists()

    def test_ambiguous_state_db_needs_from(self, tmp_path: Path) -> None:
        host = legacy_host(tmp_path, active=False)
        (host["units"] / "sbxloop-daemon.service").unlink()  # no unit → no runner dir → ambiguous
        with pytest.raises(MigrateError, match="--from"):
            make_migration(tmp_path, host).execute()
        report = make_migration(tmp_path, host, state_db=host["live"] / "state.db").execute()
        assert StateStore(host["home"].state_db).list_runs()[0].run_id == "rlive0001"
        assert report.backup is not None

    def test_existing_home_config_is_kept(self, tmp_path: Path) -> None:
        host = legacy_host(tmp_path, active=False)
        home: SbxloopHome = host["home"]
        home.config.mkdir(parents=True)
        home.config_toml.write_text('model = "already"\n')
        report = make_migration(tmp_path, host).execute()
        assert home.config_toml.read_text() == 'model = "already"\n'
        assert any("already exists; kept" in n for n in report.notes)

    def test_secrets_are_private_and_pem_path_untouched_without_a_key(self, tmp_path: Path) -> None:
        host = legacy_host(tmp_path, active=False)
        (host["config_dir"] / "github-app.pem").unlink()
        make_migration(tmp_path, host).execute()
        text = host["home"].secrets_env.read_text()
        assert "GITHUB_APP_PRIVATE_KEY_PATH=" + str(host["config_dir"] / "github-app.pem") in text
        assert host["home"].secrets_env.stat().st_mode & 0o777 == 0o600
