"""GitLab payloads as the loop's records (#1017): pure folds, one per
payload shape, pinned by ``tests/unit/test_gl_ops.py`` against the shapes
GitLab CE 19.3 answered with in #1016. Nothing here talks to a forge.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sbxloop.vcs.model import ChecksVerdict


def iso_utc(value: Any) -> str:
    """A GitLab timestamp (``2026-09-12T21:31:48.478Z``) in the one form the
    daemon's claim protocol parses and compares, ``%Y-%m-%dT%H:%M:%SZ``:
    fractional seconds dropped, a ``+00:00`` offset spelt ``Z``. Anything
    that is not a UTC timestamp is passed through unchanged."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if text.endswith("+00:00"):
        text = text[: -len("+00:00")] + "Z"
    if text.endswith("Z") and "." in text:
        head, _, _ = text[:-1].partition(".")
        text = head + "Z"
    return text


def issue_state(payload: Mapping[str, Any]) -> str:
    """GitLab's ``opened``/``closed``/``locked`` as the loop's ``open``/``closed``."""
    state = str(payload.get("state") or "")
    return "closed" if state == "closed" else "open"


def user_record(user: Any) -> dict[str, Any]:
    """A GitLab ``author``/``user`` object as the loop's ``user``: the login
    and the id. No ``type``: the bot flag is not on an author object
    (field-verified, #1016 V3) and is looked up per user when a role
    needs it, so the kind stays ``None`` — unknown — rather than a guess."""
    if not isinstance(user, dict):
        return {"login": ""}
    record: dict[str, Any] = {"login": str(user.get("username") or "")}
    if isinstance(user.get("id"), int):
        record["id"] = user["id"]
    return record


def labels_record(labels: Any) -> list[dict[str, str]]:
    """GitLab's list of label names as the loop's ``labels[].name``."""
    if not isinstance(labels, list):
        return []
    return [{"name": str(name)} for name in labels if isinstance(name, str) and name]


