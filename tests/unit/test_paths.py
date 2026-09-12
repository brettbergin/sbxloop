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
    user_home_from_env,
)


class TestRoot:
    def test_sbxloop_home_wins(self, tmp_path: Path) -> None:
        env = {HOME_ENV: str(tmp_path / "elsewhere"), "HOME": str(tmp_path / "home")}
        assert home_root_from_env(env) == (tmp_path / "elsewhere").resolve()

    def test_home_dot_sbxloop_is_the_default(self, tmp_path: Path) -> None:
        assert home_root_from_env({"HOME": str(tmp_path)}) == tmp_path / ".sbxloop"

    def test_hermetic_mapping_names_no_home(self) -> None:
        assert home_root_from_env({}) is None
        assert user_home_from_env({}) is None

    def test_a_windows_session_names_its_home_as_userprofile(self, tmp_path: Path) -> None:
        """#899: a native Windows session need not set HOME at all. The
        environment-only lookup used to find no home there while
        `resolve_home_root` fell back to one, so the process ran out of one
        home and read its config and secrets out of another."""
        profile = tmp_path / "Users" / "Ada"
        env = {"USERPROFILE": str(profile)}
        assert user_home_from_env(env) == profile
        assert home_root_from_env(env) == profile / ".sbxloop"
        assert resolve_home_root(env) == home_root_from_env(env)

    def test_a_home_with_spaces_in_it_survives(self, tmp_path: Path) -> None:
        profile = tmp_path / "Users" / "Ada Lovelace"
        assert home_root_from_env({"USERPROFILE": str(profile)}) == profile / ".sbxloop"
        assert home_root_from_env({HOME_ENV: str(profile / "loop")}) == (profile / "loop").resolve()

    def test_the_older_homedrive_homepath_pair_is_read_too(self, tmp_path: Path) -> None:
        env = {"HOMEDRIVE": str(tmp_path), "HOMEPATH": "/Users/Ada"}
        assert user_home_from_env(env) == tmp_path / "Users" / "Ada"

    def test_posix_home_wins_over_userprofile(self, tmp_path: Path) -> None:
        """A WSL2 distribution can carry both; HOME is the host it is."""
        env = {"HOME": str(tmp_path / "wsl"), "USERPROFILE": str(tmp_path / "win")}
        assert user_home_from_env(env) == tmp_path / "wsl"

    def test_sbxloop_home_still_wins_over_userprofile(self, tmp_path: Path) -> None:
        env = {HOME_ENV: str(tmp_path / "explicit"), "USERPROFILE": str(tmp_path / "win")}
        assert home_root_from_env(env) == (tmp_path / "explicit").resolve()

    def test_tilde_expands_against_userprofile_too(self, tmp_path: Path) -> None:
        env = {HOME_ENV: "~/loop", "USERPROFILE": str(tmp_path)}
        assert home_root_from_env(env) == (tmp_path / "loop").resolve()

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


class TestHostExecutables:
    """#899: where a venv puts its entry points, and whether an executable
    carries a suffix, is the *host's* platform — not the sandbox guest's."""

    def test_a_posix_home_keeps_the_layout_it_had(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path, os_name="posix")
        assert not home.windows
        assert home.venv_python == tmp_path / "venv" / "bin" / "python"
        assert home.venv_sbxloop == tmp_path / "venv" / "bin" / "sbxloop"
        assert home.uv == tmp_path / "bin" / "uv"
        assert home.sbx_binary == tmp_path / "sbx" / "bin" / "sbx"
        assert home.launcher == tmp_path / "bin" / "sbxloop"

    def test_a_windows_home_uses_scripts_and_exe(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path, os_name="nt")
        assert home.windows
        assert home.venv_bin == tmp_path / "venv" / "Scripts"
        assert home.venv_python == tmp_path / "venv" / "Scripts" / "python.exe"
        assert home.venv_sbxloop == tmp_path / "venv" / "Scripts" / "sbxloop.exe"
        assert home.uv == tmp_path / "bin" / "uv.exe"
        assert home.sbx_binary == tmp_path / "sbx" / "bin" / "sbx.exe"

    def test_the_windows_launcher_is_a_cmd_file(self, tmp_path: Path) -> None:
        """A `#!/bin/sh` file named `bin\\sbxloop` is not something cmd or
        PowerShell can run, so the entry point is named for what it is."""
        assert SbxloopHome(tmp_path, os_name="nt").launcher == tmp_path / "bin" / "sbxloop.cmd"

    def test_nothing_else_in_the_tree_moves(self, tmp_path: Path) -> None:
        posix = SbxloopHome(tmp_path, os_name="posix")
        windows = SbxloopHome(tmp_path, os_name="nt")
        for name in ("config_toml", "secrets_env", "state_db", "runs", "logs", "tmp", "record"):
            assert getattr(posix, name) == getattr(windows, name), name
        assert posix.directories == windows.directories

    def test_a_home_defaults_to_this_process_platform(self, tmp_path: Path) -> None:
        assert SbxloopHome(tmp_path).os_name == os.name

    @pytest.mark.windows_host
    @pytest.mark.slow
    def test_a_real_venv_puts_its_interpreter_where_this_host_says(self, tmp_path: Path) -> None:
        """The claim that decides every interpreter path, checked against a
        venv this interpreter actually built — the one thing a table of
        expected paths cannot settle for the host it is running on."""
        import venv

        home = SbxloopHome(tmp_path / "h")
        venv.create(home.venv, with_pip=False)
        assert home.venv_python.is_file(), sorted(p.name for p in home.venv_bin.iterdir())


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

    def test_a_windows_session_reads_the_home_config_it_runs_out_of(self, tmp_path: Path) -> None:
        """#899: config discovery and home resolution have to agree on a
        host with no Unix ``HOME``, or the operator edits one file and the
        daemon reads another."""
        profile = tmp_path / "Users" / "Ada"
        home = SbxloopHome(profile / ".sbxloop")
        home.config.mkdir(parents=True, exist_ok=True)
        home.config_toml.write_text('model = "from-userprofile"\n')
        project = tmp_path / "proj"
        project.mkdir()
        config, sources = load_config_with_sources(cwd=project, env={"USERPROFILE": str(profile)})
        assert config.home == home.root
        assert config.model == "from-userprofile" and sources["model"] == "home config"

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
