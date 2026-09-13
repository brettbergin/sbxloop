"""A scripted stand-in for :class:`sbxloop.vcs.gitea.ops.GiteaOps`.

The Gitea every Gitea test runs against (#1021), at the fidelity the
GitHub and GitLab fakes give their backends: the real backend class with
no worker behind it, its generic transport (``raw``, ``raw_lookup``,
``raw_text``) answered from in-memory state shaped the way Gitea 1.24.7
shaped its answers in #1016 and the #1021 probe, and a ledger of what it
was asked.

State is a repository ``acme/widgets`` with ``main`` at ``base123`` (a
README), a write collaborator ``sbxloop-bot``, a reviewer ``rev-bob`` and
a bot account ``ci-bot`` that reads exactly like a human (Gitea has no bot
flag), and the knobs below:

- ``branch_rules``: what ``GET /branches/main`` reports of its protection
  (``None`` = not protected); ``admin`` makes the token an admin, which
  reads the protection rule too.
- ``branches``: name -> sha; ``trees``: sha -> path -> (mode, bytes);
  ``commits``: sha -> record; ``empty`` for a repository with no commit.
- ``issues``, ``comments``, ``timeline``, ``labels``.
- ``statuses``: sha -> the newest status per context.
- ``pulls``: by number, with their reviews and review comments.
- ``fail_once`` / ``fail_always``: method name -> exception.

A ``raw`` call this fake does not model fails loudly rather than
pretending, exactly as the other fakes do.
"""

from __future__ import annotations

import base64
import posixpath
import re
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qs, unquote

from sbxloop.errors import GithubOpsError
from sbxloop.vcs.gitea.ops import GiteaOps
from sbxloop.vcs.gitea.records import is_draft_title
from sbxloop.vcs.model import ChecksVerdict, FailedCheck

WEB = "https://gitea.example"
# The diff every seeded pull request carries unless told otherwise.
_DEFAULT_DIFF = (
    "diff --git a/a.py b/a.py\nnew file mode 100644\nindex 0000000..c1827f0\n"
    "--- /dev/null\n+++ b/a.py\n@@ -0,0 +1,3 @@\n+x = 1\n+y = 2\n+z = 3\n"
)


def gitea_error(status: int, message: str) -> GithubOpsError:
    """An error the way the REST transport words it, so a caller matching
    on words sees Gitea's own."""
    return GithubOpsError(
        f"gitea op raw.api failed: GithubOpError: HTTP {status}: {message}", http_status=status
    )


