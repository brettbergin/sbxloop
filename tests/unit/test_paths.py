"""The sbxloop home (:mod:`sbxloop.paths`): one root, every path derived
from it, and the layouts it replaced reported rather than read."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sbxloop.config import Config, ConfigError, load_config, load_config_with_sources
from sbxloop.paths import (
    HOME_ENV,
    LAYOUT_VERSION,
    SbxloopHome,
    home_root_from_env,
    legacy_paths,
    resolve_home_root,
)


class TestRoot:
    def test_sbxloop_home_wins(self, tmp_path: Path) -> None:
        env = {HOME_ENV: str(tmp_path / "elsewhere"), "HOME": str(tmp_path / "home")}
        assert home_root_from_env(env) == (tmp_path / "elsewhere").resolve()

    def test_home_dot_sbxloop_is_the_default(self, tmp_path: Path) -> None:
        assert home_root_from_env({"HOME": str(tmp_path)}) == tmp_path / ".sbxloop"

    def test_hermetic_mapping_names_no_home(self) -> None:
        assert home_root_from_env({}) is None

    def test_tilde_expands_against_the_mapped_home(self, tmp_path: Path) -> None:
        env = {HOME_ENV: "~/loop", "HOME": str(tmp_path)}
        assert home_root_from_env(env) == (tmp_path / "loop").resolve()

    def test_relative_value_is_anchored_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert home_root_from_env({HOME_ENV: "rel"}) == (tmp_path / "rel").resolve()

    def test_process_fallback(self, tmp_path: Path) -> None:
        # The autouse fixture points HOME at tmp_path.
        assert resolve_home_root() == tmp_path / ".sbxloop"
        assert resolve_home_root({}) == Path.home() / ".sbxloop"


class TestLayout:
    def test_every_path_hangs_off_the_root(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / "h")
        for path in (
            home.bin,
            home.venv,
            home.config,
            home.state,
            home.runs,
            home.workspaces,
            home.logs,
            home.cache,
            home.tmp,
            home.systemd,
            home.backups,
            home.record,
            home.launcher,
            home.sbx_launcher,
            home.config_toml,
            home.secrets_env,
            home.github_app_pem,
            home.state_db,
            home.bake_json,
            home.conformance,
            home.daemon,
            home.ctl,
            home.github_workspace,
            home.concierge_workspace,
            home.gc_pending,
            home.daemon_log,
            home.console,
            home.deploy_logs,
            home.worker_wheels,
            home.run_dir("r1"),
            home.run_workspace("r1"),
            home.run_artifacts("r1"),
            home.run_data("r1"),
            home.workspace_for("o/n"),
        ):
            assert path.is_relative_to(home.root), path
        assert home.state_db == home.root / "state" / "state.db"
        assert home.run_workspace("rabc") == home.root / "runs" / "rabc" / "workspace"
        assert home.ctl == home.root / "state" / "daemon" / "ctl"
        assert home.workspace_for("owner/name") == home.root / "workspaces" / "owner" / "name"

    @pytest.mark.parametrize("repo", ["", "owner", "owner/", "/name", "a/b/c", "../x", "o/.."])
    def test_workspace_for_rejects_non_repos(self, tmp_path: Path, repo: str) -> None:
        with pytest.raises(ValueError):
            SbxloopHome(tmp_path).workspace_for(repo)

    def test_ensure_tree_is_idempotent_and_private_where_it_matters(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / "h")
        home.ensure_tree()
        assert home.missing_directories() == []
        home.ensure_tree()
        if os.name == "posix":
            assert home.config.stat().st_mode & 0o777 == 0o700
        assert not home.venv.exists()  # the installer's, not a plain directory

    def test_record_round_trip(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / "h")
        home.ensure_tree()
        assert not home.initialised and home.read_record() is None
        first = home.write_record(sbxloop_version="1.2.3", created_by="sbxloop init")
        assert home.initialised
        assert first.layout_version == LAYOUT_VERSION
        again = home.write_record(sbxloop_version="1.2.4", created_by="install.sh")
        assert again.created_at == first.created_at and again.created_by == "sbxloop init"
        assert again.sbxloop_version == "1.2.4"
        assert home.read_record() == again

    def test_as_env_points_a_child_here(self, tmp_path: Path) -> None:
        assert SbxloopHome(tmp_path).as_env() == {HOME_ENV: str(tmp_path)}


class TestConfigHome:
    """``Config.home`` comes from the environment, never from a file."""

    def test_default_home_follows_the_mapped_home(self, tmp_path: Path) -> None:
        mapped = tmp_path / "mapped"
        config = load_config(cwd=tmp_path, env={"HOME": str(mapped)})
        assert config.home == mapped / ".sbxloop"
        assert config.paths.state_db == mapped / ".sbxloop" / "state" / "state.db"

    def test_sbxloop_home_moves_the_whole_home(self, tmp_path: Path) -> None:
        config, sources = load_config_with_sources(
            cwd=tmp_path, env={HOME_ENV: str(tmp_path / "x"), "HOME": str(tmp_path)}
        )
        assert config.home == (tmp_path / "x").resolve()
        assert "home" not in {k for k, v in sources.items() if v == "env"}

    def test_hermetic_env_falls_back_to_the_process_home(self, tmp_path: Path) -> None:
        # HOME is tmp_path (autouse fixture); an empty mapping still lands there.
        assert load_config(cwd=tmp_path, env={}).home == tmp_path / ".sbxloop"

    def test_a_config_object_can_be_pointed_anywhere(self, tmp_path: Path) -> None:
        config = Config.model_validate({"home": str(tmp_path / "h")})
        assert config.paths.runs == tmp_path / "h" / "runs"
        assert Config.model_validate({"home": "~/h"}).home == tmp_path / "h"

    @pytest.mark.parametrize(
        ("text", "key"),
        [
            ('state_dir = ".sbxloop"\n', "state_dir"),
            ('[daemon]\nstate_dir = "/var/lib/sbxloop"\n', "daemon.state_dir"),
            (f'home = "{"/tmp/elsewhere"}"\n', "home"),
        ],
    )
    def test_retired_path_keys_are_refused_by_name(
        self, tmp_path: Path, text: str, key: str
    ) -> None:
        (tmp_path / "sbxloop.toml").write_text(text)
        with pytest.raises(ConfigError, match=f"'{key}'.*no longer a setting.*SBXLOOP_HOME"):
            load_config(cwd=tmp_path, env={})

    def test_retired_env_override_is_refused_too(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="state_dir"):
            load_config(cwd=tmp_path, env={"SBXLOOP_STATE_DIR": str(tmp_path)})
        with pytest.raises(ConfigError, match=r"daemon\.state_dir"):
            load_config(cwd=tmp_path, env={"SBXLOOP_DAEMON__STATE_DIR": str(tmp_path)})

    def test_home_config_is_the_lowest_layer(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / ".sbxloop")
        home.config.mkdir(parents=True, exist_ok=True)
        home.config_toml.write_text('model = "from-home"\n[budgets]\nmax_tasks = 3\n')
        project = tmp_path / "proj"
        project.mkdir()
        config, sources = load_config_with_sources(cwd=project, env={"HOME": str(tmp_path)})
        assert config.model == "from-home" and sources["model"] == "home config"
        assert config.budgets.max_tasks == 3
        (project / "sbxloop.toml").write_text('model = "from-project"\n')
        config, sources = load_config_with_sources(cwd=project, env={"HOME": str(tmp_path)})
        assert config.model == "from-project" and sources["model"] == "sbxloop.toml"
        assert config.budgets.max_tasks == 3

    def test_hermetic_mapping_reads_no_home_config(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / ".sbxloop")
        home.config.mkdir(parents=True, exist_ok=True)
        home.config_toml.write_text('model = "from-home"\n')
        assert load_config(cwd=tmp_path, env={}).model == "auto"


class TestLegacy:
    def test_nothing_on_a_clean_host(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / ".sbxloop")
        home.ensure_tree()
        assert legacy_paths(home, {"HOME": str(tmp_path)}, cwd=tmp_path / "work") == []

    def test_every_old_location_is_named(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / ".sbxloop")
        home.ensure_tree()
        (tmp_path / ".config" / "sbxloop").mkdir(parents=True)
        (tmp_path / ".local" / "state" / "sbxloop" / "runner").mkdir(parents=True)
        (tmp_path / ".sbxloop-venv").mkdir()
        (tmp_path / ".local" / "bin").mkdir()
        (tmp_path / ".local" / "bin" / "sbxloop").write_text("#!/bin/sh\n")
        (home.root / "state.db").write_bytes(b"")  # the flat layout
        work = tmp_path / "work"
        (work / ".sbxloop").mkdir(parents=True)
        (work / ".sbxloop" / "state.db").write_bytes(b"")
        found = {p.path for p in legacy_paths(home, {"HOME": str(tmp_path)}, cwd=work)}
        assert found == {
            tmp_path / ".config" / "sbxloop",
            tmp_path / ".local" / "state" / "sbxloop",
            tmp_path / ".sbxloop-venv",
            tmp_path / ".local" / "bin" / "sbxloop",
            home.root / "state.db",
            work / ".sbxloop" / "state.db",
        }

    def test_xdg_variables_are_honoured_when_looking(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / ".sbxloop")
        (tmp_path / "xdg-state" / "sbxloop").mkdir(parents=True)
        env = {"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "xdg-state")}
        assert [p.path for p in legacy_paths(home, env)] == [tmp_path / "xdg-state" / "sbxloop"]

    def test_the_home_itself_is_never_a_leftover(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / ".sbxloop")
        home.ensure_tree()
        home.state_db.write_bytes(b"")
        assert legacy_paths(home, {"HOME": str(tmp_path)}, cwd=home.root) == []
