"""Daemon state-dir anchoring (#255): absolute, outside the workspace, and
never moving out from under an already-running deployment."""

from __future__ import annotations

from pathlib import Path

from sbxloop.config import Config, load_config_with_sources
from sbxloop.daemon.paths import resolve_cli_state_dir, resolve_state_dir


def resolve(
    tmp_path: Path, config: Config, sources: dict[str, str], **env: str
) -> tuple[Path, str]:
    cwd = tmp_path / "runner"
    cwd.mkdir(exist_ok=True)
    choice = resolve_state_dir(config, sources, cwd=cwd, env=env, home=tmp_path / "home")
    return choice.path, choice.reason


def test_default_lands_in_xdg_state_home_named_after_runner_dir(tmp_path: Path) -> None:
    path, reason = resolve(tmp_path, Config(), {})
    assert path == (tmp_path / "home" / ".local" / "state" / "sbxloop" / "runner").resolve()
    assert path.is_absolute()
    assert "XDG" in reason


def test_xdg_state_home_env_is_honored(tmp_path: Path) -> None:
    path, _ = resolve(tmp_path, Config(), {}, XDG_STATE_HOME=str(tmp_path / "xdg"))
    assert path == (tmp_path / "xdg" / "sbxloop" / "runner").resolve()


def test_daemon_state_dir_wins_and_expands_home(tmp_path: Path) -> None:
    config = Config.model_validate({"daemon": {"state_dir": str(tmp_path / "custom")}})
    path, reason = resolve(tmp_path, config, {"state_dir": "sbxloop.toml"})
    assert path == (tmp_path / "custom").resolve()
    assert reason == "[daemon] state_dir"
    rel = Config.model_validate({"daemon": {"state_dir": "relstate"}})
    path, _ = resolve(tmp_path, rel, {})
    assert path == (tmp_path / "runner" / "relstate").resolve()


def test_explicit_top_level_state_dir_is_kept_but_made_absolute(tmp_path: Path) -> None:
    config = Config.model_validate({"state_dir": "mystate"})
    path, reason = resolve(tmp_path, config, {"state_dir": "env"})
    assert path == (tmp_path / "runner" / "mystate").resolve()
    assert reason == "state_dir"


def test_legacy_dot_sbxloop_with_state_db_is_kept(tmp_path: Path) -> None:
    """An upgraded deployment keeps its queue/ledger where they are; moving
    the default out from under a live daemon would orphan in-progress
    issues."""
    legacy = tmp_path / "runner" / ".sbxloop"
    legacy.mkdir(parents=True)
    (legacy / "state.db").write_bytes(b"")
    path, reason = resolve(tmp_path, Config(), {})
    assert path == legacy.resolve()
    assert "legacy" in reason


def test_legacy_dir_without_state_db_does_not_count(tmp_path: Path) -> None:
    (tmp_path / "runner" / ".sbxloop" / "inbox").mkdir(parents=True)
    path, _ = resolve(tmp_path, Config(), {})
    assert path.name == "runner" and "sbxloop" in path.parts


def test_sources_from_real_loader_drive_the_choice(tmp_path: Path) -> None:
    cwd = tmp_path / "runner"
    cwd.mkdir()
    (cwd / "sbxloop.toml").write_text('state_dir = "here"\n')
    config, sources = load_config_with_sources(cwd=cwd, env={})
    choice = resolve_state_dir(config, sources, cwd=cwd, env={}, home=tmp_path / "home")
    assert choice.path == (cwd / "here").resolve()
    default_config, default_sources = load_config_with_sources(cwd=tmp_path, env={})
    choice = resolve_state_dir(
        default_config, default_sources, cwd=cwd, env={}, home=tmp_path / "home"
    )
    assert choice.path.parts[-3:] == ("state", "sbxloop", "runner")


def resolve_cli(
    tmp_path: Path, config: Config, sources: dict[str, str], **env: str
) -> tuple[Path, str]:
    cwd = tmp_path / "runner"
    cwd.mkdir(exist_ok=True)
    choice = resolve_cli_state_dir(config, sources, cwd=cwd, env=env, home=tmp_path / "home")
    return choice.path, choice.reason


