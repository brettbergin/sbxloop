"""Folds from Gitea 1.24's payloads into the loop's records (#1021).

Gitea's API is GitHub-shaped in most places (``login``, ``number``,
``html_url``, ``head.sha``), which is why so little folds; what differs
is folded here, with the field observation that decided it:

- an issue payload carries ``pull_request: null`` for an issue, and the
  consumers test the key's presence (#631), so the key is dropped when
  null;
- the combined status's rows carry the state under ``status``, not
  ``state`` (field-verified: the ``statuses`` list and the status POST
  answer ``state: null``), so the fold reads both;
- a review's ``REQUEST_CHANGES`` is GitHub's ``CHANGES_REQUESTED``, and a
  dismissed review no longer stands;
- Gitea has no bot flag anywhere (#1016 V3), so a user's kind is the
  operator's list (``[vcs] bot_logins``), passed in as ``kind_of``;
- a draft is the ``WIP:`` title prefix (field-verified); retitling clears
  it;
- a review comment is its own thread: Gitea has no reply and no resolve
  path (#1016 V1), only a read-only ``resolver``.

Everything here is pure; the calls are in ``ops.py``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sbxloop.errors import GithubOpsError
from sbxloop.vcs.gitlab.records import iso_utc
from sbxloop.vcs.model import ChecksVerdict, ReviewThread, ThreadComment

#: A login's kind from the operator's list: True a bot, False a human.
KindOf = Callable[[str], bool]

# Gitea's default work-in-progress prefixes (``WORK_IN_PROGRESS_PREFIXES``);
# the first is what a draft is created with. An instance may configure
# others (field-unverified).
WIP_PREFIXES = ("WIP:", "[WIP]", "Draft:", "[Draft]")
DRAFT_PREFIX = "WIP: "


def undrafted_title(title: str) -> str:
    """``title`` without its work-in-progress marker."""
    stripped = title.strip()
    lowered = stripped.lower()
    for prefix in WIP_PREFIXES:
        if lowered.startswith(prefix.lower()):
            return stripped[len(prefix) :].strip()
    return stripped


def is_draft_title(title: str) -> bool:
    lowered = title.strip().lower()
    return any(lowered.startswith(prefix.lower()) for prefix in WIP_PREFIXES)


def user_record(user: Any, kind_of: KindOf) -> dict[str, Any]:
    """A Gitea ``user`` as the loop's ``user``: the login, the id, and a
    ``type`` the operator's list decides (Gitea has none, #1016 V3): a
    listed login is a ``Bot``, every other a ``User``."""
    if not isinstance(user, dict):
        return {"login": ""}
    login = str(user.get("login") or user.get("username") or "")
    record: dict[str, Any] = {"login": login, "type": "Bot" if kind_of(login) else "User"}
    if isinstance(user.get("id"), int):
        record["id"] = user["id"]
    return record


def labels_record(labels: Any) -> list[dict[str, str]]:
    if not isinstance(labels, list):
        return []
    out: list[dict[str, str]] = []
    for label in labels:
        if isinstance(label, dict) and label.get("name"):
            out.append({"name": str(label["name"])})
        elif isinstance(label, str) and label:
            out.append({"name": label})
    return out


def issue_state(payload: Mapping[str, Any]) -> str:
    return "closed" if str(payload.get("state") or "") == "closed" else "open"


def issue_record(payload: Mapping[str, Any], kind_of: KindOf) -> dict[str, Any]:
    """A Gitea issue as the loop's issue record. ``pull_request`` is
    present only when the payload's is not null (Gitea lists pull
    requests among issues unless asked not to, and carries the key as
    ``null`` on an issue, field-verified)."""
    record: dict[str, Any] = {
        "number": int(payload.get("number") or 0),
        "id": payload.get("id"),
        "title": str(payload.get("title") or ""),
        "body": str(payload.get("body") or ""),
        "state": issue_state(payload),
        "state_reason": None,
        "html_url": str(payload.get("html_url") or ""),
        "labels": labels_record(payload.get("labels")),
        "user": user_record(payload.get("user"), kind_of),
        "comments": int(payload.get("comments") or 0),
        "created_at": iso_utc(payload.get("created_at")),
        "updated_at": iso_utc(payload.get("updated_at")),
    }
    if payload.get("pull_request"):
        record["pull_request"] = dict(payload["pull_request"])
    return record


def comment_record(payload: Mapping[str, Any], kind_of: KindOf) -> dict[str, Any]:
    comment_id = payload.get("id")
    return {
        "id": int(comment_id) if isinstance(comment_id, int) else 0,
        "body": str(payload.get("body") or ""),
        "user": user_record(payload.get("user"), kind_of),
        "created_at": iso_utc(payload.get("created_at")),
        "html_url": str(payload.get("html_url") or ""),
    }


def timeline_event_record(payload: Mapping[str, Any], kind_of: KindOf) -> dict[str, Any] | None:
    """A timeline entry of type ``label`` as the loop's issue event:
    ``labeled`` when the entry's body is ``"1"`` (field-verified for an
    added label), ``unlabeled`` otherwise; other entry types are not
    events the loop reads."""
    if str(payload.get("type") or "") != "label":
        return None
    label = payload.get("label")
    if not isinstance(label, dict):
        return None
    return {
        "event": "labeled" if str(payload.get("body") or "") == "1" else "unlabeled",
        "label": {"name": str(label.get("name") or "")},
        "actor": user_record(payload.get("user"), kind_of),
        "created_at": iso_utc(payload.get("created_at")),
    }


def repo_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A Gitea repository as the loop's repository record; the
    ``permissions`` block is the token's own (field-verified: a write
    collaborator reads ``{admin: false, push: true, pull: true}``)."""
    permissions = payload.get("permissions")
    perms = permissions if isinstance(permissions, dict) else {}
    return {
        "id": payload.get("id"),
        "name": str(payload.get("name") or ""),
        "full_name": str(payload.get("full_name") or ""),
        "html_url": str(payload.get("html_url") or ""),
        "default_branch": payload.get("default_branch"),
        "private": bool(payload.get("private")),
        "has_issues": bool(payload.get("has_issues", True)),
        "empty": bool(payload.get("empty")),
        "allow_squash_merge": bool(payload.get("allow_squash_merge", True)),
        "allow_merge_commit": bool(payload.get("allow_merge_commits", True)),
        "allow_rebase_merge": bool(payload.get("allow_rebase", True)),
        "permissions": {
            "admin": bool(perms.get("admin")),
            "maintain": bool(perms.get("admin")),
            "push": bool(perms.get("push")),
            "pull": bool(perms.get("pull", True)),
        },
    }


