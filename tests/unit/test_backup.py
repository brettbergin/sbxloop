"""``sbxloop backup``: snapshots of what the home cannot regenerate."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sbxloop.backup import (
    BackupError,
    create_backup,
    find_backup,
    list_backups,
    prune_backups,
    restore_backup,
)
from sbxloop.cli.app import app
from sbxloop.engine.store import StateStore
from sbxloop.paths import SbxloopHome

runner = CliRunner()


def seeded_home(root: Path) -> SbxloopHome:
    home = SbxloopHome(root)
    home.ensure_tree()
    home.config_toml.write_text('model = "mine"\n')
    home.secrets_env.write_text("GH_TOKEN=x\n")
    home.secrets_env.chmod(0o600)
    home.github_app_pem.write_text("-----BEGIN-----\n")
    store = StateStore(home.state_db)
    store.create_run("r1abcdefg", "an outcome")
    store.close()
    home.unit("sbxloop-daemon.service").write_text("[Unit]\n")
    home.launcher.write_text("#!/bin/sh\n")
    home.write_record(sbxloop_version="1.2.3", created_by="test")
    (home.runs / "r1abcdefg" / "workspace").mkdir(parents=True)
    (home.runs / "r1abcdefg" / "workspace" / "big").write_bytes(b"x" * 4096)
    return home


class TestCreate:
    def test_carries_config_state_units_and_launchers_not_runs(self, tmp_path: Path) -> None:
        home = seeded_home(tmp_path / "h")
        clock = iter([1_700_000_000.0, 1_700_000_000.0]).__next__
        info = create_backup(home, label="before-edit", reason="editing", clock=clock)
        assert info.path == home.backups / "20231114T221320Z-before-edit"
        names = sorted(str(p.relative_to(info.path)) for p in info.path.rglob("*") if p.is_file())
        assert names == [
            "MANIFEST",
            "bin/sbxloop",
            "config/github-app.pem",
            "config/sbxloop.toml",
            "config/secrets.env",
            "home.json",
            "meta.json",
            "state/state.db",
            "systemd/sbxloop-daemon.service",
        ]
        assert (info.path / "config" / "secrets.env").stat().st_mode & 0o777 == 0o600
        manifest = (info.path / "MANIFEST").read_text()
        assert "config/sbxloop.toml" in manifest and len(manifest.splitlines()) == info.files == 7
        # the database copy is a real, independent SQLite file
        rows = sqlite3.connect(info.path / "state" / "state.db").execute("SELECT run_id FROM runs")
        assert [r[0] for r in rows] == ["r1abcdefg"]

    def test_extra_files_ride_along(self, tmp_path: Path) -> None:
        home = seeded_home(tmp_path / "h")
        old = tmp_path / "old-secrets.env"
        old.write_text("OLD=1\n")
        info = create_backup(home, extra={"legacy/old-secrets.env": old})
        assert (info.path / "legacy" / "old-secrets.env").read_text() == "OLD=1\n"
        assert (info.path / "legacy" / "old-secrets.env").stat().st_mode & 0o777 == 0o600

    def test_label_is_validated(self, tmp_path: Path) -> None:
        home = seeded_home(tmp_path / "h")
        with pytest.raises(BackupError, match="label"):
            create_backup(home, label="../x")

    def test_empty_home_still_snapshots(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / "h")
        home.ensure_tree()
        info = create_backup(home)
        assert info.files == 0 and (info.path / "MANIFEST").read_text() == ""


class TestListRestorePrune:
    def test_list_is_newest_first_and_ignores_strangers(self, tmp_path: Path) -> None:
        home = seeded_home(tmp_path / "h")
        clock = iter([1.0, 1.0, 2.0, 2.0, 3.0, 3.0]).__next__
        create_backup(home, label="a", clock=clock)
        create_backup(home, label="b", clock=clock)
        create_backup(home, label="c", clock=clock)
        (home.backups / "not-a-backup").mkdir()
        assert [b.label for b in list_backups(home)] == ["c", "b", "a"]
        assert find_backup(home, list_backups(home)[0].name).label == "c"
        with pytest.raises(BackupError, match="no backup"):
            find_backup(home, "nope")

    def test_restore_puts_files_back_but_not_legacy_extras(self, tmp_path: Path) -> None:
        home = seeded_home(tmp_path / "h")
        old = tmp_path / "old.env"
        old.write_text("OLD=1\n")
        info = create_backup(home, extra={"legacy/old.env": old})
        home.config_toml.write_text('model = "broken"\n')
        home.state_db.unlink()
        restored = restore_backup(home, info.name)
        assert "config/sbxloop.toml" in restored and "state/state.db" in restored
        assert not any(r.startswith("legacy/") for r in restored)
        assert home.config_toml.read_text() == 'model = "mine"\n'
        assert StateStore(home.state_db).list_runs()[0].run_id == "r1abcdefg"
        assert not (home.root / "legacy").exists()

    def test_restore_refuses_a_live_daemon(self, tmp_path: Path) -> None:
        home = seeded_home(tmp_path / "h")
        info = create_backup(home)
        with pytest.raises(BackupError, match="daemon is running"):
            restore_backup(home, info.name, daemon_live=True)

    def test_prune_keeps_the_newest(self, tmp_path: Path) -> None:
        home = seeded_home(tmp_path / "h")
        clock = iter(float(i) for i in range(1, 20)).__next__
        for label in ("a", "b", "c", "d"):
            create_backup(home, label=label, clock=clock)
        removed = prune_backups(home, keep=2)
        assert [b.label for b in removed] == ["b", "a"]
        assert [b.label for b in list_backups(home)] == ["d", "c"]
        assert prune_backups(home, keep=0) == []  # 0 keeps everything


class TestCli:
    def test_backup_list_restore_prune(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = seeded_home(tmp_path / ".sbxloop")  # HOME is tmp_path (autouse fixture)
        result = runner.invoke(app, ["backup", "--label", "first"])
        assert result.exit_code == 0, result.output
        assert "-first" in result.output.replace("\n", "")
        listed = runner.invoke(app, ["backup", "list"], env={"COLUMNS": "200"})
        assert listed.exit_code == 0 and "first" in listed.output
        name = list_backups(home)[0].name
        home.config_toml.write_text("model = 'x'\n")
        restored = runner.invoke(app, ["backup", "restore", name])
        assert restored.exit_code == 0, restored.output
        assert home.config_toml.read_text() == 'model = "mine"\n'
        pruned = runner.invoke(app, ["backup", "prune", "--keep", "0"])
        assert pruned.exit_code == 0 and "nothing to prune" in pruned.output
        pruned = runner.invoke(app, ["backup", "prune", "--keep", "1"])
        assert pruned.exit_code == 0 and "nothing to prune" in pruned.output
        runner.invoke(app, ["backup", "--label", "second"])
        pruned = runner.invoke(app, ["backup", "prune", "--keep", "1"])
        assert pruned.exit_code == 0 and "removed" in pruned.output
        assert [b.label for b in list_backups(home)] == ["second"]

    def test_daemon_sweep_prunes_backups(self, tmp_path: Path) -> None:
        from sbxloop.config import Config
        from sbxloop.daemon.loop import DaemonLoop
        from sbxloop.daemon.store import DaemonStore
        from tests.unit.test_daemon_loop import FakeSource

        home = seeded_home(tmp_path / "h")
        clock = iter(float(i) for i in range(1, 40)).__next__
        for label in ("a", "b", "c"):
            create_backup(home, label=label, clock=clock)
        config = Config.model_validate({"home": str(home.root), "daemon": {"backups_keep": 1}})
        store = StateStore(home.state_db)
        loop = DaemonLoop(
            config, store=store, dstore=DaemonStore(home.state_db), source=FakeSource([])
        )
        loop.gc(now=1.0)
        assert [b.label for b in list_backups(home)] == ["c"]
