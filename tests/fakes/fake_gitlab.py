"""A scripted stand-in for :class:`sbxloop.vcs.gitlab.ops.GitlabOps`.

The GitLab every GitLab test runs against (#1017), at the fidelity
``fake_github.py`` gives the first backend: the real backend class with
no worker behind it, its generic transport (``raw``, ``raw_lookup``,
``raw_text``) answered from in-memory state shaped the way GitLab CE 19.3
shaped its answers in #1016, and a ledger of what it was asked. Every
named operation on the backend reaches this fake's ``raw`` with the path
the real one would send, so a test pins the request and the fold at once.

State is a project ``acme/widgets`` (id 1) with ``main`` at ``base123``,
a Developer token for ``sbxloop-bot``, and the knobs below:

- ``settings``: the project payload's merge settings (pipeline must
  succeed, discussions resolved, merge method, squash option), the
  issues flag and the token's access level.
- ``protected``: the protected-branch payload for ``main`` (None = 404,
  not protected); ``protected_forbidden`` answers 403 instead.
- ``enterprise``: what ``GET /metadata`` says (None = unreadable);
  ``approval_rules`` is what an enterprise instance lists.
- ``branches``: name -> sha; ``files``: (branch, path) -> bytes;
  ``missing_project`` and ``empty_repo`` for the miss shapes.
- ``issues`` (by iid), ``notes``, ``label_events``, ``labels``.
- ``statuses``: sha -> the commit-status rows; ``traces``: job id ->
  trace text; ``pipelines``: the pipeline rows on a branch.
- ``users``: id -> user payload (the ``bot`` flag lives here, #1016 V3);
  ``token_self``: the token's own record, or None for a token that
  cannot read itself.
- ``fail_once`` / ``fail_always``: method name -> exception.

A ``raw`` call this fake does not model fails loudly rather than
pretending, exactly as ``FakeGithub`` does.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qs, unquote

from sbxloop.errors import GithubOpsError
from sbxloop.vcs.gitlab.ops import GitlabOps
from sbxloop.vcs.model import ChecksVerdict, FailedCheck

WEB = "https://gitlab.example"
PROJECT_ID = 1


def gitlab_error(status: int, message: str) -> GithubOpsError:
    """An error the way the REST transport words it (the stdlib client's
    ``METHOD url -> HTTP status: body``), so a caller matching on words
    sees GitLab's own."""
    return GithubOpsError(
        f"gitlab op raw.api failed: GithubOpError: HTTP {status}: {message}", http_status=status
    )


