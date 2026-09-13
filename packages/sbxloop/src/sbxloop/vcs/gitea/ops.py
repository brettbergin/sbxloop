"""The Gitea backend (#1021): every role in :mod:`sbxloop.vcs.protocol`
over Gitea's REST API (``/api/v1``), through the worker's generic
transport.

Field-verified against Gitea 1.24.7 (#1016 and the #1021 probe) where a
docstring says so; everything else is Gitea's documented API, labelled
**field-unverified** where it is load-bearing. What Gitea does not have,
the backend says plainly: no merge queue (the landing merges directly),
no review threads (a review comment is its own thread, never replied to
in place and never resolved by API), no bot flag (the operator's
``[vcs] bot_logins`` decides a reviewer's kind), no token that reads its
own expiry (a token never expires, and the doctor says so), and no
blob/tree/commit objects a client creates (a delivery is staged here and
written as one ``POST .../contents`` changeset, and a fix round commits
on top of the branch, because Gitea has no call that moves a branch and
deleting one closes its open pull request, field-verified).
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar
from urllib.parse import quote, urlencode
from uuid import uuid4

from sbxloop.config import MergeMethod
from sbxloop.errors import GithubOpsError
from sbxloop.log import get_logger
from sbxloop.vcs.gitea.permissions import READ_PROBES
from sbxloop.vcs.gitea.protection import read_base_requirements
from sbxloop.vcs.gitea.records import (
    DRAFT_PREFIX,
    KindOf,
    change_record,
    check_run_record,
    comment_record,
    commit_record,
    file_record,
    fold_statuses,
    is_draft_title,
    issue_record,
    label_record,
    parse_node_id,
    parse_thread_id,
    repo_record,
    review_comment_record,
    review_record,
    review_thread,
    split_diff,
    status_state,
    thread_id_for,
    timeline_event_record,
    undrafted_title,
    user_record,
)
from sbxloop.vcs.github.ops import (
    fold_review_verdicts,
    fold_reviews,
    user_identity,
)
from sbxloop.vcs.github.review_locations import right_side_ranges
from sbxloop.vcs.gitlab.content import (
    EXECUTABLE_MODE,
    GITLINK_MODE,
    REGULAR_MODE,
    SYMLINK_MODE,
    blob_sha,
    tree_handle,
)
from sbxloop.vcs.jobs import MAX_PAGES, JobBackend, PaginationError
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
    QueueState,
    ReviewComment,
    ReviewEvent,
    ReviewThread,
    ReviewVerdict,
    SubmittedReview,
    identities_match,
    normalize_login,
)
from sbxloop.vcs.protocol import Capability, VcsOps
from sbxloop.vcs.query import parse_issue_query
from sbxloop_worker.protocol import TransportSpec

log = get_logger(__name__)

#: The variable the sandbox holds the Gitea token in (#1029); the host's
#: own variable is ``[vcs] token_env``.
SANDBOX_TOKEN_ENV = "GITEA_TOKEN"  # nosec B105 - a variable name, not a value

#: Gitea's default page cap (``MAX_RESPONSE_ITEMS``): a list asked for
#: more comes back with 50, so a page of 50 is not the end. An instance
#: that raised the cap is served correctly too (a page shorter than 50
#: is still the end); one that lowered it is **field-unverified**.
PAGE_SIZE = 50
#: The tree endpoint's own page size (``per_page``); the response says
#: when it is ``truncated``.
TREE_PAGE_SIZE = 1000


def gitea_transport(api_url: str, *, token_env: str = SANDBOX_TOKEN_ENV) -> TransportSpec:
    """The descriptor every job to the Gitea backend carries (#1015): the
    API root (``https://gitea.example.com/api/v1``), the token as
    ``Authorization: token``, lists paged by ``Link`` headers, a plain
    JSON ``Accept`` and no API-version header, and the ``gh`` CLI
    refused. Field-verified: every read in #1016 and the #1021 probe rode
    ``Authorization: token``."""
    return TransportSpec(
        api_url=api_url,
        auth="token",
        pagination="link",
        accept="application/json",
        api_version_header=None,
        api_version=None,
        token_env=[token_env],
        gh_cli=False,
    )


# The loop's review events as Gitea's ``ReviewStateType`` words.
_GITEA_EVENTS: dict[str, str] = {
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "REQUEST_CHANGES",
    "COMMENT": "COMMENT",
}


def _clip_head_tail(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n...(clipped {dropped} chars)...\n{text[len(text) - tail :]}"


class _Staged:
    """A tree not written yet (#1021): the base commit, the whole tree it
    makes (path -> (mode, blob sha)), and the contents operations that
    turn the base into it."""

    def __init__(
        self,
        base: str,
        wanted: dict[str, tuple[str, str]],
        files: list[dict[str, Any]],
    ) -> None:
        self.base = base
        self.wanted = wanted
        self.files = files


class _Pending:
    """A commit written on a pending branch, with what it takes to bring
    another branch to the same tree."""

    def __init__(self, message: str, start: str, staged: _Staged, branch: str) -> None:
        self.message = message
        self.start = start
        self.staged = staged
        self.branch = branch


class GiteaOps(JobBackend):
    KIND = "gitea"
    LABEL = "gitea"

    # The verified matrix for Gitea 1.24.7 (#1016): no queue, no review
    # threads, drafts by WIP prefix, an enforced request-changes review
    # (when the rule blocks on rejected reviews), long-lived tokens, a
    # remote commit through the contents API, required contexts readable
    # by a write collaborator, no bot signal, unsigned API commits on a
    # default install.
    CAPABILITIES: ClassVar[dict[str, Capability]] = {
        "merge_queue": Capability.UNSUPPORTED,
        "review_threads": Capability.UNSUPPORTED,
        "draft_changes": Capability.SUPPORTED,
        "request_changes_review": Capability.SUPPORTED,
        "short_lived_token": Capability.UNSUPPORTED,
        "remote_commit": Capability.SUPPORTED,
        "required_checks_introspection": Capability.SUPPORTED,
        "bot_identity": Capability.UNSUPPORTED,
        "signed_api_commits": Capability.UNSUPPORTED,
    }
    CAPABILITY_NOTE: ClassVar[str] = ""
    UNIMPLEMENTED_OPERATIONS: ClassVar[tuple[str, ...]] = ()

    def __init__(self, *args: Any, bot_logins: Sequence[str] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The operator's list of automated reviewers (#1016 V3): Gitea has
        # no bot flag, so every login not listed is a human.
        self.bot_logins: frozenset[str] = frozenset(normalize_login(n) for n in bot_logins if n)
        self._repos: dict[str, dict[str, Any]] = {}
        self._pr_urls: dict[tuple[str, int], str] = {}
        # The content role's staging (#1021).
        self._blobs: dict[tuple[str, str], bytes] = {}
        self._trees: dict[tuple[str, str], _Staged] = {}
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._tree_cache: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}

    # -- plumbing ------------------------------------------------------------------

    def raw_pages(self, path: str) -> list[Any]:
        """Every entry of a list endpoint, ``limit``/``page`` paged at
        Gitea's default cap (:data:`PAGE_SIZE`)."""
        sep = "&" if "?" in path else "?"
        rows: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            data = self.raw("GET", f"{path}{sep}limit={PAGE_SIZE}&page={page}")
            if not isinstance(data, list):
                return rows
            rows.extend(data)
            if len(data) < PAGE_SIZE:
                return rows
        raise PaginationError(
            f"GET {path} has more than {MAX_PAGES * PAGE_SIZE} entries; "
            "the list was not read to its end"
        )

    @staticmethod
    def _repo(repo: str) -> str:
        return f"/repos/{repo}"

    def _repo_payload(self, repo: str) -> dict[str, Any]:
        if repo not in self._repos:
            path = self._repo(repo)
            self._repos[repo] = self._dict(f"GET {path}", self.raw("GET", path))
        return self._repos[repo]

    def _kind_of(self, login: str) -> bool:
        return normalize_login(login) in self.bot_logins

    @property
    def kind_of(self) -> KindOf:
        return self._kind_of

    def _user_of(self, user: Any) -> dict[str, Any]:
        return user_record(user, self._kind_of)

    # -- what this backend can do --------------------------------------------

    def capabilities(self) -> dict[str, Capability]:
        return dict(self.CAPABILITIES)

    # -- RepoOps -------------------------------------------------------------

    def repo_get(self, repo: str) -> dict[str, Any]:
        return repo_record(self._repo_payload(repo))

    def repo_lookup(self, repo: str) -> dict[str, Any] | None:
        """The repository record, or ``None`` on Gitea's 404 (a repository
        that does not exist, or one this token cannot see)."""
        path = self._repo(repo)
        data = self.raw_lookup("GET", path)
        if data is None:
            return None
        payload = self._dict(f"GET {path}", data)
        self._repos[repo] = payload
        return repo_record(payload)

    def repo_create(
        self, repo: str, *, private: bool = True, for_user: bool = False
    ) -> dict[str, Any]:
        """Create ``owner/name`` with an initial commit, under the token's
        own account (``for_user``) or the organisation ``owner``.
        **Field-unverified**: the harness repository was seeded by the
        administrator."""
        owner, name = repo.split("/", 1)
        body = {"name": name, "private": private, "auto_init": True, "default_branch": "main"}
        path = "/user/repos" if for_user else f"/orgs/{quote(owner, safe='')}/repos"
        payload = self._dict(f"POST {path}", self.raw("POST", path, body))
        self._repos[repo] = payload
        return repo_record(payload)

    def default_branch(self, repo: str) -> str:
        name = self._repo_payload(repo).get("default_branch")
        if not isinstance(name, str) or not name:
            raise GithubOpsError(
                f"Gitea did not report a default branch for {repo}; "
                "set [github] deliver_base to name the branch to deliver against"
            )
        return name

    def ref_lookup(self, repo: str, ref: str) -> str | None:
        """``heads/<branch>`` or ``tags/<tag>`` to its commit sha, or
        ``None`` for a ref Gitea answers 404 for (field-verified for a
        missing branch and a missing tag)."""
        if ref.startswith("heads/"):
            path = f"{self._repo(repo)}/branches/{quote(ref[6:], safe='')}"
        elif ref.startswith("tags/"):
            path = f"{self._repo(repo)}/tags/{quote(ref[5:], safe='')}"
        else:
            raise GithubOpsError(f"ref_lookup needs heads/<branch> or tags/<tag>, got {ref!r}")
        data = self.raw_lookup("GET", path)
        if data is None:
            return None
        payload = self._dict(f"GET {path}", data)
        commit = payload.get("commit")
        sha = (commit.get("id") or commit.get("sha")) if isinstance(commit, dict) else None
        if not sha:
            raise GithubOpsError(f"GET {path} returned no commit sha for {ref!r}: {payload!r}")
        return str(sha)

    def branch_delete(self, repo: str, branch: str) -> None:
        """Delete a branch, tolerating one already gone (404) and one the
        rules protect (403)."""
        self.raw_lookup(
            "DELETE", f"{self._repo(repo)}/branches/{quote(branch, safe='')}", missing=(404, 403)
        )

    def _compare(self, repo: str, base: str, head: str) -> dict[str, Any] | None:
        path = f"{self._repo(repo)}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
        data = self.raw_lookup("GET", path)
        return data if isinstance(data, dict) else None

    def merge_base(self, repo: str, base: str, head: str) -> str | None:
        """The merge base of ``base`` and ``head``. Gitea has no merge-base
        call: an open pull request whose head is ``head`` carries its
        ``merge_base``; otherwise the comparison lists the commits ``head``
        has over ``base``, and the parent of the oldest that is not itself
        listed is the base (exact for a linear branch, **field-unverified**
        past that). ``None`` when neither can say."""
        for pull in self.raw_pages(f"{self._repo(repo)}/pulls?state=open"):
            if not isinstance(pull, dict):
                continue
            pull_head_raw = pull.get("head")
            pull_head: dict[str, Any] = pull_head_raw if isinstance(pull_head_raw, dict) else {}
            if head in (pull_head.get("sha"), pull_head.get("ref")) and pull.get("merge_base"):
                return str(pull["merge_base"])
        compare = self._compare(repo, base, head)
        if compare is None:
            return None
        commits = compare.get("commits")
        if not isinstance(commits, list) or not commits:
            # Nothing over the base: the head is the base, or behind it.
            return self.ref_lookup(repo, f"heads/{head}") if not _is_sha(head) else head
        listed = {str(c.get("sha") or "") for c in commits if isinstance(c, dict)}
        oldest = commits[-1] if isinstance(commits[-1], dict) else {}
        parents = oldest.get("parents")
        if isinstance(parents, list):
            for parent in parents:
                sha = str(parent.get("sha") or "") if isinstance(parent, dict) else ""
                if sha and sha not in listed:
                    return sha
        return None

    def compare_lookup(self, repo: str, base: str, head: str) -> dict[str, Any] | None:
        """Gitea's comparison of ``base`` with ``head`` (commits and their
        count, field-verified), with the merge base the loop reads under
        ``merge_base_commit``; ``None`` on a 404."""
        compare = self._compare(repo, base, head)
        if compare is None:
            return None
        commits = compare.get("commits") if isinstance(compare.get("commits"), list) else []
        merge_base = self.merge_base(repo, base, head)
        return {
            "merge_base_commit": {"sha": merge_base} if merge_base else None,
            "status": "identical" if not compare.get("total_commits") else "ahead",
            "html_url": f"{self._web_url(repo)}/compare/{base}...{head}",
            "commits": commits,
        }

    def _web_url(self, repo: str) -> str:
        return str(self._repo_payload(repo).get("html_url") or "")

    def _contents(self, repo: str, path: str, ref: str) -> Any:
        target = f"{self._repo(repo)}/contents/{quote(path, safe='/')}"
        return self.raw_lookup("GET", f"{target}?{urlencode({'ref': ref})}")

    def contents_read(self, repo: str, path: str, ref: str | None = None) -> str:
        """One file's text at ``ref`` (the default branch when unset);
        binary content comes back base64-encoded."""
        payload = self._contents(repo, path, ref or self.default_branch(repo))
        if payload is None:
            raise GithubOpsError(f"{repo} has no {path!r} at {ref or 'the default branch'}")
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str):
            return ""
        raw = base64.b64decode(content)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(raw).decode()

    # -- IssueOps -------------------------------------------------------------

    def _issue_path(self, repo: str, number: int | str) -> str:
        return f"{self._repo(repo)}/issues/{number}"

    def _label_ids(self, repo: str, names: Sequence[str]) -> list[int]:
        """The ids of the labels ``names``; one the repository lacks is
        refused by name (Gitea would drop it silently, field-verified)."""
        wanted = {name.casefold(): name for name in names}
        ids: dict[str, int] = {}
        for row in self.raw_pages(f"{self._repo(repo)}/labels"):
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").casefold()
            if name in wanted and isinstance(row.get("id"), int):
                ids[name] = int(row["id"])
        missing = [wanted[n] for n in wanted if n not in ids]
        if missing:
            raise GithubOpsError(
                f"{repo} has no label {', '.join(repr(m) for m in missing)}; create it first"
            )
        return [ids[n] for n in wanted]

    def issue_create(
        self,
        repo: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> IssueRef:
        request: dict[str, Any] = {"title": title}
        if body:
            request["body"] = body
        if labels:
            request["labels"] = self._label_ids(repo, labels)
        path = f"{self._repo(repo)}/issues"
        record = issue_record(
            self._dict(f"POST {path}", self.raw("POST", path, request)), self._kind_of
        )
        return IssueRef(number=record["number"], url=record["html_url"])

    def issue_get(self, repo: str, number: int | str) -> dict[str, Any]:
        path = self._issue_path(repo, number)
        return issue_record(self._dict(f"GET {path}", self.raw("GET", path)), self._kind_of)

    def issue_comment(self, repo: str, number: int, body: str) -> str:
        path = f"{self._issue_path(repo, number)}/comments"
        payload = self._dict(f"POST {path}", self.raw("POST", path, {"body": body}))
        return str(comment_record(payload, self._kind_of)["html_url"])

    def issue_comments(self, repo: str, number: int | str) -> list[Any]:
        rows = self.raw_pages(f"{self._issue_path(repo, number)}/comments")
        return [comment_record(row, self._kind_of) for row in rows if isinstance(row, dict)]

    def issue_comment_delete(
        self, repo: str, comment_id: int, *, number: int | None = None
    ) -> None:
        """Delete one comment: Gitea addresses it by id alone
        (``DELETE .../issues/comments/:id``, field-verified)."""
        self.raw("DELETE", f"{self._repo(repo)}/issues/comments/{comment_id}")

    def issue_events(self, repo: str, number: int | str) -> list[Any]:
        """The issue's label events, oldest first, from its timeline
        (``GET .../issues/:n/timeline``, field-verified for an added label)."""
        rows = self.raw_pages(f"{self._issue_path(repo, number)}/timeline")
        events = [
            timeline_event_record(row, self._kind_of) for row in rows if isinstance(row, dict)
        ]
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
        """One page of the repository's issues (never pull requests:
        ``type=issues``), filtered by ``state`` and every label in
        ``labels``. Gitea caps a page at its own maximum."""
        params: dict[str, Any] = {
            "state": state if state in ("open", "closed") else "all",
            "type": "issues",
            "limit": per_page,
            "page": page,
        }
        if labels:
            params["labels"] = ",".join(labels)
        if sort:
            params["sort"] = {"updated": "recentupdate", "created": "newest"}.get(sort, "newest")
        path = f"{self._repo(repo)}/issues?{urlencode(params)}"
        rows = self._list(f"GET {self._repo(repo)}/issues", self.raw("GET", path))
        return [issue_record(row, self._kind_of) for row in rows if isinstance(row, dict)]

    def issue_search(self, query: str, *, per_page: int) -> dict[str, Any]:
        """The follow-up lookup's search, answered from the repository's
        issue list with ``q``; a page as long as ``per_page`` may not be
        the whole answer and says so."""
        asked = parse_issue_query(query)
        if asked.repo is None:
            raise GithubOpsError("issue search on Gitea needs a repo: qualifier")
        params: dict[str, Any] = {
            "type": "issues",
            "q": " ".join(asked.terms),
            "limit": per_page,
            "state": asked.state if asked.state in ("open", "closed") else "all",
        }
        path = f"{self._repo(asked.repo)}/issues?{urlencode(params)}"
        rows = self._list(f"GET {self._repo(asked.repo)}/issues", self.raw("GET", path))
        items = [issue_record(row, self._kind_of) for row in rows if isinstance(row, dict)]
        return {
            "items": items,
            "total_count": len(items),
            "incomplete_results": len(items) >= per_page,
        }

    def search_issues(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
        asked = parse_issue_query(query)
        if asked.repo is None:
            raise GithubOpsError("issue search on Gitea needs a repo: qualifier")
        if asked.kind != "issue":
            raise GithubOpsError("the Gitea backend searches issues only")
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
        """Add ``labels`` by name. Gitea takes names and drops one the
        repository lacks without a word (field-verified), so the answer
        is checked against what was asked."""
        if not labels:
            return
        path = f"{self._issue_path(repo, number)}/labels"
        data = self.raw("POST", path, {"labels": list(labels)})
        present = {
            str(row.get("name") or "").casefold()
            for row in (data if isinstance(data, list) else [])
            if isinstance(row, dict)
        }
        missing = [name for name in labels if name.casefold() not in present]
        if missing:
            raise GithubOpsError(
                f"{repo} has no label {', '.join(repr(m) for m in missing)}; create it first"
            )

    def issue_label_remove(self, repo: str, number: int | str, label: str) -> None:
        """Take ``label`` off the issue; Gitea addresses it by id, and one
        that is not on the issue (or not in the repository) is a success."""
        found = self.label_lookup(repo, label)
        if found is None or not isinstance(found.get("id"), int):
            return
        self.raw_lookup(
            "DELETE", f"{self._issue_path(repo, number)}/labels/{found['id']}", missing=(404, 422)
        )

    def issue_close(
        self, repo: str, number: int | str, *, reason: CloseReason = "completed"
    ) -> None:
        """Close the issue; Gitea records no reason."""
        self.raw("PATCH", self._issue_path(repo, number), {"state": "closed"})

    def label_lookup(self, repo: str, name: str) -> dict[str, Any] | None:
        for row in self.raw_pages(f"{self._repo(repo)}/labels"):
            if isinstance(row, dict) and str(row.get("name") or "").casefold() == name.casefold():
                return label_record(row)
        return None

    def label_create(self, repo: str, *, name: str, color: str, description: str) -> dict[str, Any]:
        """Create a repository label. Gitea creates a second label of the
        same name rather than refusing (field-verified), so an existing
        one is refused here, with the words GitHub would use."""
        if self.label_lookup(repo, name) is not None:
            raise GithubOpsError(f"label {name!r} already exists on {repo}", http_status=422)
        path = f"{self._repo(repo)}/labels"
        body = {"name": name, "color": f"#{color.removeprefix('#')}", "description": description}
        return label_record(self._dict(f"POST {path}", self.raw("POST", path, body)))

    def labels_list(self, repo: str) -> list[Any]:
        rows = self.raw_pages(f"{self._repo(repo)}/labels")
        return [label_record(row) for row in rows if isinstance(row, dict)]

    # -- ChecksOps -------------------------------------------------------------

    def _statuses(self, repo: str, sha: str) -> list[Any]:
        """The newest status per context (``GET .../commits/:sha/status``,
        field-verified: the rows carry ``status``)."""
        path = f"{self._repo(repo)}/commits/{sha}/status"
        data = self._dict(f"GET {path}", self.raw("GET", path))
        rows = data.get("statuses")
        return rows if isinstance(rows, list) else []

    def pr_checks(self, repo: str, sha: str) -> ChecksVerdict:
        return fold_statuses(self._statuses(repo, sha))

    def check_runs(self, repo: str, sha: str) -> list[Any]:
        return [check_run_record(row) for row in self._statuses(repo, sha) if isinstance(row, dict)]

    def checks_failed_logs(
        self, repo: str, sha: str, *, max_chars: int = 6000
    ) -> list[FailedCheck]:
        """The red statuses on ``sha`` with the one line each carries: a
        Gitea status has a description and a target url, and which
        Actions job (``GET .../actions/jobs/:id/logs``) a status came from
        is **field-unverified**, so no log is fetched."""
        head = min(1500, max_chars)
        tail = max_chars - head
        failed: list[FailedCheck] = []
        for row in self._statuses(repo, sha):
            if not isinstance(row, dict):
                continue
            state = status_state(row)
            if state in ("success", "pending"):
                continue
            failed.append(
                FailedCheck(
                    name=str(row.get("context") or "status"),
                    conclusion=state or "failure",
                    excerpt=_clip_head_tail(str(row.get("description") or ""), head, tail),
                    url=str(row.get("target_url") or ""),
                )
            )
        return failed

    def pr_required_checks(self, repo: str, number: int) -> tuple[str, ...]:
        """The contexts the pull request's base requires, from the base
        branch (field-verified readable by a write collaborator)."""
        base = str(self._pull(repo, number).get("base", {}).get("ref") or "")
        if not base:
            raise GithubOpsError(f"pull request #{number} names no base branch")
        requirements = read_base_requirements(self, repo, base)
        return tuple(requirements.required_contexts or ())

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
        body: dict[str, Any] = {"state": state, "context": context}
        if description:
            body["description"] = description
        if target_url:
            body["target_url"] = target_url
        self.raw("POST", f"{self._repo(repo)}/statuses/{sha}", body)

    def workflows_list(self, repo: str) -> list[Any]:
        """The repository's Actions workflows (``GET .../actions/workflows``,
        field-verified to answer ``{workflows, total_count}``)."""
        data = self.raw_lookup("GET", f"{self._repo(repo)}/actions/workflows")
        rows = data.get("workflows") if isinstance(data, dict) else None
        return [
            {
                "id": row.get("id"),
                "name": str(row.get("name") or ""),
                "state": str(row.get("state") or "active"),
                "path": str(row.get("path") or ""),
            }
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict)
        ]

    def workflow_runs(self, repo: str, *, branch: str, per_page: int = 1) -> list[Any]:
        """The latest Actions runs on ``branch`` (``GET .../actions/tasks``,
        field-verified to answer ``{workflow_runs, total_count}``; the
        per-run fields are the documented ones, **field-unverified** on an
        instance with a runner)."""
        path = f"{self._repo(repo)}/actions/tasks?{urlencode({'limit': max(per_page, 1)})}"
        data = self.raw_lookup("GET", path)
        rows = data.get("workflow_runs") if isinstance(data, dict) else None
        runs: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            if row.get("head_branch") and row.get("head_branch") != branch:
                continue
            status = str(row.get("status") or "")
            runs.append(
                {
                    "id": row.get("id"),
                    "name": str(row.get("name") or row.get("display_title") or "workflow"),
                    "status": "completed" if status == "completed" else "in_progress",
                    "conclusion": row.get("conclusion") if status == "completed" else None,
                    "html_url": str(row.get("html_url") or ""),
                    "head_sha": str(row.get("head_sha") or ""),
                }
            )
        return runs[:per_page]

    # -- PolicyOps ------------------------------------------------------------

    def base_requirements(self, repo: str, base: str) -> BaseRequirements:
        return read_base_requirements(self, repo, base)

    def rate_limit(self) -> dict[str, Any]:
        """Gitea has no rate-limit endpoint; the cheapest authenticated read
        is the instance version."""
        return self._dict("GET /version", self.raw("GET", "/version"))

    def authenticated_user(self) -> dict[str, Any]:
        payload = self._dict("GET /user", self.raw("GET", "/user"))
        record = self._user_of(payload)
        record["name"] = str(payload.get("full_name") or "")
        return record

    def token_scopes(self) -> tuple[str, ...] | None:
        """``None``: a Gitea token cannot read its own record (#1016 V6),
        so the doctor asks each read endpoint instead."""
        return None

    def credential_info(self) -> CredentialInfo | None:
        """A Gitea access token has no expiry to read or to set (#1016 V6):
        the record says it never expires, as a fact of the forge, which
        the doctor turns into its warning; whether it is still active is
        not readable either."""
        return CredentialInfo(kind="Gitea access token", expires_at=None, active=None)

    def permission_probe(self, permission: str, repo: str, base: str) -> bool | None:
        template = READ_PROBES[permission]
        if "{base}" in template and not base:
            return None
        try:
            self.raw("GET", template.format(repo=repo, base=quote(base, safe="")))
        except GithubOpsError as exc:
            if exc.http_status in (401, 403):
                return False
        return True

    # -- pull requests -----------------------------------------------------------

    def _pull_path(self, repo: str, number: int) -> str:
        return f"{self._repo(repo)}/pulls/{number}"

    def _pull(self, repo: str, number: int) -> dict[str, Any]:
        path = self._pull_path(repo, number)
        payload = self._dict(f"GET {path}", self.raw("GET", path))
        url = payload.get("html_url")
        if isinstance(url, str) and url:
            self._pr_urls[(repo, number)] = url
        return payload

    def _pull_url(self, repo: str, number: int) -> str:
        if (repo, number) not in self._pr_urls:
            self._pull(repo, number)
        return self._pr_urls.get((repo, number), "")

    def _pull_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        path = f"{self._issue_path(repo, number)}/comments"
        payload = self._dict(f"POST {path}", self.raw("POST", path, {"body": body}))
        return comment_record(payload, self._kind_of)

    # -- ChangeOps ----------------------------------------------------------------

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
        """Open a pull request from ``head`` into ``base``. A draft is the
        ``WIP:`` title prefix (field-verified: it sets ``draft: true`` and
        the merge refuses it). A second open request from the same branch
        is Gitea's 409, raised with its status."""
        request: dict[str, Any] = {
            "head": head,
            "base": base,
            "title": f"{DRAFT_PREFIX}{title}" if draft and not is_draft_title(title) else title,
        }
        if body:
            request["body"] = body
        path = f"{self._repo(repo)}/pulls"
        record = change_record(
            self._dict(f"POST {path}", self.raw("POST", path, request)), self._kind_of
        )
        self._pr_urls[(repo, record["number"])] = record["html_url"]
        return PrRef(number=record["number"], url=record["html_url"])

    def pr_get(self, repo: str, number: int) -> dict[str, Any]:
        return change_record(self._pull(repo, number), self._kind_of)

    def pr_list_open(self, repo: str, *, head: str) -> list[Any]:
        """The open pull requests from branch ``head``: Gitea's list has no
        head filter, so every open request is read and matched."""
        rows = self.raw_pages(f"{self._repo(repo)}/pulls?state=open")
        records = [change_record(row, self._kind_of) for row in rows if isinstance(row, dict)]
        return [record for record in records if record["head"]["ref"] == head]

    def pr_update(
        self, repo: str, number: int, *, title: str | None = None, body: str | None = None
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title
        if body is not None:
            fields["body"] = body
        path = self._pull_path(repo, number)
        return change_record(
            self._dict(f"PATCH {path}", self.raw("PATCH", path, fields)), self._kind_of
        )

    def pr_comment(self, repo: str, number: int, body: str) -> str:
        return str(self._pull_comment(repo, number, body)["html_url"])

    def _diff_by_file(self, repo: str, number: int) -> dict[str, str]:
        """The pull request's unified diff (``GET .../pulls/:n.diff``,
        field-verified) split per file."""
        text = self.raw_text("GET", f"{self._pull_path(repo, number)}.diff", missing=(404,))
        return split_diff(text or "")

    def pr_files(self, repo: str, number: int) -> list[Any]:
        """The files the pull request changes, each with its hunks from
        the request's diff (the files list carries no patch,
        field-verified)."""
        patches = self._diff_by_file(repo, number)
        rows = self.raw_pages(f"{self._pull_path(repo, number)}/files")
        return [
            file_record(row, patches.get(str(row.get("filename") or ""), ""))
            for row in rows
            if isinstance(row, dict)
        ]

    def pr_request_reviewers(self, repo: str, number: int, reviewers: Sequence[str]) -> None:
        """Ask for reviews from ``reviewers`` (logins). One the instance does
        not know is Gitea's 404 ``User 'x' not exist`` (field-verified),
        raised for the caller."""
        if not reviewers:
            return
        self.raw(
            "POST",
            f"{self._pull_path(repo, number)}/requested_reviewers",
            {"reviewers": list(reviewers)},
        )

    def pr_ready_for_review(self, node_id: str) -> bool:
        """Take a draft out of draft: the ``WIP:`` prefix is the whole of a
        draft on Gitea (field-verified), so retitling clears it."""
        repo, number = parse_node_id(node_id)
        current = self._pull(repo, number)
        title = str(current.get("title") or "")
        if not current.get("draft") and not is_draft_title(title):
            return True
        path = self._pull_path(repo, number)
        updated = self._dict(
            f"PATCH {path}", self.raw("PATCH", path, {"title": undrafted_title(title)})
        )
        return not bool(updated.get("draft"))

    def pr_update_branch(self, repo: str, number: int, *, expected_head_sha: str = "") -> bool:
        """Bring the head branch up to date with its base
        (``POST .../pulls/:n/update?style=rebase``). A head that moved past
        ``expected_head_sha`` is left alone; a refusal is False, not
        raised (field-verified: a 422 for a request with nothing to
        update)."""
        if expected_head_sha:
            head = str(self._pull(repo, number).get("head", {}).get("sha") or "")
            if head != expected_head_sha:
                log.info(
                    "gitea.update_skipped",
                    repo=repo,
                    pr=number,
                    head=head[:12],
                    expected=expected_head_sha[:12],
                    hint="the head moved; the next poll re-decides",
                )
                return False
        try:
            self.raw("POST", f"{self._pull_path(repo, number)}/update?style=rebase")
        except GithubOpsError as exc:
            if exc.http_status in (403, 409, 422):
                log.info("gitea.update_refused", repo=repo, pr=number, detail=str(exc)[:300])
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
        """Merge the request (``POST .../pulls/:n/merge``). ``sha`` rides as
        ``head_commit_id``, so a push in between loses the race. Gitea's
        405 is every "not mergeable right now" (a draft, a required check
        missing or red, a rejected review, field-verified with its words);
        a 409 is a head that moved (**field-unverified**: the harness
        answered 405 first for a moved head behind a missing check)."""
        body: dict[str, Any] = {"Do": method, "delete_branch_after_merge": False}
        if sha:
            body["head_commit_id"] = sha
        if title:
            body["MergeTitleField"] = title
        if message:
            body["MergeMessageField"] = message
        try:
            self.raw("POST", f"{self._pull_path(repo, number)}/merge", body)
        except GithubOpsError as exc:
            if exc.http_status in (401, 403, 405, 422):
                return MergeOutcome(False, "", str(exc), blocked=True)
            if exc.http_status == 409:
                return MergeOutcome(False, "", str(exc), stale=True)
            raise
        merged = self._pull(repo, number)
        if not merged.get("merged"):
            return MergeOutcome(False, "", f"merge was not confirmed: {merged!r}", blocked=True)
        return MergeOutcome(True, str(merged.get("merge_commit_sha") or ""), "merged")

    def pr_enqueue(self, node_id: str, *, head: str = "") -> QueueEntry:
        """Gitea has no merge queue (#1016, verified): the landing never
        asks, and an ask is refused by name."""
        raise GithubOpsError("Gitea has no merge queue; the landing merges directly")

    def pr_queue_state(self, repo: str, number: int) -> QueueState:
        current = self._pull(repo, number)
        return QueueState(
            merged=bool(current.get("merged")),
            closed=str(current.get("state") or "") == "closed",
            entry=None,
            merge_sha=str(current.get("merge_commit_sha") or ""),
        )

    # -- ReviewOps ------------------------------------------------------------------
    #
    # A Gitea review is GitHub-shaped (event, body, inline comments on a
    # commit), and a review comment is the end of its own thread: there is
    # no reply and no resolve path (#1016 V1, verified), so the loop
    # answers a comment with a change-level comment that quotes its anchor
    # and never waits on a resolution it cannot make.

    def _review_comments_of(self, repo: str, number: int, review_id: Any) -> list[Any]:
        return self.raw_pages(f"{self._pull_path(repo, number)}/reviews/{review_id}/comments")

    def _reviews(self, repo: str, number: int) -> list[Any]:
        return self.raw_pages(f"{self._pull_path(repo, number)}/reviews")

    def _capture_posted(
        self, repo: str, number: int, review_id: Any, comments: Sequence[ReviewComment]
    ) -> tuple[PostedFinding, ...]:
        wanted = [f"{c.path}:{c.line}" for c in comments]
        if not comments or review_id is None:
            return tuple(PostedFinding(anchor) for anchor in wanted)
        try:
            rows = self._review_comments_of(repo, number, review_id)
        except GithubOpsError as exc:
            log.warning("gitea.review_comments_read_failed", repo=repo, pr=number, error=str(exc))
            return tuple(PostedFinding(anchor) for anchor in wanted)
        by_anchor: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), int):
                continue
            by_anchor.setdefault(f"{row.get('path') or ''}:{row.get('position')}", int(row["id"]))
        posted: list[PostedFinding] = []
        for anchor in wanted:
            comment_id = by_anchor.get(anchor)
            posted.append(
                PostedFinding(
                    anchor,
                    comment_id,
                    thread_id_for(repo, number, comment_id) if comment_id is not None else None,
                )
            )
        return tuple(posted)

    def _review_body(
        self, event: ReviewEvent, body: str, comments: Sequence[ReviewComment], commit_id: str
    ) -> dict[str, Any]:
        # Gitea's event words are its review states: ``APPROVED``, not
        # GitHub's ``APPROVE`` (an unknown word is recorded as a PENDING
        # review that counts for nothing, field-verified).
        payload: dict[str, Any] = {"event": _GITEA_EVENTS.get(event, event), "body": body}
        if commit_id:
            payload["commit_id"] = commit_id
        if comments:
            payload["comments"] = [
                {
                    "path": c.path,
                    "body": c.body,
                    **({"old_position": c.line} if c.side == "LEFT" else {"new_position": c.line}),
                }
                for c in comments
            ]
        return payload

    def pr_review_create(
        self,
        repo: str,
        number: int,
        event: ReviewEvent,
        body: str,
        comments: Sequence[ReviewComment] = (),
    ) -> SubmittedReview:
        """Post a review (``POST .../pulls/:n/reviews``, GitHub's shape with
        ``new_position``/``old_position`` anchors, field-verified). An
        approval Gitea refuses (the author's own is a 422, #1016) is
        resubmitted as a COMMENT review; the returned ``event`` is the
        state Gitea recorded, and the caller reads that."""
        head = str(self._pull(repo, number).get("head", {}).get("sha") or "")
        path = f"{self._pull_path(repo, number)}/reviews"
        try:
            data = self._dict(
                f"POST {path}",
                self.raw("POST", path, self._review_body(event, body, comments, head)),
            )
        except GithubOpsError as exc:
            if event == "COMMENT":
                raise
            log.warning(
                "gitea.review_event_refused",
                repo=repo,
                pr=number,
                requested=event,
                http_status=exc.http_status,
                hint="posting the feedback as a COMMENT review, which does not gate the merge",
            )
            data = self._dict(
                f"POST {path}",
                self.raw("POST", path, self._review_body("COMMENT", body, comments, head)),
            )
            event = "COMMENT"
        review_id = data.get("id") if isinstance(data.get("id"), int) else None
        recorded = str(data.get("state") or "").upper()
        accepted: ReviewEvent = (
            "APPROVE"
            if recorded == "APPROVED"
            else "REQUEST_CHANGES"
            if recorded == "REQUEST_CHANGES"
            else "COMMENT"
        )
        if accepted != event:
            log.warning(
                "gitea.review_recorded_as",
                repo=repo,
                pr=number,
                requested=event,
                recorded=recorded,
                hint="Gitea recorded the review with another state; it gates accordingly",
            )
        url = str(data.get("html_url") or "") or self._pull_url(repo, number)
        return SubmittedReview(
            url, accepted, review_id, self._capture_posted(repo, number, review_id, comments)
        )

    def pr_review_comments_create(
        self,
        repo: str,
        number: int,
        comments: Sequence[ReviewComment],
        *,
        commit_id: str,
    ) -> tuple[PostedFinding, ...]:
        """Each finding as an inline comment of one COMMENT review anchored
        on ``commit_id``; a head that moved past it refuses every anchor."""
        if not comments:
            return ()
        head = str(self._pull(repo, number).get("head", {}).get("sha") or "")
        if head != commit_id:
            raise GithubOpsError(
                f"pull request #{number} head {head[:12]} no longer matches the reviewed "
                f"commit {commit_id[:12]}"
            )
        path = f"{self._pull_path(repo, number)}/reviews"
        data = self._dict(
            f"POST {path}", self.raw("POST", path, self._review_body("COMMENT", "", comments, head))
        )
        review_id = data.get("id") if isinstance(data.get("id"), int) else None
        return self._capture_posted(repo, number, review_id, comments)

    def pr_review_locations(
        self, repo: str, number: int, *, commit_id: str | None
    ) -> dict[str, tuple[range, ...]]:
        """The commentable RIGHT-side ranges of the pull request's diff at
        ``commit_id``, from ``GET .../pulls/:n.diff`` (field-verified),
        with the head checked before and after the read."""

        def head() -> str:
            sha = str(self._pull(repo, number).get("head", {}).get("sha") or "")
            if not commit_id or sha != commit_id:
                raise GithubOpsError("pull request head no longer matches the reviewed commit")
            return sha

        before = head()
        locations = {
            path: right_side_ranges(patch)
            for path, patch in self._diff_by_file(repo, number).items()
        }
        if head() != before:
            raise GithubOpsError("pull request head changed while reading its diff")
        return locations

    def pr_reviews(self, repo: str, number: int) -> list[Any]:
        return [
            review_record(row, self._kind_of)
            for row in self._reviews(repo, number)
            if isinstance(row, dict)
        ]

    def pr_review_comments(self, repo: str, number: int) -> list[Any]:
        """Every inline comment across the request's reviews."""
        records: list[dict[str, Any]] = []
        for review in self._reviews(repo, number):
            if not isinstance(review, dict) or not review.get("comments_count"):
                continue
            for row in self._review_comments_of(repo, number, review.get("id")):
                if isinstance(row, dict):
                    records.append(
                        review_comment_record(
                            row, review_id=review.get("id"), kind_of=self._kind_of
                        )
                    )
        return records

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
        """What the reviewers said, for a fix round's brief: every review
        body and inline comment not the loop's own, each with its anchor."""

        def excluded(user: Any) -> bool:
            return exclude_login is not None and identities_match(
                user_identity(user), (exclude_login, exclude_is_bot)
            )

        parts: list[str] = []
        for review in self.pr_reviews(repo, number):
            body = str(review.get("body") or "").strip()
            if body and not excluded(review.get("user")):
                parts.append(f"- {body}")
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
        """One thread per inline comment: Gitea has no thread object."""
        threads: list[ReviewThread] = []
        for review in self._reviews(repo, number):
            if not isinstance(review, dict) or not review.get("comments_count"):
                continue
            for row in self._review_comments_of(repo, number, review.get("id")):
                if isinstance(row, dict):
                    thread = review_thread(repo, number, row, kind_of=self._kind_of)
                    if thread is not None:
                        threads.append(thread)
        return threads

    def pr_comment_reply(self, repo: str, number: int, comment_id: int, body: str) -> str:
        """Gitea has no reply-in-thread (#1016 V1): the answer is a
        change-level comment quoting the finding's anchor, so a reader
        sees which comment it answers."""
        anchor = ""
        for thread in self.pr_review_threads(repo, number):
            if thread.root_comment_id == comment_id:
                anchor = thread.anchor
                break
        quoted = f"Re `{anchor}`:\n\n{body}" if anchor else body
        return str(self._pull_comment(repo, number, quoted)["html_url"])

    def pr_issue_comment(self, repo: str, number: int, body: str) -> str:
        return str(self._pull_comment(repo, number, body)["html_url"])

    def resolve_review_thread(self, thread_id: str) -> bool:
        """``False``, always: Gitea's API has no path that resolves a review
        comment (#1016 V1, verified against the swagger document); the
        loop leaves the comment for a person and says so once."""
        try:
            repo, number, comment_id = parse_thread_id(thread_id)
        except ValueError as exc:
            raise GithubOpsError(str(exc)) from exc
        log.info(
            "gitea.resolve_unsupported",
            repo=repo,
            pr=number,
            comment=comment_id,
            hint="Gitea's API cannot resolve a review comment; a person can, in the web UI",
        )
        return False

    # -- ContentOps: a changeset staged here, written through the contents API --
    #
    # Gitea has no blob, tree or commit a client creates (#1016 V5, and the
    # swagger's /git/ paths are GET only, verified): `POST .../contents`
    # takes a whole changeset (create/update/delete, base64 content) onto a
    # branch, optionally cut as `new_branch` from `branch`. A branch is
    # created at a commit with `POST .../branches {old_ref_name}` (verified)
    # and never deleted and recreated: deleting the head branch of an open
    # pull request closes it (verified). A fix round therefore commits the
    # difference between the branch's tree and the wanted tree on top of
    # the branch.

    def _tree(self, repo: str, sha: str) -> dict[str, tuple[str, str]]:
        """The whole tree behind commit ``sha``: path -> (mode, blob sha),
        blobs only (``GET .../git/trees/:sha?recursive=true``, paged by
        ``per_page``/``page`` with ``truncated``, field-verified)."""
        key = (repo, sha)
        if key not in self._tree_cache:
            found: dict[str, tuple[str, str]] = {}
            for page in range(1, MAX_PAGES + 1):
                query = urlencode({"recursive": "true", "per_page": TREE_PAGE_SIZE, "page": page})
                path = f"{self._repo(repo)}/git/trees/{sha}?{query}"
                data = self._dict(f"GET {path}", self.raw("GET", path))
                rows = data.get("tree")
                for row in rows if isinstance(rows, list) else []:
                    if isinstance(row, dict) and row.get("type") == "blob" and row.get("path"):
                        found[str(row["path"])] = (
                            str(row.get("mode") or REGULAR_MODE),
                            str(row.get("sha") or ""),
                        )
                if not data.get("truncated"):
                    break
            else:
                raise PaginationError(f"the tree of {repo}@{sha[:12]} was not read to its end")
            self._tree_cache[key] = found
        return self._tree_cache[key]

    def _blob(self, repo: str, sha: str) -> bytes:
        """The bytes behind blob ``sha``: staged here, else read from Gitea
        (``GET .../git/blobs/:sha``, base64)."""
        raw = self._blobs.get((repo, sha))
        if raw is not None:
            return raw
        path = f"{self._repo(repo)}/git/blobs/{sha}"
        data = self._dict(f"GET {path}", self.raw("GET", path))
        content = data.get("content")
        raw = base64.b64decode(str(content or ""))
        self._blobs[(repo, sha)] = raw
        return raw

    def blobs_create_many(self, repo: str, files: list[dict[str, str]]) -> dict[str, str]:
        """Hash each file as git would and keep the bytes for the commit;
        nothing reaches Gitea."""
        shas: dict[str, str] = {}
        for entry in files:
            path = str(entry.get("path") or "")
            try:
                raw = base64.b64decode(str(entry.get("content_b64") or ""), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise GithubOpsError(f"blob for {path!r} is not base64: {exc}") from exc
            sha = blob_sha(raw)
            self._blobs[(repo, sha)] = raw
            shas[path] = sha
        return shas

    def commit_get(self, repo: str, sha: str) -> dict[str, Any]:
        path = f"{self._repo(repo)}/git/commits/{sha}"
        return commit_record(self._dict(f"GET {path}", self.raw("GET", path)))

    def tree_create(
        self, repo: str, *, base_tree: str, entries: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Stage the tree ``entries`` make of ``base_tree`` (the base commit's
        sha, see :meth:`commit_get`: Gitea lists a tree by the commit that
        holds it): the whole wanted tree (for a fix round's diff) and the
        contents operations that turn the base into it. A submodule pointer
        and a symlink are refused by name (the contents API writes neither);
        an executable's bit cannot
        be set through the contents API (no mode field, verified against
        the swagger), so a new executable lands ``100644`` and the log says
        so; a deletion of a path the base lacks is dropped."""
        if not base_tree:
            raise GithubOpsError("tree_create needs the base commit's tree sha as base_tree")
        base = self._tree(repo, base_tree)
        wanted = dict(base)
        files: list[dict[str, Any]] = []
        for entry in entries:
            path = str(entry.get("path") or "")
            if not path:
                raise GithubOpsError(f"a tree entry has no path: {entry!r}")
            mode = str(entry.get("mode") or REGULAR_MODE)
            if entry.get("type") == "commit" or mode == GITLINK_MODE:
                raise GithubOpsError(
                    f"Gitea's contents API has no operation for the submodule pointer at "
                    f"{path!r}; deliver a submodule change from a checkout"
                )
            if mode == SYMLINK_MODE:
                raise GithubOpsError(
                    f"Gitea's contents API writes no symlink ({path!r}); deliver it from a checkout"
                )
            sha = entry.get("sha")
            if sha is None:
                if path in wanted:
                    del wanted[path]
                    files.append({"operation": "delete", "path": path})
                else:
                    log.info("gitea.delete_skipped", repo=repo, path=path)
                continue
            raw = self._blobs.get((repo, str(sha)))
            if raw is None:
                raise GithubOpsError(
                    f"blob {sha} for {path!r} was not staged by this backend; the blobs and the "
                    "tree of one delivery are built by the same backend object"
                )
            if mode == EXECUTABLE_MODE and base.get(path, ("", ""))[0] != EXECUTABLE_MODE:
                log.warning(
                    "gitea.executable_bit_dropped",
                    repo=repo,
                    path=path,
                    hint="Gitea's contents API has no file mode; the file lands as 100644",
                )
            kept_mode = base.get(path, (REGULAR_MODE, ""))[0]
            wanted[path] = (kept_mode, str(sha))
            files.append(
                {
                    "operation": "update" if path in base else "create",
                    "path": path,
                    "content": base64.b64encode(raw).decode("ascii"),
                }
            )
        handle = tree_handle(base_tree, files)
        self._trees[(repo, handle)] = _Staged(base_tree, wanted, files)
        return {"sha": handle, "truncated": False}

    def _write_changeset(
        self,
        repo: str,
        *,
        branch: str,
        message: str,
        files: Sequence[Mapping[str, Any]],
        new_branch: str = "",
    ) -> str:
        """One ``POST .../contents``: the commit's sha (field-verified: an
        empty ``files`` list still writes a commit)."""
        body: dict[str, Any] = {
            "branch": branch,
            "message": message,
            "files": [dict(f) for f in files],
        }
        if new_branch:
            body["new_branch"] = new_branch
        path = f"{self._repo(repo)}/contents"
        data = self._dict(f"POST {path}", self.raw("POST", path, body))
        commit_raw = data.get("commit")
        commit: dict[str, Any] = commit_raw if isinstance(commit_raw, dict) else {}
        sha = str(commit.get("sha") or "")
        if not sha:
            raise GithubOpsError(f"POST {path} returned no commit sha: {data!r}")
        return sha

    def _branch_at(self, repo: str, branch: str, sha: str) -> None:
        """``POST .../branches {new_branch_name, old_ref_name}`` (verified);
        a branch that exists is Gitea's 409, raised for the caller."""
        self.raw(
            "POST", f"{self._repo(repo)}/branches", {"new_branch_name": branch, "old_ref_name": sha}
        )

    def commit_create(
        self, repo: str, *, message: str, tree: str, parents: list[str]
    ) -> dict[str, Any]:
        """Write the staged ``tree`` as a commit on ``parents[0]``: a pending
        branch is cut at the parent and the changeset committed on it; the
        ref step then creates the run's branch at the commit and drops the
        pending one."""
        staged = self._trees.get((repo, tree))
        if staged is None:
            raise GithubOpsError(
                f"tree {tree!r} was not staged by this backend; the tree and the commit of "
                "one delivery are built by the same backend object"
            )
        if len(parents) > 1:
            raise GithubOpsError("Gitea's contents API writes a commit with one parent")
        start = parents[0] if parents else staged.base
        branch = f"sbxloop/pending/{uuid4().hex[:12]}"
        self._branch_at(repo, branch, start)
        sha = self._write_changeset(repo, branch=branch, message=message, files=staged.files)
        self._pending[(repo, sha)] = _Pending(message, start, staged, branch)
        self._tree_cache[(repo, sha)] = dict(staged.wanted)
        return {"sha": sha, "tree": {"sha": sha}, "parents": [{"sha": start}], "message": message}

    def _drop_pending(self, repo: str, sha: str) -> None:
        pending = self._pending.get((repo, sha))
        if pending is None or not pending.branch:
            return
        self.raw_lookup("DELETE", f"{self._repo(repo)}/branches/{quote(pending.branch, safe='')}")
        pending.branch = ""

    def ref_create(self, repo: str, ref: str, sha: str) -> None:
        if not ref.startswith("refs/heads/"):
            raise GithubOpsError(f"ref_create takes refs/heads/<branch>, got {ref!r}")
        self._branch_at(repo, ref[len("refs/heads/") :], sha)
        self._drop_pending(repo, sha)

    def ref_force_update(self, repo: str, branch: str, sha: str) -> None:
        """Make ``branch`` carry the tree ``sha`` stands for. Gitea has no
        call that moves a branch and deleting one closes its open pull
        request (both verified), so a branch that exists gets one more
        commit: the difference between its tree and the wanted tree. Only
        a commit this backend wrote (or any commit whose tree it can read)
        can be the target."""
        head = self.ref_lookup(repo, f"heads/{branch}")
        if head == sha:
            self._drop_pending(repo, sha)
            return
        if head is None:
            self.ref_create(repo, f"refs/heads/{branch}", sha)
            return
        pending = self._pending.get((repo, sha))
        wanted = pending.staged.wanted if pending is not None else self._tree(repo, sha)
        current = self._tree(repo, head)
        files: list[dict[str, Any]] = []
        for path, (_mode, blob) in sorted(wanted.items()):
            if current.get(path, ("", ""))[1] == blob:
                continue
            files.append(
                {
                    "operation": "update" if path in current else "create",
                    "path": path,
                    "content": base64.b64encode(self._blob(repo, blob)).decode("ascii"),
                }
            )
        for path in sorted(set(current) - set(wanted)):
            files.append({"operation": "delete", "path": path})
        message = (
            pending.message if pending is not None else f"sbxloop: bring {branch} to {sha[:12]}"
        )
        written = self._write_changeset(repo, branch=branch, message=message, files=files)
        self._tree_cache[(repo, written)] = dict(wanted)
        log.info(
            "gitea.branch_rewritten",
            repo=repo,
            branch=branch,
            previous=head[:12],
            wanted=sha[:12],
            commit=written[:12],
            changes=len(files),
            hint="one more commit on the branch, with the wanted tree; the pull request follows",
        )
        self._drop_pending(repo, sha)

    def contents_put(
        self, repo: str, path: str, *, message: str, content_b64: str, branch: str
    ) -> dict[str, Any]:
        """Create or replace one file on ``branch`` in one commit; a branch
        the repository lacks is cut from the default branch (or is the
        first commit of an empty repository, **field-unverified**)."""
        head = self.ref_lookup(repo, f"heads/{branch}")
        new_branch = ""
        source = branch
        if head is None:
            payload = self._repo_payload(repo)
            default = str(payload.get("default_branch") or "")
            if default and default != branch and not payload.get("empty"):
                source, new_branch = default, branch
        exists = self._contents(repo, path, source) is not None if (head or new_branch) else False
        operation = {
            "operation": "update" if exists else "create",
            "path": path,
            "content": content_b64,
        }
        sha = self._write_changeset(
            repo, branch=source, message=message, files=[operation], new_branch=new_branch
        )
        try:
            blob = blob_sha(base64.b64decode(content_b64, validate=True))
        except (ValueError, binascii.Error):
            blob = ""
        return {"content": {"path": path, "sha": blob}, "commit": {"sha": sha}}


def _implements(ops: GiteaOps) -> VcsOps:
    """The type checker's proof that :class:`GiteaOps` satisfies every role."""
    return ops


def _is_sha(ref: str) -> bool:
    return len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower())
