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

The operations that have not landed yet (the
:class:`~sbxloop.vcs.protocol.ContentOps` role, #1020) raise
:class:`~sbxloop.errors.RoleNotImplemented` here, so a run on a GitLab
repository fails closed at the first of them, naming the operation.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

from sbxloop.config import MergeMethod
from sbxloop.errors import GithubOpsError, RoleNotImplemented
from sbxloop.log import get_logger
from sbxloop.vcs.github.ops import fold_review_verdicts, fold_reviews, user_identity
from sbxloop.vcs.github.review_locations import right_side_ranges
from sbxloop.vcs.gitlab.changes import (
    change_record,
    file_record,
    parse_thread_id,
    review_comment_records,
    review_records,
    review_thread,
    thread_id_for,
)
from sbxloop.vcs.gitlab.permissions import READ_PROBES
from sbxloop.vcs.gitlab.protection import read_base_requirements
from sbxloop.vcs.gitlab.records import (
    APPROVAL_STATUSES as APPROVAL_STATUSES,
    DEVELOPER as DEVELOPER,
    MAINTAINER as MAINTAINER,
    OWNER as OWNER,
    PASSING_STATUSES as PASSING_STATUSES,
    PENDING_STATUSES as PENDING_STATUSES,
    RED_STATUSES as RED_STATUSES,
    access_level as access_level,
    check_run_record as check_run_record,
    fold_statuses as fold_statuses,
    iso_utc as iso_utc,
    issue_record as issue_record,
    issue_state as issue_state,
    label_event_record as label_event_record,
    label_record as label_record,
    labels_record as labels_record,
    latest_statuses as latest_statuses,
    note_record as note_record,
    repo_record as repo_record,
    user_record as user_record,
)
from sbxloop.vcs.jobs import JobBackend
from sbxloop.vcs.model import (
    BaseRequirements,
    ChecksVerdict,
    CloseReason,
    CredentialInfo,
    FailedCheck,
    Identity,
    IssueRef,
    MergeOutcome,
    PostedFinding,
    PrRef,
    QueueEntry,
    QueueEntryState,
    QueueState,
    ReviewComment,
    ReviewEvent,
    ReviewThread,
    ReviewVerdict,
    SubmittedReview,
    identities_match,
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


# What GitLab reads as a draft in a title (the `Draft:` prefix is the whole
# of a draft, field-verified; `WIP:` and `[Draft]` are the older spellings).
_DRAFT_PREFIXES = ("Draft:", "WIP:", "[Draft]", "[WIP]", "(Draft)")


def _undrafted_title(title: str) -> str:
    """``title`` without its draft marker."""
    stripped = title.strip()
    lowered = stripped.lower()
    for prefix in _DRAFT_PREFIXES:
        if lowered.startswith(prefix.lower()):
            return stripped[len(prefix) :].strip()
    return stripped


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
    #: The operations this backend does not answer yet, as ``Role.operation``;
    #: each raises :class:`RoleNotImplemented`, and the doctor lists them.
    #: ``ContentOps`` is #1020.
    UNIMPLEMENTED_OPERATIONS: ClassVar[tuple[str, ...]] = (
        "ContentOps.blobs_create_many",
        "ContentOps.commit_get",
        "ContentOps.tree_create",
        "ContentOps.commit_create",
        "ContentOps.ref_create",
        "ContentOps.ref_force_update",
        "ContentOps.contents_put",
    )

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
        # Which discussion a merge-request note belongs to, and each merge
        # request's web address (#1018): learnt from the reads and writes
        # this object made, so a reply and a url need no second read.
        self._note_discussion: dict[tuple[str, int, int], str] = {}
        self._mr_urls: dict[tuple[str, int], str] = {}

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
        """The class's report, with ``merge_queue`` answered from the
        projects this object has read (#1019): merge trains are a paid
        tier and a per-project setting, so the project payload (read by
        every landing before it merges) is the only place the answer
        lives. Nothing read yet is UNKNOWN, as the class says."""
        report = dict(self.CAPABILITIES)
        answers = {self._trains_of(payload) for payload in self._projects.values()}
        if len(answers) == 1:
            report["merge_queue"] = answers.pop()
        return report

    @staticmethod
    def _trains_of(project: Mapping[str, Any]) -> Capability:
        """``merge_trains_enabled`` as a capability: ``true`` is a train;
        ``false`` and ``null`` (the free tier, field-verified) are none."""
        return (
            Capability.SUPPORTED
            if project.get("merge_trains_enabled") is True
            else Capability.UNSUPPORTED
        )

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

    def credential_info(self) -> CredentialInfo | None:
        """What the token says about itself (``GET
        /personal_access_tokens/self``, field-verified for a personal and a
        project access token, #1016 V6): its name, scopes, expiry and
        whether it is still active. ``None`` for a token that cannot read
        itself. A missing ``expires_at`` is a token that never expires,
        which the doctor makes visible (#1019)."""
        data = self.raw_lookup("GET", "/personal_access_tokens/self", missing=(404, 401, 403))
        if not isinstance(data, dict):
            return None
        scopes = data.get("scopes")
        expires = data.get("expires_at")
        active = data.get("active")
        revoked = data.get("revoked")
        if isinstance(revoked, bool) and revoked:
            active = False
        return CredentialInfo(
            kind="GitLab access token",
            name=str(data.get("name") or ""),
            scopes=tuple(str(s) for s in scopes) if isinstance(scopes, list) else (),
            expires_at=str(expires) if expires else None,
            active=active if isinstance(active, bool) else None,
        )

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

    # -- merge requests -----------------------------------------------------

    def _mr_path(self, repo: str, number: int) -> str:
        return f"{self._project(repo)}/merge_requests/{number}"

    def _merge_request(self, repo: str, number: int) -> dict[str, Any]:
        """The merge request's payload, fresh: its head sha and
        ``diff_refs`` move with every push, so nothing here is cached."""
        path = self._mr_path(repo, number)
        payload = self._dict(f"GET {path}", self.raw("GET", path))
        web_url = payload.get("web_url")
        if isinstance(web_url, str) and web_url:
            self._mr_urls[(repo, number)] = web_url
        return payload

    def _mr_url(self, repo: str, number: int) -> str:
        if (repo, number) not in self._mr_urls:
            self._merge_request(repo, number)
        return self._mr_urls.get((repo, number), "")

    def _diff_refs(self, repo: str, number: int) -> dict[str, str]:
        """The three shas an inline position needs (``base_sha``,
        ``start_sha``, ``head_sha``). GitLab fills them once it has
        computed the diff, shortly after the merge request is opened
        (field-verified, #1016 V1); until then they are ``None`` and no
        inline comment can be anchored."""
        refs = self._merge_request(repo, number).get("diff_refs")
        refs = refs if isinstance(refs, dict) else {}
        out = {k: str(refs.get(k) or "") for k in ("base_sha", "start_sha", "head_sha")}
        if not all(out.values()):
            raise GithubOpsError(
                f"merge request !{number} has no diff refs yet; GitLab is still computing its diff"
            )
        return out

    def _mr_note(self, repo: str, number: int, body: str) -> dict[str, Any]:
        path = f"{self._mr_path(repo, number)}/notes"
        payload = self._dict(f"POST {path}", self.raw("POST", path, {"body": body}))
        return note_record(payload, issue_url=self._mr_url(repo, number))

    # -- ChangeOps: the merge request itself (#1018); landing is #1019 --------

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
        """Open a merge request from ``head`` into ``base``. A draft is the
        ``Draft:`` title prefix (field-verified: it sets ``draft: true`` and
        holds the merge as ``draft_status``). A second open request from
        the same branch is GitLab's 409, raised with its status."""
        request: dict[str, Any] = {
            "source_branch": head,
            "target_branch": base,
            "title": f"Draft: {title}" if draft and not title.startswith("Draft:") else title,
        }
        if body:
            request["description"] = body
        path = f"{self._project(repo)}/merge_requests"
        payload = self._dict(f"POST {path}", self.raw("POST", path, request))
        record = change_record(payload)
        self._mr_urls[(repo, record["number"])] = record["html_url"]
        return PrRef(number=record["number"], url=record["html_url"])

    def pr_get(self, repo: str, number: int) -> dict[str, Any]:
        """The merge request as the loop's change record: the head sha the
        checks hang off, the branch a fix run lands on, and where it stands
        (:func:`change_record`)."""
        return change_record(self._merge_request(repo, number))

    def pr_list_open(self, repo: str, *, head: str) -> list[Any]:
        """The open merge requests from branch ``head``: none, or the one a
        re-delivery refreshes."""
        query = urlencode({"state": "opened", "source_branch": head, "per_page": 100})
        path = f"{self._project(repo)}/merge_requests?{query}"
        rows = self._list(f"GET {self._project(repo)}/merge_requests", self.raw("GET", path))
        return [change_record(row) for row in rows if isinstance(row, dict)]

    def pr_update(
        self, repo: str, number: int, *, title: str | None = None, body: str | None = None
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title
        if body is not None:
            fields["description"] = body
        path = self._mr_path(repo, number)
        return change_record(self._dict(f"PUT {path}", self.raw("PUT", path, fields)))

    def pr_comment(self, repo: str, number: int, body: str) -> str:
        return str(self._mr_note(repo, number, body)["html_url"])

    def pr_files(self, repo: str, number: int) -> list[Any]:
        """The files the merge request changes, with their hunks, across
        every page of ``GET .../diffs``."""
        rows = self.raw_pages(f"{self._mr_path(repo, number)}/diffs")
        return [file_record(row) for row in rows if isinstance(row, dict)]

    # -- ChangeOps: landing the merge request (#1019) --------------------------
    #
    # GitLab's merge endpoint answers the refusals the loop reads as data
    # (field-verified for 405 in #1016 V2): 405 is "not mergeable right now"
    # (a red pipeline, an unresolved discussion, a draft), 406 a conflict,
    # 409 a head that moved past the sha the caller judged, 401 a token that
    # may not merge. The method is the project's own (`merge_method`); the
    # loop's choice only decides whether the merge squashes.

    @staticmethod
    def _node(node_id: str) -> tuple[str, int]:
        """``(project, iid)`` from the ``<project id>!<iid>`` node id the
        change record carries."""
        project, bang, iid = node_id.rpartition("!")
        if not bang or not project or not iid.isdigit():
            raise GithubOpsError(f"not a GitLab merge request id: {node_id!r}")
        return project, int(iid)

    def _user_id(self, username: str) -> int:
        """The id of user ``username``, or a refusal naming them: GitLab
        addresses reviewers by id."""
        path = f"/users?{urlencode({'username': username})}"
        rows = self._list("GET /users", self.raw("GET", path))
        for row in rows:
            if not isinstance(row, dict) or str(row.get("username") or "") != username:
                continue
            if isinstance(row.get("id"), int):
                return int(row["id"])
        raise GithubOpsError(f"GitLab has no user {username!r} to request a review from")

    def pr_request_reviewers(self, repo: str, number: int, reviewers: Sequence[str]) -> None:
        """Ask for reviews from ``reviewers`` (usernames; a ``group/name``
        slug is not a GitLab reviewer and is refused by name). One the
        instance does not know fails the whole request, as on GitHub."""
        if not reviewers:
            return
        slugs = [name for name in reviewers if "/" in name]
        if slugs:
            raise GithubOpsError(f"GitLab reviewers are users, not groups: {', '.join(slugs)}")
        ids = [self._user_id(name) for name in reviewers]
        self.raw("PUT", self._mr_path(repo, number), {"reviewer_ids": ids})

    def pr_ready_for_review(self, node_id: str) -> bool:
        """Take a draft merge request out of draft: the ``Draft:`` prefix is
        the whole of a draft on GitLab (field-verified), so retitling
        clears it. True when the request is now not a draft."""
        project, iid = self._node(node_id)
        path = f"/projects/{quote(project, safe='')}/merge_requests/{iid}"
        current = self._dict(f"GET {path}", self.raw("GET", path))
        if not current.get("draft") and not current.get("work_in_progress"):
            return True
        title = _undrafted_title(str(current.get("title") or ""))
        updated = self._dict(f"PUT {path}", self.raw("PUT", path, {"title": title}))
        return not bool(updated.get("draft"))

    def pr_update_branch(self, repo: str, number: int, *, expected_head_sha: str = "") -> bool:
        """Bring the source branch up to date with its target: GitLab's
        rebase (``PUT .../rebase``), which runs asynchronously, so the
        caller observes the new head on its next poll, as on GitHub. A
        head that has moved past ``expected_head_sha`` is not rebased; a
        refusal (a token without push, a conflict) is False, not raised."""
        if expected_head_sha:
            head = str(self._merge_request(repo, number).get("sha") or "")
            if head != expected_head_sha:
                log.info(
                    "gitlab.rebase_skipped",
                    repo=repo,
                    mr=number,
                    head=head[:12],
                    expected=expected_head_sha[:12],
                    hint="the head moved; the next poll re-decides",
                )
                return False
        try:
            self.raw("PUT", f"{self._mr_path(repo, number)}/rebase", {"skip_ci": False})
        except GithubOpsError as exc:
            if exc.http_status in (403, 409, 422):
                log.info("gitlab.rebase_refused", repo=repo, mr=number, detail=str(exc)[:300])
                return False
            raise
        return True

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
        """Merge the request. ``sha`` is the head the caller judged: a push
        in between loses the race with a 409 (``stale``) instead of being
        merged over. ``method`` decides the squash; the merge commit or
        fast-forward is the project's setting."""
        body: dict[str, Any] = {"squash": method == "squash", "should_remove_source_branch": False}
        if sha:
            body["sha"] = sha
        commit_message = message or title
        if commit_message:
            body["squash_commit_message" if method == "squash" else "merge_commit_message"] = (
                commit_message
            )
        try:
            data = self.raw("PUT", f"{self._mr_path(repo, number)}/merge", body)
        except GithubOpsError as exc:
            if exc.http_status in (401, 403, 405, 406, 422):
                return MergeOutcome(False, "", str(exc), blocked=True)
            if exc.http_status == 409:
                return MergeOutcome(False, "", str(exc), stale=True)
            raise
        if not isinstance(data, dict) or str(data.get("state") or "") != "merged":
            # A 200 that does not say merged is not one: the request was
            # queued to merge when its pipeline succeeds, or the answer is
            # not a merge request at all. No retry fixes it.
            return MergeOutcome(False, "", f"merge was not confirmed: {data!r}", blocked=True)
        merged_sha = (
            data.get("merge_commit_sha") or data.get("squash_commit_sha") or data.get("sha")
        )
        return MergeOutcome(True, str(merged_sha or ""), "merged")

    # -- merge trains ------------------------------------------------------------
    #
    # A paid tier and a per-project setting (#1016): the free tier has no
    # train at all (`merge_trains_enabled` is null and the endpoint is 404,
    # verified), and a project that has them is **field-unverified** beyond
    # GitLab's documented API: the shapes below are the documented ones.

    _TRAIN_STATES: ClassVar[dict[str, QueueEntryState]] = {
        "idle": "queued",
        "fresh": "testing",
        "stale": "testing",
        "merging": "mergeable",
        "merged": "mergeable",
    }

    def _merge_trains(self, repo: str) -> Capability:
        """Whether *this* project merges through a train: SUPPORTED when it
        does, UNSUPPORTED when the project says it does not (or the
        instance has none), UNKNOWN when the project could not be read."""
        try:
            payload = self._project_payload(repo)
        except GithubOpsError:
            return Capability.UNKNOWN
        return (
            Capability.SUPPORTED
            if payload.get("merge_trains_enabled") is True
            else Capability.UNSUPPORTED
        )

    def _train_entry(self, node: Any) -> QueueEntry | None:
        if not isinstance(node, dict) or node.get("id") is None:
            return None
        pipeline = node.get("pipeline")
        index = node.get("index")
        return QueueEntry(
            id=str(node["id"]),
            state=self._TRAIN_STATES.get(str(node.get("status") or ""), "unknown"),
            position=(int(index) + 1) if isinstance(index, int) else None,
            head=str(pipeline.get("sha") or "") if isinstance(pipeline, dict) else "",
        )

    def pr_enqueue(self, node_id: str, *, head: str = "") -> QueueEntry:
        """Add the merge request to its target's merge train
        (``POST .../merge_trains/merge_requests/:iid``); ``head`` is the sha
        the caller judged. A refusal (no train on this project, the
        request not mergeable) is GitLab's 404 or 409, raised with its
        words."""
        project, iid = self._node(node_id)
        path = f"/projects/{quote(project, safe='')}/merge_trains/merge_requests/{iid}"
        body: dict[str, Any] = {"when_pipeline_succeeds": True}
        if head:
            body["sha"] = head
        data = self.raw("POST", path, body)
        entry = self._train_entry(data)
        if entry is None:
            raise GithubOpsError(f"merge train returned no entry: {data!r}")
        return entry

    def pr_queue_state(self, repo: str, number: int) -> QueueState:
        """Where the merge request stands with its train: merged or closed
        from the request itself, the live entry from the train (a 404 is
        "not on the train"), and no removal count: GitLab keeps no event
        the loop can count, so a request that left the train reads as
        removed with no reason."""
        current = self._merge_request(repo, number)
        state = str(current.get("state") or "")
        path = f"{self._project(repo)}/merge_trains/merge_requests/{number}"
        data = self.raw_lookup("GET", path)
        return QueueState(
            merged=state == "merged",
            closed=state == "closed",
            entry=self._train_entry(data),
            merge_sha=str(
                current.get("merge_commit_sha") or current.get("squash_commit_sha") or ""
            ),
        )

    # -- ReviewOps: discussions, approvals, reviewer states (#1018) -----------
    #
    # A review on GitLab is not one object. An inline finding is a
    # discussion anchored by a position (base, start and head sha plus the
    # path and line); a reply is a note on that discussion; resolving it is
    # a flag on the discussion (all field-verified, #1016 V1). A verdict is
    # an approval (which the author may give on CE) or a reviewer state,
    # and "request changes" is recorded but not enforced on CE, so the
    # capability is UNSUPPORTED and the review degrades to a comment.

    def _kind_of(self, user: Any) -> bool | None:
        """The per-user bot flag for a payload's author object."""
        user_id = user.get("id") if isinstance(user, dict) else None
        return self.user_is_bot(int(user_id)) if isinstance(user_id, int) else None

    def _discussions(self, repo: str, number: int) -> list[Any]:
        rows = self.raw_pages(f"{self._mr_path(repo, number)}/discussions")
        for row in rows:
            if not isinstance(row, dict):
                continue
            discussion_id = str(row.get("id") or "")
            for note in row.get("notes") or []:
                if isinstance(note, dict) and isinstance(note.get("id"), int):
                    self._note_discussion[(repo, number, int(note["id"]))] = discussion_id
        return rows

    def _position(self, refs: Mapping[str, str], comment: ReviewComment) -> dict[str, Any]:
        position: dict[str, Any] = {
            "base_sha": refs["base_sha"],
            "start_sha": refs["start_sha"],
            "head_sha": refs["head_sha"],
            "position_type": "text",
            "old_path": comment.path,
            "new_path": comment.path,
        }
        if comment.side == "LEFT":
            position["old_line"] = comment.line
        else:
            position["new_line"] = comment.line
        return position

    def _post_discussions(
        self, repo: str, number: int, comments: Sequence[ReviewComment], refs: Mapping[str, str]
    ) -> tuple[PostedFinding, ...]:
        """One discussion per finding, per anchor: a position GitLab refuses
        fails its own finding (returned with ``comment_id=None`` for the
        caller to put in the body) and no other."""
        posted: list[PostedFinding] = []
        path = f"{self._mr_path(repo, number)}/discussions"
        for comment in comments:
            anchor = f"{comment.path}:{comment.line}"
            try:
                data = self.raw(
                    "POST", path, {"body": comment.body, "position": self._position(refs, comment)}
                )
            except GithubOpsError as exc:
                log.warning(
                    "gitlab.review_comment_refused",
                    repo=repo,
                    mr=number,
                    anchor=anchor,
                    error=str(exc)[:300],
                    hint="the finding goes in the review comment's body instead",
                )
                posted.append(PostedFinding(anchor))
                continue
            discussion_id = str(data.get("id") or "") if isinstance(data, dict) else ""
            notes = data.get("notes") if isinstance(data, dict) else None
            root = (
                notes[0] if isinstance(notes, list) and notes and isinstance(notes[0], dict) else {}
            )
            note_id = root.get("id")
            if not discussion_id or not isinstance(note_id, int):
                posted.append(PostedFinding(anchor))
                continue
            self._note_discussion[(repo, number, note_id)] = discussion_id
            posted.append(
                PostedFinding(anchor, note_id, thread_id_for(repo, number, discussion_id))
            )
        return tuple(posted)

    def pr_review_create(
        self,
        repo: str,
        number: int,
        event: ReviewEvent,
        body: str,
        comments: Sequence[ReviewComment] = (),
    ) -> SubmittedReview:
        """Post a review: each inline finding as a discussion on the diff,
        the body as a note, and the verdict as GitLab expresses it.

        ``APPROVE`` is an approval (``POST .../approve`` with the head sha,
        so a push in between refuses it); an approval GitLab refuses (a
        Premium rule against the author approving, a stale head) falls
        back to a plain comment, and the returned ``event`` says so.
        ``REQUEST_CHANGES`` is a comment with the finding count: the free
        tier records a reviewer's requested changes and does not enforce
        them (#1016), so the loop never claims a gate the forge does not
        hold. A caller reads ``event`` to learn what was accepted.
        """
        posted: tuple[PostedFinding, ...] = ()
        if comments:
            posted = self._post_discussions(repo, number, comments, self._diff_refs(repo, number))
        accepted: ReviewEvent = event
        text = body
        if event == "REQUEST_CHANGES":
            accepted = "COMMENT"
            text = (
                f"{body}\n\n_Changes requested: {len(comments)} finding(s) inline. GitLab's "
                "free tier records a requested change without holding the merge for it, "
                "so this review does not gate._"
            )
        note = self._mr_note(repo, number, text)
        if event == "APPROVE":
            try:
                head = self._merge_request(repo, number).get("sha")
                self.raw(
                    "POST",
                    f"{self._mr_path(repo, number)}/approve",
                    {"sha": str(head)} if head else {},
                )
            except GithubOpsError as exc:
                log.warning(
                    "gitlab.approve_refused",
                    repo=repo,
                    mr=number,
                    http_status=exc.http_status,
                    error=str(exc)[:300],
                    hint="the review stands as a comment, which does not gate the merge",
                )
                accepted = "COMMENT"
        return SubmittedReview(str(note["html_url"]), accepted, int(note["id"]) or None, posted)

    def pr_review_comments_create(
        self,
        repo: str,
        number: int,
        comments: Sequence[ReviewComment],
        *,
        commit_id: str,
    ) -> tuple[PostedFinding, ...]:
        """Each finding as its own discussion, anchored on ``commit_id``: the
        single-identity review (#513). A head that has moved past
        ``commit_id`` refuses every anchor rather than anchoring them on a
        diff the reviewer did not read."""
        if not comments:
            return ()
        refs = self._diff_refs(repo, number)
        if refs["head_sha"] != commit_id:
            raise GithubOpsError(
                f"merge request !{number} head {refs['head_sha'][:12]} no longer matches the "
                f"reviewed commit {commit_id[:12]}"
            )
        return self._post_discussions(repo, number, comments, refs)

    def pr_review_locations(
        self, repo: str, number: int, *, commit_id: str | None
    ) -> dict[str, tuple[range, ...]]:
        """The commentable RIGHT-side ranges of the merge request's diff at
        ``commit_id``, from ``GET .../diffs`` (each entry's ``diff`` is the
        hunks of one file, **field-unverified** beyond the documented
        shape). The head is checked before and after the paged read, as on
        GitHub: a moving head cannot authorise an inline post."""

        def head() -> str:
            refs = self._diff_refs(repo, number)
            if not commit_id or refs["head_sha"] != commit_id:
                raise GithubOpsError("merge request head no longer matches the reviewed commit")
            return refs["head_sha"]

        before = head()
        locations: dict[str, tuple[range, ...]] = {}
        for entry in self.raw_pages(f"{self._mr_path(repo, number)}/diffs"):
            if not isinstance(entry, dict):
                raise GithubOpsError("merge request diffs need a list of changed files")
            name = entry.get("new_path") or entry.get("old_path")
            if not isinstance(name, str) or not name or name in locations:
                raise GithubOpsError("merge request diffs contain missing or duplicate paths")
            locations[name] = right_side_ranges(entry.get("diff"))
        if head() != before:
            raise GithubOpsError("merge request head changed while reading its diff")
        return locations

    def pr_reviews(self, repo: str, number: int) -> list[Any]:
        """The standing verdicts as review records (:func:`review_records`):
        the approvals, then the reviewers who requested changes."""
        base = self._mr_path(repo, number)
        approvals = self.raw_lookup("GET", f"{base}/approvals")
        reviewers = self.raw_lookup("GET", f"{base}/reviewers")
        return review_records(
            approvals if isinstance(approvals, dict) else None,
            reviewers if isinstance(reviewers, list) else [],
            kind_of=self._kind_of,
        )

    def pr_review_comments(self, repo: str, number: int) -> list[Any]:
        """Every inline note on the merge request's diff, as review comment
        records."""
        return review_comment_records(self._discussions(repo, number), kind_of=self._kind_of)

    def pr_review_verdicts(
        self, repo: str, number: int, *, exclude: Identity | None = None
    ) -> tuple[ReviewVerdict, ...]:
        return fold_review_verdicts(self.pr_reviews(repo, number), exclude=exclude)

    def pr_review_state(self, repo: str, number: int, *, login: str | None = None) -> str:
        return fold_reviews(self.pr_reviews(repo, number), login=login)

    def pr_review_feedback(
        self,
        repo: str,
        number: int,
        *,
        exclude_login: str | None = None,
        exclude_is_bot: bool | None = None,
        clip: int = 6000,
    ) -> str:
        """What the reviewers said, for a fix round's brief: every inline
        note not the loop's own, with its anchor. A GitLab review has no
        body of its own, so the notes are the whole of the feedback."""

        def excluded(user: Any) -> bool:
            return exclude_login is not None and identities_match(
                user_identity(user), (exclude_login, exclude_is_bot)
            )

        parts: list[str] = []
        for comment in self.pr_review_comments(repo, number):
            if excluded(comment.get("user")):
                continue
            body = str(comment.get("body") or "").strip()
            if not body:
                continue
            path, line = str(comment.get("path") or ""), comment.get("line")
            anchor = f"`{path}:{line}`: " if path and line else f"`{path}`: " if path else ""
            parts.append(f"- {anchor}{body}")
        return "\n\n".join(parts)[:clip]

    def pr_review_threads(self, repo: str, number: int) -> list[ReviewThread]:
        """Every inline thread on the merge request, with its replies, across
        every page of discussions."""
        threads: list[ReviewThread] = []
        for discussion in self._discussions(repo, number):
            if not isinstance(discussion, dict):
                continue
            thread = review_thread(repo, number, discussion, kind_of=self._kind_of)
            if thread is not None:
                threads.append(thread)
        return threads

    def pr_comment_reply(self, repo: str, number: int, comment_id: int, body: str) -> str:
        """Reply in the discussion that holds note ``comment_id``: learnt
        from a read or a write this object made, else found by listing."""
        key = (repo, number, int(comment_id))
        if key not in self._note_discussion:
            self._discussions(repo, number)
        discussion_id = self._note_discussion.get(key)
        if discussion_id is None:
            raise GithubOpsError(
                f"note {comment_id} is not on a discussion of merge request !{number}"
            )
        path = f"{self._mr_path(repo, number)}/discussions/{discussion_id}/notes"
        data = self._dict(f"POST {path}", self.raw("POST", path, {"body": body}))
        note_id = data.get("id")
        if isinstance(note_id, int):
            self._note_discussion[(repo, number, note_id)] = discussion_id
        return f"{self._mr_url(repo, number)}#note_{note_id}" if note_id is not None else ""

    def pr_issue_comment(self, repo: str, number: int, body: str) -> str:
        """A plain merge-request note: the fallback for body-only findings."""
        return str(self._mr_note(repo, number, body)["html_url"])

    def resolve_review_thread(self, thread_id: str) -> bool:
        """Mark the discussion resolved (``PUT .../discussions/:id`` with
        ``resolved: true``, field-verified); True when it now is. The id
        is the opaque one a :class:`ReviewThread` or :class:`PostedFinding`
        carries."""
        try:
            repo, number, discussion_id = parse_thread_id(thread_id)
        except ValueError as exc:
            raise GithubOpsError(str(exc)) from exc
        path = f"{self._mr_path(repo, number)}/discussions/{discussion_id}"
        data = self._dict(f"PUT {path}", self.raw("PUT", path, {"resolved": True}))
        notes = data.get("notes")
        if isinstance(notes, list) and notes:
            resolvable = [n for n in notes if isinstance(n, dict) and n.get("resolvable")]
            if resolvable:
                return all(bool(n.get("resolved")) for n in resolvable)
        # GitLab answered without the notes (it does for a reopen): read back.
        for discussion in self._discussions(repo, number):
            if isinstance(discussion, dict) and str(discussion.get("id")) == discussion_id:
                thread = review_thread(repo, number, discussion, kind_of=self._kind_of)
                return thread.is_resolved if thread is not None else False
        return False

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
