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
from sbxloop.sbx.cli import SbxCLI
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
            "home": str(tmp_path / "state"),
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

    def boom(url: str, target: Path, branch: str, **kwargs: object) -> str:
        raise ProvisionError(f"cloning {url} failed: repository not found")

    monkeypatch.setattr(hostgit, "clone_from_remote", boom)
    with pytest.raises(ProvisionError) as excinfo:
        provisioner._resolve_workspace("r1", "o/two")
    # The legacy sbxloop checkout is never the source; the run fails instead.
    assert "o/two" in str(excinfo.value)
    assert str(legacy) not in str(excinfo.value)
    # ...and nothing was cloned into the run dir.
    assert not (config.paths.runs / "r1" / "workspace" / ".git").exists()


def test_no_workspace_no_credential_fails_explicitly(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host with no GitHub credential at all can still clone a public
    remote, and a private one fails naming that as the reason."""
    legacy = _checkout(tmp_path / "sbxloop", "o/one")
    config = _config(
        tmp_path,
        [{"repo": "o/one", "workspace": str(legacy)}, {"repo": "o/private"}],
    )
    provisioner = Provisioner(SbxCLI(binary=str(fake_sbx.binary)), config, env={})

    from sbxloop import hostgit

    seen: dict[str, object] = {}

    def boom(url: str, target: Path, branch: str, **kwargs: object) -> str:
        seen.update(kwargs)
        raise ProvisionError(f"cloning {url} failed: could not read Username")

    monkeypatch.setattr(hostgit, "clone_from_remote", boom)
    with pytest.raises(ProvisionError) as excinfo:
        provisioner._resolve_workspace("r1", "o/private")
    message = str(excinfo.value)
    assert seen["token"] is None
    assert "no workspace is configured for o/private" in message
    assert "No GitHub credential is configured on the host" in message
    assert "GH_TOKEN" in message


def test_remote_clone_carries_the_runs_token(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#683: the clone authenticates with the same credential the github
    sandbox delivers with — daemon-wide GH_TOKEN, or the entry's own
    token_env — and the clone event says so."""
    legacy = _checkout(tmp_path / "sbxloop", "o/one")
    upstream = _checkout(tmp_path / "upstream", "o/private")
    config = _config(
        tmp_path,
        [
            {"repo": "o/one", "workspace": str(legacy)},
            {"repo": "o/private"},
            {"repo": "o/other", "token_env": "OTHER_TOKEN"},
        ],
    )
    env = {**TOKENS, "OTHER_TOKEN": "github_pat_other"}
    provisioner = Provisioner(SbxCLI(binary=str(fake_sbx.binary)), config, env=env)

    from sbxloop import hostgit

    seen: list[str | None] = []

    def fake_clone(url: str, target: Path, branch: str, **kwargs: object) -> str:
        seen.append(kwargs.get("token"))  # type: ignore[arg-type]
        return hostgit.clone_for_run(upstream, target, branch)

    monkeypatch.setattr(hostgit, "clone_from_remote", fake_clone)
    events: list[object] = []
    provisioner.bus.subscribe(events.append)
    provisioner._resolve_workspace("r1", "o/private")
    provisioner._resolve_workspace("r2", "o/other")
    assert seen == ["github_pat_user", "github_pat_other"]
    clones = [e for e in events if e.type == "sandbox.workspace_clone"]  # type: ignore[attr-defined]
    assert [e.data["authenticated"] for e in clones] == [True, True]  # type: ignore[attr-defined]
    assert "with the run's GitHub credential" in clones[0].data["message"]  # type: ignore[attr-defined]
    # The token itself is in no event.
    assert all("github_pat" not in str(e.data) for e in clones)  # type: ignore[attr-defined]


def test_remote_clone_failure_with_a_token_names_the_permission(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _checkout(tmp_path / "sbxloop", "o/one")
    config = _config(tmp_path, [{"repo": "o/one", "workspace": str(legacy)}, {"repo": "o/private"}])
    provisioner = _provisioner(fake_sbx, config)

    from sbxloop import hostgit

    def boom(url: str, target: Path, branch: str, **kwargs: object) -> str:
        raise ProvisionError(f"cloning {url} failed: Authentication failed")

    monkeypatch.setattr(hostgit, "clone_from_remote", boom)
    with pytest.raises(ProvisionError) as excinfo:
        provisioner._resolve_workspace("r1", "o/private")
    message = str(excinfo.value)
    assert "authenticated with the run's GitHub credential" in message
    assert "contents:read" in message
    assert "github_pat_user" not in message


def test_remote_clone_with_a_misconfigured_credential_fails_before_cloning(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-repo token_env that is unset is a misconfiguration, not "no
    credential": the run fails naming it rather than cloning unauthenticated
    and reporting a confusing remote error."""
    legacy = _checkout(tmp_path / "sbxloop", "o/one")
    config = _config(
        tmp_path,
        [
            {"repo": "o/one", "workspace": str(legacy)},
            {"repo": "o/private", "token_env": "MISSING_TOKEN"},
        ],
    )
    provisioner = _provisioner(fake_sbx, config)

    from sbxloop import hostgit

    def never(url: str, target: Path, branch: str, **kwargs: object) -> str:
        raise AssertionError("clone must not be attempted")

    monkeypatch.setattr(hostgit, "clone_from_remote", never)
    with pytest.raises(ProvisionError, match="MISSING_TOKEN"):
        provisioner._resolve_workspace("r1", "o/private")


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

    seen: dict[str, object] = {}

    def fake_clone(
        url: str,
        target: Path,
        branch: str,
        *,
        existing: bool = False,
        clone_filter: str | None = None,
        token: str | None = None,
    ) -> str:
        seen["url"] = url
        seen["clone_filter"] = clone_filter
        return hostgit.clone_for_run(upstream, target, branch)

    monkeypatch.setattr(hostgit, "clone_from_remote", fake_clone)
    ws = provisioner._resolve_workspace("r1", "o/public")
    assert seen["url"] == "https://github.com/o/public"
    assert seen["clone_filter"] is None  # opt-in (#632)
    assert (ws / "README.md").read_text() == "# o/public\n"


def test_remote_clone_follows_api_url_and_clone_filter(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clone URL derives from [github] api_url (#623) and the partial
    clone filter from [sandbox] clone_filter (#632)."""
    legacy = _checkout(tmp_path / "sbxloop", "o/one")
    upstream = _checkout(tmp_path / "upstream", "o/public")
    config = Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "github": {
                "repos": [{"repo": "o/one", "workspace": str(legacy)}, {"repo": "o/public"}],
                "api_url": "https://ghe.example.com/api/v3",
            },
            "sandbox": {"workspace_isolation": "clone", "clone_filter": "blob:none"},
        }
    )
    provisioner = _provisioner(fake_sbx, config)

    from sbxloop import hostgit

    seen: dict[str, object] = {}

    def fake_clone(
        url: str,
        target: Path,
        branch: str,
        *,
        existing: bool = False,
        clone_filter: str | None = None,
        token: str | None = None,
    ) -> str:
        seen["url"] = url
        seen["clone_filter"] = clone_filter
        return hostgit.clone_for_run(upstream, target, branch)

    monkeypatch.setattr(hostgit, "clone_from_remote", fake_clone)
    provisioner._resolve_workspace("r1", "o/public")
    assert seen == {"url": "https://ghe.example.com/o/public", "clone_filter": "blob:none"}


def test_single_repo_legacy_workspace_still_clones(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    legacy = _checkout(tmp_path / "tree", "o/one")
    config = _config(tmp_path, [{"repo": "o/one"}], workspace=str(legacy))
    provisioner = _provisioner(fake_sbx, config)
    ws = provisioner._resolve_workspace("r1", "o/one")
    assert ws == (config.paths.runs / "r1" / "workspace").resolve()
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

    def boom(url: str, target: Path, branch: str, **kwargs: object) -> str:
        raise ProvisionError(f"cloning {url} failed: repository not found")

    monkeypatch.setattr(hostgit, "clone_from_remote", boom)
    provisioner = _provisioner(fake_sbx, narrowed)
    with pytest.raises(ProvisionError) as excinfo:
        provisioner._resolve_workspace("r1", "o/b")
    assert str(legacy) not in str(excinfo.value)
