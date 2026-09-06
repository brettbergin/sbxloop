"""Tags in the run workspace (#694): a fresh ``--no-tags`` clone is given the
repository's tags when the workspace derives its version from them — from
the host checkout when it has them, from origin under the run's token
otherwise — and only a fresh one; ``[sandbox] fetch_tags`` overrides the
detection either way."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop import hostgit
from sbxloop.config import Config
from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.provision import Provisioner
from tests.conftest import FakeSbx
from tests.unit.test_hostgit import describe, git, make_repo
from tests.unit.test_provision import TOKENS

SCM_PYPROJECT = """\
[build-system]
requires = ["setuptools>=64", "setuptools_scm>=8"]
build-backend = "setuptools.build_meta"

[project]
name = "app"
dynamic = ["version"]
requires-python = ">=3.13"

[tool.setuptools_scm]
"""


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


def _tagged_scm_repo(tmp_path: Path, name: str = "app", *, tag: str | None = "v1.2.3") -> Path:
    app = make_repo(tmp_path, name)
    (app / "pyproject.toml").write_text(SCM_PYPROJECT)
    git("add", ".", cwd=app)
    git("commit", "-q", "-m", "tag-derived version", cwd=app)
    if tag is not None:
        git("tag", "-a", tag, "-m", "release", cwd=app)
    git("remote", "add", "origin", f"https://github.com/o/{name}.git", cwd=app)
    return app


def _never(*args: object, **kwargs: object) -> hostgit.TagFetch:
    raise AssertionError("fetch_tags must not run here")


def test_a_fresh_clone_of_a_tag_versioned_checkout_gets_its_tags(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _tagged_scm_repo(tmp_path)
    config = _config(tmp_path, [{"repo": "o/app", "workspace": str(app)}])
    provisioner = _provisioner(fake_sbx, config)
    events: list[object] = []
    provisioner.bus.subscribe(events.append)

    ws = provisioner._resolve_workspace("r1", "o/app")
    assert describe(ws) == "v1.2.3"
    (event,) = [e for e in events if e.type == "sandbox.workspace_tags"]  # type: ignore[attr-defined]
    assert event.data["mode"] == "auto"  # type: ignore[attr-defined]
    assert event.data["markers"] == ["pyproject.toml: setuptools_scm"]  # type: ignore[attr-defined]
    assert (event.data["tags"], event.data["source"]) == (1, "local")  # type: ignore[attr-defined]
    assert "1 tag(s) from the host checkout" in event.data["message"]  # type: ignore[attr-defined]

    # A resume re-entering the clone keeps whatever tags it has: no re-fetch.
    monkeypatch.setattr(hostgit, "fetch_tags", _never)
    assert provisioner._resolve_workspace("r1", "o/app") == ws


def test_a_workspace_with_a_static_version_is_left_without_tags(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_repo(tmp_path, "app")
    git("tag", "v1.2.3", cwd=app)
    git("remote", "add", "origin", "https://github.com/o/app.git", cwd=app)
    config = _config(tmp_path, [{"repo": "o/app", "workspace": str(app)}])
    provisioner = _provisioner(fake_sbx, config)
    monkeypatch.setattr(hostgit, "fetch_tags", _never)
    ws = provisioner._resolve_workspace("r1", "o/app")
    assert hostgit.tag_count(ws) == 0


def test_fetch_tags_never_wins_over_the_markers(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _tagged_scm_repo(tmp_path)
    config = _config(tmp_path, [{"repo": "o/app", "workspace": str(app)}], fetch_tags="never")
    provisioner = _provisioner(fake_sbx, config)
    monkeypatch.setattr(hostgit, "fetch_tags", _never)
    ws = provisioner._resolve_workspace("r1", "o/app")
    assert hostgit.tag_count(ws) == 0


def test_fetch_tags_always_fetches_without_markers(fake_sbx: FakeSbx, tmp_path: Path) -> None:
    app = make_repo(tmp_path, "app")
    git("tag", "v1.2.3", cwd=app)
    git("remote", "add", "origin", "https://github.com/o/app.git", cwd=app)
    config = _config(tmp_path, [{"repo": "o/app", "workspace": str(app)}], fetch_tags="always")
    provisioner = _provisioner(fake_sbx, config)
    events: list[object] = []
    provisioner.bus.subscribe(events.append)
    ws = provisioner._resolve_workspace("r1", "o/app")
    assert describe(ws) == "v1.2.3"
    (event,) = [e for e in events if e.type == "sandbox.workspace_tags"]  # type: ignore[attr-defined]
    assert event.data["mode"] == "always"  # type: ignore[attr-defined]
    assert event.data["markers"] == []  # type: ignore[attr-defined]
    assert event.data["message"].endswith("fetch_tags = always")  # type: ignore[attr-defined]


def test_a_remote_clone_fetches_from_origin_under_the_runs_token(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = _tagged_scm_repo(tmp_path, "upstream")
    legacy = make_repo(tmp_path, "legacy")
    git("remote", "add", "origin", "https://github.com/o/one.git", cwd=legacy)
    config = _config(tmp_path, [{"repo": "o/one", "workspace": str(legacy)}, {"repo": "o/app"}])
    provisioner = _provisioner(fake_sbx, config)

    def fake_clone(url: str, target: Path, branch: str, **kwargs: object) -> str:
        return hostgit.clone_for_run(upstream, target, branch)

    seen: list[tuple[Path | None, str | None]] = []

    def spy(clone: Path, *, source: Path | None, token: str | None) -> hostgit.TagFetch:
        seen.append((source, token))
        return hostgit.TagFetch(1, "remote")

    monkeypatch.setattr(hostgit, "clone_from_remote", fake_clone)
    monkeypatch.setattr(hostgit, "fetch_tags", spy)
    events: list[object] = []
    provisioner.bus.subscribe(events.append)
    provisioner._resolve_workspace("r1", "o/app")
    assert seen == [(None, "github_pat_user")]
    (event,) = [e for e in events if e.type == "sandbox.workspace_tags"]  # type: ignore[attr-defined]
    assert "1 tag(s) from the remote" in event.data["message"]  # type: ignore[attr-defined]
    assert event.data["source"] == "remote"  # type: ignore[attr-defined]


def test_a_host_checkout_without_tags_sends_the_token_to_origin(
    fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _tagged_scm_repo(tmp_path, tag=None)
    config = _config(tmp_path, [{"repo": "o/app", "workspace": str(app)}])
    provisioner = _provisioner(fake_sbx, config)
    seen: list[tuple[Path | None, str | None]] = []

    def spy(clone: Path, *, source: Path | None, token: str | None) -> hostgit.TagFetch:
        seen.append((source, token))
        return hostgit.TagFetch(0, "remote")

    monkeypatch.setattr(hostgit, "fetch_tags", spy)
    provisioner._resolve_workspace("r1", "o/app")
    assert seen == [(app, "github_pat_user")]
