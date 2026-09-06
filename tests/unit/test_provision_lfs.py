"""Git LFS in the run workspace (#693): a fresh clone's pointer files are
populated — from the host checkout's store when it can be, the repository's
LFS endpoint under the run's token otherwise — and only a fresh one; the
``git-lfs`` workspace tool and its hosts ride along into the agent sandbox."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from sbxloop import hostgit, toolchains
from sbxloop.config import Config
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner
from tests.conftest import FakeSbx
from tests.unit.test_hostgit import git, is_pointer, make_lfs_repo, make_repo
from tests.unit.test_provision import TOKENS

pytestmark = pytest.mark.skipif(
    hostgit.lfs_version() is None, reason="git-lfs is not installed on this host"
)


def _config(tmp_path: Path, repos: list[dict[str, object]], **sandbox: object) -> Config:
    return Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "github": {"repos": repos},
            "sandbox": {"workspace_isolation": "clone", **sandbox},
        }
    )


def _provisioner(fake_sbx: FakeSbx, config: Config) -> Provisioner:
    return Provisioner(SbxCLI(binary=str(fake_sbx.binary)), config, env=TOKENS)


def test_a_fresh_clone_of_the_host_checkout_populates_from_its_store(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, payload = make_lfs_repo(tmp_path)
    git("remote", "add", "origin", "https://github.com/o/app.git", cwd=app)
    config = _config(tmp_path, [{"repo": "o/app", "workspace": str(app)}])
    provisioner = _provisioner(fake_sbx, config)
    events: list[object] = []
    provisioner.bus.subscribe(events.append)

    ws = provisioner._resolve_workspace("r1", "o/app")
    assert (ws / "asset.bin").read_bytes() == payload
    (event,) = [e for e in events if e.type == "sandbox.workspace_lfs"]  # type: ignore[attr-defined]
    assert event.data["attributes"] == [".gitattributes"]  # type: ignore[attr-defined]
    assert (event.data["files"], event.data["linked"], event.data["fetched"]) == (1, 1, 0)  # type: ignore[attr-defined]
    assert "1 object(s) from the host checkout" in event.data["message"]  # type: ignore[attr-defined]

    # A resume re-entering the clone leaves the files where the agent left
    # them: no populate at all.
    def never(*args: object, **kwargs: object) -> hostgit.LfsPopulation:
        raise AssertionError("populate_lfs must not run on a reused clone")

    monkeypatch.setattr(hostgit, "populate_lfs", never)
    assert provisioner._resolve_workspace("r1", "o/app") == ws


def test_a_remote_clone_fetches_from_the_repositorys_endpoint_under_the_runs_token(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, _ = make_lfs_repo(tmp_path, "upstream")
    legacy = make_repo(tmp_path, "legacy")
    git("remote", "add", "origin", "https://github.com/o/one.git", cwd=legacy)
    config = _config(tmp_path, [{"repo": "o/one", "workspace": str(legacy)}, {"repo": "o/app"}])
    provisioner = _provisioner(fake_sbx, config)

    def fake_clone(url: str, target: Path, branch: str, **kwargs: object) -> str:
        return hostgit.clone_for_run(upstream, target, branch)

    seen: list[tuple[Path | None, str | None, str | None]] = []

    def spy(
        clone: Path, *, source: Path | None, lfs_url: str | None, token: str | None
    ) -> hostgit.LfsPopulation:
        seen.append((source, lfs_url, token))
        return hostgit.LfsPopulation(1, 0, 1)

    monkeypatch.setattr(hostgit, "clone_from_remote", fake_clone)
    monkeypatch.setattr(hostgit, "populate_lfs", spy)
    events: list[object] = []
    provisioner.bus.subscribe(events.append)
    provisioner._resolve_workspace("r1", "o/app")
    assert seen == [(None, "https://github.com/o/app.git/info/lfs", "github_pat_user")]
    (event,) = [e for e in events if e.type == "sandbox.workspace_lfs"]  # type: ignore[attr-defined]
    assert "1 fetched from https://github.com/o/app.git/info/lfs" in event.data["message"]  # type: ignore[attr-defined]


def test_clone_lfs_off_leaves_the_pointer_files(
    fake_sbx: FakeSbx, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app, _ = make_lfs_repo(tmp_path)
    git("remote", "add", "origin", "https://github.com/o/app.git", cwd=app)
    config = _config(tmp_path, [{"repo": "o/app", "workspace": str(app)}], clone_lfs=False)
    provisioner = _provisioner(fake_sbx, config)
    events: list[object] = []
    provisioner.bus.subscribe(events.append)
    with caplog.at_level(logging.INFO):
        ws = provisioner._resolve_workspace("r1", "o/app")
    assert is_pointer(ws / "asset.bin")
    assert not [e for e in events if e.type == "sandbox.workspace_lfs"]  # type: ignore[attr-defined]
    assert any("workspace.lfs_skipped" in r.getMessage() for r in caplog.records)


def test_a_workspace_without_lfs_never_asks_for_git_lfs(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_repo(tmp_path, "app")
    git("remote", "add", "origin", "https://github.com/o/app.git", cwd=app)
    config = _config(tmp_path, [{"repo": "o/app", "workspace": str(app)}])
    provisioner = _provisioner(fake_sbx, config)

    def never(*args: object, **kwargs: object) -> hostgit.LfsPopulation:
        raise AssertionError("populate_lfs must not run without a filter=lfs attribute")

    monkeypatch.setattr(hostgit, "populate_lfs", never)
    provisioner._resolve_workspace("r1", "o/app")


class TestLfsInTheSandbox:
    def test_git_lfs_and_its_hosts_ride_along(self, fake_sbx: FakeSbx, tmp_path: Path) -> None:
        ws = make_repo(tmp_path, "ws")
        (ws / ".gitattributes").write_text("*.png filter=lfs diff=lfs merge=lfs -text\n")
        config = _config(tmp_path, [{"repo": "o/app"}])
        provisioner = _provisioner(fake_sbx, config)
        resolved = provisioner.resolve_languages(ws)
        assert resolved.languages == (*toolchains.DEFAULT_LANGUAGES, "git-lfs")
        agent, _ = provisioner.build_specs("r1", ws, "o/app", languages=resolved.languages)
        for host in toolchains.GIT_LFS.install_domains:
            assert host in agent.policy_allows

    def test_a_workspace_without_lfs_opens_no_lfs_host(
        self, fake_sbx: FakeSbx, tmp_path: Path
    ) -> None:
        ws = make_repo(tmp_path, "ws")
        config = _config(tmp_path, [{"repo": "o/app"}])
        provisioner = _provisioner(fake_sbx, config)
        resolved = provisioner.resolve_languages(ws)
        assert "git-lfs" not in resolved.languages
        agent, _ = provisioner.build_specs("r1", ws, "o/app", languages=resolved.languages)
        assert "lfs.github.com" not in agent.policy_allows
