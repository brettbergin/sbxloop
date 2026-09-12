"""GitLab merge requests and their discussions in the loop's records
(#1018): pure folds from the payload shapes GitLab CE 19.3 answered with
in #1016 into the change record, the file record, the review records and
:class:`~sbxloop.vcs.model.ReviewThread`.

A merge request's ``detailed_merge_status`` is the one field that carries
what GitHub spreads over ``mergeable`` and ``mergeable_state``: the loop's
landing reads ``mergeable`` as a tri-state (``None`` while the forge is
still deciding) and ``mergeable_state`` as ``behind`` / ``blocked`` /
``draft`` / clean, and the mapping below is what those mean on GitLab
(field-verified values: ``preparing``, ``checking``, ``mergeable``,
``ci_must_pass``, ``ci_still_running``, ``draft_status``,
``discussions_not_resolved``, ``requested_changes``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sbxloop.vcs.gitlab.records import iso_utc, user_record
from sbxloop.vcs.model import ReviewThread, ThreadComment

# GitLab is still deciding: the landing ticks and reads again.
_DECIDING = frozenset({"", "checking", "preparing", "unchecked"})
# Where a merge request stands, in the loop's mergeable_state words.
_MERGE_STATES: dict[str, str] = {
    "mergeable": "clean",
    "need_rebase": "behind",
    "conflict": "dirty",
    "draft_status": "draft",
}


def merge_state(detailed: str) -> tuple[bool | None, str]:
    """``(mergeable, mergeable_state)`` from ``detailed_merge_status``: not
    known yet while GitLab is deciding; a real conflict is not mergeable;
    everything else is mergeable in principle and the state says what
    still holds it (``blocked`` for a rule the loop reads elsewhere: a red
    pipeline, an unresolved discussion, a requested change, a missing
    approval)."""
    if detailed in _DECIDING:
        return None, ""
    if detailed == "conflict":
        return False, "dirty"
    return True, _MERGE_STATES.get(detailed, "blocked")


def change_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A merge request as the loop's change record. ``number`` is the
    ``iid``; ``node_id`` is ``<project id>!<iid>``, what the operations
    that take only a node id (un-draft, enqueue) address the merge request
    by; ``merged`` is GitLab's ``merged`` state and ``state`` reads
    ``closed`` for both a closed and a merged request, the way the landing
    checks ``merged`` first."""
    state = str(payload.get("state") or "opened")
    merged = state == "merged"
    detailed = str(payload.get("detailed_merge_status") or "")
    mergeable, mergeable_state = merge_state(detailed)
    refs = payload.get("diff_refs")
    refs = refs if isinstance(refs, dict) else {}
    reviewers = payload.get("reviewers")
    return {
        "number": int(payload.get("iid") or 0),
        "id": payload.get("id"),
        "node_id": f"{payload.get('project_id')}!{payload.get('iid')}",
        "html_url": str(payload.get("web_url") or ""),
        "title": str(payload.get("title") or ""),
        "body": str(payload.get("description") or ""),
        "state": "closed" if state in ("closed", "merged") else "open",
        "merged": merged,
        "merge_commit_sha": payload.get("merge_commit_sha") or payload.get("squash_commit_sha"),
        "draft": bool(payload.get("draft")),
        "head": {
            "sha": str(payload.get("sha") or ""),
            "ref": str(payload.get("source_branch") or ""),
        },
        "base": {
            "ref": str(payload.get("target_branch") or ""),
            "sha": str(refs.get("base_sha") or ""),
        },
        "user": user_record(payload.get("author")),
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "detailed_merge_status": detailed,
        "requested_reviewers": [user_record(r) for r in reviewers]
        if isinstance(reviewers, list)
        else [],
        "created_at": iso_utc(payload.get("created_at")),
        "updated_at": iso_utc(payload.get("updated_at")),
    }


def file_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    """One entry of a merge request's diffs as the loop's file record:
    the name, GitHub's status words, the counts, and the unified patch
    (GitLab serves the hunks without the ``---``/``+++`` header, which is
    the part the commentable-range reader needs)."""
    diff = str(payload.get("diff") or "")
    if payload.get("new_file"):
        status = "added"
    elif payload.get("deleted_file"):
        status = "removed"
    elif payload.get("renamed_file"):
        status = "renamed"
    else:
        status = "modified"
    lines = diff.split("\n")
    return {
        "filename": str(payload.get("new_path") or payload.get("old_path") or ""),
        "previous_filename": str(payload.get("old_path") or ""),
        "status": status,
        "additions": sum(
            1 for line in lines if line.startswith("+") and not line.startswith("+++")
        ),
        "deletions": sum(
            1 for line in lines if line.startswith("-") and not line.startswith("---")
        ),
        "patch": diff,
    }


KindOf = Callable[[Any], bool | None]


def _typed_user(user: Any, kind_of: KindOf) -> dict[str, Any]:
    """A user record with ``type`` filled from the per-user bot lookup
    (#1016 V3) when the lookup can answer; left out otherwise, so the
    identity model reads the kind as unknown rather than human."""
    record = user_record(user)
    kind = kind_of(user)
    if kind is not None:
        record["type"] = "Bot" if kind else "User"
    return record


