"""Host reads must not execute commands from an agent-writable checkout."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sbxloop import hostgit
from sbxloop.errors import ProvisionError
from sbxloop.safegit import read_repo
from tests.unit.test_hostgit import git, make_repo, make_run_clone, push_upstream_commit


@pytest.mark.parametrize(
    "operation", ["is_dirty", "submodule_is_dirty", "changes_since", "diff_text"]
)
@pytest.mark.parametrize("driver", ["fsmonitor", "filter"])
def test_host_reads_do_not_execute_repository_commands(
    tmp_path: Path, operation: str, driver: str
) -> None:
    root = make_repo(tmp_path)
    base = hostgit.head_commit(root)
    assert base is not None
    marker = tmp_path / "executed-on-host"
    command = tmp_path / "repository-command"
    command.write_text(f'#!/bin/sh\nprintf marker > "{marker}"\ncat\n')
    command.chmod(0o700)
    if driver == "fsmonitor":
        git("config", "core.fsmonitor", str(command), cwd=root)
        git("config", "core.fsmonitorHookVersion", "1", cwd=root)
    else:
        (root / ".gitattributes").write_text("*.txt filter=untrusted\n")
        git("config", "filter.untrusted.clean", str(command), cwd=root)
    config = (root / ".git/config").read_bytes()
    (root / "hello.txt").write_text("changed content\n")

    if operation == "changes_since":
        assert any(c.path == "hello.txt" for c in hostgit.changes_since(root, base))
    elif operation == "diff_text":
        assert "+changed content" in (hostgit.diff_text(root, base) or "")
    else:
        assert getattr(hostgit, operation)(root) is True

    assert not marker.exists(), f"{operation} executed the checkout's {driver} command on the host"
    assert (root / ".git/config").read_bytes() == config


@pytest.mark.parametrize("driver", ["textconv", "external"])
def test_review_diff_does_not_execute_diff_drivers(tmp_path: Path, driver: str) -> None:
    root = make_repo(tmp_path)
    base = hostgit.head_commit(root)
    marker = tmp_path / "executed-on-host"
    command = tmp_path / "diff-command"
    command.write_text(f'#!/bin/sh\nprintf marker > "{marker}"\ncat "$1"\n')
    command.chmod(0o700)
    if driver == "textconv":
        (root / ".gitattributes").write_text("*.txt diff=untrusted\n")
        git("config", "diff.untrusted.textconv", str(command), cwd=root)
    else:
        git("config", "diff.external", str(command), cwd=root)
    (root / "hello.txt").write_text("changed content\n")
    text = hostgit.diff_text(root, base)

    assert not marker.exists(), f"review ran the checkout's {driver} on the host"
    assert "+changed content" in (text or "")


@pytest.mark.parametrize("operation", ["is_dirty", "changes_since", "diff_text"])
def test_nested_submodule_config_cannot_execute_on_host(tmp_path: Path, operation: str) -> None:
    root = make_repo(tmp_path)
    library = make_repo(tmp_path, "library")
    git(
        "-c", "protocol.file.allow=always", "submodule", "add", str(library), "vendor/lib", cwd=root
    )
    git("commit", "-am", "add library", cwd=root)
    base = hostgit.head_commit(root)
    assert base is not None
    sub = root / "vendor/lib"
    marker = tmp_path / "submodule-command-ran"
    command = tmp_path / "submodule-command"
    command.write_text(f'#!/bin/sh\nprintf marker > "{marker}"\n')
    command.chmod(0o700)
    git("config", "core.fsmonitor", str(command), cwd=sub)
    git("config", "core.fsmonitorHookVersion", "1", cwd=sub)
    (sub / "hello.txt").write_text("library edit\n")
    if operation == "changes_since":
        notes: list[str] = []
        assert hostgit.changes_since(root, base, notes=notes) == []
        assert notes == ["changes inside submodule `vendor/lib` are not delivered"]
    elif operation == "is_dirty":
        assert hostgit.is_dirty(root)
    else:
        hostgit.diff_text(root, base)
    assert not marker.exists()


def test_base_fetch_ignores_agent_remote_and_keeps_credentials_out_of_bundle(
    tmp_path: Path,
) -> None:
    upstream, clone = make_run_clone(tmp_path)
    expected = push_upstream_commit(tmp_path, upstream)
    marker = tmp_path / "host-remote-helper-ran"
    git("config", "remote.origin.url", f"ext::touch {marker}", cwd=clone)
    git("config", "protocol.ext.allow", "always", cwd=clone)
    git("config", "core.fsmonitor", f"touch {marker}", cwd=clone)
    before = {
        p.relative_to(clone / ".git"): p.read_bytes()
        for p in (clone / ".git").rglob("*")
        if p.is_file()
    }
    dummy = "TEST_ONLY_CLONE_CREDENTIAL_8f80"

    with hostgit.base_bundle(clone, str(upstream), "main", token=dummy) as (sha, bundle):
        assert sha == expected
        assert bundle is not None and bundle.is_file()
        assert dummy.encode() not in bundle.read_bytes()
        assert not marker.exists()
    assert not bundle.exists()
    after = {
        p.relative_to(clone / ".git"): p.read_bytes()
        for p in (clone / ".git").rglob("*")
        if p.is_file()
    }
    assert after == before, "fetch must not mutate the agent-writable metadata on the host"


def test_base_already_in_the_clone_needs_no_bundle(tmp_path: Path) -> None:
    upstream, clone = make_run_clone(tmp_path)
    expected = hostgit.head_commit(clone)
    with hostgit.base_bundle(clone, str(upstream), "main", token=None) as (sha, bundle):
        assert sha == expected
        assert bundle is None


@pytest.mark.parametrize(
    "extension,value", [("objectFormat", "sha256"), ("refStorage", "reftable")]
)
def test_unsupported_metadata_format_fails_explicitly(
    tmp_path: Path, extension: str, value: str
) -> None:
    root = make_repo(tmp_path)
    git("config", f"extensions.{extension}", value, cwd=root)
    with pytest.raises(ProvisionError, match="unsupported repository format"), read_repo(root):
        pass


def test_inherited_git_environment_cannot_redirect_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_repo(tmp_path)
    other = make_repo(tmp_path, "other")
    base = hostgit.head_commit(root)
    (root / "hello.txt").write_text("the requested checkout\n")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert "+the requested checkout" in (hostgit.diff_text(root, base) or "")


def test_linked_worktree_retains_its_own_index_and_common_objects(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    linked = tmp_path / "linked"
    git("worktree", "add", "-b", "topic", str(linked), cwd=root)
    base = hostgit.head_commit(linked)
    assert base is not None
    (linked / "hello.txt").write_text("linked checkout edit\n")
    assert hostgit.is_dirty(linked)
    assert not hostgit.is_dirty(root)
    assert "+linked checkout edit" in (hostgit.diff_text(linked, base) or "")


def test_lfs_comparison_hashes_content_without_running_the_configured_driver(
    tmp_path: Path,
) -> None:
    root = make_repo(tmp_path)
    content = b"asset contents\x00" * 100
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{hashlib.sha256(content).hexdigest()}\nsize {len(content)}\n"
    )
    (root / "asset.bin").write_text(pointer)
    (root / ".gitattributes").write_text("*.bin filter=lfs -text\n")
    git("add", ".", cwd=root)
    git("commit", "-m", "asset pointer", cwd=root)
    base = hostgit.head_commit(root)
    assert base is not None
    marker = tmp_path / "untrusted-lfs-ran"
    git("config", "filter.lfs.clean", f"touch '{marker}'", cwd=root)
    (root / "asset.bin").write_bytes(content)
    assert hostgit.changes_since(root, base) == []
    assert not hostgit.is_dirty(root)
    (root / "asset.bin").write_bytes(content + b"changed")
    assert [c.path for c in hostgit.changes_since(root, base)] == ["asset.bin"]
    assert not marker.exists()
