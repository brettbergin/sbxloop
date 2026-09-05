"""Submodules in the run workspace (#692): a fresh clone is populated — from
the host checkout when it can be — and only a fresh one; the hosts the
submodules fetch from join the agent sandbox's egress allow list."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from sbxloop import hostgit
from sbxloop.config import Config
from sbxloop.errors import ProvisionError
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner
from tests.conftest import FakeSbx
from tests.fakes.gitserver import PrivateGitServer
from tests.unit.test_hostgit import git, make_repo, make_submodule_setup
from tests.unit.test_provision import TOKENS


@pytest.fixture
def remote(tmp_path: Path):  # type: ignore[no-untyped-def]
    (tmp_path / "remotes").mkdir()
    with PrivateGitServer(tmp_path / "remotes", username="x", token="y", public=True) as srv:
        yield srv


def _config(tmp_path: Path, repos: list[dict[str, object]], **sandbox: object) -> Config:
    return Config.model_validate(
        {
            "state_dir": str(tmp_path / "state"),
            "github": {"repos": repos},
            "sandbox": {"workspace_isolation": "clone", **sandbox},
        }
    )


def _provisioner(fake_sbx: FakeSbx, config: Config) -> Provisioner:
    return Provisioner(SbxCLI(binary=str(fake_sbx.binary)), config, env=TOKENS)


def test_a_fresh_clone_of_the_host_checkout_populates_its_submodules(
    fake_sbx: FakeSbx, tmp_path: Path, remote: PrivateGitServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _, _ = make_submodule_setup(tmp_path, remote.url)
    git("remote", "add", "origin", "https://github.com/o/app.git", cwd=app)
    config = _config(tmp_path, [{"repo": "o/app", "workspace": str(app)}])
    provisioner = _provisioner(fake_sbx, config)
    events: list[object] = []
    provisioner.bus.subscribe(events.append)

    ws = provisioner._resolve_workspace("r1", "o/app")
    assert (ws / "vendor" / "lib" / "lib.txt").read_text() == "v1\n"
    (event,) = [e for e in events if e.type == "sandbox.workspace_submodules"]  # type: ignore[attr-defined]
    assert event.data["submodules"] == [{"path": "vendor/lib", "source": "local"}]  # type: ignore[attr-defined]
    assert "vendor/lib (local)" in event.data["message"]  # type: ignore[attr-defined]

    # A resume re-entering the clone leaves the submodule exactly where the
    # agent left it: no populate at all.
    def never(*args: object, **kwargs: object) -> list[tuple[str, str]]:
        raise AssertionError("populate_submodules must not run on a reused clone")

    monkeypatch.setattr(hostgit, "populate_submodules", never)
    assert provisioner._resolve_workspace("r1", "o/app") == ws


def test_a_remote_clone_populates_from_the_remotes_under_the_runs_token(
    fake_sbx: FakeSbx, tmp_path: Path, remote: PrivateGitServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, _, _ = make_submodule_setup(tmp_path, remote.url)
    legacy = make_repo(tmp_path, "legacy")
    git("remote", "add", "origin", "https://github.com/o/one.git", cwd=legacy)
    config = _config(tmp_path, [{"repo": "o/one", "workspace": str(legacy)}, {"repo": "o/app"}])
    provisioner = _provisioner(fake_sbx, config)

    def fake_clone(url: str, target: Path, branch: str, **kwargs: object) -> str:
        return hostgit.clone_for_run(upstream, target, branch)

    seen: list[tuple[Path | None, str | None]] = []
    real = hostgit.populate_submodules

    def spy(clone: Path, *, source: Path | None, token: str | None) -> list[tuple[str, str]]:
        seen.append((source, token))
        return real(clone, source=source, token=token)

    monkeypatch.setattr(hostgit, "clone_from_remote", fake_clone)
    monkeypatch.setattr(hostgit, "populate_submodules", spy)
    ws = provisioner._resolve_workspace("r1", "o/app")
    # (the second call is populate's own recursion into the submodule)
    assert seen[0] == (None, "github_pat_user")
    assert {token for _, token in seen} == {"github_pat_user"}
    assert (ws / "vendor" / "lib" / "lib.txt").read_text() == "v1\n"


def test_clone_submodules_off_leaves_them_empty(
    fake_sbx: FakeSbx, tmp_path: Path, remote: PrivateGitServer, caplog: pytest.LogCaptureFixture
) -> None:
    app, _, _ = make_submodule_setup(tmp_path, remote.url)
    git("remote", "add", "origin", "https://github.com/o/app.git", cwd=app)
    config = _config(tmp_path, [{"repo": "o/app", "workspace": str(app)}], clone_submodules=False)
    provisioner = _provisioner(fake_sbx, config)
    events: list[object] = []
    provisioner.bus.subscribe(events.append)
    with caplog.at_level(logging.INFO):
        ws = provisioner._resolve_workspace("r1", "o/app")
    assert not (ws / "vendor" / "lib" / "lib.txt").exists()
    assert not [e for e in events if e.type == "sandbox.workspace_submodules"]  # type: ignore[attr-defined]
    assert any("workspace.submodules_skipped" in r.getMessage() for r in caplog.records)


def test_an_unpopulatable_submodule_fails_the_run_by_name(
    fake_sbx: FakeSbx, tmp_path: Path, remote: PrivateGitServer
) -> None:
    app, _, lib_bare = make_submodule_setup(tmp_path, remote.url)
    git("remote", "add", "origin", "https://github.com/o/app.git", cwd=app)
    # the host's copy is gone and so is the remote: nowhere to get it from
    shutil.rmtree(app / ".git" / "modules")
    shutil.rmtree(app / "vendor" / "lib")
    (app / "vendor" / "lib").mkdir()
    shutil.rmtree(lib_bare)
    config = _config(tmp_path, [{"repo": "o/app", "workspace": str(app)}])
    provisioner = _provisioner(fake_sbx, config)
    with pytest.raises(ProvisionError, match="vendor/lib"):
        provisioner._resolve_workspace("r1", "o/app")


class TestSubmoduleHostsInEgress:
    def _workspace(self, tmp_path: Path, *urls: str) -> Path:
        ws = make_repo(tmp_path, "ws")
        (ws / ".gitmodules").write_text(
            "".join(
                f'[submodule "s{i}"]\n\tpath = s{i}\n\turl = {url}\n' for i, url in enumerate(urls)
            )
        )
        return ws

    def test_foreign_hosts_are_allowed_and_announced(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        ws = self._workspace(
            tmp_path, "https://gitlab.example.com/o/a.git", "git@github.com:o/b.git"
        )
        config = _config(tmp_path, [{"repo": "o/app"}])
        provisioner = _provisioner(fake_sbx, config)
        events: list[object] = []
        provisioner.bus.subscribe(events.append)
        agent, _ = provisioner.build_specs("r1", ws, "o/app")
        assert "gitlab.example.com" in agent.policy_allows
        assert agent.policy_allows.count("github.com") == 1
        (event,) = [e for e in events if e.type == "sandbox.submodule_hosts"]  # type: ignore[attr-defined]
        assert event.data["hosts"] == ["gitlab.example.com"]  # type: ignore[attr-defined]

    def test_hosts_already_reachable_add_nothing_and_say_nothing(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        ws = self._workspace(tmp_path, "https://github.com/o/b.git")
        config = _config(tmp_path, [{"repo": "o/app"}])
        provisioner = _provisioner(fake_sbx, config)
        events: list[object] = []
        provisioner.bus.subscribe(events.append)
        agent, _ = provisioner.build_specs("r1", ws, "o/app")
        assert agent.policy_allows.count("github.com") == 1
        assert not [e for e in events if e.type == "sandbox.submodule_hosts"]  # type: ignore[attr-defined]

    def test_a_workspace_without_submodules_is_unchanged(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        config = _config(tmp_path, [{"repo": "o/app"}])
        provisioner = _provisioner(fake_sbx, config)
        plain, _ = provisioner.build_specs("r1", make_repo(tmp_path, "plain"), "o/app")
        bare, _ = provisioner.build_specs("r2", tmp_path / "nothing", "o/app")
        assert plain.policy_allows == bare.policy_allows
