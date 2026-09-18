"""The home's own workspaces: ``workspaces/<owner>/<name>``, cloned by the
daemon on first use when the operator pointed it at no checkout."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner
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
    def test_default_counts_only_once_it_is_a_checkout(self, tmp_path: Path) -> None:
        home = SbxloopHome(tmp_path / "h")
        config = Config.model_validate({"home": str(home.root), "github": {"repo": "Acme/Widget"}})
        default = config.default_workspace_for_repo("Acme/Widget")
        assert default == home.workspaces / "Acme" / "Widget"
        assert config.workspace_for_repo("Acme/Widget") is None
        assert config.workspace_source("Acme/Widget") == "none"
        # An existing directory is not yet the repository: a clone that never
        # finished leaves one behind, and a run handed it starts from nothing.
        default.mkdir(parents=True)
        assert config.workspace_for_repo("Acme/Widget") is None
        (default / "leftover.txt").write_text("not a checkout\n")
        assert config.workspace_for_repo("Acme/Widget") is None
        Repo.init(default)
        assert config.workspace_for_repo("Acme/Widget") == default
        assert config.workspace_source("Acme/Widget") == "configured"

    def test_a_subdirectory_of_another_checkout_is_not_the_default(self, tmp_path: Path) -> None:
        """A home that happens to live inside some git checkout: the default
        directory is inside a work tree, but it is not the repository's own."""
        Repo.init(tmp_path)
        home = SbxloopHome(tmp_path / "h")
        config = Config.model_validate({"home": str(home.root), "github": {"repo": "o/n"}})
        default = config.default_workspace_for_repo("o/n")
        assert default is not None
        default.mkdir(parents=True)
        assert config.workspace_for_repo("o/n") is None

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
    @pytest.mark.parametrize(
        "repo,origin,token",
        [
            ("o/n", "http://forge.example:8929", "gitlab-token"),
            ("other/project", "https://github.com", "github-token"),
        ],
    )
    def test_refresh_uses_the_selected_repositories_forge_credential(
        self, tmp_path, monkeypatch, repo, origin, token
    ):
        loop, home = self.make_loop(tmp_path)
        loop.config = Config.model_validate(
            {
                "home": str(home.root),
                "vcs": {"kind": "gitlab", "api_url": "http://forge.example:8929/api/v4"},
                "github": {"repos": [{"repo": "other/project", "kind": "github"}]},
            }
        )
        loop.github = SimpleNamespace(
            provisioner=Provisioner(
                SbxCLI(),
                loop.config,
                env={"GITLAB_TOKEN": "gitlab-token", "GH_TOKEN": "github-token"},
            )
        )
        checkout = home.workspaces / repo
        monkeypatch.setattr(loop, "_ensure_workspace", lambda repo: None)
        monkeypatch.setattr(loop, "_workspace_checkout", lambda repo: checkout)
        monkeypatch.setattr(hostgit, "origin_matches_repo", lambda *a: True)
        calls = []

        def refresh(path, **kwargs):
            calls.append((path, kwargs))
            return hostgit.RefreshResult(False, "head", "head", "up to date")

        monkeypatch.setattr(hostgit, "refresh_from_origin", refresh)
        loop._refresh_workspace(repo)
        assert calls == [(checkout, {"token": token, "credential_url": origin})]

    def test_a_repoless_item_refreshes_the_sole_repository_with_its_credential(
        self, tmp_path, monkeypatch
    ):
        """Field failure: a chat workload on a single-repo GitLab daemon
        resolved the checkout from ``None`` but the token from ``None`` too,
        so `git fetch` ran anonymously against the private project and
        every ask posted "workspace refresh failed" to the channel."""
        loop, home = self.make_loop(tmp_path)
        loop.config = Config.model_validate(
            {
                "home": str(home.root),
                "vcs": {"kind": "gitlab", "api_url": "http://forge.example:8929/api/v4"},
                "github": {"repo": "o/n"},
            }
        )
        loop.github = SimpleNamespace(
            provisioner=Provisioner(SbxCLI(), loop.config, env={"GITLAB_TOKEN": "gitlab-token"})
        )
        checkout = home.workspaces / "o" / "n"
        ensured = []
        monkeypatch.setattr(loop, "_ensure_workspace", lambda repo: ensured.append(repo))
        monkeypatch.setattr(loop, "_workspace_checkout", lambda repo: checkout)
        monkeypatch.setattr(hostgit, "origin_matches_repo", lambda *a: True)
        calls = []

        def refresh(path, **kwargs):
            calls.append((path, kwargs))
            return hostgit.RefreshResult(False, "head", "head", "up to date")

        monkeypatch.setattr(hostgit, "refresh_from_origin", refresh)
        loop._refresh_workspace(None)
        # The first-use clone is not started for a repo-less item: the
        # daemon's own issue runs are what populate the home's checkout.
        assert ensured == [None]
        assert calls == [
            (checkout, {"token": "gitlab-token", "credential_url": "http://forge.example:8929"})
        ]

    def test_a_repoless_item_on_a_multi_repo_daemon_refreshes_nothing(self, tmp_path, monkeypatch):
        """With several repositories there is no sole checkout to stand in,
        so nothing is fetched, and no credential is sent anywhere."""
        loop, home = self.make_loop(tmp_path)
        loop.config = Config.model_validate(
            {
                "home": str(home.root),
                "vcs": {"kind": "gitlab", "api_url": "http://forge.example:8929/api/v4"},
                "github": {"repos": [{"repo": "o/a"}, {"repo": "o/b"}]},
            }
        )
        loop.github = SimpleNamespace(
            provisioner=Provisioner(SbxCLI(), loop.config, env={"GITLAB_TOKEN": "gitlab-token"})
        )
        calls = []
        monkeypatch.setattr(hostgit, "refresh_from_origin", lambda *a, **k: calls.append(a))
        loop._refresh_workspace(None)
        assert calls == []

    def test_a_repoless_item_leaves_the_primary_checkout_alone_on_a_multi_repo_daemon(
        self, tmp_path, monkeypatch
    ):
        """Field failure: a GitLab daemon with two repositories, the first
        one's home checkout on disk. A chat ask or a scheduled workload names
        no repository, so no token was selected, yet the checkout fell back
        to the primary repository's and `git fetch` ran anonymously: "could
        not read Username ... terminal prompts disabled", posted as a
        warning to the run. With no repository to act on there is nothing
        to refresh."""
        loop, home = self.make_loop(tmp_path)
        loop.config = Config.model_validate(
            {
                "home": str(home.root),
                "vcs": {"kind": "gitlab", "api_url": "http://forge.example:8929/api/v4"},
                "github": {"repos": [{"repo": "o/a"}, {"repo": "o/b"}]},
            }
        )
        loop.github = SimpleNamespace(
            provisioner=Provisioner(SbxCLI(), loop.config, env={"GITLAB_TOKEN": "gitlab-token"})
        )
        checkout = home.workspaces / "o" / "a"
        checkout.mkdir(parents=True)
        Repo.init(checkout).create_remote("origin", "http://forge.example:8929/o/a")
        assert loop.config.workspace_for_repo("o/a") == checkout
        calls = []
        notices: list[str] = []

        def refresh(*a, **k):
            calls.append((a, k))
            return hostgit.RefreshResult(False, "head", "head", "up to date")

        monkeypatch.setattr(hostgit, "refresh_from_origin", refresh)
        monkeypatch.setattr(loop, "_notice", lambda kind, text, **kw: notices.append(kind))
        loop._refresh_workspace(None)
        assert calls == []
        assert notices == []

    @pytest.mark.parametrize("empty_directory", [False, True])
    @pytest.mark.parametrize("repo", ["o/n", "group/subgroup/project"])
    def test_gitlab_bootstrap_uses_its_configured_origin(
        self, tmp_path, monkeypatch, empty_directory, repo
    ):
        loop, home = self.make_loop(tmp_path)
        loop.config = Config.model_validate(
            {
                "home": str(home.root),
                "vcs": {"kind": "gitlab", "api_url": "http://forge.example:8929/api/v4"},
                "github": {"repo": repo},
            }
        )
        calls = []
        if empty_directory:
            (home.workspaces / repo).mkdir(parents=True)
        monkeypatch.setattr(hostgit, "find_git", lambda: "git")

        def clone(url, target, **kwargs):
            calls.append((url, target))
            return "a" * 40

        monkeypatch.setattr(hostgit, "clone_workspace", clone)
        loop._ensure_workspace(repo)
        assert calls == [(f"http://forge.example:8929/{repo}", home.workspaces / repo)]

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

    def test_a_leftover_directory_is_never_cloned_over_or_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The home default exists with files in it but is not a checkout: it
        no longer passes for the repository's workspace, and the clone that
        would replace it must not run (a failed clone removes its target)."""
        loop, home = self.make_loop(tmp_path)
        target = home.workspaces / "o" / "n"
        target.mkdir(parents=True)
        (target / "notes.txt").write_text("keep me\n")
        monkeypatch.setattr(
            hostgit, "clone_workspace", lambda *a, **k: pytest.fail("must not clone")
        )
        notices: list[tuple[str, str]] = []
        monkeypatch.setattr(loop, "_notice", lambda kind, text, **kw: notices.append((kind, text)))
        loop._ensure_workspace("o/n")
        assert (target / "notes.txt").read_text() == "keep me\n"
        assert [kind for kind, _ in notices] == ["workspace.refresh_failed"]
        assert "is not a git checkout of o/n" in notices[0][1]

    def test_home_clone_already_present_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop, home = self.make_loop(tmp_path)
        Repo.init(home.workspaces / "o" / "n", mkdir=True)
        monkeypatch.setattr(
            hostgit, "clone_workspace", lambda *a, **k: pytest.fail("must not clone")
        )
        loop._ensure_workspace("o/n")

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
        Repo.init(home.workspaces / "o" / "c", mkdir=True)
        rows = {c.name: c for c in workspace_checks(config)}
        assert rows["workspace o/a"].ok and "operator's" in rows["workspace o/a"].detail
        assert rows["workspace o/b"].ok and "clones it into" in rows["workspace o/b"].detail
        assert str(home.workspaces / "o" / "b") in rows["workspace o/b"].detail
        assert rows["workspace o/c"].ok and "the home's" in rows["workspace o/c"].detail
        assert all(not c.hard for c in rows.values())