def label_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": payload.get("id"),
        "name": str(payload.get("name") or ""),
        "color": str(payload.get("color") or "").removeprefix("#"),
        "description": str(payload.get("description") or ""),
    }


# -- pull requests -----------------------------------------------------------------


def node_id_for(repo: str, number: int) -> str:
    """The opaque id the operations that take only a node id address a
    pull request by: ``<owner/name>#<number>``."""
    return f"{repo}#{number}"


def parse_node_id(node_id: str) -> tuple[str, int]:
    repo, hash_, number = node_id.rpartition("#")
    if not hash_ or not repo or not number.isdigit():
        raise GithubOpsError(f"not a Gitea pull request id: {node_id!r}")
    return repo, int(number)


def merge_state(payload: Mapping[str, Any]) -> tuple[bool | None, str]:
    """``(mergeable, mergeable_state)`` from a pull request. Gitea's
    ``mergeable`` is false for a conflict, while it is still checking,
    and for a work-in-progress request alike (field-verified for the
    last), so a false that is not a draft is *not known* rather than a
    conflict: the merge call's own refusal says which."""
    if payload.get("merged"):
        return False, "merged"
    if payload.get("draft") or is_draft_title(str(payload.get("title") or "")):
        return None, "draft"
    if payload.get("mergeable") is True:
        return True, "clean"
    return None, "unknown"


