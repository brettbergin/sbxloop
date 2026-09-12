"""The GitLab backend: every role in :mod:`sbxloop.vcs.protocol`, answered
against the GitLab REST API v4 through ``vcs.op`` jobs (#1017).

What the roles answer in is the shared vocabulary of
:mod:`sbxloop.vcs.model`: an operation that returns a record (an issue, a
repository, a comment) returns the keys the engine and the daemon read,
spelt as the first backend spelt them (``html_url``, ``labels[].name``,
``user.login``, ``state`` as ``open``/``closed``), never GitLab's own
payload. The folds below are the whole translation; ``tests/unit/test_gl_ops.py``
pins each one against the shapes GitLab CE 19.3 answered with in #1016.

Field-verified against GitLab CE 19.3.2 (#1016) where a docstring says
so. Everything else is GitLab's documented API, labelled
**field-unverified** where it is load-bearing.

The roles that write — :class:`~sbxloop.vcs.protocol.ChangeOps`,
:class:`~sbxloop.vcs.protocol.ReviewOps` and
:class:`~sbxloop.vcs.protocol.ContentOps` — raise
:class:`~sbxloop.errors.RoleNotImplemented` here; they land in the
issues that follow #1017, and until then a run on a GitLab repository
fails closed at its first write, naming the operation.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

from sbxloop.config import MergeMethod
from sbxloop.errors import GithubOpsError, RoleNotImplemented
from sbxloop.log import get_logger
from sbxloop.vcs.gitlab.permissions import READ_PROBES
from sbxloop.vcs.gitlab.protection import read_base_requirements
from sbxloop.vcs.jobs import JobBackend
from sbxloop.vcs.model import (
    BaseRequirements,
    ChecksVerdict,
    CloseReason,
    FailedCheck,
    Identity,
    IssueRef,
    MergeOutcome,
    PostedFinding,
    PrRef,
    QueueEntry,
    QueueState,
    ReviewComment,
    ReviewEvent,
    ReviewThread,
    ReviewVerdict,
    SubmittedReview,
)
from sbxloop.vcs.protocol import Capability, VcsOps
from sbxloop.vcs.query import parse_issue_query
from sbxloop_worker.protocol import TransportSpec

log = get_logger(__name__)

#: The variable the sandbox holds the GitLab token in. A name the
#: transport descriptor carries (#1015); the host's own variable is
#: ``[vcs] token_env``, and the provisioner writes the value under this
#: name inside the box whatever the host calls it.
SANDBOX_TOKEN_ENV = "GITLAB_TOKEN"  # nosec B105 - a variable name, not a value


def gitlab_transport(api_url: str, *, token_env: str = SANDBOX_TOKEN_ENV) -> TransportSpec:
    """The descriptor every job to the GitLab backend carries (#1015):
    the API root (``https://gitlab.example.com/api/v4``), the token as
    ``PRIVATE-TOKEN``, lists paged by GitLab's ``X-Next-Page``, a plain
    JSON ``Accept`` and no API-version header, and the ``gh`` CLI refused.
    Field-verified on CE 19.3: every read in #1016 rode ``PRIVATE-TOKEN``."""
    return TransportSpec(
        api_url=api_url,
        auth="private-token",
        pagination="x-next-page",
        accept="application/json",
        api_version_header=None,
        api_version=None,
        token_env=[token_env],
        gh_cli=False,
    )


# -- folds -----------------------------------------------------------------
#
# GitLab's payloads into the loop's records. Each is a pure function of
# one payload, so a test can pin it against a captured shape.


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


def _clip_head_tail(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n...(clipped {dropped} chars)...\n{text[len(text) - tail :]}"


# The GitLab status-API states the loop posts, from the four the Status
# API vocabulary of ``status_create`` has always taken.
_STATUS_STATES = {
    "pending": "pending",
    "success": "success",
    "failure": "failed",
    "error": "failed",
    "running": "running",
    "canceled": "canceled",
}


class GitlabOps(JobBackend):
    KIND = "gitlab"
    LABEL = "gitlab"

    # The verified matrix for GitLab CE 19.3 (#1016). ``merge_queue`` is
    # a paid tier and a per-project setting: the free tier has none
    # (verified) and a Premium instance is field-unverified, so the
    # backend cannot decide from the class alone — the landing probes
    # the project (#1019). ``request_changes_review`` is UNSUPPORTED
    # because CE records the state and does not enforce it: reading a
    # recorded state as a merge gate would wait on something the forge
    # ignores. ``signed_api_commits`` is the default install's answer;
    # an instance with signing configured is field-unverified.
    CAPABILITIES: ClassVar[dict[str, Capability]] = {
        "merge_queue": Capability.UNKNOWN,
        "review_threads": Capability.SUPPORTED,
        "draft_changes": Capability.SUPPORTED,
        "request_changes_review": Capability.UNSUPPORTED,
        "short_lived_token": Capability.UNSUPPORTED,
        "remote_commit": Capability.SUPPORTED,
        "required_checks_introspection": Capability.SUPPORTED,
        "bot_identity": Capability.SUPPORTED,
        "signed_api_commits": Capability.UNSUPPORTED,
    }
    CAPABILITY_NOTE: ClassVar[str] = (
        "merge trains are a paid tier and a per-project setting; the landing asks the project"
    )
    #: The roles this backend does not answer yet; every operation on them
    #: raises :class:`RoleNotImplemented`, and the doctor lists them.
    UNIMPLEMENTED_ROLES: ClassVar[tuple[str, ...]] = ("ChangeOps", "ReviewOps", "ContentOps")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The project read once per repository per backend object: its
        # id, web address and default branch serve several operations.
        self._projects: dict[str, dict[str, Any]] = {}
        # Which issue a note belongs to, learnt from the reads and writes
        # this object made: GitLab deletes a note under its issue, and the
        # role names only the note (#1017).
        self._note_issue: dict[int, int] = {}
        # The bot flag per user id (#1016 V3), one lookup per user per object.
        self._bots: dict[int, bool | None] = {}

    # -- paths ------------------------------------------------------------------

    @staticmethod
    def _project(repo: str) -> str:
        return f"/projects/{quote(repo, safe='')}"

    def _project_payload(self, repo: str) -> dict[str, Any]:
        if repo not in self._projects:
            path = self._project(repo)
            self._projects[repo] = self._dict(f"GET {path}", self.raw("GET", path))
        return self._projects[repo]

    def _web_url(self, repo: str) -> str:
        return str(self._project_payload(repo).get("web_url") or "")

    def _unimplemented(self, role: str, operation: str) -> RoleNotImplemented:
        return RoleNotImplemented(self.KIND, role, operation)

    # -- what this backend can do --------------------------------------------

    def capabilities(self) -> dict[str, Capability]:
        return dict(self.CAPABILITIES)

    # -- RepoOps -------------------------------------------------------------

    def repo_get(self, repo: str) -> dict[str, Any]:
        """The project as the loop's repository record."""
        payload = self._project_payload(repo)
        return repo_record(payload)

    def repo_lookup(self, repo: str) -> dict[str, Any] | None:
        """The repository record, or ``None`` when GitLab answers 404 — a
        project that does not exist, or one this token cannot see (GitLab
        hides a private project's existence with a 404, field-verified)."""
        path = self._project(repo)
        data = self.raw_lookup("GET", path)
        if data is None:
            return None
        payload = self._dict(f"GET {path}", data)
        self._projects[repo] = payload
        return repo_record(payload)

    def repo_create(
        self, repo: str, *, private: bool = True, for_user: bool = False
    ) -> dict[str, Any]:
        """Create ``namespace/path`` with an initial commit: under the
        token's own namespace (``for_user``) or the group named by the
        namespace. **Field-unverified**: #1016 created its project as the
        administrator; a Developer needs the group's create-project
        permission."""
        namespace, name = repo.split("/", 1)
        body: dict[str, Any] = {
            "name": name,
            "path": name,
            "visibility": "private" if private else "public",
            "initialize_with_readme": True,
            "default_branch": "main",
        }
        if not for_user:
            group = self._dict(
                f"GET /groups/{namespace}", self.raw("GET", f"/groups/{quote(namespace, safe='')}")
            )
            body["namespace_id"] = group.get("id")
        payload = self._dict("POST /projects", self.raw("POST", "/projects", body))
        self._projects[repo] = payload
        return repo_record(payload)

    def default_branch(self, repo: str) -> str:
        name = self._project_payload(repo).get("default_branch")
        if not isinstance(name, str) or not name:
            raise GithubOpsError(
                f"GitLab did not report a default branch for {repo}; "
                "set [github] deliver_base to name the branch to deliver against"
            )
        return name

    def ref_lookup(self, repo: str, ref: str) -> str | None:
        """``heads/<branch>`` or ``tags/<tag>`` to its commit sha, or
        ``None`` when there is no such ref. GitLab answers 404 for a
        missing branch and for a project with no commits at all."""
        if ref.startswith("heads/"):
            path = f"{self._project(repo)}/repository/branches/{quote(ref[6:], safe='')}"
        elif ref.startswith("tags/"):
            path = f"{self._project(repo)}/repository/tags/{quote(ref[5:], safe='')}"
        else:
            raise GithubOpsError(f"ref_lookup needs heads/<branch> or tags/<tag>, got {ref!r}")
        data = self.raw_lookup("GET", path)
        if data is None:
            return None
        payload = self._dict(f"GET {path}", data)
        commit = payload.get("commit")
        sha = commit.get("id") if isinstance(commit, dict) else None
        if not sha:
            raise GithubOpsError(f"GET {path} returned no commit sha for {ref!r}: {payload!r}")
        return str(sha)

    def branch_delete(self, repo: str, branch: str) -> None:
        """Delete a branch, tolerating one already gone (404) and one the
        project protects (403): neither is a failure of the merge that
        just succeeded."""
        self.raw_lookup(
            "DELETE",
            f"{self._project(repo)}/repository/branches/{quote(branch, safe='')}",
            missing=(404, 403),
        )

    def merge_base(self, repo: str, base: str, head: str) -> str | None:
        """The merge base of ``base`` and ``head``
        (``GET .../repository/merge_base``), or ``None`` when GitLab
        cannot name one (unrelated histories, a ref it cannot resolve)."""
        query = urlencode({"refs[]": [base, head]}, doseq=True)
        data = self.raw_lookup(
            "GET", f"{self._project(repo)}/repository/merge_base?{query}", missing=(404, 400)
        )
        sha = data.get("id") if isinstance(data, dict) else None
        return str(sha) if sha else None

    def compare_lookup(self, repo: str, base: str, head: str) -> dict[str, Any] | None:
        """GitLab's comparison of ``base`` with ``head``, with the merge
        base the loop reads under ``merge_base_commit`` (GitLab reports
        it from its own endpoint, not on the comparison); ``None`` when
        either read answers 404."""
        query = urlencode({"from": base, "to": head})
        data = self.raw_lookup("GET", f"{self._project(repo)}/repository/compare?{query}")
        if data is None:
            return None
        payload = self._dict(f"GET {self._project(repo)}/repository/compare", data)
        merge_base = self.merge_base(repo, base, head)
        return {
            "merge_base_commit": {"sha": merge_base} if merge_base else None,
            "status": "identical" if payload.get("compare_same_ref") else "ahead",
            "html_url": str(payload.get("web_url") or ""),
            "commits": payload.get("commits") if isinstance(payload.get("commits"), list) else [],
        }

    def contents_read(self, repo: str, path: str, ref: str | None = None) -> str:
        """One file's text at ``ref`` (the default branch when unset);
        binary content comes back base64-encoded, as the GitHub op
        answers it."""
        target = f"{self._project(repo)}/repository/files/{quote(path, safe='')}"
        if ref:
            target += f"?{urlencode({'ref': ref})}"
        else:
            target += f"?{urlencode({'ref': self.default_branch(repo)})}"
        payload = self._dict(f"GET {target}", self.raw("GET", target))
        content = payload.get("content")
        if not isinstance(content, str):
            return ""
        raw = base64.b64decode(content)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(raw).decode()

    # -- IssueOps -------------------------------------------------------------

    def _issue_path(self, repo: str, number: int | str) -> str:
        return f"{self._project(repo)}/issues/{number}"

    def issue_create(
        self,
        repo: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> IssueRef:
        request: dict[str, Any] = {"title": title}
        if body:
            request["description"] = body
        if labels:
            request["labels"] = ",".join(labels)
        path = f"{self._project(repo)}/issues"
        payload = self._dict(f"POST {path}", self.raw("POST", path, request))
        record = issue_record(payload)
        return IssueRef(number=record["number"], url=record["html_url"])

    def issue_get(self, repo: str, number: int | str) -> dict[str, Any]:
        path = self._issue_path(repo, number)
        return issue_record(self._dict(f"GET {path}", self.raw("GET", path)))

    def issue_comment(self, repo: str, number: int, body: str) -> str:
        path = f"{self._issue_path(repo, number)}/notes"
        payload = self._dict(f"POST {path}", self.raw("POST", path, {"body": body}))
        record = note_record(payload, issue_url=f"{self._web_url(repo)}/-/issues/{number}")
        if record["id"]:
            self._note_issue[int(record["id"])] = int(number)
        return str(record["html_url"])

    def issue_comments(self, repo: str, number: int | str) -> list[Any]:
        """Every comment a person or the loop left on the issue, oldest
        first; GitLab's system notes (a label added, the issue closed)
        are not comments and are left out."""
        issue_url = f"{self._web_url(repo)}/-/issues/{number}"
        rows = self.raw_pages(
            f"{self._issue_path(repo, number)}/notes?sort=asc&order_by=created_at"
        )
        records: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("system"):
                continue
            record = note_record(row, issue_url=issue_url)
            if record["id"]:
                self._note_issue[int(record["id"])] = int(number)
            records.append(record)
        return records

    def issue_comment_delete(
        self, repo: str, comment_id: int, *, number: int | None = None
    ) -> None:
        """Delete one comment. GitLab addresses a note under its issue;
        ``number`` names it, or the backend remembers it from the read or
        write that produced the id — a note this object never saw cannot
        be deleted, and says so."""
        issue = number if number is not None else self._note_issue.get(int(comment_id))
        if issue is None:
            raise GithubOpsError(
                f"cannot delete note {comment_id}: GitLab addresses a note under its issue, "
                "and this backend was not told which issue it is on"
            )
        self.raw("DELETE", f"{self._issue_path(repo, issue)}/notes/{comment_id}")

    def issue_events(self, repo: str, number: int | str) -> list[Any]:
        """The issue's label events (``labeled`` / ``unlabeled``), oldest
        first, from GitLab's resource label events."""
        rows = self.raw_pages(f"{self._issue_path(repo, number)}/resource_label_events")
        events = [label_event_record(row) for row in rows if isinstance(row, dict)]
        return [event for event in events if event is not None]

    def issues_list(
        self,
        repo: str,
        *,
        state: str = "open",
        labels: Sequence[str] = (),
        per_page: int = 100,
        page: int = 1,
        sort: str = "",
        direction: str = "",
    ) -> list[Any]:
        """One page of the project's issues (never merge requests: GitLab
        keeps them apart), filtered by ``state`` and, when given, every
        label in ``labels``."""
        params: dict[str, Any] = {
            "state": {"open": "opened", "closed": "closed"}.get(state, "all"),
            "per_page": per_page,
            "page": page,
        }
        if labels:
            params["labels"] = ",".join(labels)
        if sort:
            params["order_by"] = {"updated": "updated_at", "created": "created_at"}.get(
                sort, "created_at"
            )
        if direction:
            params["sort"] = direction
        path = f"{self._project(repo)}/issues?{urlencode(params)}"
        rows = self._list(f"GET {self._project(repo)}/issues", self.raw("GET", path))
        return [issue_record(row) for row in rows if isinstance(row, dict)]

    def issue_search(self, query: str, *, per_page: int) -> dict[str, Any]:
        """The follow-up lookup's search, answered from the project's issue
        search. ``total_count`` is what was read; a page as long as
        ``per_page`` may not be the whole answer, and says so with
        ``incomplete_results`` — the caller refuses an answer it cannot
        know to be whole."""
        asked = parse_issue_query(query)
        if asked.repo is None:
            raise GithubOpsError("issue search on GitLab needs a repo: qualifier")
        params: dict[str, Any] = {
            "scope": "issues",
            "search": " ".join(asked.terms),
            "per_page": per_page,
        }
        if asked.state in ("open", "closed"):
            params["state"] = "opened" if asked.state == "open" else "closed"
        path = f"{self._project(asked.repo)}/search?{urlencode(params)}"
        rows = self._list(f"GET {self._project(asked.repo)}/search", self.raw("GET", path))
        items = [issue_record(row) for row in rows if isinstance(row, dict)]
        return {
            "items": items,
            "total_count": len(items),
            "incomplete_results": len(items) >= per_page,
        }

    def search_issues(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
        """The daemon's labelled-issue poll, answered from the issue list:
        the query's ``repo:``, ``is:open`` and ``label:`` qualifiers become
        list parameters, so merge requests never appear."""
        asked = parse_issue_query(query)
        if asked.repo is None:
            raise GithubOpsError("issue search on GitLab needs a repo: qualifier")
        if asked.kind != "issue":
            raise GithubOpsError("the GitLab backend searches issues only")
        rows = self.issues_list(
            asked.repo, state=asked.state, labels=asked.labels, per_page=per_page, page=1
        )
        if asked.terms:
            needle = " ".join(asked.terms).lower()
            rows = [
                row
                for row in rows
                if needle in f"{row.get('title', '')} {row.get('body', '')}".lower()
            ]
        return [row for row in rows if isinstance(row, dict)]

    def issue_labels_add(self, repo: str, number: int | str, labels: Sequence[str]) -> None:
        self.raw("PUT", self._issue_path(repo, number), {"add_labels": ",".join(labels)})

    def issue_label_remove(self, repo: str, number: int | str, label: str) -> None:
        """Take ``label`` off the issue; one that is not there is a success."""
        self.raw("PUT", self._issue_path(repo, number), {"remove_labels": label})

    def issue_close(
        self, repo: str, number: int | str, *, reason: CloseReason = "completed"
    ) -> None:
        """Close the issue. GitLab records no reason; ``reason`` is dropped."""
        self.raw("PUT", self._issue_path(repo, number), {"state_event": "close"})

    def label_lookup(self, repo: str, name: str) -> dict[str, Any] | None:
        """One project label by exact name, or ``None``: GitLab's label list
        searches by substring, so the answer is matched here."""
        path = f"{self._project(repo)}/labels?{urlencode({'search': name, 'per_page': 100})}"
        rows = self._list(f"GET {self._project(repo)}/labels", self.raw("GET", path))
        for row in rows:
            if isinstance(row, dict) and str(row.get("name") or "").casefold() == name.casefold():
                return label_record(row)
        return None

    def label_create(self, repo: str, *, name: str, color: str, description: str) -> dict[str, Any]:
        """Create a project label; one that exists is GitLab's 409, raised
        with its ``Label already exists`` message for the caller to read."""
        path = f"{self._project(repo)}/labels"
        body = {"name": name, "color": f"#{color.removeprefix('#')}", "description": description}
        return label_record(self._dict(f"POST {path}", self.raw("POST", path, body)))

    def labels_list(self, repo: str) -> list[Any]:
        rows = self.raw_pages(f"{self._project(repo)}/labels")
        return [label_record(row) for row in rows if isinstance(row, dict)]

    # -- ChecksOps -------------------------------------------------------------

    def _statuses(self, repo: str, sha: str) -> list[Any]:
        return self.raw_pages(f"{self._project(repo)}/repository/commits/{sha}/statuses")

    def pr_checks(self, repo: str, sha: str) -> ChecksVerdict:
        """Every commit status on ``sha`` — CI jobs and external statuses
        alike, the one namespace GitLab keeps — folded to one verdict."""
        return fold_statuses(self._statuses(repo, sha))

    def check_runs(self, repo: str, sha: str) -> list[Any]:
        return [check_run_record(row) for row in latest_statuses(self._statuses(repo, sha))]

    def checks_failed_logs(
        self, repo: str, sha: str, *, max_chars: int = 6000
    ) -> list[FailedCheck]:
        """The red statuses on ``sha``, each with what explains it: a CI
        job's trace (``GET .../jobs/:id/trace``, clipped head+tail) when the
        status is a job of this project's pipeline, else the status's own
        description. A commit status's ``id`` is the job id for a pipeline
        job — GitLab's status rows are its job rows — **field-unverified**
        beyond the API's documented shape (#1016 posted external statuses,
        which have no trace and answer 404, the fallback here)."""
        head = min(1500, max_chars)
        tail = max_chars - head
        failed: list[FailedCheck] = []
        for row in latest_statuses(self._statuses(repo, sha)):
            record = check_run_record(row)
            if record["conclusion"] in (None, "success", "skipped", "neutral", "action_required"):
                continue
            excerpt = ""
            job_id = row.get("id")
            if isinstance(job_id, int):
                trace = self.raw_text(
                    "GET", f"{self._project(repo)}/jobs/{job_id}/trace", missing=(404, 403)
                )
                excerpt = trace or ""
            if not excerpt.strip():
                excerpt = str(record["description"] or "")
            failed.append(
                FailedCheck(
                    name=record["name"],
                    conclusion=str(record["conclusion"]),
                    excerpt=_clip_head_tail(excerpt, head, tail),
                    url=str(record["html_url"]),
                )
            )
        return failed

    def pr_required_checks(self, repo: str, number: int) -> tuple[str, ...]:
        """GitLab has no per-change rollup that names required checks: the
        base's rules say whether the whole pipeline is required (#1016
        V2). Raised, so the caller keeps its requirements as read rather
        than treating an empty rollup as an answer."""
        raise GithubOpsError(
            "GitLab reports no per-change list of required checks; the base's rules "
            "say whether the pipeline must succeed"
        )

    def status_create(
        self,
        repo: str,
        sha: str,
        state: str,
        *,
        context: str = "sbxloop",
        description: str = "",
        target_url: str = "",
    ) -> None:
        body: dict[str, Any] = {"state": _STATUS_STATES.get(state, state), "name": context}
        if description:
            body["description"] = description
        if target_url:
            body["target_url"] = target_url
        self.raw("POST", f"{self._project(repo)}/statuses/{sha}", body)

    def workflows_list(self, repo: str) -> list[Any]:
        """GitLab has one pipeline definition, ``.gitlab-ci.yml``: listed
        as an active workflow when the default branch carries it."""
        try:
            base = self.default_branch(repo)
        except GithubOpsError:
            return []
        target = (
            f"{self._project(repo)}/repository/files/{quote('.gitlab-ci.yml', safe='')}"
            f"?{urlencode({'ref': base})}"
        )
        if self.raw_lookup("HEAD", target) is None:
            return []
        return [{"name": ".gitlab-ci.yml", "state": "active", "path": ".gitlab-ci.yml"}]

    def workflow_runs(self, repo: str, *, branch: str, per_page: int = 1) -> list[Any]:
        """The latest pipelines on ``branch``, newest first."""
        path = f"{self._project(repo)}/pipelines?{urlencode({'ref': branch, 'per_page': per_page})}"
        rows = self._list(f"GET {self._project(repo)}/pipelines", self.raw("GET", path))
        runs: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "")
            finished = status not in PENDING_STATUSES
            runs.append(
                {
                    "id": row.get("id"),
                    "name": "pipeline",
                    "status": "completed" if finished else "in_progress",
                    "conclusion": status if finished else None,
                    "html_url": str(row.get("web_url") or ""),
                    "head_sha": str(row.get("sha") or ""),
                }
            )
        return runs

    # -- PolicyOps ------------------------------------------------------------

    def base_requirements(self, repo: str, base: str) -> BaseRequirements:
        """What ``base`` requires before a merge, read from the protected
        branch, the project's merge settings and the instance's edition
        (:func:`read_base_requirements`); never raises."""
        return read_base_requirements(self, repo, base)

    def rate_limit(self) -> dict[str, Any]:
        """GitLab has no rate-limit endpoint; the cheapest authenticated
        read is the instance version, which is what the health check
        wants."""
        return self._dict("GET /version", self.raw("GET", "/version"))

    def authenticated_user(self) -> dict[str, Any]:
        """The token's own account: ``login`` and, from the ``bot`` flag
        GitLab puts on the user (field-verified, #1016 V3), ``type``."""
        payload = self._dict("GET /user", self.raw("GET", "/user"))
        record = user_record(payload)
        bot = payload.get("bot")
        if isinstance(bot, bool):
            record["type"] = "Bot" if bot else "User"
            if isinstance(record.get("id"), int):
                self._bots[int(record["id"])] = bot
        record["name"] = str(payload.get("name") or "")
        return record

    def token_scopes(self) -> tuple[str, ...] | None:
        """The token's scopes from its own record
        (``GET /personal_access_tokens/self``, field-verified for a
        personal and a project access token, #1016 V6); ``None`` for a
        token that cannot read itself (an OAuth token, field-unverified)."""
        data = self.raw_lookup("GET", "/personal_access_tokens/self", missing=(404, 401, 403))
        scopes = data.get("scopes") if isinstance(data, dict) else None
        if not isinstance(scopes, list):
            return None
        return tuple(str(s) for s in scopes)

    def permission_probe(self, permission: str, repo: str, base: str) -> bool | None:
        """Whether the token can make :data:`READ_PROBES`'s read for
        ``permission``: False on 401/403, True on any other answer, None
        when the probe needs a ``base`` the project does not have yet."""
        template = READ_PROBES[permission]
        if "{base}" in template and not base:
            return None
        try:
            self.raw("GET", template.format(project=self._project(repo), base=quote(base, safe="")))
        except GithubOpsError as exc:
            if exc.http_status in (401, 403):
                return False
        return True

    def user_is_bot(self, user_id: int) -> bool | None:
        """Whether GitLab flags user ``user_id`` as a bot (#1016 V3): the
        signal lives on ``GET /users/:id`` and nowhere a review is read
        from, so it is looked up once per user per backend object. A
        lookup that fails leaves the kind ``None`` — not known, never
        "human"."""
        if user_id in self._bots:
            return self._bots[user_id]
        try:
            data = self.raw_lookup("GET", f"/users/{user_id}")
        except GithubOpsError as exc:
            log.info("gitlab.user_kind_unread", user=user_id, error=str(exc))
            data = None
        bot = data.get("bot") if isinstance(data, dict) else None
        self._bots[user_id] = bot if isinstance(bot, bool) else None
        return self._bots[user_id]

    # -- ChangeOps (not implemented yet; #1019) --------------------------------

    def pr_create(
        self,
        repo: str,
        base: str,
        head: str,
        title: str,
        body: str = "",
        *,
        draft: bool = False,
    ) -> PrRef:
        raise self._unimplemented("ChangeOps", "pr_create")

    def pr_get(self, repo: str, number: int) -> dict[str, Any]:
        raise self._unimplemented("ChangeOps", "pr_get")

    def pr_list_open(self, repo: str, *, head: str) -> list[Any]:
        raise self._unimplemented("ChangeOps", "pr_list_open")

    def pr_update(
        self, repo: str, number: int, *, title: str | None = None, body: str | None = None
    ) -> dict[str, Any]:
        raise self._unimplemented("ChangeOps", "pr_update")

    def pr_comment(self, repo: str, number: int, body: str) -> str:
        raise self._unimplemented("ChangeOps", "pr_comment")

    def pr_files(self, repo: str, number: int) -> list[Any]:
        raise self._unimplemented("ChangeOps", "pr_files")

    def pr_request_reviewers(self, repo: str, number: int, reviewers: Sequence[str]) -> None:
        raise self._unimplemented("ChangeOps", "pr_request_reviewers")

    def pr_ready_for_review(self, node_id: str) -> bool:
        raise self._unimplemented("ChangeOps", "pr_ready_for_review")

    def pr_update_branch(self, repo: str, number: int, *, expected_head_sha: str = "") -> bool:
        raise self._unimplemented("ChangeOps", "pr_update_branch")

    def pr_merge(
        self,
        repo: str,
        number: int,
        *,
        method: MergeMethod = "squash",
        sha: str = "",
        title: str = "",
        message: str = "",
    ) -> MergeOutcome:
        raise self._unimplemented("ChangeOps", "pr_merge")

    def pr_enqueue(self, node_id: str, *, head: str = "") -> QueueEntry:
        raise self._unimplemented("ChangeOps", "pr_enqueue")

    def pr_queue_state(self, repo: str, number: int) -> QueueState:
        raise self._unimplemented("ChangeOps", "pr_queue_state")

    # -- ReviewOps (not implemented yet; #1018) --------------------------------

    def pr_review_create(
        self,
        repo: str,
        number: int,
        event: ReviewEvent,
        body: str,
        comments: Sequence[ReviewComment] = (),
    ) -> SubmittedReview:
        raise self._unimplemented("ReviewOps", "pr_review_create")

    def pr_review_comments_create(
        self,
        repo: str,
        number: int,
        comments: Sequence[ReviewComment],
        *,
        commit_id: str,
    ) -> tuple[PostedFinding, ...]:
        raise self._unimplemented("ReviewOps", "pr_review_comments_create")

    def pr_review_locations(
        self, repo: str, number: int, *, commit_id: str | None
    ) -> dict[str, tuple[range, ...]]:
        raise self._unimplemented("ReviewOps", "pr_review_locations")

    def pr_reviews(self, repo: str, number: int) -> list[Any]:
        raise self._unimplemented("ReviewOps", "pr_reviews")

    def pr_review_comments(self, repo: str, number: int) -> list[Any]:
        raise self._unimplemented("ReviewOps", "pr_review_comments")

    def pr_review_verdicts(
        self, repo: str, number: int, *, exclude: Identity | None = None
    ) -> tuple[ReviewVerdict, ...]:
        raise self._unimplemented("ReviewOps", "pr_review_verdicts")

    def pr_review_state(self, repo: str, number: int, *, login: str | None = None) -> str:
        raise self._unimplemented("ReviewOps", "pr_review_state")

    def pr_review_feedback(
        self,
        repo: str,
        number: int,
        *,
        exclude_login: str | None = None,
        exclude_is_bot: bool | None = None,
        clip: int = 6000,
    ) -> str:
        raise self._unimplemented("ReviewOps", "pr_review_feedback")

    def pr_review_threads(self, repo: str, number: int) -> list[ReviewThread]:
        raise self._unimplemented("ReviewOps", "pr_review_threads")

    def pr_comment_reply(self, repo: str, number: int, comment_id: int, body: str) -> str:
        raise self._unimplemented("ReviewOps", "pr_comment_reply")

    def pr_issue_comment(self, repo: str, number: int, body: str) -> str:
        raise self._unimplemented("ReviewOps", "pr_issue_comment")

    def resolve_review_thread(self, thread_id: str) -> bool:
        raise self._unimplemented("ReviewOps", "resolve_review_thread")

    # -- ContentOps (not implemented yet; #1020) -------------------------------

    def blobs_create_many(self, repo: str, files: list[dict[str, str]]) -> dict[str, str]:
        raise self._unimplemented("ContentOps", "blobs_create_many")

    def commit_get(self, repo: str, sha: str) -> dict[str, Any]:
        raise self._unimplemented("ContentOps", "commit_get")

    def tree_create(
        self, repo: str, *, base_tree: str, entries: list[dict[str, Any]]
    ) -> dict[str, Any]:
        raise self._unimplemented("ContentOps", "tree_create")

    def commit_create(
        self, repo: str, *, message: str, tree: str, parents: list[str]
    ) -> dict[str, Any]:
        raise self._unimplemented("ContentOps", "commit_create")

    def ref_create(self, repo: str, ref: str, sha: str) -> None:
        raise self._unimplemented("ContentOps", "ref_create")

    def ref_force_update(self, repo: str, branch: str, sha: str) -> None:
        raise self._unimplemented("ContentOps", "ref_force_update")

    def contents_put(
        self, repo: str, path: str, *, message: str, content_b64: str, branch: str
    ) -> dict[str, Any]:
        raise self._unimplemented("ContentOps", "contents_put")


def _implements(ops: GitlabOps) -> VcsOps:
    """The type checker's proof that :class:`GitlabOps` satisfies every
    role in :mod:`sbxloop.vcs.protocol`."""
    return ops
