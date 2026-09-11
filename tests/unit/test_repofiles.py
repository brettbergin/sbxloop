"""Containment also holds when an agent swaps paths while the host reads."""

import os
from pathlib import Path

import pytest

from sbxloop import repofiles


@pytest.mark.parametrize("absolute", [False, True])
def test_links_inside_the_checkout_remain_readable(tmp_path: Path, absolute: bool) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/rules").write_text("repository rules")
    (tmp_path / "AGENTS.md").symlink_to(tmp_path / "docs/rules" if absolute else "docs/rules")
    assert repofiles.read_text(tmp_path, "AGENTS.md") == "repository rules"


@pytest.mark.parametrize("shape", ["file", "parent", "absolute", "loop", "fifo"])
def test_unsafe_inputs_are_refused(tmp_path: Path, shape: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "secret").write_text("synthetic secret")
    name = "input"
    if shape == "parent":
        (root / name).symlink_to("..", target_is_directory=True)
        name += "/secret"
    elif shape == "fifo":
        os.mkfifo(root / name)
    else:
        target = {"file": "../secret", "absolute": str(tmp_path / "secret"), "loop": "input"}[shape]
        (root / name).symlink_to(target)
    with pytest.raises(OSError):
        repofiles.read_bytes(root, name)


@pytest.mark.parametrize("parent", [False, True])
def test_replacement_after_stat_cannot_redirect_the_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent: bool
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "rules").write_text("synthetic secret")
    (root / "docs").mkdir()
    (root / "docs/rules").write_text("safe")
    original = os.stat
    swapped = False

    def stat_then_swap(path, *args, **kwargs):
        nonlocal swapped
        result = original(path, *args, **kwargs)
        if not swapped and path == ("docs" if parent else "rules") and "dir_fd" in kwargs:
            swapped = True
            if parent:
                (root / "docs").rename(root / "old-docs")
                (root / "docs").symlink_to(outside, target_is_directory=True)
            else:
                (root / "docs/rules").unlink()
                (root / "docs/rules").symlink_to(outside / "rules")
        return result

    monkeypatch.setattr(os, "stat", stat_then_swap)
    with pytest.raises(OSError):
        repofiles.read_text(root, "docs/rules")
    assert swapped


def test_reads_are_bounded(tmp_path: Path) -> None:
    (tmp_path / "rules").write_bytes(b"x" * 100)
    assert repofiles.read_bytes(tmp_path, "rules", limit=8) == b"x" * 8