def change_record(payload: Mapping[str, Any], kind_of: KindOf) -> dict[str, Any]:
    """A pull request as the loop's change record."""
    head_raw = payload.get("head")
    head: dict[str, Any] = head_raw if isinstance(head_raw, dict) else {}
    base_raw = payload.get("base")
    base: dict[str, Any] = base_raw if isinstance(base_raw, dict) else {}
    base_repo_raw = base.get("repo")
    base_repo: dict[str, Any] = base_repo_raw if isinstance(base_repo_raw, dict) else {}
    repo = str(base_repo.get("full_name") or "")
    number = int(payload.get("number") or 0)
    mergeable, state = merge_state(payload)
    reviewers = payload.get("requested_reviewers")
    return {
        "number": number,
        "id": payload.get("id"),
        "node_id": node_id_for(repo, number) if repo else str(number),
        "title": str(payload.get("title") or ""),
        "body": str(payload.get("body") or ""),
        "state": "closed" if str(payload.get("state") or "") == "closed" else "open",
        "merged": bool(payload.get("merged")),
        "merged_at": iso_utc(payload.get("merged_at")) if payload.get("merged_at") else None,
        "merge_commit_sha": payload.get("merge_commit_sha"),
        "draft": bool(payload.get("draft")) or is_draft_title(str(payload.get("title") or "")),
        "html_url": str(payload.get("html_url") or ""),
        "user": user_record(payload.get("user"), kind_of),
        "head": {
            "sha": str(head.get("sha") or ""),
            "ref": str(head.get("ref") or ""),
            "label": str(head.get("label") or head.get("ref") or ""),
        },
        "base": {"ref": str(base.get("ref") or ""), "sha": str(base.get("sha") or "")},
        "mergeable": mergeable,
        "mergeable_state": state,
        "requested_reviewers": [
            user_record(user, kind_of) for user in reviewers if isinstance(user, dict)
        ]
        if isinstance(reviewers, list)
        else [],
        "labels": labels_record(payload.get("labels")),
        "created_at": iso_utc(payload.get("created_at")),
        "updated_at": iso_utc(payload.get("updated_at")),
    }


# Gitea's review states (``ReviewStateType``) as the loop reads them; the
# rest (``PENDING``, ``REQUEST_REVIEW``) stand for no verdict.
_REVIEW_STATES = {
    "APPROVED": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
    "COMMENT": "COMMENTED",
}


def review_record(payload: Mapping[str, Any], kind_of: KindOf) -> dict[str, Any]:
    """A pull review as the loop's review record: GitHub's state words, a
    dismissed review as ``DISMISSED`` (it no longer stands)."""
    state = str(payload.get("state") or "").upper()
    if payload.get("dismissed"):
        state = "DISMISSED"
    return {
        "id": payload.get("id"),
        "user": user_record(payload.get("user"), kind_of),
        "state": _REVIEW_STATES.get(state, state),
        "body": str(payload.get("body") or ""),
        "submitted_at": iso_utc(payload.get("submitted_at")),
        "html_url": str(payload.get("html_url") or ""),
        "commit_id": str(payload.get("commit_id") or ""),
    }


def review_comment_record(
    payload: Mapping[str, Any], *, review_id: Any, kind_of: KindOf
) -> dict[str, Any]:
    """An inline review comment as the loop's review comment record; the
    line is Gitea's ``position`` (the new side's line, field-verified)."""
    position = payload.get("position")
    line = int(position) if isinstance(position, int) and position > 0 else None
    original = payload.get("original_position")
    return {
        "id": payload.get("id"),
        "user": user_record(payload.get("user"), kind_of),
        "body": str(payload.get("body") or ""),
        "path": str(payload.get("path") or ""),
        "line": line,
        "original_line": int(original) if isinstance(original, int) and original > 0 else line,
        "created_at": iso_utc(payload.get("created_at")),
        "html_url": str(payload.get("html_url") or ""),
        "pull_request_review_id": review_id,
        "commit_id": str(payload.get("commit_id") or ""),
        "diff_hunk": str(payload.get("diff_hunk") or ""),
    }


def thread_id_for(repo: str, number: int, comment_id: int) -> str:
    """The opaque thread id: on Gitea a review comment is its own thread
    (no reply, no resolve path, #1016 V1)."""
    return f"{repo}#{number}:{comment_id}"


def parse_thread_id(thread_id: str) -> tuple[str, int, int]:
    head, sep, comment = thread_id.rpartition(":")
    if not sep or not comment.isdigit():
        raise ValueError(f"not a Gitea thread id: {thread_id!r}")
    repo, number = parse_node_id(head)
    return repo, number, int(comment)


def review_thread(
    repo: str, number: int, comment: Mapping[str, Any], *, kind_of: KindOf
) -> ReviewThread | None:
    """One inline review comment as a one-comment thread; resolution is
    the read-only ``resolver`` Gitea sets from its web UI."""
    comment_id = comment.get("id")
    if not isinstance(comment_id, int):
        return None
    user = comment.get("user")
    login = str(user.get("login") or "") if isinstance(user, dict) else ""
    position = comment.get("position")
    return ReviewThread(
        thread_id=thread_id_for(repo, number, comment_id),
        is_resolved=comment.get("resolver") is not None,
        path=str(comment.get("path") or ""),
        line=int(position) if isinstance(position, int) and position > 0 else None,
        comments=(
            ThreadComment(
                comment_id=comment_id,
                login=login,
                body=str(comment.get("body") or ""),
                is_bot=kind_of(login),
            ),
        ),
    )


