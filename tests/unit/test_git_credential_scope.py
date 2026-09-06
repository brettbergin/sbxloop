"""A repository-supplied URL must not choose who receives the GitHub token."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from sbxloop import hostgit
from sbxloop.errors import ProvisionError
from tests.fakes.gitserver import PrivateGitServer, bare_from
from tests.unit.test_hostgit import git, make_repo

TOKEN = "TEST_ONLY_GITHUB_CREDENTIAL_7f19"


def credential_fill(
    tmp_path: Path, protocol: str, host: str, **kwargs: str
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        **hostgit._clone_env(TOKEN, **kwargs),
    }
    return subprocess.run(
        ["git", "credential", "fill"],
        cwd=tmp_path,
        env=env,
        input=f"protocol={protocol}\nhost={host}\n\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize(
    "protocol,host",
    [
        ("https", "untrusted.example.invalid"),
        ("http", "github.com"),
        ("http", "untrusted.example.invalid"),
        ("https", "github.com.attacker.invalid"),
        ("https", "github.com:8443"),
    ],
)
def test_untrusted_destination_never_receives_the_token(
    tmp_path: Path, protocol: str, host: str
) -> None:
    result = credential_fill(tmp_path, protocol, host)
    assert TOKEN not in result.stdout
    assert TOKEN not in result.stderr
    assert result.returncode != 0


def test_default_github_https_authentication_still_works(tmp_path: Path) -> None:
    result = credential_fill(tmp_path, "https", "github.com")
    assert result.returncode == 0
    assert "username=x-access-token" in result.stdout
    assert f"password={TOKEN}" in result.stdout


@pytest.mark.parametrize("host", ["ghe.example.invalid", "ghe.example.invalid:8443"])
def test_enterprise_authority_is_explicit_and_does_not_grant_dotcom(
    tmp_path: Path, host: str
) -> None:
    url = f"https://{host}/operator/configured/path"
    result = credential_fill(tmp_path, "https", host, credential_url=url)
    assert result.returncode == 0
    assert f"password={TOKEN}" in result.stdout
    assert TOKEN not in credential_fill(tmp_path, "https", "github.com", credential_url=url).stdout
    assert TOKEN not in credential_fill(tmp_path, "http", host, credential_url=url).stdout


@pytest.mark.parametrize(
    "url",
    ["http://github.com", "file:///tmp/repo", "https://user@github.com", "https://github.com:bad"],
)
def test_invalid_or_insecure_scope_cannot_authorize_a_token(tmp_path: Path, url: str) -> None:
    assert TOKEN not in credential_fill(tmp_path, "https", "github.com", credential_url=url).stdout


def add_gitlink(repo: Path, path: str, url: str, sha: str) -> None:
    (repo / ".gitmodules").write_text(f'[submodule "{path}"]\n\tpath = {path}\n\turl = {url}\n')
    git("add", ".gitmodules", cwd=repo)
    git("update-index", "--add", "--cacheinfo", f"160000,{sha},{path}", cwd=repo)
    git("commit", "-m", "add dependency", cwd=repo)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("tls", [False, True])
def test_remote_submodule_challenges_do_not_receive_the_root_credential(
    tmp_path: Path, nested: bool, tls: bool
) -> None:
    with (
        PrivateGitServer(tmp_path / "trusted", username="x-access-token", token=TOKEN) as trusted,
        PrivateGitServer(
            tmp_path / "foreign", username="x-access-token", token=TOKEN, tls=tls
        ) as foreign,
    ):
        leaf = make_repo(tmp_path, "leaf")
        leaf_sha = hostgit.head_commit(leaf)
        assert leaf_sha is not None
        bare_from(leaf, foreign.root, "leaf.git")
        root = make_repo(tmp_path, "root")
        url = f"{foreign.url}/leaf.git"
        sha = leaf_sha
        if nested:
            middle = make_repo(tmp_path, "middle")
            add_gitlink(middle, "inner", url, sha)
            bare_from(middle, trusted.root, "middle.git")
            url = f"{trusted.url}/middle.git"
            sha = hostgit.head_commit(middle)
            assert sha is not None
        add_gitlink(root, "dependency", url, sha)
        bare_from(root, trusted.root, "root.git")
        clone = tmp_path / "run"
        hostgit.clone_from_remote(f"{trusted.url}/root.git", clone, "run", token=TOKEN)
        with pytest.raises(ProvisionError, match="configured HTTPS host"):
            hostgit.populate_submodules(clone, source=None, token=TOKEN, credential_url=trusted.url)
        assert foreign.requests, "the foreign server must actually challenge the client"
        assert all(value is None for value in foreign.requests)
        assert any(value is not None for value in trusted.requests)


def test_nested_private_submodules_keep_the_configured_authority(tmp_path: Path) -> None:
    with PrivateGitServer(tmp_path / "trusted", username="x-access-token", token=TOKEN) as trusted:
        leaf = make_repo(tmp_path, "leaf")
        leaf_sha = hostgit.head_commit(leaf)
        assert leaf_sha is not None
        bare_from(leaf, trusted.root, "leaf.git")
        middle = make_repo(tmp_path, "middle")
        add_gitlink(middle, "inner", "../leaf.git", leaf_sha)
        bare_from(middle, trusted.root, "middle.git")
        middle_sha = hostgit.head_commit(middle)
        assert middle_sha is not None
        root = make_repo(tmp_path, "root")
        add_gitlink(root, "dependency", "../middle.git", middle_sha)
        bare_from(root, trusted.root, "root.git")
        clone = tmp_path / "run"
        hostgit.clone_from_remote(f"{trusted.url}/root.git", clone, "run", token=TOKEN)
        populated = hostgit.populate_submodules(
            clone, source=None, token=TOKEN, credential_url=trusted.url
        )
        assert populated == [("dependency", "remote"), ("dependency/inner", "remote")]
        assert (clone / "dependency/inner/hello.txt").read_text() == "hi\n"
        for config in (clone / ".git").rglob("config"):
            assert TOKEN not in config.read_text()
