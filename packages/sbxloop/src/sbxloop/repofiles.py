"""Read untrusted repository files without following links outside the checkout.

Open each component relative to an already open directory, never following
symlinks in the kernel. Resolve links ourselves against that directory stack.
This keeps a concurrent replacement with a symlink from escaping between a
path check and the read. Internal relative links remain valid. Unsupported
hosts fail closed instead of falling back to a check-then-open operation.
"""

from __future__ import annotations

import errno
import os
import stat
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


@contextmanager
def open_file(root: Path, relative: str | Path) -> Iterator[BinaryIO]:
    """Open a regular file beneath a trusted checkout root, or raise OSError."""
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise OSError(errno.ENOTSUP, "safe repository reads require directory-relative opens")
    relative = Path(relative)
    if relative.is_absolute():
        raise PermissionError("repository file must be relative to the checkout")
    root = root.resolve()
    directories = [os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    pending = deque(relative.parts)
    links = 0
    fd: int | None = None
    try:
        while pending:
            name = pending.popleft()
            if name == ".":
                continue
            if name == "..":
                if len(directories) == 1:
                    raise PermissionError("repository link escapes the checkout")
                os.close(directories.pop())
                continue
            info = os.stat(name, dir_fd=directories[-1], follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                links += 1
                if links > 40:
                    raise OSError(errno.ELOOP, "too many repository symlinks")
                target = Path(os.readlink(name, dir_fd=directories[-1]))
                if target.is_absolute():
                    try:
                        target = target.relative_to(root)
                    except ValueError as exc:
                        raise PermissionError("repository link escapes the checkout") from exc
                    while len(directories) > 1:
                        os.close(directories.pop())
                pending.extendleft(reversed(target.parts))
                continue
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if pending:
                directories.append(os.open(name, flags | os.O_DIRECTORY, dir_fd=directories[-1]))
            else:
                fd = os.open(name, flags, dir_fd=directories[-1])
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(errno.EINVAL, "repository input is not a regular file")
        if fd is None:
            raise IsADirectoryError("repository input is not a regular file")
        stream = os.fdopen(fd, "rb")
        fd = None  # the stream now owns the descriptor
        with stream:
            yield stream
    finally:
        if fd is not None:
            os.close(fd)
        for directory in reversed(directories):
            os.close(directory)


def read_bytes(root: Path, relative: str | Path, *, limit: int = 1_000_000) -> bytes:
    """Read at most limit bytes from a contained, regular repository file."""
    with open_file(root, relative) as stream:
        return stream.read(limit)


def read_text(root: Path, relative: str | Path, *, limit: int = 1_000_000) -> str:
    return read_bytes(root, relative, limit=limit).decode("utf-8", errors="replace")