def issue_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A GitLab issue as the loop's issue record. ``number`` is the
    project-scoped ``iid``, the number a person sees and types. There is
    no ``pull_request`` key, ever: GitLab issues are never merge requests
    (the consumers test the key's presence, #631). ``state_reason`` is
    ``None``: GitLab records no reason for a close."""
    return {
        "number": int(payload.get("iid") or 0),
        "id": payload.get("id"),
        "title": str(payload.get("title") or ""),
        "body": str(payload.get("description") or ""),
        "state": issue_state(payload),
        "state_reason": None,
        "html_url": str(payload.get("web_url") or ""),
        "labels": labels_record(payload.get("labels")),
        "user": user_record(payload.get("author")),
        "comments": int(payload.get("user_notes_count") or 0),
        "created_at": iso_utc(payload.get("created_at")),
        "updated_at": iso_utc(payload.get("updated_at")),
    }


def note_record(payload: Mapping[str, Any], *, issue_url: str = "") -> dict[str, Any]:
    """A GitLab note as the loop's comment record."""
    note_id = payload.get("id")
    return {
        "id": int(note_id) if isinstance(note_id, int) else 0,
        "body": str(payload.get("body") or ""),
        "user": user_record(payload.get("author")),
        "created_at": iso_utc(payload.get("created_at")),
        "html_url": f"{issue_url}#note_{note_id}" if issue_url and note_id is not None else "",
    }


def label_event_record(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """A GitLab resource label event as the loop's issue event: ``labeled``
    for ``add``, ``unlabeled`` for ``remove``; anything else is not an
    event the loop reads."""
    action = str(payload.get("action") or "")
    event = {"add": "labeled", "remove": "unlabeled"}.get(action)
    label = payload.get("label")
    if event is None or not isinstance(label, dict):
        return None
    return {
        "event": event,
        "label": {"name": str(label.get("name") or "")},
        "actor": user_record(payload.get("user")),
        "created_at": iso_utc(payload.get("created_at")),
    }


# GitLab access levels: 10 guest, 15 planner, 20 reporter, 30 developer,
# 40 maintainer, 50 owner. A Developer pushes (to unprotected branches)
# and opens merge requests, which is the loop's write level.
DEVELOPER = 30
MAINTAINER = 40
OWNER = 50


def access_level(payload: Mapping[str, Any]) -> int:
    """The token's highest access level on the project, from the
    ``permissions`` block a project read carries (field-verified, #1016
    V2: ``project_access.access_level`` for a Developer)."""
    permissions = payload.get("permissions")
    if not isinstance(permissions, dict):
        return 0
    levels = []
    for key in ("project_access", "group_access"):
        access = permissions.get(key)
        if isinstance(access, dict) and isinstance(access.get("access_level"), int):
            levels.append(int(access["access_level"]))
    return max(levels, default=0)


def repo_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A GitLab project as the loop's repository record.

    The merge-method flags are what the landing's ``allowed_merge_methods``
    reads: squash is allowed unless the project's ``squash_option`` is
    ``never``; a merge commit is allowed under GitLab's ``merge`` and
    ``rebase_merge`` methods; ``rebase`` (a fast-forward, no merge commit)
    is what GitLab's ``ff`` method does. ``permissions`` is the token's
    effective access as the doctor reads it (``push`` from Developer up).
    """
    merge_method = str(payload.get("merge_method") or "merge")
    squash = str(payload.get("squash_option") or "default_off")
    level = access_level(payload)
    issues = payload.get("issues_enabled")
    if not isinstance(issues, bool):
        issues = str(payload.get("issues_access_level") or "") not in ("disabled",)
    return {
        "id": payload.get("id"),
        "name": str(payload.get("path") or payload.get("name") or ""),
        "full_name": str(payload.get("path_with_namespace") or ""),
        "html_url": str(payload.get("web_url") or ""),
        "default_branch": payload.get("default_branch"),
        "private": str(payload.get("visibility") or "private") != "public",
        "has_issues": issues,
        "allow_squash_merge": squash != "never",
        "allow_merge_commit": merge_method in ("merge", "rebase_merge"),
        "allow_rebase_merge": merge_method == "ff",
        "permissions": {
            "admin": level >= OWNER,
            "maintain": level >= MAINTAINER,
            "push": level >= DEVELOPER,
            "pull": level > 0,
        },
    }


def label_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    color = str(payload.get("color") or "")
    return {
        "id": payload.get("id"),
        "name": str(payload.get("name") or ""),
        "color": color.removeprefix("#"),
        "description": str(payload.get("description") or ""),
    }


# A commit status on GitLab is a CI job or an external status, and its
# ``status`` is a job status. ``success`` passes; ``failed`` and
# ``canceled`` are red; ``skipped`` is not a red build (the same reading
# as GitHub's ``skipped`` conclusion); ``manual`` is a job waiting for a
# person to start it — like an unapproved workflow, neither red nor
# going to finish on its own — and everything that is still moving is
# pending. An unknown status fails closed as red. Field-verified on CE
# 19.3 (#1016 V2): ``running`` and ``success`` and ``failed`` as posted.
PASSING_STATUSES = frozenset({"success", "skipped"})
PENDING_STATUSES = frozenset(
    {"pending", "running", "created", "waiting_for_resource", "preparing", "scheduled"}
)
APPROVAL_STATUSES = frozenset({"manual"})
RED_STATUSES = frozenset({"failed", "canceled", "cancelled"})


def latest_statuses(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """One entry per status name: the commit's statuses list every
    pipeline that ran on the sha, oldest first, and a retried job appears
    twice. The newest (highest id) wins, as GitLab's own merge check
    reads it."""
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "status")
        previous = latest.get(name)
        if previous is None or int(row.get("id") or 0) >= int(previous.get("id") or 0):
            latest[name] = row
    return list(latest.values())


def fold_statuses(rows: Sequence[Any]) -> ChecksVerdict:
    """``GET /projects/:id/repository/commits/:sha/statuses`` folded to a
    verdict. A head with no statuses at all reads as ``green``: a project
    without CI must not deadlock the loop waiting for a report that will
    never come. A job GitLab lets fail (``allow_failure``) is not red:
    the pipeline passes with a warning and the merge is not held."""
    pending: list[str] = []
    failed: list[str] = []
    passed: list[str] = []
    approval: list[str] = []
    entries = latest_statuses(rows)
    for row in entries:
        name = str(row.get("name") or "status")
        status = str(row.get("status") or "").lower()
        if status in PASSING_STATUSES:
            passed.append(name)
        elif status in PENDING_STATUSES:
            pending.append(name)
        elif status in APPROVAL_STATUSES:
            approval.append(name)
        elif status in RED_STATUSES and row.get("allow_failure") is True:
            passed.append(name)
        else:
            failed.append(name)
    total = len(entries)
    if failed:
        return ChecksVerdict(
            "red", total, tuple(pending), tuple(failed), tuple(passed), tuple(approval)
        )
    if pending or approval:
        return ChecksVerdict("pending", total, tuple(pending), (), tuple(passed), tuple(approval))
    return ChecksVerdict("green", total, (), (), tuple(passed))


def check_run_record(row: Mapping[str, Any]) -> dict[str, Any]:
    """A commit status as the loop's check-run row (name, status,
    conclusion, url) — what the concierge summarises."""
    status = str(row.get("status") or "").lower()
    finished = status not in PENDING_STATUSES
    conclusion: str | None
    if not finished:
        conclusion = None
    elif status in PASSING_STATUSES:
        conclusion = "success" if status == "success" else "skipped"
    elif status in APPROVAL_STATUSES:
        conclusion = "action_required"
    elif status in RED_STATUSES and row.get("allow_failure") is True:
        conclusion = "neutral"
    elif status in RED_STATUSES:
        conclusion = "failure" if status == "failed" else "cancelled"
    else:
        conclusion = status or "failure"
    return {
        "id": row.get("id"),
        "name": str(row.get("name") or "status"),
        "status": "completed" if finished else "in_progress",
        "conclusion": conclusion,
        "html_url": str(row.get("target_url") or ""),
        "description": str(row.get("description") or ""),
    }
