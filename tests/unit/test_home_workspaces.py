"""The home's own workspaces: ``workspaces/<owner>/<name>``, cloned by the
daemon on first use when the operator pointed it at no checkout."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from git import Repo

from sbxloop import hostgit
from sbxloop.cli.doctor import workspace_checks
from sbxloop.config import Config
from sbxloop.daemon.loop import DaemonLoop
from sbxloop.daemon.store import DaemonStore
from sbxloop.engine.store import StateStore
from sbxloop.errors import ProvisionError
from sbxloop.paths import SbxloopHome
from tests.unit.test_daemon_loop import FakeSource


def bare_remote(tmp_path: Path, name: str = "origin.git") -> Path:
    """A local remote with one commit on `main`."""
    seed = tmp_path / "seed"
    seed.mkdir()
    repo = Repo.init(seed, initial_branch="main")
    (seed / "README").write_text("hello\n")
    repo.index.add(["README"])
    repo.index.commit("init")
    bare = tmp_path / name
    Repo.init(bare, bare=True, initial_branch="main")
    repo.create_remote("origin", str(bare)).push("main:main")
    return bare


class TestCloneWorkspace:
    def test_clones_the_default_branch_tracking_origin(self, tmp_path: Path) -> None:
        remote = bare_remote(tmp_path)
        target = tmp_path / "ws" / "o" / "n"
        sha = hostgit.clone_workspace(str(remote), target)
        clone = Repo(target)
        assert clone.head.commit.hexsha == sha
        assert clone.active_branch.name == "main"
        assert clone.active_branch.tracking_branch() is not None
        assert (target / "README").read_text() == "hello\n"
        # and it refreshes the way the daemon does before each run
        result = hostgit.refresh_from_origin(target)
        assert result.advanced is False

    def test_failure_leaves_nothing_behind(self, tmp_path: Path) -> None:
        target = tmp_path / "ws" / "o" / "n"
        with pytest.raises(ProvisionError, match="cloning"):
            hostgit.clone_workspace(str(tmp_path / "nowhere.git"), target)
        assert not target.exists()


class TestConfigDefault:
    def test_default_counts_only_once_it_exists(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / "h")
        config = Config.model_validate({"home": str(home.root), "github": {"repo": "Acme/Widget"}})
        default = config.default_workspace_for_repo("Acme/Widget")
        assert default == home.workspaces / "Acme" / "Widget"
        assert config.workspace_for_repo("Acme/Widget") is None
        assert config.workspace_source("Acme/Widget") == "none"
        default.mkdir(parents=True)
        assert config.workspace_for_repo("Acme/Widget") == default
        assert config.workspace_source("Acme/Widget") == "configured"

    def test_operators_checkout_wins(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / "h")
        mine = tmp_path / "mine"
        mine.mkdir()
        (home.workspaces / "o" / "n").mkdir(parents=True)
        config = Config.model_validate(
            {"home": str(home.root), "github": {"repos": [{"repo": "o/n", "workspace": str(mine)}]}}
        )
        assert config.workspace_for_repo("o/n") == mine

    def test_no_repository_means_no_default(self, tmp_path: Path) -> None:
        config = Config.model_validate({"home": str(tmp_path / "h")})
        assert config.default_workspace_for_repo(None) is None
        assert config.workspace_for_repo(None) is None


class TestDaemonClonesOnFirstUse:
    def make_loop(self, tmp_path: Path, repo: str = "o/n") -> tuple[DaemonLoop, SbxloopHome]:
        home = SbxloopHome(tmp_path / "h")
        home.ensure_tree()
        config = Config.model_validate({"home": str(home.root), "github": {"repo": repo}})
        loop = DaemonLoop(
            config,
            store=StateStore(home.state_db),
            dstore=DaemonStore(home.state_db),
            source=FakeSource([]),
        )
        return loop, home

    def test_missing_workspace_is_cloned_then_refreshed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop, home = self.make_loop(tmp_path)
        remote = bare_remote(tmp_path)
        calls: list[dict[str, Any]] = []
        real_clone = hostgit.clone_workspace

        def fake_clone(url: str, target: Path, **kw: Any) -> str:
            calls.append({"url": url, "target": target, **kw})
            return real_clone(str(remote), target)

        monkeypatch.setattr(hostgit, "clone_workspace", fake_clone)
        notices: list[tuple[str, str]] = []
        monkeypatch.setattr(loop, "_notice", lambda kind, text, **kw: notices.append((kind, text)))
        loop._refresh_workspace("o/n")
        assert calls and calls[0]["url"] == "https://github.com/o/n"
        assert calls[0]["target"] == home.workspaces / "o" / "n"
        assert calls[0]["token"] is None  # no github box: nothing to mint from
        assert (home.workspaces / "o" / "n" / "README").exists()
        assert notices[0][0] == "workspace.cloned" and "o/n" in notices[0][1]
        # the second run finds it and only refreshes
        loop._refresh_workspace("o/n")
        assert len(calls) == 1

    def test_clone_failure_is_a_warning_not_a_failed_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop, home = self.make_loop(tmp_path)

        def failing(url: str, target: Path, **kw: Any) -> str:
            raise ProvisionError("no network")

        monkeypatch.setattr(hostgit, "clone_workspace", failing)
        notices: list[tuple[str, str]] = []
        monkeypatch.setattr(loop, "_notice", lambda kind, text, **kw: notices.append((kind, text)))
        loop._refresh_workspace("o/n")
        assert notices == [
            (
                "workspace.refresh_failed",
                "⚠ could not clone o/n into "
                f"{home.workspaces / 'o' / 'n'}; runs will clone from the remote: no network",
            )
        ]
        assert not (home.workspaces / "o" / "n").exists()

    def test_operators_checkout_is_never_cloned_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = SbxloopHome(tmp_path / "h")
        home.ensure_tree()
        mine = tmp_path / "mine"
        Repo.init(mine)
        config = Config.model_validate(
            {"home": str(home.root), "github": {"repos": [{"repo": "o/n", "workspace": str(mine)}]}}
        )
        loop = DaemonLoop(
            config,
            store=StateStore(home.state_db),
            dstore=DaemonStore(home.state_db),
            source=FakeSource([]),
        )
        monkeypatch.setattr(
            hostgit, "clone_workspace", lambda *a, **k: pytest.fail("must not clone")
        )
        loop._ensure_workspace("o/n")
        assert not (home.workspaces / "o" / "n").exists()


class TestDoctor:
    def test_rows_say_where_each_repository_works(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / "h")
        mine = tmp_path / "mine"
        mine.mkdir()
        config = Config.model_validate(
            {
                "home": str(home.root),
                "github": {
                    "repos": [
                        {"repo": "o/a", "workspace": str(mine)},
                        {"repo": "o/b"},
                        {"repo": "o/c"},
                    ]
                },
            }
        )
        (home.workspaces / "o" / "c").mkdir(parents=True)
        rows = {c.name: c for c in workspace_checks(config)}
        assert rows["workspace o/a"].ok and "operator's" in rows["workspace o/a"].detail
        assert rows["workspace o/b"].ok and "clones it into" in rows["workspace o/b"].detail
        assert str(home.workspaces / "o" / "b") in rows["workspace o/b"].detail
        assert rows["workspace o/c"].ok and "the home's" in rows["workspace o/c"].detail
        assert all(not c.hard for c in rows.values())
