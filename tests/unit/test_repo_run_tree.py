"""A run for a repository never starts in an empty directory.

The field failure: a daemon run for a configured repository whose home
clone had failed (or left an empty directory behind) was provisioned into a
bare per-run directory under the default ``workspace_isolation = "auto"``.
The agent found no repository and built the ask from nothing. A run that
acts on a repository takes its tree from a checkout of it, from its remote,
or fails naming why; only a run with no repository at all starts bare.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from git import Repo

from sbxloop import hostgit
from sbxloop.config import Config
from sbxloop.errors import ProvisionError
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner
from tests.unit.test_daemon_loop import Harness, gh_item


def _upstream(tmp_path: Path, repo: str = "o/r") -> Path:
    """A checkout standing in for the repository's remote."""
    path = tmp_path / "upstream"
    path.mkdir()
    checkout = Repo.init(path)
    (path / "README.md").write_text(f"# {repo}\n")
    checkout.index.add(["README.md"])
    checkout.index.commit("init")
    return path


def _remote_clones(monkeypatch: pytest.MonkeyPatch, upstream: Path) -> list[str]:
    """Route the run's remote clone to ``upstream``; return the URLs asked for."""
    urls: list[str] = []

    def clone(url: str, target: Path, branch: str, **kwargs: Any) -> str:
        urls.append(url)
        return hostgit.clone_for_run(upstream, target, branch)

    monkeypatch.setattr(hostgit, "clone_from_remote", clone)
    return urls


def _no_remote_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("a run with no repository must not clone")

    monkeypatch.setattr(hostgit, "clone_from_remote", never)


def _provisioner(config: Config) -> Provisioner:
    return Provisioner(SbxCLI(), config, env={})


class TestDaemonRunWithoutACheckout:
    @pytest.mark.parametrize("daemon_mode", ["clone", "auto", "in-place"])
    def test_run_clones_from_the_remote(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, daemon_mode: str
    ) -> None:
        """No workspace configured, the operator's `auto`, and no home clone:
        the run's tree is the repository's remote, whatever the daemon's
        isolation knob says (there is no checkout for it to govern)."""
        config = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": {"workspace_isolation": daemon_mode},
            }
        )
        assert config.sandbox.workspace_isolation == "auto"
        urls = _remote_clones(monkeypatch, _upstream(tmp_path))
        run_config = Harness(tmp_path, config).loop._item_config(gh_item())
        workspace, mounted = _provisioner(run_config)._resolve_workspace_source("r1", "o/r")
        assert urls == ["https://github.com/o/r"]
        assert mounted is True
        assert workspace == run_config.paths.run_workspace("r1").resolve()
        assert (workspace / "README.md").read_text() == "# o/r\n"

    def test_failed_remote_clone_fails_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The clone failing is a provisioning error with its reason, not a
        quiet fall back to the empty per-run directory."""
        config = Config.model_validate({"home": str(tmp_path / "state"), "github": {"repo": "o/r"}})

        def boom(url: str, target: Path, branch: str, **kwargs: Any) -> str:
            raise ProvisionError(f"cloning {url} failed: could not resolve host")

        monkeypatch.setattr(hostgit, "clone_from_remote", boom)
        run_config = Harness(tmp_path, config).loop._item_config(gh_item())
        with pytest.raises(ProvisionError, match="could not resolve host"):
            _provisioner(run_config)._resolve_workspace_source("r1", "o/r")

    @pytest.mark.parametrize("leftover", [False, True])
    def test_non_checkout_home_default_is_not_the_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leftover: bool
    ) -> None:
        """The home's `workspaces/<owner>/<name>` exists but is not a git
        checkout (empty, or with stray files): it is not handed to the run,
        which clones from the remote instead and leaves the directory be."""
        config = Config.model_validate({"home": str(tmp_path / "state"), "github": {"repo": "o/r"}})
        default = config.default_workspace_for_repo("o/r")
        assert default is not None
        default.mkdir(parents=True)
        if leftover:
            (default / "stray.txt").write_text("stray\n")
        assert config.workspace_for_repo("o/r") is None
        urls = _remote_clones(monkeypatch, _upstream(tmp_path))
        run_config = Harness(tmp_path, config).loop._item_config(gh_item())
        workspace, mounted = _provisioner(run_config)._resolve_workspace_source("r1", "o/r")
        assert urls == ["https://github.com/o/r"] and mounted is True
        assert workspace != default.resolve()
        assert (workspace / "README.md").is_file()
        assert sorted(p.name for p in default.iterdir()) == (["stray.txt"] if leftover else [])


class TestConfiguredWorkspaceThatIsNotACheckout:
    @staticmethod
    def _config(tmp_path: Path, workspace: Path, *, legacy: bool, mode: str = "auto") -> Config:
        if legacy:
            github: dict[str, Any] = {"repo": "o/r"}
            sandbox: dict[str, Any] = {"workspace": str(workspace)}
        else:
            github = {"repos": [{"repo": "o/r", "workspace": str(workspace)}]}
            sandbox = {}
        return Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": github,
                "sandbox": {**sandbox, "workspace_isolation": mode},
            }
        )

    @pytest.mark.parametrize("legacy", [True, False])
    @pytest.mark.parametrize("shape", ["empty", "files", "missing"])
    def test_auto_refuses_it_for_a_repository_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool, shape: str
    ) -> None:
        plain = tmp_path / "plain"
        if shape != "missing":
            plain.mkdir()
        if shape == "files":
            (plain / "notes.txt").write_text("not a checkout\n")
        _no_remote_clone(monkeypatch)
        provisioner = _provisioner(self._config(tmp_path, plain, legacy=legacy))
        with pytest.raises(ProvisionError) as excinfo:
            provisioner._resolve_workspace_source("r1", "o/r")
        message = str(excinfo.value)
        assert str(plain.resolve()) in message
        assert "is not a git checkout" in message and "o/r" in message
        # the three ways out are named
        assert "git checkout of o/r" in message
        assert "remove the setting" in message
        assert "workspace_isolation = 'in-place'" in message

    @pytest.mark.parametrize("legacy", [True, False])
    def test_in_place_still_runs_in_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        _no_remote_clone(monkeypatch)
        config = self._config(tmp_path, plain, legacy=legacy, mode="in-place")
        workspace, mounted = _provisioner(config)._resolve_workspace_source("r1", "o/r")
        assert (workspace, mounted) == (plain.resolve(), True)


class TestRunsWithoutARepository:
    def test_nothing_configured_is_the_bare_run_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A GitHub-less local run has no repository to clone: harvest mode,
        as before."""
        config = Config.model_validate({"home": str(tmp_path / "state")})
        _no_remote_clone(monkeypatch)
        workspace, mounted = _provisioner(config)._resolve_workspace_source("r1", None)
        assert (workspace, mounted) == (config.paths.run_workspace("r1").resolve(), False)

    def test_plain_directory_without_a_repository_is_used_in_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        config = Config.model_validate(
            {"home": str(tmp_path / "state"), "sandbox": {"workspace": str(plain)}}
        )
        _no_remote_clone(monkeypatch)
        workspace, mounted = _provisioner(config)._resolve_workspace_source("r1", None)
        assert (workspace, mounted) == (plain.resolve(), True)
