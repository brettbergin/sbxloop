"""Typed facade over github.op jobs running in the github-ops sandbox.

The host never talks to GitHub with the user PAT directly — every operation
becomes a ``github.op`` JobRequest submitted to the github sandbox, which is
the only environment holding ``GH_TOKEN``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sbxloop.errors import GithubOpsError
from sbxloop.ids import new_job_id
from sbxloop.worker.client import WorkerClient
from sbxloop_worker.protocol import JobRequest


class IssueRef(BaseModel):
    number: int
    url: str


class PrRef(BaseModel):
    number: int
    url: str


class GithubOps:
    def __init__(
        self,
        client: WorkerClient,
        run_id: str,
        *,
        timeout_s: float = 120.0,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.timeout_s = timeout_s

    def _op(self, op: str, params: dict[str, Any], *, timeout_s: float | None = None) -> Any:
        job = JobRequest(
            job_id=new_job_id(),
            run_id=self.run_id,
            kind="github.op",
            op=op,
            params=params,
            timeout_s=timeout_s if timeout_s is not None else self.timeout_s,
        )
        result = self.client.submit(job)
        if result.status != "ok":
            assert result.error is not None
            raise GithubOpsError(
                f"github op {op} failed: {result.error.type}: {result.error.message}",
                http_status=result.error.http_status,
            )
        return result.output_json

    def issue_create(
        self,
        repo: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> IssueRef:
        params: dict[str, Any] = {"repo": repo, "title": title, "body": body}
        if labels:
            params["labels"] = labels
        return IssueRef.model_validate(self._op("issue.create", params))

    def issue_comment(self, repo: str, number: int, body: str) -> str:
        data = self._op("issue.comment", {"repo": repo, "number": number, "body": body})
        return str(data.get("url", ""))

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
        return PrRef.model_validate(
            self._op(
                "pr.create",
                {
                    "repo": repo,
                    "base": base,
                    "head": head,
                    "title": title,
                    "body": body,
                    "draft": draft,
                },
            )
        )

    def pr_comment(self, repo: str, number: int, body: str) -> str:
        data = self._op("pr.comment", {"repo": repo, "number": number, "body": body})
        return str(data.get("url", ""))

    def contents_read(self, repo: str, path: str, ref: str | None = None) -> str:
        params: dict[str, Any] = {"repo": repo, "path": path}
        if ref:
            params["ref"] = ref
        data = self._op("contents.read", params)
        return str(data.get("content", ""))

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
        params: dict[str, Any] = {"repo": repo, "sha": sha, "state": state, "context": context}
        if description:
            params["description"] = description
        if target_url:
            params["target_url"] = target_url
        self._op("status.create", params)

    def repo_get(self, repo: str) -> dict[str, Any]:
        data = self._op("repo.get", {"repo": repo})
        assert isinstance(data, dict)
        return data

    def repo_lookup(self, repo: str) -> dict[str, Any] | None:
        """Probe a repository: its data, or None when it does not exist.

        The miss travels as data (``allow_missing``) rather than as a failed
        job, so an expected "no" never raises the worker's error event and
        never paints a red panel in the transcript (#222).
        """
        data = self._op("repo.get", {"repo": repo, "allow_missing": True})
        assert isinstance(data, dict)
        return None if data.get("missing") else data

    def ref_lookup(self, repo: str, ref: str) -> str | None:
        """Resolve ``ref`` (e.g. ``heads/main``) to a commit sha, or None
        when there is no such ref — including the empty-repository case
        GitHub reports as 409 rather than 404. Same rationale as
        :meth:`repo_lookup`: the miss is an answer, not an error."""
        data = self._op("ref.get", {"repo": repo, "ref": ref, "allow_missing": True})
        if not isinstance(data, dict):
            raise GithubOpsError(f"ref.get returned a malformed result: {data!r}")
        if data.get("missing"):
            return None
        sha = data.get("sha")
        if not sha:
            raise GithubOpsError(f"ref.get returned no sha for {ref!r}: {data!r}")
        return str(sha)

    def search_issues(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
        data = self._op("search.issues", {"query": query, "per_page": per_page})
        return data if isinstance(data, list) else []

    def raw(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        params: dict[str, Any] = {"method": method, "path": path}
        if body is not None:
            params["body"] = body
        return self._op("raw.api", params)

    # Extra seconds of job timeout granted per file in a blob batch: the
    # batch job makes one REST call per file, so the flat per-op timeout
    # would starve large manifests.
    BLOB_BATCH_TIMEOUT_PER_FILE_S = 2.0

    def blobs_create_many(self, repo: str, files: list[dict[str, str]]) -> dict[str, str]:
        """Create git blobs for a manifest of {path, content_b64} entries in
        one worker job; returns path -> blob sha."""
        data = self._op(
            "blobs.create_many",
            {"repo": repo, "files": files},
            timeout_s=self.timeout_s + self.BLOB_BATCH_TIMEOUT_PER_FILE_S * len(files),
        )
        blobs = data.get("blobs") if isinstance(data, dict) else None
        if not isinstance(blobs, list):
            raise GithubOpsError(f"blobs.create_many returned no blob list: {data!r}")
        shas: dict[str, str] = {}
        for blob in blobs:
            if not isinstance(blob, dict) or not blob.get("path") or not blob.get("sha"):
                raise GithubOpsError(f"blobs.create_many returned a malformed entry: {blob!r}")
            shas[str(blob["path"])] = str(blob["sha"])
        return shas