class FakeGitlab(GitlabOps):
    def __init__(self, *, repo: str = "acme/widgets") -> None:
        # Deliberately no super().__init__: there is no worker client.
        self.run_id = "fake"
        self.timeout_s = 0.0
        self.transport = None
        self._projects = {}
        self._note_issue = {}
        self._bots = {}
        self._note_discussion = {}
        self._mr_urls = {}
        self.repo = repo
        self.user_login = "sbxloop-bot"
        self.user_id = 2
        self.users: dict[int, dict[str, Any]] = {
            2: {"id": 2, "username": "sbxloop-bot", "name": "sbxloop bot", "bot": False},
            3: {"id": 3, "username": "rev-bob", "name": "rev-bob", "bot": False},
            4: {"id": 4, "username": "project_1_bot_05b4", "name": "ci-bot", "bot": True},
        }
        self.settings: dict[str, Any] = {
            "only_allow_merge_if_pipeline_succeeds": False,
            "only_allow_merge_if_all_discussions_are_resolved": False,
            "allow_merge_on_skipped_pipeline": False,
            "merge_method": "merge",
            "squash_option": "default_off",
            "visibility": "private",
            "issues_enabled": True,
            "access_level": 30,
        }
        self.missing_project = False
        self.empty_repo = False
        self.protected: dict[str, Any] | None = None
        self.protected_forbidden = False
        self.enterprise: bool | None = False
        self.approval_rules: list[dict[str, Any]] = []
        self.branches: dict[str, str] = {"main": "base123"}
        self.files: dict[tuple[str, str], bytes] = {("main", "README.md"): b"# widgets\n"}
        self.ci_file = False
        self.issues: dict[int, dict[str, Any]] = {}
        self.notes: dict[int, list[dict[str, Any]]] = {}
        self.label_events: dict[int, list[dict[str, Any]]] = {}
        self.labels: list[dict[str, Any]] = []
        self.statuses: dict[str, list[dict[str, Any]]] = {}
        self.traces: dict[int, str] = {}
        self.pipelines: list[dict[str, Any]] = []
        self.token_self: dict[str, Any] | None = {
            "id": 2,
            "name": "sbxloop",
            "scopes": ["api"],
            "active": True,
            "revoked": False,
            "expires_at": "2026-11-11",
            "user_id": 2,
        }
        self.fail_once: dict[str, Exception] = {}
        self.fail_always: dict[str, Exception] = {}
        # The ledger.
        self.raw_calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.text_calls: list[str] = []
        self.failed_jobs: list[tuple[str, str, str, int | None]] = []
        self.issues_created: list[tuple[str, str, list[str]]] = []
        self.issue_answers: list[tuple[int, str]] = []
        self.issues_closed: list[int] = []
        self.labels_added: list[tuple[int, str]] = []
        self.labels_removed: list[tuple[int, str]] = []
        self.label_creates: list[str] = []
        self.label_lookups: list[str] = []
        self.notes_deleted: list[tuple[int, int]] = []
        self.statuses_posted: list[tuple[str, dict[str, Any]]] = []
        self.deleted_branches: list[str] = []
        # Merge requests (#1018): by iid; their discussions by iid; the
        # diff every seeded request carries unless told otherwise.
        self.merge_requests: dict[int, dict[str, Any]] = {}
        self.discussions: dict[int, list[dict[str, Any]]] = {}
        self.default_diffs: list[dict[str, Any]] = [
            {
                "old_path": "hello.txt",
                "new_path": "hello.txt",
                "diff": "@@ -1,3 +1,3 @@\n-hi\n+hello\n second\n third\n",
                "new_file": False,
                "renamed_file": False,
                "deleted_file": False,
            }
        ]
        # GitLab has not computed the diff yet: diff_refs is null.
        self.diff_refs_pending = False
        # Whether the token may approve (a Premium rule against the author
        # approving answers 401).
        self.approve_ok = True
        # Anchors GitLab refuses a position for (a line outside the diff).
        self.refuse_positions: set[str] = set()
        self.mr_created: list[dict[str, Any]] = []
        self.mr_updates: list[tuple[int, dict[str, Any]]] = []
        self.mr_notes_posted: list[tuple[int, str]] = []
        self.discussions_posted: list[tuple[int, str, str]] = []
        self.replies: list[tuple[int, str, str]] = []
        self.resolved: list[tuple[str, bool]] = []
        self.approvals_posted: list[tuple[int, dict[str, Any]]] = []
        self._discussion_seq = 0
        self.head_sha = "commit0"
        self._note_id = 0
        self._status_id = 100
        self._label_id = 0
        self._missing_ok = False

    # -- plumbing ------------------------------------------------------------

    @property
    def web_url(self) -> str:
        return f"{WEB}/{self.repo}"

    def _op(self, op: str, params: dict[str, Any], *, timeout_s: float | None = None) -> Any:
        raise AssertionError(f"FakeGitlab does not model worker op {op!r} ({params})")

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
        exc = gitlab_error(status, message)
        if not (self._missing_ok and status in (404,)):
            self.failed_jobs.append(("raw.api", method, path, status))
        return exc

    def assert_no_failed_jobs(self) -> None:
        if self.failed_jobs:
            raise AssertionError(f"failed worker jobs recorded: {self.failed_jobs}")

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
        match = re.fullmatch(r"/projects/[^/]+/jobs/(\d+)/trace", path)
        assert match, f"FakeGitlab: unexpected text call {method} {path}"
        trace = self.traces.get(int(match.group(1)))
        if trace is None:
            if 404 in missing:
                return None
            raise gitlab_error(404, "404 Not Found")
        return trace

    # -- state helpers -----------------------------------------------------------

    def _project_json(self) -> dict[str, Any]:
        level = self.settings["access_level"]
        return {
            "id": PROJECT_ID,
            "name": self.repo.split("/", 1)[1],
            "path": self.repo.split("/", 1)[1],
            "path_with_namespace": self.repo,
            "web_url": self.web_url,
            "default_branch": "main",
            "visibility": self.settings["visibility"],
            "issues_enabled": self.settings["issues_enabled"],
            "only_allow_merge_if_pipeline_succeeds": self.settings[
                "only_allow_merge_if_pipeline_succeeds"
            ],
            "only_allow_merge_if_all_discussions_are_resolved": self.settings[
                "only_allow_merge_if_all_discussions_are_resolved"
            ],
            "allow_merge_on_skipped_pipeline": self.settings["allow_merge_on_skipped_pipeline"],
            "merge_method": self.settings["merge_method"],
            "squash_option": self.settings["squash_option"],
            "permissions": {
                "project_access": {"access_level": level, "notification_level": 3}
                if level
                else None,
                "group_access": None,
            },
        }

    def _user(self, user_id: int) -> dict[str, Any]:
        user = self.users[user_id]
        return {
            "id": user["id"],
            "username": user["username"],
            "name": user["name"],
            "state": "active",
            "web_url": f"{WEB}/{user['username']}",
        }

    def _issue_payload(self, issue: dict[str, Any]) -> dict[str, Any]:
        iid = issue["iid"]
        return {
            "id": 100 + iid,
            "iid": iid,
            "project_id": PROJECT_ID,
            "title": issue["title"],
            "description": issue.get("description"),
            "state": issue["state"],
            "labels": list(issue.get("labels") or []),
            "author": self._user(issue.get("author_id", self.user_id)),
            "user_notes_count": len(self.notes.get(iid, [])),
            "created_at": issue.get("created_at", "2026-09-12T20:00:00.000Z"),
            "updated_at": issue.get("updated_at", "2026-09-12T20:00:00.000Z"),
            "web_url": f"{self.web_url}/-/issues/{iid}",
        }

    def seed_issue(
        self,
        iid: int,
        title: str,
        labels: Sequence[str] = (),
        *,
        state: str = "opened",
        description: str = "",
        author_id: int | None = None,
    ) -> None:
        self.issues[iid] = {
            "iid": iid,
            "title": title,
            "description": description,
            "state": state,
            "labels": list(labels),
            "author_id": author_id if author_id is not None else self.user_id,
        }

    def seed_note(
        self,
        iid: int,
        body: str,
        *,
        author_id: int | None = None,
        created_at: str = "2026-09-12T20:01:00.000Z",
        system: bool = False,
    ) -> int:
        self._note_id += 1
        self.notes.setdefault(iid, []).append(
            {
                "id": self._note_id,
                "body": body,
                "author": self._user(author_id if author_id is not None else self.user_id),
                "created_at": created_at,
                "updated_at": created_at,
                "system": system,
                "noteable_iid": iid,
            }
        )
        return self._note_id

    def seed_status(
        self,
        sha: str,
        name: str,
        status: str,
        *,
        allow_failure: bool = False,
        description: str = "",
        target_url: str = "",
        trace: str | None = None,
    ) -> int:
        self._status_id += 1
        self.statuses.setdefault(sha, []).append(
            {
                "id": self._status_id,
                "sha": sha,
                "ref": "main",
                "status": status,
                "name": name,
                "allow_failure": allow_failure,
                "description": description or None,
                "target_url": target_url or None,
                "pipeline_id": 1,
            }
        )
        if trace is not None:
            self.traces[self._status_id] = trace
        return self._status_id

    def seed_verdict(
        self, sha: str, verdict: ChecksVerdict, *, logs: Sequence[FailedCheck] = ()
    ) -> None:
        """Statuses on ``sha`` that fold to ``verdict``; ``logs`` gives a
        red one its trace."""
        self.statuses[sha] = []
        excerpts = {log.name: log.excerpt for log in logs}
        for name in verdict.passed:
            self.seed_status(sha, name, "success")
        for name in verdict.pending:
            self.seed_status(sha, name, "running")
        for name in verdict.failed:
            self.seed_status(sha, name, "failed", trace=excerpts.get(name))
        for name in verdict.needs_approval:
            self.seed_status(sha, name, "manual")

    # -- merge requests (#1018) ---------------------------------------------------

    def _mr_payload(self, mr: dict[str, Any]) -> dict[str, Any]:
        iid = mr["iid"]
        refs = (
            None
            if self.diff_refs_pending
            else {
                "base_sha": "base123",
                "start_sha": "base123",
                "head_sha": mr["sha"],
            }
        )
        return {
            "id": 200 + iid,
            "iid": iid,
            "project_id": PROJECT_ID,
            "title": mr["title"],
            "description": mr.get("description"),
            "state": mr["state"],
            "draft": mr["title"].startswith("Draft:"),
            "source_branch": mr["source_branch"],
            "target_branch": mr["target_branch"],
            "sha": mr["sha"],
            "merge_commit_sha": mr.get("merge_commit_sha"),
            "squash_commit_sha": None,
            "author": self._user(mr.get("author_id", self.user_id)),
            "reviewers": [self._user(r["user_id"]) for r in mr.get("reviewers", [])],
            "detailed_merge_status": mr.get("detailed_merge_status", "mergeable"),
            "blocking_discussions_resolved": all(
                not n.get("resolvable") or n.get("resolved")
                for d in self.discussions.get(iid, [])
                for n in d["notes"][:1]
            ),
            "diff_refs": refs,
            "web_url": f"{self.web_url}/-/merge_requests/{iid}",
            "created_at": mr.get("created_at", "2026-09-12T21:20:41.723Z"),
            "updated_at": mr.get("updated_at", "2026-09-12T21:20:42.705Z"),
        }

    def seed_mr(
        self,
        iid: int,
        *,
        source_branch: str = "sbxloop/r1",
        title: str = "sbxloop: ship it",
        head_sha: str | None = None,
        state: str = "opened",
        author_id: int | None = None,
        detailed_merge_status: str = "mergeable",
        diffs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        sha = head_sha or self.branches.get(source_branch) or self.head_sha
        self.branches.setdefault(source_branch, sha)
        self.merge_requests[iid] = {
            "iid": iid,
            "title": title,
            "description": "",
            "state": state,
            "source_branch": source_branch,
            "target_branch": "main",
            "sha": sha,
            "author_id": author_id if author_id is not None else self.user_id,
            "reviewers": [],
            "approvals": [],
            "detailed_merge_status": detailed_merge_status,
            "diffs": diffs if diffs is not None else list(self.default_diffs),
        }
        return self.merge_requests[iid]

    def seed_discussion(
        self,
        iid: int,
        path: str,
        line: int | None,
        body: str,
        *,
        author_id: int | None = None,
        created_at: str = "2026-09-12T21:20:46.092Z",
    ) -> tuple[str, int]:
        """A diff discussion on the merge request (a plain note when ``line``
        is None); ``(discussion id, root note id)``."""
        self._note_id += 1
        self._discussion_seq += 1
        discussion_id = f"{self._discussion_seq:040x}"
        note: dict[str, Any] = {
            "id": self._note_id,
            "type": "DiffNote" if line is not None else None,
            "body": body,
            "author": self._user(author_id if author_id is not None else self.user_id),
            "created_at": created_at,
            "updated_at": created_at,
            "system": False,
            "resolvable": line is not None,
            "resolved": False,
        }
        if line is not None:
            note["position"] = {
                "base_sha": "base123",
                "start_sha": "base123",
                "head_sha": self.merge_requests[iid]["sha"],
                "old_path": path,
                "new_path": path,
                "position_type": "text",
                "old_line": None,
                "new_line": line,
            }
        self.discussions.setdefault(iid, []).append(
            {"id": discussion_id, "individual_note": line is None, "notes": [note]}
        )
        return discussion_id, self._note_id

    def seed_reviewer_state(self, iid: int, user_id: int, state: str) -> None:
        reviewers = self.merge_requests[iid]["reviewers"]
        reviewers[:] = [r for r in reviewers if r["user_id"] != user_id]
        reviewers.append({"user_id": user_id, "state": state})

    def seed_approval(self, iid: int, user_id: int) -> None:
        approvals = self.merge_requests[iid]["approvals"]
        if user_id not in approvals:
            approvals.append(user_id)

    def _mr_routes(
        self, method: str, path: str, rest: str, params: dict[str, list[str]], body: Any
    ) -> Any:
        """The merge-request endpoints; ``None`` when ``rest`` is not one."""
        if rest == "/merge_requests" and method == "POST":
            assert body is not None
            self._maybe_fail("pr_create")
            source = str(body["source_branch"])
            if any(
                m["state"] == "opened" and m["source_branch"] == source
                for m in self.merge_requests.values()
            ):
                raise self._failed(
                    method,
                    path,
                    409,
                    '{"message":["Another open merge request already exists '
                    'for this source branch"]}',
                )
            iid = max(self.merge_requests, default=0) + 1
            self.mr_created.append(dict(body))
            mr = self.seed_mr(iid, source_branch=source, title=str(body["title"]))
            mr["target_branch"] = str(body.get("target_branch") or "main")
            mr["description"] = str(body.get("description") or "")
            return self._mr_payload(mr)
        if rest == "/merge_requests" and method == "GET":
            state = params.get("state", [""])[0]
            source = params.get("source_branch", [""])[0]
            rows = [
                m
                for m in self.merge_requests.values()
                if (not state or m["state"] == state)
                and (not source or m["source_branch"] == source)
            ]
            return [self._mr_payload(m) for m in rows]
        match = re.fullmatch(r"/merge_requests/(\d+)(/.*)?", rest)
        if match is None:
            return None
        iid, tail = int(match.group(1)), match.group(2) or ""
        mr = self.merge_requests.get(iid)
        if mr is None:
            raise self._failed(method, path, 404, "404 Not found")
        if tail == "" and method == "GET":
            self._maybe_fail("pr_get")
            return self._mr_payload(mr)
        if tail == "" and method == "PUT":
            assert body is not None
            self.mr_updates.append((iid, dict(body)))
            if "title" in body:
                mr["title"] = str(body["title"])
            if "description" in body:
                mr["description"] = str(body["description"])
            if body.get("state_event") == "close":
                mr["state"] = "closed"
            return self._mr_payload(mr)
        if tail == "/diffs":
            per_page = int(params.get("per_page", ["20"])[0])
            page = int(params.get("page", ["1"])[0])
            self._maybe_fail("pr_files")
            return [dict(d) for d in mr["diffs"][(page - 1) * per_page : page * per_page]]
        if tail == "/notes" and method == "POST":
            assert body is not None
            self._maybe_fail("pr_issue_comment")
            self._note_id += 1
            self.mr_notes_posted.append((iid, str(body["body"])))
            note = {
                "id": self._note_id,
                "type": None,
                "body": str(body["body"]),
                "author": self._user(self.user_id),
                "created_at": "2026-09-12T21:31:48.478Z",
                "system": False,
            }
            self.discussions.setdefault(iid, []).append(
                {"id": f"{self._note_id:040x}", "individual_note": True, "notes": [note]}
            )
            return dict(note)
        if tail == "/discussions" and method == "GET":
            self._maybe_fail("pr_review_threads")
            per_page = int(params.get("per_page", ["20"])[0])
            page = int(params.get("page", ["1"])[0])
            rows = self.discussions.get(iid, [])
            return [
                {**d, "notes": [dict(n) for n in d["notes"]]}
                for d in rows[(page - 1) * per_page : page * per_page]
            ]
        if tail == "/discussions" and method == "POST":
            assert body is not None
            position = body.get("position") or {}
            line = position.get("new_line") or position.get("old_line")
            anchor = f"{position.get('new_path')}:{line}"
            self.discussions_posted.append((iid, anchor, str(body["body"])))
            if anchor in self.refuse_positions or str(position.get("head_sha")) != mr["sha"]:
                raise self._failed(
                    method,
                    path,
                    400,
                    '{"message":"400 Bad request - Note {:line_code=>[\\"can\'t be blank\\"]}"}',
                )
            discussion_id, _ = self.seed_discussion(
                iid, str(position.get("new_path")), int(line), str(body["body"])
            )
            found = next(d for d in self.discussions[iid] if d["id"] == discussion_id)
            return {**found, "notes": [dict(n) for n in found["notes"]]}
        if match_note := re.fullmatch(r"/discussions/([0-9a-f]+)/notes", tail):
            assert body is not None
            discussion_id = match_note.group(1)
            found = next(
                (d for d in self.discussions.get(iid, []) if d["id"] == discussion_id), None
            )
            if found is None:
                raise self._failed(method, path, 404, "404 Discussion Not Found")
            self._note_id += 1
            self.replies.append((iid, discussion_id, str(body["body"])))
            root = found["notes"][0]
            note = {
                "id": self._note_id,
                "type": root.get("type"),
                "body": str(body["body"]),
                "author": self._user(self.user_id),
                "created_at": "2026-09-12T21:32:00.000Z",
                "system": False,
                "resolvable": bool(root.get("resolvable")),
                "resolved": bool(root.get("resolved")),
                "position": dict(root["position"]) if root.get("position") else None,
            }
            found["notes"].append(note)
            return dict(note)
        if match_discussion := re.fullmatch(r"/discussions/([0-9a-f]+)", tail):
            assert body is not None and method == "PUT"
            discussion_id = match_discussion.group(1)
            found = next(
                (d for d in self.discussions.get(iid, []) if d["id"] == discussion_id), None
            )
            if found is None:
                raise self._failed(method, path, 404, "404 Discussion Not Found")
            self.resolved.append((discussion_id, bool(body["resolved"])))
            for note in found["notes"]:
                if note.get("resolvable"):
                    note["resolved"] = bool(body["resolved"])
            if body["resolved"]:
                return {"id": discussion_id, "notes": [dict(n) for n in found["notes"]]}
            return {"id": discussion_id}
        if tail == "/approvals":
            return {
                "approved": bool(mr["approvals"]),
                "user_has_approved": self.user_id in mr["approvals"],
                "user_can_approve": self.approve_ok,
                "approved_by": [
                    {"user": self._user(uid), "approved_at": "2026-09-12T21:31:57.388Z"}
                    for uid in mr["approvals"]
                ],
            }
        if tail == "/reviewers":
            return [
                {"user": self._user(r["user_id"]), "state": r["state"]} for r in mr["reviewers"]
            ]
        if tail == "/approve" and method == "POST":
            self.approvals_posted.append((iid, dict(body or {})))
            if not self.approve_ok:
                raise self._failed(method, path, 401, "401 Unauthorized")
            if body and body.get("sha") and body["sha"] != mr["sha"]:
                raise self._failed(
                    method, path, 409, "409 SHA does not match HEAD of source branch"
                )
            self.seed_approval(iid, self.user_id)
            return {"approved": True, "user_has_approved": True}
        raise AssertionError(f"FakeGitlab: unexpected merge request call {method} {path}")

    # -- the transport ---------------------------------------------------------

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.raw_calls.append((method, path, body))
        self._maybe_fail("raw")
        full = path
        path, _, query = path.partition("?")
        params = parse_qs(query)
        if method == "GET" and params.get("per_page") == ["1"]:
            # The doctor's permission probes (#696) read one entry per endpoint.
            self._maybe_fail("permission_probe")
        project = re.match(r"/projects/([^/]+)(/.*)?", path)
        if path == "/version":
            return {"version": "19.3.2", "revision": "34042bf7d00"}
        if path == "/metadata":
            if self.enterprise is None:
                raise self._failed(method, path, 403, "403 Forbidden")
            return {"version": "19.3.2", "revision": "34042bf7d00", "enterprise": self.enterprise}
        if path == "/user":
            self._maybe_fail("authenticated_user")
            user = self._user(self.user_id)
            user["bot"] = bool(self.users[self.user_id].get("bot"))
            return user
        if re.fullmatch(r"/users/\d+", path):
            user_id = int(path.rsplit("/", 1)[1])
            if user_id not in self.users:
                raise self._failed(method, path, 404, "404 User Not Found")
            user = self._user(user_id)
            user["bot"] = bool(self.users[user_id].get("bot"))
            return user
        if path == "/personal_access_tokens/self":
            if self.token_self is None:
                raise self._failed(method, path, 404, "404 Not Found")
            return dict(self.token_self)
        if re.fullmatch(r"/groups/[^/]+", path):
            return {"id": 10, "full_path": unquote(path.rsplit("/", 1)[1])}
        if method == "POST" and path == "/projects":
            assert body is not None
            self.repo = f"{'acme' if body.get('namespace_id') else self.user_login}/{body['path']}"
            return self._project_json()
        if project is None:
            raise AssertionError(f"FakeGitlab: unexpected raw call {method} {full}")
        encoded, rest = project.group(1), project.group(2) or ""
        if unquote(encoded) != self.repo and encoded != str(PROJECT_ID):
            raise self._failed(method, path, 404, "404 Project Not Found")
        if self.missing_project:
            raise self._failed(method, path, 404, "404 Project Not Found")
        if rest == "":
            self._maybe_fail("repo_get")
            return self._project_json()
        if rest == "/protected_branches/main" or re.fullmatch(r"/protected_branches/[^/]+", rest):
            if self.protected_forbidden:
                raise self._failed(method, path, 403, "403 Forbidden")
            if self.protected is None or unquote(rest.rsplit("/", 1)[1]) != "main":
                raise self._failed(method, path, 404, "404 Not found")
            return dict(self.protected)
        if rest == "/approval_rules":
            if self.enterprise is not True:
                raise self._failed(method, path, 404, "404 Not Found")
            return list(self.approval_rules)
        if match := re.fullmatch(r"/repository/branches/([^/]+)", rest):
            name = unquote(match.group(1))
            if method == "DELETE":
                self.deleted_branches.append(name)
                if name not in self.branches:
                    raise self._failed(method, path, 404, "404 Branch Not Found")
                del self.branches[name]
                return {}
            if self.empty_repo or name not in self.branches:
                raise self._failed(method, path, 404, "404 Branch Not Found")
            return {
                "name": name,
                "commit": {"id": self.branches[name]},
                "protected": name == "main",
            }
        if match := re.fullmatch(r"/repository/tags/([^/]+)", rest):
            raise self._failed(method, path, 404, "404 Tag Not Found")
        if rest == "/repository/merge_base":
            refs = params.get("refs[]", [])
            if any(r not in self.branches and r not in self.branches.values() for r in refs):
                raise self._failed(method, path, 404, "404 Not Found")
            return {"id": "base123"}
        if rest == "/repository/compare":
            if params.get("to", [""])[0] not in self.branches:
                raise self._failed(method, path, 404, "404 Not Found")
            return {
                "commits": [],
                "diffs": [],
                "compare_same_ref": False,
                "web_url": f"{self.web_url}/-/compare/x",
            }
        if match := re.fullmatch(r"/repository/files/([^/]+)", rest):
            file_path = unquote(match.group(1))
            ref = params.get("ref", ["main"])[0]
            content = self.files.get((ref, file_path))
            if file_path == ".gitlab-ci.yml" and self.ci_file:
                content = b"stages: [test]\n"
            if content is None:
                raise self._failed(method, path, 404, "404 File Not Found")
            if method == "HEAD":
                return {}
            return {
                "file_path": file_path,
                "ref": ref,
                "encoding": "base64",
                "content": base64.b64encode(content).decode(),
            }
        if rest == "/repository/tree" or (
            rest == "/pipelines" and method == "GET" and "ref" not in params
        ):
            self._maybe_fail("permission_probe")
            return []
        if rest == "/pipelines":
            ref = params.get("ref", [""])[0]
            return [p for p in self.pipelines if p.get("ref") == ref]
        if match := re.fullmatch(r"/repository/commits/([^/]+)/statuses", rest):
            sha = match.group(1)
            return list(self.statuses.get(sha, []))
        if match := re.fullmatch(r"/statuses/([^/]+)", rest):
            assert body is not None
            self.statuses_posted.append((match.group(1), dict(body)))
            self.seed_status(match.group(1), str(body.get("name")), str(body["state"]))
            return {"name": body.get("name"), "status": body["state"]}
        if rest == "/labels":
            if method == "POST":
                assert body is not None
                self.label_creates.append(str(body["name"]))
                if any(lb["name"] == body["name"] for lb in self.labels):
                    raise self._failed(method, path, 409, '{"message":"Label already exists"}')
                self._label_id += 1
                label = {
                    "id": self._label_id,
                    "name": body["name"],
                    "color": body["color"],
                    "description": body.get("description") or None,
                }
                self.labels.append(label)
                return dict(label)
            search = params.get("search", [""])[0]
            if search:
                self.label_lookups.append(search)
            return [dict(lb) for lb in self.labels if search.lower() in lb["name"].lower()]
        if rest == "/issues" and method == "POST":
            assert body is not None
            self._maybe_fail("issue_create")
            if not self.settings["issues_enabled"]:
                raise self._failed(method, path, 403, "403 Forbidden")
            labels = [lb for lb in str(body.get("labels") or "").split(",") if lb]
            self.issues_created.append((body["title"], str(body.get("description") or ""), labels))
            iid = 900 + len(self.issues_created)
            self.seed_issue(
                iid, body["title"], labels, description=str(body.get("description") or "")
            )
            for name in labels:
                if not any(lb["name"] == name for lb in self.labels):
                    self._label_id += 1
                    self.labels.append(
                        {
                            "id": self._label_id,
                            "name": name,
                            "color": "#428BCA",
                            "description": None,
                        }
                    )
            return self._issue_payload(self.issues[iid])
        if rest == "/issues":
            self._maybe_fail("issue_list")
            state = params.get("state", ["all"])[0]
            wanted = [lb for lb in params.get("labels", [""])[0].split(",") if lb]
            rows = [
                i
                for i in self.issues.values()
                if (state == "all" or i["state"] == state)
                and all(lb in i["labels"] for lb in wanted)
            ]
            if params.get("order_by", [""])[0] == "updated_at":
                rows.sort(
                    key=lambda i: i.get("updated_at", ""),
                    reverse=params.get("sort", ["desc"])[0] == "desc",
                )
            per_page = int(params.get("per_page", ["20"])[0])
            page = int(params.get("page", ["1"])[0])
            return [self._issue_payload(i) for i in rows[(page - 1) * per_page : page * per_page]]
        if rest == "/search":
            self._maybe_fail("issue_search")
            terms = params.get("search", [""])[0].lower().split()
            state = params.get("state", [""])[0]
            rows = [
                i
                for i in self.issues.values()
                if all(t in f"{i['title']} {i.get('description') or ''}".lower() for t in terms)
                and (not state or i["state"] == state)
            ]
            per_page = int(params.get("per_page", ["20"])[0])
            return [self._issue_payload(i) for i in rows[:per_page]]
        if rest.startswith("/merge_requests"):
            return self._mr_routes(method, path, rest, params, body)
        if match := re.fullmatch(r"/issues/(\d+)(/.*)?", rest):
            iid, tail = int(match.group(1)), match.group(2) or ""
            issue = self.issues.get(iid)
            if issue is None:
                raise self._failed(method, path, 404, "404 Not found")
            if tail == "" and method == "GET":
                self._maybe_fail("issue_read")
                return self._issue_payload(issue)
            if tail == "" and method == "PUT":
                assert body is not None
                if "add_labels" in body:
                    for name in str(body["add_labels"]).split(","):
                        if name and name not in issue["labels"]:
                            issue["labels"].append(name)
                            self.labels_added.append((iid, name))
                            self.label_events.setdefault(iid, []).append(
                                {
                                    "id": len(self.label_events.get(iid, [])) + 1,
                                    "action": "add",
                                    "label": {"name": name},
                                    "user": self._user(self.user_id),
                                    "created_at": "2026-09-12T20:02:00.000Z",
                                }
                            )
                if "remove_labels" in body:
                    name = str(body["remove_labels"])
                    self.labels_removed.append((iid, name))
                    if name in issue["labels"]:
                        issue["labels"].remove(name)
                if body.get("state_event") == "close":
                    issue["state"] = "closed"
                    self.issues_closed.append(iid)
                return self._issue_payload(issue)
            if tail == "/notes" and method == "POST":
                assert body is not None
                self._maybe_fail("issue_comment")
                self.issue_answers.append((iid, str(body["body"])))
                note_id = self.seed_note(iid, str(body["body"]))
                return dict(self.notes[iid][-1])
            if tail == "/notes" and method == "GET":
                return [dict(n) for n in self.notes.get(iid, [])]
            if tail.startswith("/notes/") and method == "DELETE":
                note_id = int(tail.rsplit("/", 1)[1])
                self.notes_deleted.append((iid, note_id))
                self.notes[iid] = [n for n in self.notes.get(iid, []) if n["id"] != note_id]
                return {}
            if tail == "/resource_label_events":
                return [dict(e) for e in self.label_events.get(iid, [])]
        raise AssertionError(f"FakeGitlab: unexpected raw call {method} {full}")
