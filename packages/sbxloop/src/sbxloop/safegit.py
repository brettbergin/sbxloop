"""Read an agent's Git data without loading its executable configuration.

The host-owned temporary repository carries only refs, index and object
access. Config, hooks and executable drivers never enter it. This is a
read-only view: operations that modify the run checkout belong in its VM.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from git import Repo

from sbxloop.errors import ProvisionError

# The only clean driver the host permits. It hashes bytes into an LFS
# pointer without invoking git-lfs, reading repository config or importing
# code from the checkout. Streaming keeps large assets out of host memory.
_LFS_CLEAN = """\
import hashlib, sys
head = sys.stdin.buffer.read(1024)
if len(head) < 1024 and head.startswith(b'version https://git-lfs.github.com/spec/v1\\n'):
    sys.stdout.buffer.write(head)
else:
    digest = hashlib.sha256(head)
    size = len(head)
    while chunk := sys.stdin.buffer.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    sys.stdout.write('version https://git-lfs.github.com/spec/v1\\n'
                     + 'oid sha256:' + digest.hexdigest() + '\\nsize ' + str(size) + '\\n')
"""


def clean_environment() -> dict[str, str | None]:
    """Remove inherited Git routing/config before selecting the trusted view."""
    return {
        **{key: None for key in os.environ if key.startswith("GIT_")},
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "filter.lfs.clean",
        "GIT_CONFIG_VALUE_0": f"{shlex.quote(sys.executable)} -I -S -c {shlex.quote(_LFS_CLEAN)}",
        "GIT_CONFIG_KEY_1": "filter.lfs.required",
        "GIT_CONFIG_VALUE_1": "true",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _copy_file(source: Path, target: Path) -> None:
    if source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


@contextmanager
def read_repo(root: Path) -> Iterator[Repo]:
    """A private Git metadata snapshot, reading the requested working tree.

    Git has no switch to omit local config. Command-line overrides for a
    few known hook names miss arbitrary filter/merge driver names and race
    an agent that can still edit config. A new gitdir avoids both problems.
    Object bytes are read through an alternate store; no source config is
    consulted and no missing object can trigger a promisor remote fetch.

    Callers must disable Git's recursive submodule work-tree scans and open
    submodules through this same helper instead: a child Git would otherwise
    discover that submodule's original gitdir and executable configuration.
    """
    root = root.absolute()
    with Repo(root) as source, tempfile.TemporaryDirectory(prefix="sbxloop-read-git-") as tmp:
        with source.config_reader(config_level="repository") as config:
            values = {
                f"{section}.{key}".lower(): str(value).lower()
                for section in config.sections()
                for key, value in config.items(section)
            }
        if (
            values.get("extensions.objectformat", "sha1") != "sha1"
            or values.get("extensions.refstorage", "files") != "files"
        ):
            raise ProvisionError(
                "host Git inspection requires SHA-1 objects and files refs; "
                "the checkout uses an unsupported repository format"
            )
        gitdir = Path(source.git_dir)
        common = Path(source.common_dir)
        private = Path(tmp) / ".git"
        (private / "objects" / "info").mkdir(parents=True)
        (private / "refs").mkdir()
        (private / "HEAD").write_text("ref: refs/heads/main\n")
        filemode = "false" if values.get("core.filemode") == "false" else "true"
        (private / "config").write_text(
            f"[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tfilemode = {filemode}\n"
        )
        # Git's alternates file uses C-style quoting for unusual paths.
        objects = str((common / "objects").resolve())
        quoted = '"' + objects.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if "\n" in objects or "\r" in objects:
            raise ValueError("Git object directory must not contain a newline")
        (private / "objects/info/alternates").write_text(quoted + "\n")
        for name in ("HEAD", "index"):
            _copy_file(gitdir / name, private / name)
        for index in gitdir.glob("sharedindex.*"):
            _copy_file(index, private / index.name)
        for name in ("packed-refs", "shallow", "info/attributes", "info/exclude"):
            _copy_file(common / name, private / name)
        if (common / "refs").is_dir():
            shutil.copytree(common / "refs", private / "refs", dirs_exist_ok=True)
        with (
            Repo(tmp) as repo,
            repo.git.custom_environment(
                **{**clean_environment(), "GIT_DIR": str(private), "GIT_WORK_TREE": str(root)}
            ),
        ):
            yield repo
