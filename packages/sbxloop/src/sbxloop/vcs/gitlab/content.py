"""The GitLab backend's content role (#1020): GitHub's blob, tree, commit
and ref vocabulary, which ``deliver.py`` speaks, staged inside the backend
and written to GitLab as one commits-API changeset.

GitLab has no blob, tree or commit object a client creates on its own
(#1016 V5): ``POST /repository/commits`` takes a whole changeset (create,
update, delete, move, chmod; binary as base64) and writes the commit on a
branch, atomically. So a blob is hashed here and kept until it is
committed, a tree is the list of actions that turns the base commit's
tree into it, and the commit is written when it is created, on a pending
branch, because the API writes no commit that no branch points at. The
ref step then creates the run's branch at that commit or, for a branch
that already exists, writes the same actions from the same parent onto
it again under ``force`` (field-verified on GitLab CE 19.3.2). A branch
is never deleted and recreated: deleting the source branch of an open
merge request closes the request (field-verified, the same day), and
GitLab has no call that moves a branch to a commit.

Everything in this module is pure; the calls are in ``ops.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import posixpath
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NamedTuple

from sbxloop.errors import GithubOpsError

REGULAR_MODE = "100644"
EXECUTABLE_MODE = "100755"
SYMLINK_MODE = "120000"
GITLINK_MODE = "160000"

#: One directory of a base tree as GitLab lists it: ``name -> (type, mode)``.
Listing = Mapping[str, tuple[str, str]]


def blob_sha(raw: bytes) -> str:
    """Git's own id for ``raw`` as a blob, so the shas the tree entries
    name are the ones git would give the same bytes."""
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw, usedforsecurity=False).hexdigest()


class Staged(NamedTuple):
    """A tree not written yet: the commit whose tree it is built on and
    the actions that make it."""

    base: str
    actions: tuple[dict[str, Any], ...]


class Pending(NamedTuple):
    """A commit already written on a pending branch, with what it takes
    to write it again onto another branch (``branch`` is empty once the
    pending branch is gone)."""

    message: str
    start: str
    actions: tuple[dict[str, Any], ...]
    branch: str


def tree_handle(base: str, actions: Sequence[Mapping[str, Any]]) -> str:
    """The opaque id a staged tree is committed by: a digest of what it
    is, so the same entries on the same base name the same tree."""
    digest = hashlib.sha1(
        json.dumps([base, list(actions)], sort_keys=True).encode(), usedforsecurity=False
    ).hexdigest()
    return f"tree:{digest}"


def plan_actions(
    entries: Sequence[Mapping[str, Any]],
    base_listing: Callable[[str], Listing],
    blobs: Mapping[str, bytes],
) -> tuple[list[dict[str, Any]], list[str]]:
    """GitLab's actions for GitHub-shaped tree ``entries`` on top of a base
    tree read through ``base_listing`` (one directory at a time, cached by
    the caller), and the paths of deletions skipped because the base has
    no such file. ``blobs`` are the staged contents by sha.

    The changeset must say create versus update (GitLab refuses each in
    the other's place, #1016 V5), so every entry is looked up in its
    directory first. A submodule pointer and a symlink are refused by
    name: the commits API has no action that writes either.
    """
    actions: list[dict[str, Any]] = []
    skipped: list[str] = []
    for entry in entries:
        path = str(entry.get("path") or "")
        if not path:
            raise GithubOpsError(f"a tree entry has no path: {entry!r}")
        mode = str(entry.get("mode") or REGULAR_MODE)
        kind = str(entry.get("type") or "blob")
        if kind == "commit" or mode == GITLINK_MODE:
            raise GithubOpsError(
                f"GitLab's commits API has no action for the submodule pointer at {path!r}; "
                "deliver a submodule change from a checkout"
            )
        if mode == SYMLINK_MODE:
            raise GithubOpsError(
                f"GitLab's commits API writes no symlink ({path!r}); "
                "deliver a symlink from a checkout"
            )
        directory, name = posixpath.split(path)
        present = base_listing(directory).get(name)
        sha = entry.get("sha")
        if sha is None:
            if present is None:
                skipped.append(path)
                continue
            actions.append({"action": "delete", "file_path": path})
            continue
        raw = blobs.get(str(sha))
        if raw is None:
            raise GithubOpsError(
                f"blob {sha} for {path!r} was not staged by this backend; the blobs and the "
                "tree of one delivery are built by the same backend object"
            )
        content = base64.b64encode(raw).decode("ascii")
        executable = mode == EXECUTABLE_MODE
        if present is None or present[0] != "blob":
            action: dict[str, Any] = {
                "action": "create",
                "file_path": path,
                "content": content,
                "encoding": "base64",
            }
            if executable:
                # Honoured on a create (field-verified: the entry lists as
                # 100755), whatever the documentation says about chmod.
                action["execute_filemode"] = True
            actions.append(action)
            continue
        actions.append(
            {"action": "update", "file_path": path, "content": content, "encoding": "base64"}
        )
        if (present[1] == EXECUTABLE_MODE) != executable:
            actions.append({"action": "chmod", "file_path": path, "execute_filemode": executable})
    return actions, skipped


def commit_record(data: Mapping[str, Any]) -> dict[str, Any]:
    """A GitLab commit as the commit record the loop reads: ``sha``, a
    ``tree`` whose sha is the commit's own (GitLab addresses a tree by the
    commit that holds it, and the payload carries no tree id), the
    parents and the message."""
    sha = str(data.get("id") or "")
    if not sha:
        raise GithubOpsError(f"GitLab returned a commit without an id: {data!r}")
    parents = data.get("parent_ids")
    return {
        "sha": sha,
        "tree": {"sha": sha},
        "parents": [{"sha": str(p)} for p in parents] if isinstance(parents, list) else [],
        "message": str(data.get("message") or ""),
        "html_url": str(data.get("web_url") or ""),
    }