class FakeGitea(GiteaOps):
    def __init__(self, *, repo: str = "acme/widgets", bot_logins: Sequence[str] = ()) -> None:
        # Deliberately no super().__init__: there is no worker client.
        self.run_id = "fake"
        self.timeout_s = 0.0
        self.transport = None
        self.bot_logins = frozenset(n.casefold() for n in bot_logins)
        self._repos = {}
        self._pr_urls = {}
        self._blobs = {}
        self._trees = {}
        self._pending = {}
        self._tree_cache = {}
        self.repo = repo
        self.user_login = "sbxloop-bot"
        self.user_id = 2
        self.users: dict[str, dict[str, Any]] = {
            "sbxloop-bot": {"id": 2, "login": "sbxloop-bot", "full_name": "sbxloop bot"},
            "rev-bob": {"id": 3, "login": "rev-bob", "full_name": ""},
            "ci-bot": {"id": 4, "login": "ci-bot", "full_name": "ci bot"},
        }
        self.admin = False
        self.push = True
        self.empty = False
        self.missing_repo = False
        self.has_issues = True
        # What GET /branches/main reports of its protection (#1016 V4).
        self.branch_rules: dict[str, Any] | None = None
        # What GET /branch_protections/main reports to an admin.
        self.protection_rule: dict[str, Any] = {
            "rule_name": "main",
            "block_on_rejected_reviews": True,
            "dismiss_stale_approvals": True,
            "require_signed_commits": False,
        }
        self.branches: dict[str, str] = {"main": "base123"}
        self.trees: dict[str, dict[str, tuple[str, bytes]]] = {
            "base123": {"README.md": ("100644", b"# widgets\n")}
        }
        self.commits: dict[str, dict[str, Any]] = {
            "base123": {"sha": "base123", "parents": [], "message": "Initial commit\n"}
        }
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.timeline: dict[int, list[dict[str, Any]]] = {}
        self.labels: list[dict[str, Any]] = [
            {"id": 1, "name": "bug", "color": "ee0701", "description": ""}
        ]
        self.statuses: dict[str, dict[str, dict[str, Any]]] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.workflows: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.merge_ok = True
        self.update_ok = True
        self.fail_once: dict[str, Exception] = {}
        self.fail_always: dict[str, Exception] = {}
        # The ledger.
        self.raw_calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.text_calls: list[str] = []
        self.failed_jobs: list[tuple[str, str, str, int | None]] = []
        self.statuses_posted: list[tuple[str, dict[str, Any]]] = []
        self.deleted_branches: list[str] = []
        self.branch_creates: list[tuple[str, str]] = []
        self.content_posts: list[dict[str, Any]] = []
        self.reviews_posted: list[tuple[int, dict[str, Any]]] = []
        self.merges: list[tuple[int, dict[str, Any]]] = []
        self.updates: list[int] = []
        self.pull_patches: list[tuple[int, dict[str, Any]]] = []
        self.reviewer_requests: list[tuple[int, list[str]]] = []
        self.label_posts: list[tuple[int, list[Any]]] = []
        self.label_deletes: list[tuple[int, int]] = []
        self.comments_deleted: list[int] = []
        self._seq = 100
        self._missing_ok = False

    # -- plumbing ------------------------------------------------------------

    @property
    def web_url(self) -> str:
        return f"{WEB}/{self.repo}"

    def _op(self, op: str, params: dict[str, Any], *, timeout_s: float | None = None) -> Any:
        raise AssertionError(f"FakeGitea: unexpected worker op {op}")

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def _maybe_fail(self, method: str) -> None:
        exc = self.fail_once.pop(method, None)
        if exc is None:
            exc = self.fail_always.get(method)
        if exc is not None:
            self.failed_jobs.append((method, "", method, getattr(exc, "http_status", None)))
            raise exc

    @contextmanager
    def _allow_missing(self):  # type: ignore[no-untyped-def]
        previous = self._missing_ok
        self._missing_ok = True
        try:
            yield
        finally:
            self._missing_ok = previous

    def _failed(self, method: str, path: str, status: int, message: str) -> GithubOpsError:
        exc = gitea_error(status, message)
        if not (self._missing_ok and status in (404,)):
            self.failed_jobs.append(("raw.api", method, path, status))
        return exc

    def raw_lookup(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        missing: Sequence[int] = (404,),
    ) -> Any:
        before = len(self.failed_jobs)
        with self._allow_missing():
            try:
                return self.raw(method, path, body)
            except GithubOpsError as exc:
                if exc.http_status in missing:
                    del self.failed_jobs[before:]
                    return None
                raise

    def raw_text(self, method: str, path: str, *, missing: Sequence[int] = ()) -> str | None:
        self.text_calls.append(path)
        match = re.fullmatch(rf"/repos/{re.escape(self.repo)}/pulls/(\d+)\.diff", path)
        assert match, f"FakeGitea: unexpected text call {method} {path}"
        pull = self.pulls.get(int(match.group(1)))
        if pull is None:
            if 404 in missing:
                return None
            raise gitea_error(404, "The target couldn't be found.")
        return str(pull.get("diff") or "")

    # -- payload builders ---------------------------------------------------------

    def _user(self, login: str) -> dict[str, Any]:
        user = self.users.get(login) or {"id": 99, "login": login, "full_name": ""}
        return {
            "id": user["id"],
            "login": user["login"],
            "login_name": user["login"],
            "full_name": user.get("full_name", ""),
            "username": user["login"],
            "html_url": f"{WEB}/{login}",
            "is_admin": False,
        }

    def _repo_json(self) -> dict[str, Any]:
        owner, name = self.repo.split("/", 1)
        return {
            "id": 1,
            "owner": self._user(owner),
            "name": name,
            "full_name": self.repo,
            "private": False,
            "empty": self.empty,
            "html_url": self.web_url,
            "default_branch": "main",
            "has_issues": self.has_issues,
            "allow_merge_commits": True,
            "allow_squash_merge": True,
            "allow_rebase": True,
            "permissions": {"admin": self.admin, "push": self.push, "pull": True},
        }

    def _branch_json(self, name: str) -> dict[str, Any]:
        sha = self.branches[name]
        payload: dict[str, Any] = {
            "name": name,
            "commit": {"id": sha, "message": self.commits.get(sha, {}).get("message", "")},
            "protected": False,
            "required_approvals": 0,
            "enable_status_check": False,
            "status_check_contexts": [],
            "user_can_push": True,
            "user_can_merge": True,
        }
        if name == "main" and self.branch_rules is not None:
            payload.update({"protected": True, **self.branch_rules})
        return payload

    def _label_json(self, label: dict[str, Any]) -> dict[str, Any]:
        return {**label, "url": f"{WEB}/api/v1/repos/{self.repo}/labels/{label['id']}"}

    def _issue_json(self, issue: dict[str, Any]) -> dict[str, Any]:
        number = issue["number"]
        labels = [lb for lb in self.labels if lb["id"] in issue["label_ids"]]
        return {
            "id": 1000 + number,
            "number": number,
            "title": issue["title"],
            "body": issue.get("body") or "",
            "state": issue["state"],
            "labels": [self._label_json(lb) for lb in labels],
            "user": self._user(issue.get("author", self.user_login)),
            "html_url": f"{self.web_url}/issues/{number}",
            "comments": len(self.comments.get(number, [])),
            "created_at": issue.get("created_at", "2026-09-12T20:00:00Z"),
            "updated_at": issue.get("updated_at", "2026-09-12T20:00:00Z"),
            "closed_at": None,
            "pull_request": None,
        }

    def _comment_json(self, number: int, comment: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": comment["id"],
            "body": comment["body"],
            "user": self._user(comment.get("author", self.user_login)),
            "html_url": f"{self.web_url}/issues/{number}#issuecomment-{comment['id']}",
            "created_at": comment.get("created_at", "2026-09-12T20:01:00Z"),
        }

    def _pull_json(self, pull: dict[str, Any]) -> dict[str, Any]:
        number = pull["number"]
        return {
            "id": 2000 + number,
            "number": number,
            "title": pull["title"],
            "body": pull.get("body") or "",
            "state": pull["state"],
            "draft": is_draft_title(pull["title"]),
            "merged": pull.get("merged", False),
            "merged_at": pull.get("merged_at"),
            "merge_commit_sha": pull.get("merge_commit_sha"),
            "mergeable": pull.get("mergeable", True) and not is_draft_title(pull["title"]),
            "merge_base": "base123",
            "html_url": f"{self.web_url}/pulls/{number}",
            "user": self._user(pull.get("author", self.user_login)),
            "head": {
                "label": pull["head"],
                "ref": pull["head"],
                "sha": self.branches.get(pull["head"], pull.get("head_sha", "")),
                "repo": self._repo_json(),
            },
            "base": {"ref": pull["base"], "sha": self.branches["main"], "repo": self._repo_json()},
            "requested_reviewers": [self._user(login) for login in pull.get("reviewers", [])],
            "labels": [],
            "created_at": "2026-09-12T21:20:41Z",
            "updated_at": "2026-09-12T21:20:42Z",
        }

    def _review_json(self, review: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": review["id"],
            "state": review["state"],
            "body": review.get("body", ""),
            "user": self._user(review.get("author", self.user_login)),
            "commit_id": review.get("commit_id", ""),
            "comments_count": len(review.get("comments", [])),
            "dismissed": review.get("dismissed", False),
            "html_url": f"{self.web_url}/pulls/{review['number']}#issuecomment-{review['id']}",
            "submitted_at": "2026-09-12T21:30:00Z",
        }

    def _review_comment_json(
        self, review: dict[str, Any], comment: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "id": comment["id"],
            "body": comment["body"],
            "path": comment["path"],
            "position": comment.get("position", 0),
            "original_position": comment.get("position", 0),
            "commit_id": review.get("commit_id", ""),
            "diff_hunk": "@@ -0,0 +1 @@\n+a",
            "html_url": f"{self.web_url}/pulls/{review['number']}#issuecomment-{comment['id']}",
            "user": self._user(review.get("author", self.user_login)),
            "pull_request_review_id": review["id"],
            "resolver": comment.get("resolver"),
            "created_at": "2026-09-12T21:30:00Z",
        }

    def _commit_json(self, sha: str) -> dict[str, Any]:
        record = self.commits[sha]
        return {
            "sha": sha,
            "html_url": f"{self.web_url}/commit/{sha}",
            "tree": None,
            "parents": [{"sha": p} for p in record["parents"]],
            "commit": {"message": record["message"], "tree": {"sha": f"tree-{sha}"}},
        }

    # -- seeds ------------------------------------------------------------------

    def seed_issue(
        self,
        number: int,
        title: str,
        labels: Sequence[str] = (),
        *,
        state: str = "open",
        body: str = "",
        author: str | None = None,
    ) -> None:
        ids = [self._ensure_label(name)["id"] for name in labels]
        self.issues[number] = {
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "label_ids": ids,
            "author": author or self.user_login,
        }

    def _ensure_label(self, name: str) -> dict[str, Any]:
        for label in self.labels:
            if label["name"].casefold() == name.casefold():
                return label
        label = {"id": self._next(), "name": name, "color": "cccccc", "description": ""}
        self.labels.append(label)
        return label

    def seed_status(
        self, sha: str, context: str, state: str, *, description: str = "", target_url: str = ""
    ) -> None:
        self.statuses.setdefault(sha, {})[context] = {
            "id": self._next(),
            "status": state,
            "context": context,
            "description": description,
            "target_url": target_url,
            "creator": self._user(self.user_login),
        }

    def seed_verdict(
        self, sha: str, verdict: ChecksVerdict, *, logs: Sequence[FailedCheck] = ()
    ) -> None:
        self.statuses[sha] = {}
        excerpts = {log.name: log.excerpt for log in logs}
        for name in verdict.passed:
            self.seed_status(sha, name, "success")
        for name in verdict.pending:
            self.seed_status(sha, name, "pending")
        for name in verdict.failed:
            self.seed_status(sha, name, "failure", description=excerpts.get(name, ""))

    def seed_pull(
        self,
        number: int,
        *,
        head: str = "sbxloop/r1",
        title: str = "sbxloop: ship it",
        head_sha: str = "commit0",
        state: str = "open",
        author: str | None = None,
        mergeable: bool = True,
        diff: str = "",
    ) -> dict[str, Any]:
        self.branches.setdefault(head, head_sha)
        self.pulls[number] = {
            "number": number,
            "title": title,
            "body": "",
            "state": state,
            "head": head,
            "base": "main",
            "author": author or self.user_login,
            "mergeable": mergeable,
            "reviewers": [],
            "reviews": [],
            "diff": diff or _DEFAULT_DIFF,
        }
        return self.pulls[number]

    def seed_review(
        self,
        number: int,
        state: str,
        *,
        author: str = "rev-bob",
        body: str = "",
        comments: Sequence[tuple[str, int, str]] = (),
        dismissed: bool = False,
    ) -> int:
        review = {
            "id": self._next(),
            "number": number,
            "state": state,
            "body": body,
            "author": author,
            "dismissed": dismissed,
            "commit_id": self.branches.get(self.pulls[number]["head"], ""),
            "comments": [
                {"id": self._next(), "path": path, "position": line, "body": text}
                for path, line, text in comments
            ],
        }
        self.pulls[number]["reviews"].append(review)
        return int(review["id"])

    # -- routes ----------------------------------------------------------------------

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.raw_calls.append((method, path, body))
        self._maybe_fail("raw")
        full = path
        path, _, query = path.partition("?")
        params = parse_qs(query)
        if method == "GET" and params.get("limit") == ["1"]:
            # The doctor's permission probes (#696) read one entry per endpoint.
            self._maybe_fail("permission_probe")
        if path == "/version":
            return {"version": "1.24.7"}
        if path == "/user":
            self._maybe_fail("authenticated_user")
            return self._user(self.user_login)
        if match := re.fullmatch(r"/users/([^/]+)", path):
            login = unquote(match.group(1))
            if login not in self.users:
                raise self._failed(
                    method, path, 404, f"user redirect does not exist [name: {login}]"
                )
            return self._user(login)
        if method == "POST" and (path == "/user/repos" or re.fullmatch(r"/orgs/[^/]+/repos", path)):
            assert body is not None
            owner = self.user_login if path == "/user/repos" else path.split("/")[2]
            self.repo = f"{owner}/{body['name']}"
            self.empty = False
            return self._repo_json()
        repo = re.match(rf"/repos/{re.escape(self.repo)}(/.*)?$", path)
        if repo is None:
            if path.startswith("/repos/"):
                raise self._failed(method, path, 404, "The target couldn't be found.")
            raise AssertionError(f"FakeGitea: unexpected raw call {method} {full}")
        if self.missing_repo:
            raise self._failed(method, path, 404, "The target couldn't be found.")
        rest = repo.group(1) or ""
        if rest == "":
            self._maybe_fail("repo_get")
            return self._repo_json()
        for handler in (
            self._branch_routes,
            self._content_routes,
            self._issue_routes,
            self._pull_routes,
            self._status_routes,
        ):
            answer = handler(method, path, rest, params, body)
            if answer is not None:
                return answer
        raise AssertionError(f"FakeGitea: unexpected raw call {method} {full}")

    def _branch_routes(
        self, method: str, path: str, rest: str, params: dict[str, list[str]], body: Any
    ) -> Any:
        if rest == "/branches" and method == "POST":
            assert body is not None
            name = str(body["new_branch_name"])
            ref = str(body.get("old_ref_name") or body.get("old_branch_name") or "main")
            self.branch_creates.append((name, ref))
            if name in self.branches:
                raise self._failed(method, path, 409, "The branch already exists.")
            sha = self.branches.get(ref, ref)
            if sha not in self.commits:
                raise self._failed(method, path, 404, "The target couldn't be found.")
            self.branches[name] = sha
            return self._branch_json(name)
        if rest == "/branches" and method == "GET":
            return [self._branch_json(name) for name in sorted(self.branches)]
        if match := re.fullmatch(r"/branches/([^/]+)", rest):
            name = unquote(match.group(1))
            if method == "DELETE":
                self.deleted_branches.append(name)
                if name not in self.branches:
                    raise self._failed(method, path, 404, "The target couldn't be found.")
                del self.branches[name]
                for pull in self.pulls.values():
                    if pull["head"] == name and pull["state"] == "open":
                        pull["state"] = "closed"  # deleting the head closes the request (verified)
                return {}
            if self.empty or name not in self.branches:
                raise self._failed(method, path, 404, "The target couldn't be found.")
            return self._branch_json(name)
        if re.fullmatch(r"/tags/([^/]+)", rest):
            raise self._failed(method, path, 404, "tag doesn't exist")
        if match := re.fullmatch(r"/branch_protections/([^/]+)", rest):
            if not self.admin:
                raise self._failed(
                    method,
                    path,
                    403,
                    "user should be an owner or a collaborator with admin write of a repository",
                )
            if unquote(match.group(1)) != "main" or self.branch_rules is None:
                raise self._failed(method, path, 404, "The target couldn't be found.")
            return dict(self.protection_rule)
        if match := re.fullmatch(r"/compare/([^.]+)\.\.\.(.+)", rest):
            base, head = unquote(match.group(1)), unquote(match.group(2))
            base_sha = self.branches.get(base, base)
            head_sha = self.branches.get(head, head)
            if head_sha not in self.commits:
                raise self._failed(method, path, 404, "The target couldn't be found.")
            commits = []
            sha = head_sha
            while sha and sha != base_sha and sha in self.commits:
                commits.append(
                    {
                        "sha": sha,
                        "parents": [{"sha": p} for p in self.commits[sha]["parents"]],
                        "commit": {"message": self.commits[sha]["message"]},
                    }
                )
                parents = self.commits[sha]["parents"]
                sha = parents[0] if parents else ""
            return {"total_commits": len(commits), "commits": commits}
        return None

    def _tree_at(self, ref: str) -> dict[str, tuple[str, bytes]] | None:
        return self.trees.get(self.branches.get(ref, ref))

    def _content_routes(
        self, method: str, path: str, rest: str, params: dict[str, list[str]], body: Any
    ) -> Any:
        if match := re.fullmatch(r"/git/commits/([^/]+)", rest):
            sha = unquote(match.group(1))
            if sha not in self.commits:
                raise self._failed(method, path, 404, "The target couldn't be found.")
            return self._commit_json(sha)
        if match := re.fullmatch(r"/git/trees/([^/]+)", rest):
            tree = self._tree_at(unquote(match.group(1)))
            if tree is None:
                raise self._failed(method, path, 404, "The target couldn't be found.")
            rows = [
                {
                    "path": p,
                    "mode": mode,
                    "type": "blob",
                    "sha": _git_blob_sha(raw),
                    "size": len(raw),
                }
                for p, (mode, raw) in sorted(tree.items())
            ]
            dirs = sorted({posixpath.dirname(p) for p in tree if "/" in p})
            rows += [
                {"path": d, "mode": "040000", "type": "tree", "sha": f"tree-{d}"} for d in dirs
            ]
            per_page = int(params.get("per_page", ["1000"])[0])
            page = int(params.get("page", ["1"])[0])
            chunk = rows[(page - 1) * per_page : page * per_page]
            return {
                "sha": match.group(1),
                "tree": chunk,
                "page": page,
                "total_count": len(rows),
                "truncated": page * per_page < len(rows),
            }
        if match := re.fullmatch(r"/git/blobs/([^/]+)", rest):
            wanted = unquote(match.group(1))
            for tree in self.trees.values():
                for _mode, raw in tree.values():
                    if _git_blob_sha(raw) == wanted:
                        return {
                            "sha": wanted,
                            "content": base64.b64encode(raw).decode(),
                            "encoding": "base64",
                        }
            raise self._failed(method, path, 404, "The target couldn't be found.")
        if rest == "/contents" and method == "POST":
            assert body is not None
            return self._change_files(method, path, dict(body))
        if rest == "/contents" and method == "GET":
            tree = self._tree_at(params.get("ref", ["main"])[0])
            if tree is None:
                raise self._failed(method, path, 404, "The target couldn't be found.")
            return [{"name": p, "path": p, "type": "file"} for p in sorted(tree)]
        if match := re.fullmatch(r"/contents/(.+)", rest):
            file_path = unquote(match.group(1))
            tree = self._tree_at(params.get("ref", ["main"])[0])
            if tree is None or file_path not in tree:
                raise self._failed(method, path, 404, "The target couldn't be found.")
            _mode, raw = tree[file_path]
            return {
                "name": posixpath.basename(file_path),
                "path": file_path,
                "sha": _git_blob_sha(raw),
                "type": "file",
                "size": len(raw),
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
            }
        return None

    def _change_files(self, method: str, path: str, body: dict[str, Any]) -> Any:
        self.content_posts.append(body)
        self._maybe_fail("contents")
        branch = str(body["branch"])
        new_branch = str(body.get("new_branch") or "")
        if branch not in self.branches:
            if self.empty and not new_branch:
                parent = None
            else:
                raise self._failed(method, path, 404, "The target couldn't be found.")
        else:
            parent = self.branches[branch]
        if branch == "main" and self.branch_rules is not None and not new_branch:
            raise self._failed(
                method, path, 403, "user should have a permission to write to the branch"
            )
        target = new_branch or branch
        if new_branch and new_branch in self.branches:
            raise self._failed(method, path, 422, "branch already exists")
        tree = dict(self.trees.get(parent, {})) if parent else {}
        for op in body.get("files") or []:
            kind, file_path = str(op["operation"]), str(op["path"])
            if kind == "create":
                if file_path in tree:
                    raise self._failed(
                        method, path, 422, f"repository file already exists [path: {file_path}]"
                    )
                tree[file_path] = ("100644", base64.b64decode(str(op.get("content") or "")))
            elif kind == "update":
                if file_path not in tree:
                    raise self._failed(
                        method, path, 404, f"repository file does not exist [path: {file_path}]"
                    )
                tree[file_path] = (
                    tree[file_path][0],
                    base64.b64decode(str(op.get("content") or "")),
                )
            elif kind == "delete":
                if file_path not in tree:
                    raise self._failed(
                        method, path, 404, f"repository file does not exist [path: {file_path}]"
                    )
                del tree[file_path]
            else:
                raise self._failed(method, path, 422, f"unknown operation {kind}")
        sha = f"gt{self._next():06d}"
        self.trees[sha] = tree
        self.commits[sha] = {
            "sha": sha,
            "parents": [parent] if parent else [],
            "message": str(body.get("message") or ""),
        }
        self.branches[target] = sha
        self.empty = False
        return {
            "commit": self._commit_json(sha),
            "files": [{"path": op["path"]} for op in body.get("files") or []],
        }

    def _issue_routes(
        self, method: str, path: str, rest: str, params: dict[str, list[str]], body: Any
    ) -> Any:
        if rest == "/labels":
            if method == "POST":
                assert body is not None
                label = {
                    "id": self._next(),
                    "name": str(body["name"]),
                    "color": str(body.get("color") or "").removeprefix("#"),
                    "description": str(body.get("description") or ""),
                }
                self.labels.append(label)
                return self._label_json(label)
            return [self._label_json(lb) for lb in self.labels]
        if rest == "/issues" and method == "POST":
            assert body is not None
            number = max([*self.issues, *self.pulls], default=0) + 1
            for label_id in body.get("labels") or []:
                if not any(lb["id"] == label_id for lb in self.labels):
                    raise self._failed(
                        method, path, 422, f"label does not exist [label_id: {label_id}]"
                    )
            self.issues[number] = {
                "number": number,
                "title": str(body["title"]),
                "body": str(body.get("body") or ""),
                "state": "open",
                "label_ids": list(body.get("labels") or []),
                "author": self.user_login,
            }
            return self._issue_json(self.issues[number])
        if rest == "/issues" and method == "GET":
            state = params.get("state", ["open"])[0]
            wanted = [n for n in params.get("labels", [""])[0].split(",") if n]
            q = params.get("q", [""])[0].lower()
            rows = []
            for issue in self.issues.values():
                if state != "all" and issue["state"] != state:
                    continue
                names = {lb["name"] for lb in self.labels if lb["id"] in issue["label_ids"]}
                if any(n not in names for n in wanted):
                    continue
                if q and q not in f"{issue['title']} {issue.get('body', '')}".lower():
                    continue
                rows.append(self._issue_json(issue))
            if params.get("type", ["issues"])[0] != "issues":
                rows += [
                    {**self._pull_json(p), "pull_request": {"merged": p.get("merged", False)}}
                    for p in self.pulls.values()
                ]
            limit = int(params.get("limit", ["30"])[0])
            page = int(params.get("page", ["1"])[0])
            return rows[(page - 1) * limit : page * limit]
        if match := re.fullmatch(r"/issues/comments/(\d+)", rest):
            comment_id = int(match.group(1))
            if method == "DELETE":
                for rows in self.comments.values():
                    for comment in rows:
                        if comment["id"] == comment_id:
                            rows.remove(comment)
                            self.comments_deleted.append(comment_id)
                            return {}
                raise self._failed(method, path, 404, "The target couldn't be found.")
            return None
        match = re.fullmatch(r"/issues/(\d+)(/.*)?", rest)
        if match is None:
            return None
        number, tail = int(match.group(1)), match.group(2) or ""
        issue = self.issues.get(number)
        pull = self.pulls.get(number)
        if issue is None and pull is None:
            raise self._failed(method, path, 404, "The target couldn't be found.")
        if tail == "":
            if method == "PATCH" and issue is not None:
                assert body is not None
                if "state" in body:
                    issue["state"] = str(body["state"])
                if "title" in body:
                    issue["title"] = str(body["title"])
                return self._issue_json(issue)
            return self._issue_json(issue) if issue is not None else self._pull_json(pull)  # type: ignore[arg-type]
        if tail == "/comments":
            if method == "POST":
                assert body is not None
                comment = {"id": self._next(), "body": str(body["body"]), "author": self.user_login}
                self.comments.setdefault(number, []).append(comment)
                self.timeline.setdefault(number, []).append(
                    {"id": comment["id"], "type": "comment", "body": comment["body"]}
                )
                return self._comment_json(number, comment)
            return [self._comment_json(number, c) for c in self.comments.get(number, [])]
        if tail == "/timeline":
            rows = []
            for entry in self.timeline.get(number, []):
                row = {
                    "id": entry["id"],
                    "type": entry["type"],
                    "created_at": "2026-09-12T20:02:00Z",
                    "user": self._user(self.user_login),
                }
                if entry["type"] == "label":
                    row["label"] = self._label_json(entry["label"])
                    row["body"] = entry["body"]
                else:
                    row["body"] = entry.get("body", "")
                rows.append(row)
            return rows
        if tail == "/labels" and method == "POST" and issue is not None:
            assert body is not None
            self.label_posts.append((number, list(body.get("labels") or [])))
            for wanted in body.get("labels") or []:
                for label in self.labels:
                    matches = (isinstance(wanted, int) and label["id"] == wanted) or (
                        isinstance(wanted, str) and label["name"].casefold() == wanted.casefold()
                    )
                    if matches and label["id"] not in issue["label_ids"]:
                        issue["label_ids"].append(label["id"])
                        self.timeline.setdefault(number, []).append(
                            {"id": self._next(), "type": "label", "label": label, "body": "1"}
                        )
            # An unknown name is dropped without a word (field-verified).
            return [self._label_json(lb) for lb in self.labels if lb["id"] in issue["label_ids"]]
        if (
            (label_match := re.fullmatch(r"/labels/(\d+)", tail))
            and method == "DELETE"
            and issue is not None
        ):
            label_id = int(label_match.group(1))
            self.label_deletes.append((number, label_id))
            if label_id not in issue["label_ids"]:
                raise self._failed(
                    method, path, 422, f"label does not exist [label_id: {label_id}]"
                )
            issue["label_ids"].remove(label_id)
            label = next(lb for lb in self.labels if lb["id"] == label_id)
            self.timeline.setdefault(number, []).append(
                {"id": self._next(), "type": "label", "label": label, "body": ""}
            )
            return {}
        return None

    def _pull_routes(
        self, method: str, path: str, rest: str, params: dict[str, list[str]], body: Any
    ) -> Any:
        if rest == "/pulls" and method == "POST":
            assert body is not None
            head = str(body["head"])
            if head not in self.branches:
                raise self._failed(method, path, 404, "The target couldn't be found.")
            if any(p["head"] == head and p["state"] == "open" for p in self.pulls.values()):
                raise self._failed(
                    method, path, 409, "pull request already exists for these targets"
                )
            number = max([*self.issues, *self.pulls], default=0) + 1
            self.pulls[number] = {
                "number": number,
                "title": str(body["title"]),
                "body": str(body.get("body") or ""),
                "state": "open",
                "head": head,
                "base": str(body.get("base") or "main"),
                "author": self.user_login,
                "mergeable": True,
                "reviewers": [],
                "reviews": [],
                "diff": _DEFAULT_DIFF,
            }
            return self._pull_json(self.pulls[number])
        if rest == "/pulls" and method == "GET":
            state = params.get("state", ["open"])[0]
            rows = [
                self._pull_json(p)
                for p in self.pulls.values()
                if state == "all" or p["state"] == state
            ]
            limit = int(params.get("limit", ["30"])[0])
            page = int(params.get("page", ["1"])[0])
            return rows[(page - 1) * limit : page * limit]
        match = re.fullmatch(r"/pulls/(\d+)(/.*)?", rest)
        if match is None:
            return None
        number, tail = int(match.group(1)), match.group(2) or ""
        pull = self.pulls.get(number)
        if pull is None:
            raise self._failed(method, path, 404, "The target couldn't be found.")
        if tail == "":
            if method == "PATCH":
                assert body is not None
                self.pull_patches.append((number, dict(body)))
                for key in ("title", "body"):
                    if key in body:
                        pull[key] = str(body[key])
                if body.get("state"):
                    pull["state"] = str(body["state"])
                return self._pull_json(pull)
            return self._pull_json(pull)
        if tail == "/files":
            return [
                {
                    "filename": "a.py",
                    "status": "added",
                    "additions": 3,
                    "deletions": 0,
                    "changes": 3,
                    "patch": None,
                }
            ]
        if tail == "/requested_reviewers" and method == "POST":
            assert body is not None
            logins = [str(n) for n in body.get("reviewers") or []]
            self.reviewer_requests.append((number, logins))
            for login in logins:
                if login not in self.users:
                    raise self._failed(method, path, 404, f"User '{login}' not exist")
            pull["reviewers"] = logins
            return [
                {"id": self._next(), "state": "REQUEST_REVIEW", "user": self._user(login)}
                for login in logins
            ]
        if tail == "/reviews" and method == "GET":
            return [self._review_json(r) for r in pull["reviews"]]
        if tail == "/reviews" and method == "POST":
            assert body is not None
            self.reviews_posted.append((number, dict(body)))
            self._maybe_fail("review_create")
            event = str(body.get("event") or "COMMENT")
            if event not in ("APPROVED", "REQUEST_CHANGES", "COMMENT"):
                # An unknown word is a pending review that counts for nothing.
                event = "PENDING"
            if event == "APPROVED" and pull["author"] == self.user_login:
                raise self._failed(method, path, 422, "approve your own pull is not allowed")
            state = event
            review = {
                "id": self._next(),
                "number": number,
                "state": state,
                "body": str(body.get("body") or ""),
                "author": self.user_login,
                "commit_id": str(body.get("commit_id") or ""),
                "comments": [
                    {
                        "id": self._next(),
                        "path": str(c["path"]),
                        "position": int(c.get("new_position") or c.get("old_position") or 0),
                        "body": str(c["body"]),
                    }
                    for c in body.get("comments") or []
                ],
            }
            pull["reviews"].append(review)
            return self._review_json(review)
        if (review_match := re.fullmatch(r"/reviews/(\d+)/comments", tail)) and method == "GET":
            review_id = int(review_match.group(1))
            for review in pull["reviews"]:
                if review["id"] == review_id:
                    return [self._review_comment_json(review, c) for c in review["comments"]]
            raise self._failed(method, path, 404, "The target couldn't be found.")
        if tail == "/update" and method == "POST":
            self.updates.append(number)
            if not self.update_ok or pull["state"] != "open":
                raise self._failed(method, path, 422, "%!s(<nil>)")
            sha = f"gt{self._next():06d}"
            head_sha = self.branches[pull["head"]]
            self.trees[sha] = dict(self.trees.get(head_sha, {}))
            self.commits[sha] = {
                "sha": sha,
                "parents": [head_sha, self.branches["main"]],
                "message": "update",
            }
            self.branches[pull["head"]] = sha
            return {}
        if tail == "/merge" and method == "POST":
            request = dict(body or {})
            self.merges.append((number, request))
            self._maybe_fail("pr_merge")
            if pull["state"] != "open":
                raise self._failed(method, path, 404, "The target couldn't be found.")
            if not self.merge_ok:
                raise self._failed(method, path, 403, "user should have a permission to merge")
            if is_draft_title(pull["title"]):
                raise self._failed(method, path, 405, "Work in progress PRs cannot be merged")
            head_sha = self.branches[pull["head"]]
            if self.branch_rules is not None:
                required = (
                    list(self.branch_rules.get("status_check_contexts") or [])
                    if self.branch_rules.get("enable_status_check")
                    else []
                )
                present = self.statuses.get(head_sha, {})
                if any(present.get(c, {}).get("status") != "success" for c in required):
                    raise self._failed(
                        method, path, 405, "Not all required status checks successful"
                    )
                approvals = sum(
                    1
                    for r in pull["reviews"]
                    if r["state"] == "APPROVED" and not r.get("dismissed")
                )
                if approvals < int(self.branch_rules.get("required_approvals") or 0):
                    raise self._failed(method, path, 405, "Not enough approvals")
                if self.protection_rule.get("block_on_rejected_reviews") and any(
                    r["state"] == "REQUEST_CHANGES" and not r.get("dismissed")
                    for r in pull["reviews"]
                ):
                    raise self._failed(method, path, 405, "There are requested changes")
            if request.get("head_commit_id") and request["head_commit_id"] != head_sha:
                raise self._failed(method, path, 409, "head commit id does not match")
            if not pull.get("mergeable", True):
                raise self._failed(method, path, 405, "Merge conflict")
            sha = f"merge{self._next():04d}"
            self.trees[sha] = dict(self.trees.get(head_sha, {}))
            self.commits[sha] = {
                "sha": sha,
                "parents": [self.branches["main"], head_sha],
                "message": str(request.get("MergeMessageField") or "merge"),
            }
            self.branches["main"] = sha
            pull.update(
                {
                    "state": "closed",
                    "merged": True,
                    "merge_commit_sha": sha,
                    "merged_at": "2026-09-12T22:00:00Z",
                }
            )
            return {}
        return None

    def _status_routes(
        self, method: str, path: str, rest: str, params: dict[str, list[str]], body: Any
    ) -> Any:
        if match := re.fullmatch(r"/statuses/([^/]+)", rest):
            assert body is not None
            sha = match.group(1)
            self.statuses_posted.append((sha, dict(body)))
            self.seed_status(
                sha,
                str(body.get("context") or "default"),
                str(body["state"]),
                description=str(body.get("description") or ""),
                target_url=str(body.get("target_url") or ""),
            )
            return {**self.statuses[sha][str(body.get("context") or "default")], "state": None}
        if match := re.fullmatch(r"/commits/([^/]+)/status", rest):
            rows = list(self.statuses.get(match.group(1), {}).values())
            states = {r["status"] for r in rows}
            state = (
                "failure"
                if states - {"success", "pending"}
                else "pending"
                if "pending" in states
                else "success"
                if rows
                else ""
            )
            return {
                "state": state,
                "sha": match.group(1),
                "total_count": len(rows),
                "statuses": rows,
            }
        if match := re.fullmatch(r"/commits/([^/]+)/statuses", rest):
            return [{**r, "state": None} for r in self.statuses.get(match.group(1), {}).values()]
        if rest == "/actions/workflows":
            return {"workflows": list(self.workflows), "total_count": len(self.workflows)}
        if rest == "/actions/tasks":
            return {"workflow_runs": list(self.runs), "total_count": len(self.runs)}
        return None


def _git_blob_sha(raw: bytes) -> str:
    import hashlib

    return hashlib.sha1(b"blob %d\0" % len(raw) + raw, usedforsecurity=False).hexdigest()