def review_records(
    approvals: Mapping[str, Any] | None,
    reviewers: Sequence[Any],
    *,
    kind_of: KindOf,
) -> list[dict[str, Any]]:
    """The merge request's standing verdicts as the loop's review records:
    one ``APPROVED`` per approver (``GET .../approvals``, ``approved_by``),
    then one ``CHANGES_REQUESTED`` per reviewer whose state is
    ``requested_changes`` (``GET .../reviewers``), last so it wins the
    per-reviewer fold. ``id`` is stable per reviewer and verdict, which
    the objection keys the loop persists across rounds need. A review on
    GitLab carries no body of its own; the reviewer's notes are the
    comments."""
    records: list[dict[str, Any]] = []
    approved_by = approvals.get("approved_by") if isinstance(approvals, dict) else None
    for entry in approved_by if isinstance(approved_by, list) else []:
        user = entry.get("user") if isinstance(entry, dict) else None
        if not isinstance(user, dict):
            continue
        records.append(
            {
                "id": f"approval-{user.get('id')}",
                "user": _typed_user(user, kind_of),
                "state": "APPROVED",
                "body": "",
                "submitted_at": iso_utc(entry.get("approved_at")),
            }
        )
    for entry in reviewers:
        if not isinstance(entry, dict):
            continue
        user = entry.get("user")
        state = str(entry.get("state") or "")
        if not isinstance(user, dict) or state != "requested_changes":
            continue
        records.append(
            {
                "id": f"reviewer-{user.get('id')}-requested_changes",
                "user": _typed_user(user, kind_of),
                "state": "CHANGES_REQUESTED",
                "body": "",
                "submitted_at": iso_utc(entry.get("updated_at")),
            }
        )
    return records


def note_position(note: Mapping[str, Any]) -> tuple[str, int | None]:
    """The path and line a diff note anchors on: the new side when it has
    one, else the old side (a note on a deleted line)."""
    position = note.get("position")
    position = position if isinstance(position, dict) else {}
    path = str(position.get("new_path") or position.get("old_path") or "")
    line = position.get("new_line")
    if line is None:
        line = position.get("old_line")
    return path, int(line) if isinstance(line, int) else None


def is_diff_discussion(discussion: Mapping[str, Any]) -> bool:
    """Whether a discussion is an inline thread on the diff: its first
    note is a ``DiffNote`` (field-verified shape, #1016 V1)."""
    notes = discussion.get("notes")
    first = notes[0] if isinstance(notes, list) and notes else None
    return isinstance(first, dict) and first.get("type") == "DiffNote" and not first.get("system")


def thread_id_for(repo: str, number: int, discussion_id: str) -> str:
    """The opaque thread id the loop carries: enough to address the
    discussion again with nothing but the id."""
    return f"{repo}!{number}:{discussion_id}"


def parse_thread_id(thread_id: str) -> tuple[str, int, str]:
    """``(repo, number, discussion_id)`` from :func:`thread_id_for`."""
    head, sep, discussion = thread_id.rpartition(":")
    repo, bang, number = head.rpartition("!")
    if not sep or not bang or not number.isdigit() or not discussion:
        raise ValueError(f"not a GitLab thread id: {thread_id!r}")
    return repo, int(number), discussion


def review_thread(
    repo: str, number: int, discussion: Mapping[str, Any], *, kind_of: KindOf
) -> ReviewThread | None:
    """One diff discussion as a :class:`ReviewThread`; ``None`` for a
    discussion that is not an inline thread (a plain note, a system
    note). Resolution is the root note's ``resolved`` flag."""
    if not is_diff_discussion(discussion):
        return None
    discussion_id = str(discussion.get("id") or "")
    notes = discussion.get("notes")
    if not discussion_id or not isinstance(notes, list):
        return None
    root = notes[0]
    path, line = note_position(root)
    comments: list[ThreadComment] = []
    for note in notes:
        if not isinstance(note, dict) or note.get("system"):
            continue
        note_id = note.get("id")
        author = note.get("author")
        comments.append(
            ThreadComment(
                comment_id=int(note_id) if isinstance(note_id, int) else None,
                login=str(author.get("username") or "") if isinstance(author, dict) else "",
                body=str(note.get("body") or ""),
                is_bot=kind_of(author),
            )
        )
    return ReviewThread(
        thread_id=thread_id_for(repo, number, discussion_id),
        is_resolved=bool(root.get("resolved")),
        path=path,
        line=line,
        comments=tuple(comments),
    )


def review_comment_records(discussions: Sequence[Any], *, kind_of: KindOf) -> list[dict[str, Any]]:
    """Every inline note across the diff discussions as the loop's review
    comment record (``id``, ``user``, ``body``, ``path``, ``line``)."""
    records: list[dict[str, Any]] = []
    for discussion in discussions:
        if not isinstance(discussion, dict) or not is_diff_discussion(discussion):
            continue
        for note in discussion.get("notes") or []:
            if not isinstance(note, dict) or note.get("system"):
                continue
            path, line = note_position(note)
            records.append(
                {
                    "id": note.get("id"),
                    "user": _typed_user(note.get("author"), kind_of),
                    "body": str(note.get("body") or ""),
                    "path": path,
                    "line": line,
                    "original_line": line,
                    "created_at": iso_utc(note.get("created_at")),
                }
            )
    return records
