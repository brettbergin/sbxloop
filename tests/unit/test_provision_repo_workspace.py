"""Provisioner clone-source resolution is per repository (#526).

The field failure: with one daemon-wide ``[sandbox] workspace`` and two
configured repos, every run cloned that one checkout — so a run for repo B
was built from repo A's tree. The Provisioner must resolve the source from
the run's repo, refuse a checkout whose ``origin`` names another repo, and
fail explicitly (never fall back) when a repo has no workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo

from sbxloop.config import Config
from sbxloop.errors import ProvisionError
from sbxloop.sbx.provision import Provisioner
from tests.conftest import FakeSbx
from tests.unit.test_provision import TOKENS


def _checkout(path: Path, origin: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(path)
    (path / "README.md").write_text(f"# {origin}\n")
    repo.index.add(["README.md"])
    repo.index.commit("init")
    repo.create_remote("origin", f"https://github.com/{origin}.git")
    return path


def _provisioner(fake_sbx: FakeSbx, config: Config) -> Provisioner:
    from sbxloop.sbx.cli import SbxCLI

    return Provisioner(SbxCLI(binary=str(fake_sbx.binary)), config, env=TOKENS)


def _config(tmp_path: Path, repos: list[dict[str, object]], **sandbox: object) -> Config:
    return Config.model_validate(
        {
            "state_dir": str(tmp_path / "state"),
            "github": {"repos": repos},
            "sandbox": {"workspace_isolation": "clone", **sandbox},
        }
    )


def test_clone_source_is_the_runs_own_repo(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    one = _checkout(tmp_path / "one", "o/one")
    two = _checkout(tmp_path / "two", "o/two")
    config = _config(
        tmp_path,
        [
            {"repo": "o/one", "workspace": str(one)},
            {"repo": "o/two", "workspace": str(two)},
        ],
    )
    provisioner = _provisioner(fake_sbx, config)
    ws = provisioner._resolve_workspace("r1", "o/two")
    assert (ws / "README.md").read_text() == "# o/two\n"


def test_origin_mismatch_is_refused(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    """Even if resolution somehow hands back the wrong tree, the clone is
    refused — belt and braces behind the preflight."""
    one = _checkout(tmp_path / "one", "o/one")
    config = _config(tmp_path, [{"repo": "o/two", "workspace": str(one)}])
    provisioner = _provisioner(fake_sbx, config)
    with pytest.raises(ProvisionError) as excinfo:
        provisioner._resolve_workspace("r1", "o/two")
    message = str(excinfo.value)
    assert "o/one" in message and "o/two" in message


def test_legacy_workspace_not_used_for_another_repo(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact field shape: legacy [sandbox] workspace = a sbxloop
    checkout, two enabled repos, a run for the other one."""
    legacy = _checkout(tmp_path / "sbxloop", "o/one")
    config = _config(
        tmp_path,
        [{"repo": "o/one"}, {"repo": "o/two"}],
        workspace=str(legacy),
    )
    provisioner = _provisioner(fake_sbx, config)

    from sbxloop import hostgit

    def boom(url: str, target: Path, branch: str, *, existing: bool = False) -> str:
        raise ProvisionError(f"cloning {url} failed: repository not found")

    monkeypatch.setattr(hostgit, "clone_from_remote", boom)
    with pytest.raises(ProvisionError) as excinfo:
        provisioner._resolve_workspace("r1", "o/two")
    # The legacy sbxloop checkout is never the source; the run fails instead.
    assert "o/two" in str(excinfo.value)
    assert str(legacy) not in str(excinfo.value)
    # ...and nothing was cloned into the run dir.
    assert not (config.state_dir / "runs" / "r1" / "workspace" / ".git").exists()


