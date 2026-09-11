"""Host Git inspection must not load an agent-controlled include graph."""

import builtins
from pathlib import Path
from unittest.mock import patch

import pytest
from git import Repo

from sbxloop.errors import ProvisionError
from sbxloop.hostgit import diff_text


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "checkout"
    with Repo.init(root) as repo:
        repo.git.config("user.name", "Audit")
        repo.git.config("user.email", "audit@example.invalid")
        (root / "file.txt").write_text("before\n")
        repo.git.add("file.txt")
        repo.git.commit("-m", "base")
        base = repo.head.commit.hexsha
    (root / "file.txt").write_text("after\n")
    return root, base


@pytest.mark.parametrize("conditional", [False, True])
def test_git_include_cannot_read_host_secret(
    checkout: tuple[Path, str],
    tmp_path: Path,
    conditional: bool,
) -> None:
    root, base = checkout
    secret = tmp_path / "host-only-secret"
    secret.write_text("SYNTHETIC_HOST_ONLY_SECRET\n")
    header = f'includeIf "gitdir:{root.as_posix()}/.git"' if conditional else "include"
    with (root / ".git/config").open("a") as config:
        config.write(f"\n[{header}]\n path = {secret.as_posix()}\n")
    # Also observe Repo construction: it swallows config parse failures, so
    # checking only the final diff would miss an earlier read of the include.
    with patch("builtins.open", wraps=builtins.open) as opened:
        diff = diff_text(root, base)
    assert not any(call.args[0] == str(secret) for call in opened.call_args_list)
    assert diff is not None and "+after" in diff
    assert "SYNTHETIC_HOST_ONLY_SECRET" not in diff


def test_malformed_config_does_not_report_its_contents(checkout: tuple[Path, str]) -> None:
    root, base = checkout
    (root / ".git/config").write_text("SYNTHETIC_HOST_ONLY_SECRET\n")
    with pytest.raises(ProvisionError) as error:
        diff_text(root, base)
    assert "SYNTHETIC_HOST_ONLY_SECRET" not in str(error.value)


def test_included_config_cannot_change_repository_format(checkout: tuple[Path, str]) -> None:
    root, base = checkout
    extra = root / "injected-config"
    extra.write_text("[extensions]\n objectformat = sha256\n")
    with (root / ".git/config").open("a") as config:
        config.write(f"\n[include]\n path = {extra.as_posix()}\n")
    assert "+after" in (diff_text(root, base) or "")