def daemon_store_at(tmp_path: Path) -> Path:
    """The store a daemon in ``tmp_path/runner`` would have created."""
    path = tmp_path / "home" / ".local" / "state" / "sbxloop" / "runner"
    path.mkdir(parents=True, exist_ok=True)
    (path / "state.db").write_bytes(b"")
    return path.resolve()


class TestCliStateDir:
    """Where `status`, `logs`, `artifacts` and `gc` look.

    The daemon anchors its state away from the top-level default, so these
    commands used to answer about a different world than the daemon keeps —
    with no flag to talk them out of it.
    """

    def test_without_a_daemon_store_the_plain_default_is_kept(self, tmp_path: Path) -> None:
        config = Config.model_validate({"state_dir": str(tmp_path / "home" / ".sbxloop")})
        path, reason = resolve_cli(tmp_path, config, {})
        assert path == (tmp_path / "home" / ".sbxloop").resolve()
        assert reason == "state_dir default"

    def test_a_daemon_store_for_this_directory_wins(self, tmp_path: Path) -> None:
        expected = daemon_store_at(tmp_path)
        config = Config.model_validate({"state_dir": str(tmp_path / "home" / ".sbxloop")})
        path, reason = resolve_cli(tmp_path, config, {})
        assert path == expected
        assert "XDG" in reason

    def test_a_daemon_dir_without_a_store_does_not_count(self, tmp_path: Path) -> None:
        """An empty XDG directory is not a deployment — `sbxloop run` on a
        plain host must keep reporting its own runs."""
        (tmp_path / "home" / ".local" / "state" / "sbxloop" / "runner").mkdir(parents=True)
        config = Config.model_validate({"state_dir": str(tmp_path / "home" / ".sbxloop")})
        path, _ = resolve_cli(tmp_path, config, {})
        assert path == (tmp_path / "home" / ".sbxloop").resolve()

    def test_an_explicit_state_dir_is_obeyed_over_a_daemon_store(self, tmp_path: Path) -> None:
        """Configuration still wins: someone who named a state_dir gets it,
        daemon store or not."""
        daemon_store_at(tmp_path)
        config = Config.model_validate({"state_dir": "mystate"})
        path, reason = resolve_cli(tmp_path, config, {"state_dir": "sbxloop.toml"})
        assert path == (tmp_path / "runner" / "mystate").resolve()
        assert reason == "state_dir"

    def test_daemon_state_dir_setting_is_obeyed(self, tmp_path: Path) -> None:
        daemon_store_at(tmp_path)
        config = Config.model_validate({"daemon": {"state_dir": str(tmp_path / "custom")}})
        path, reason = resolve_cli(tmp_path, config, {})
        assert path == (tmp_path / "custom").resolve()
        assert reason == "[daemon] state_dir"

    def test_legacy_relative_store_is_followed(self, tmp_path: Path) -> None:
        """An upgraded deployment kept its state in ./.sbxloop; the run
        commands must look where that daemon actually writes."""
        legacy = tmp_path / "runner" / ".sbxloop"
        legacy.mkdir(parents=True)
        (legacy / "state.db").write_bytes(b"")
        config = Config.model_validate({"state_dir": str(tmp_path / "home" / ".sbxloop")})
        path, reason = resolve_cli(tmp_path, config, {})
        assert path == legacy.resolve()
        assert "legacy" in reason

    def test_the_daemon_resolution_is_unchanged_by_all_this(self, tmp_path: Path) -> None:
        """The daemon must never be redirected to the CLI default: moving a
        live deployment's state would orphan every in-progress issue."""
        config = Config.model_validate({"state_dir": str(tmp_path / "home" / ".sbxloop")})
        path, reason = resolve(tmp_path, config, {})
        assert path == (tmp_path / "home" / ".local" / "state" / "sbxloop" / "runner").resolve()
        assert "XDG" in reason