def test_no_workspace_no_credential_fails_explicitly(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _checkout(tmp_path / "sbxloop", "o/one")
    config = _config(
        tmp_path,
        [{"repo": "o/one", "workspace": str(legacy)}, {"repo": "o/private"}],
    )
    provisioner = _provisioner(fake_sbx, config)

    from sbxloop import hostgit

    def boom(url: str, target: Path, branch: str, *, existing: bool = False) -> str:
        raise ProvisionError(f"cloning {url} failed: Authentication failed")

    monkeypatch.setattr(hostgit, "clone_from_remote", boom)
    with pytest.raises(ProvisionError) as excinfo:
        provisioner._resolve_workspace("r1", "o/private")
    message = str(excinfo.value)
    assert "no workspace is configured for o/private" in message
    assert "credential" in message


def test_no_workspace_public_repo_clones_from_remote(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _checkout(tmp_path / "sbxloop", "o/one")
    upstream = _checkout(tmp_path / "upstream", "o/public")
    config = _config(
        tmp_path,
        [{"repo": "o/one", "workspace": str(legacy)}, {"repo": "o/public"}],
    )
    provisioner = _provisioner(fake_sbx, config)

    from sbxloop import hostgit

    seen: dict[str, str] = {}

    def fake_clone(url: str, target: Path, branch: str, *, existing: bool = False) -> str:
        seen["url"] = url
        return hostgit.clone_for_run(upstream, target, branch)

    monkeypatch.setattr(hostgit, "clone_from_remote", fake_clone)
    ws = provisioner._resolve_workspace("r1", "o/public")
    assert seen["url"] == "https://github.com/o/public"
    assert (ws / "README.md").read_text() == "# o/public\n"


def test_single_repo_legacy_workspace_still_clones(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    legacy = _checkout(tmp_path / "tree", "o/one")
    config = _config(tmp_path, [{"repo": "o/one"}], workspace=str(legacy))
    provisioner = _provisioner(fake_sbx, config)
    ws = provisioner._resolve_workspace("r1", "o/one")
    assert ws == (config.state_dir / "runs" / "r1" / "workspace").resolve()
    assert (ws / "README.md").read_text() == "# o/one\n"


def _checkout_with_url(path: Path, url: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(path)
    (path / "README.md").write_text(f"# {url}\n")
    repo.index.add(["README.md"])
    repo.index.commit("init")
    if url:
        repo.create_remote("origin", url)
    return path


@pytest.mark.parametrize(
    "url",
    [
        "https://gitea.example.com/team/group/proj.git",  # nested path
        "sbxloop-host:tree",  # ssh alias
        "/srv/mirrors/o-one",  # local path remote
        "",  # no origin remote at all
    ],
)
def test_unresolvable_origin_fails_closed_when_multi_repo(
    fake_sbx: FakeSbx, tmp_path: Path, url: str
) -> None:
    """An origin that cannot be shown to belong to this repo is not evidence
    that it does: with several repos configured the clone is refused (#526)."""
    tree = _checkout_with_url(tmp_path / "tree", url)
    config = _config(
        tmp_path,
        [{"repo": "o/a"}, {"repo": "o/b", "workspace": str(tree)}],
    )
    provisioner = _provisioner(fake_sbx, config)
    with pytest.raises(ProvisionError) as excinfo:
        provisioner._resolve_workspace("r1", "o/b")
    assert "o/b" in str(excinfo.value)


def test_unresolvable_origin_allowed_for_single_repo(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    """Single-repo deployments are unchanged: there is no other tree to
    confuse the run with, so an unrecognisable origin still works."""
    tree = _checkout_with_url(tmp_path / "tree", "sbxloop-host:tree")
    config = _config(tmp_path, [{"repo": "o/a", "workspace": str(tree)}])
    provisioner = _provisioner(fake_sbx, config)
    ws = provisioner._resolve_workspace("r1", "o/a")
    assert (ws / "README.md").exists()


def test_narrowed_config_still_refuses_other_repos_tree(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Provisioner holds the run's *narrowed* config; the multi-repo
    guards must survive narrowing (#526)."""
    legacy = _checkout(tmp_path / "a-tree", "o/a")
    config = _config(tmp_path, [{"repo": "o/a"}, {"repo": "o/b"}], workspace=str(legacy))
    narrowed = config.model_copy(
        update={"github": config.github.for_repo("o/b", workspace=config.workspace_for_repo("o/b"))}
    )
    assert narrowed.workspace_for_repo("o/b") is None

    from sbxloop import hostgit

    def boom(url: str, target: Path, branch: str, *, existing: bool = False) -> str:
        raise ProvisionError(f"cloning {url} failed: repository not found")

    monkeypatch.setattr(hostgit, "clone_from_remote", boom)
    provisioner = _provisioner(fake_sbx, narrowed)
    with pytest.raises(ProvisionError) as excinfo:
        provisioner._resolve_workspace("r1", "o/b")
    assert str(legacy) not in str(excinfo.value)
