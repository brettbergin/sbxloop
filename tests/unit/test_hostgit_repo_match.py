"""Unit tests for hostgit.normalise_repo_url / origin_matches_repo."""

from __future__ import annotations

from pathlib import Path

import pytest

from sbxloop import hostgit

from .test_hostgit import git, make_repo


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:brettbergin/sbxloop.git",
        "git@github.com:brettbergin/sbxloop",
        "https://github.com/brettbergin/sbxloop",
        "https://github.com/brettbergin/sbxloop.git",
        "https://github.com/brettbergin/sbxloop/",
        "https://github.com/brettbergin/sbxloop.git/",
        "https://x-access-token:secret@github.com/brettbergin/sbxloop.git",
        "git://github.com/brettbergin/sbxloop.git",
        "ssh://git@github.com/brettbergin/sbxloop.git",
        "https://GitHub.com/BrettBergin/SbxLoop.git",
        "brettbergin/sbxloop",
    ],
)
def test_normalise_repo_url_forms(url: str) -> None:
    assert hostgit.normalise_repo_url(url) == "brettbergin/sbxloop"


def test_normalise_repo_url_case_insensitive() -> None:
    assert hostgit.normalise_repo_url(
        "git@github.com:BrettBergin/SbxLoop.git"
    ) == hostgit.normalise_repo_url("https://github.com/brettbergin/sbxloop")


@pytest.mark.parametrize(
    "url",
    [None, "", "   ", "https://github.com/", "https://github.com/owner", "not a url"],
)
def test_normalise_repo_url_rejects(url: str | None) -> None:
    assert hostgit.normalise_repo_url(url) is None


def test_origin_matches_repo_true(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    git("remote", "add", "origin", "git@github.com:brettbergin/sbxloop.git", cwd=root)
    assert hostgit.origin_matches_repo(root, "brettbergin/sbxloop") is True
    assert hostgit.origin_matches_repo(root, "https://github.com/BrettBergin/sbxloop.git") is True


def test_origin_matches_repo_false_on_mismatch(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    git("remote", "add", "origin", "https://github.com/brettbergin/sbxloop", cwd=root)
    assert hostgit.origin_matches_repo(root, "brettbergin/entrygraph") is False


def test_origin_matches_repo_none_without_origin(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    assert hostgit.origin_matches_repo(root, "brettbergin/sbxloop") is None


def test_origin_matches_repo_none_for_non_git_path(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert hostgit.origin_matches_repo(plain, "brettbergin/sbxloop") is None
    assert hostgit.origin_matches_repo(tmp_path / "missing", "a/b") is None


def test_origin_matches_repo_none_for_unparsable_expectation(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    git("remote", "add", "origin", "https://github.com/brettbergin/sbxloop", cwd=root)
    assert hostgit.origin_matches_repo(root, "nonsense") is None