# -- statuses ------------------------------------------------------------------


def status_state(row: Mapping[str, Any]) -> str:
    """A status row's state: ``status`` on the combined endpoint's rows
    (field-verified), ``state`` where Gitea spells it GitHub's way."""
    return str(row.get("status") or row.get("state") or "").lower()


def fold_statuses(rows: Sequence[Any]) -> ChecksVerdict:
    """The combined status's rows (newest per context already) folded to
    one verdict: ``success`` passes, ``pending`` is pending, and anything
    else (``failure``, ``error``, ``warning``, or a word Gitea adds later)
    is red. Unknown words fail closed."""
    pending: list[str] = []
    failed: list[str] = []
    passed: list[str] = []
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        total += 1
        name = str(row.get("context") or "status")
        state = status_state(row)
        if state == "pending":
            pending.append(name)
        elif state == "success":
            passed.append(name)
        else:
            failed.append(name)
    if failed:
        verdict = "red"
    elif pending:
        verdict = "pending"
    else:
        verdict = "green"
    return ChecksVerdict(verdict, total, tuple(pending), tuple(failed), tuple(passed))  # type: ignore[arg-type]


def check_run_record(row: Mapping[str, Any]) -> dict[str, Any]:
    state = status_state(row)
    finished = state != "pending"
    return {
        "id": row.get("id"),
        "name": str(row.get("context") or "status"),
        "status": "completed" if finished else "in_progress",
        "conclusion": ("success" if state == "success" else "failure") if finished else None,
        "html_url": str(row.get("target_url") or ""),
        "description": str(row.get("description") or ""),
    }


# -- files and diffs -------------------------------------------------------------

_FILE_STATUSES = {
    "added": "added",
    "deleted": "removed",
    "renamed": "renamed",
    "changed": "modified",
}


def file_record(payload: Mapping[str, Any], patch: str = "") -> dict[str, Any]:
    """A changed file (``GET .../pulls/:n/files``) as the loop's file
    record; the payload's ``patch`` is null (field-verified), so the
    caller fills it from the pull request's ``.diff``."""
    status = str(payload.get("status") or "modified")
    return {
        "filename": str(payload.get("filename") or ""),
        "previous_filename": str(payload.get("previous_filename") or ""),
        "status": _FILE_STATUSES.get(status, status),
        "additions": int(payload.get("additions") or 0),
        "deletions": int(payload.get("deletions") or 0),
        "patch": patch,
    }


_DIFF_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$")


def split_diff(text: str) -> dict[str, str]:
    """A unified diff (``GET .../pulls/:n.diff``) split per file: the new
    path (the old one for a deletion) to its hunks, without the
    ``---``/``+++`` header, which is the part the commentable-range
    reader takes."""
    files: dict[str, str] = {}
    current: str | None = None
    hunks: list[str] = []
    in_hunks = False

    def flush() -> None:
        if current is not None:
            files[current] = "\n".join(hunks) + ("\n" if hunks else "")

    for line in text.split("\n"):
        header = _DIFF_HEADER.match(line)
        if header:
            flush()
            current = header.group(2) if header.group(2) != "/dev/null" else header.group(1)
            hunks = []
            in_hunks = False
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            in_hunks = True
        if in_hunks:
            hunks.append(line)
    flush()
    return {path: patch for path, patch in files.items() if path}


# -- commits -----------------------------------------------------------------


def commit_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``GET .../git/commits/:sha`` as the loop's commit record. The
    ``tree`` sha is the commit's own: Gitea lists a tree by the commit
    that holds it (field-verified), and the git tree id under
    ``commit.tree`` (the top-level ``tree`` is null) is not what the
    tree endpoint was exercised with."""
    sha = str(payload.get("sha") or "")
    if not sha:
        raise GithubOpsError(f"Gitea returned a commit without a sha: {payload!r}")
    commit_raw = payload.get("commit")
    commit: dict[str, Any] = commit_raw if isinstance(commit_raw, dict) else {}
    parents = payload.get("parents")
    return {
        "sha": sha,
        "tree": {"sha": sha},
        "parents": [{"sha": str(p.get("sha") or "")} for p in parents if isinstance(p, dict)]
        if isinstance(parents, list)
        else [],
        "message": str(commit.get("message") or ""),
        "html_url": str(payload.get("html_url") or ""),
    }
