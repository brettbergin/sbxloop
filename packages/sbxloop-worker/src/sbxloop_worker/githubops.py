"""GitHub operations executed inside the github-ops sandbox.

Every operation is expressed as a GitHub REST call and executed through one
of two transports:

- **gh CLI** (preferred when present): ``gh api`` — inherits `GH_TOKEN`
  handling, retries, and pagination behavior from gh.
- **urllib REST** (mandatory fallback): the sandbox `shell` template does not
  guarantee gh, so a pure-stdlib client using ``GH_TOKEN``/``GITHUB_TOKEN``
  directly is required, not optional polish.

The op registry maps stable sbxloop op names (``issue.create``, ...) to
request builders + response shapers, so both transports produce identical
results for the host.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"

JsonValue = dict[str, Any] | list[Any]


class GithubOpError(RuntimeError):
    """A GitHub operation failed (bad op, transport error, HTTP error).

    ``http_status`` is set when the failure was an HTTP response so the host
    can branch on ``== 404`` / ``== 409`` instead of grepping the message.
    Field failure (run rgwp5z40x, #219): the empty-repo bootstrap listened
    for "HTTP 404" because the stubs said so; real GitHub answered 409 via a
    gh-CLI wording nobody had modelled. A structured status survives message
    rewording on either transport.
    """

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


# gh prints HTTP failures as ``gh: <message> (HTTP 409)``; the bare form
# covers gh versions/paths that omit the parentheses. Only 3-digit codes.
_GH_PAREN_STATUS = re.compile(r"\(HTTP (\d{3})\)")
_GH_BARE_STATUS = re.compile(r"\bHTTP (\d{3})\b")


def parse_gh_http_status(stderr: str) -> int | None:
    """Extract the HTTP status ``gh api`` reports on stderr, if any.

    The parenthesized form is gh's own report of the response status and is
    appended after the (server-supplied) message, so the *last* one wins:
    a message like ``upstream said HTTP 404 (HTTP 500)`` is a 500, not a 404.
    The bare form is only a fallback when gh printed no parenthesized status.
    """
    paren = _GH_PAREN_STATUS.findall(stderr)
    if paren:
        return int(paren[-1])
    bare = _GH_BARE_STATUS.findall(stderr)
    if bare:
        return int(bare[-1])
    return None


class Transport(Protocol):
    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> JsonValue: ...


class GhCliTransport:
    """Executes REST calls through ``gh api``."""

    def __init__(self, gh: str = "gh") -> None:
        self.gh = gh

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> JsonValue:
        argv = [
            self.gh,
            "api",
            "-X",
            method,
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
            path,
        ]
        stdin: str | None = None
        if body is not None:
            argv += ["--input", "-"]
            stdin = json.dumps(body)
        proc = subprocess.run(  # nosec B603 - list argv, gh CLI, no shell
            argv, capture_output=True, text=True, input=stdin, timeout=120, check=False
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip()[:2000]
            raise GithubOpError(
                f"gh api {method} {path} failed (rc={proc.returncode}): {stderr}",
                http_status=parse_gh_http_status(stderr),
            )
        if not proc.stdout.strip():
            return {}
        parsed: JsonValue = json.loads(proc.stdout)
        return parsed


class RestTransport:
    """Pure-stdlib GitHub REST client using the injected token."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise GithubOpError(
                "no GitHub token available: GH_TOKEN/GITHUB_TOKEN are not set and gh is absent"
            )

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> JsonValue:
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        # The bearer token rides on every request: never let it travel over
        # anything but HTTPS (also rules out file:// and custom schemes).
        if not url.startswith("https://"):
            raise GithubOpError(f"refusing non-HTTPS GitHub API URL: {url}")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "sbxloop-worker",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 - https enforced above
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:2000]
            raise GithubOpError(
                f"{method} {url} -> HTTP {exc.code}: {detail}", http_status=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise GithubOpError(f"{method} {url} failed: {exc.reason}") from exc
        if not raw.strip():
            return {}
        parsed: JsonValue = json.loads(raw)
        return parsed


def select_transport() -> Transport:
    if shutil.which("gh"):
        return GhCliTransport()
    return RestTransport()


# -- op registry -------------------------------------------------------------

OpImpl = Callable[[Transport, dict[str, Any]], JsonValue]


def _require(params: dict[str, Any], *names: str) -> None:
    missing = [n for n in names if not params.get(n)]
    if missing:
        raise GithubOpError(f"missing required params: {', '.join(missing)}")


def _issue_create(t: Transport, p: dict[str, Any]) -> JsonValue:
    _require(p, "repo", "title")
    body: dict[str, Any] = {"title": p["title"]}
    if p.get("body"):
        body["body"] = p["body"]
    if p.get("labels"):
        body["labels"] = p["labels"]
    data = t.request("POST", f"/repos/{p['repo']}/issues", body)
    assert isinstance(data, dict)
    return {"number": data.get("number"), "url": data.get("html_url")}


def _issue_comment(t: Transport, p: dict[str, Any]) -> JsonValue:
    _require(p, "repo", "number", "body")
    data = t.request(
        "POST", f"/repos/{p['repo']}/issues/{p['number']}/comments", {"body": p["body"]}
    )
    assert isinstance(data, dict)
    return {"url": data.get("html_url")}


def _pr_create(t: Transport, p: dict[str, Any]) -> JsonValue:
    _require(p, "repo", "base", "head", "title")
    body: dict[str, Any] = {"title": p["title"], "base": p["base"], "head": p["head"]}
    if p.get("body"):
        body["body"] = p["body"]
    if p.get("draft"):
        body["draft"] = True
    data = t.request("POST", f"/repos/{p['repo']}/pulls", body)
    assert isinstance(data, dict)
    return {"number": data.get("number"), "url": data.get("html_url")}


def _contents_read(t: Transport, p: dict[str, Any]) -> JsonValue:
    _require(p, "repo", "path")
    path = f"/repos/{p['repo']}/contents/{p['path']}"
    if p.get("ref"):
        path += f"?ref={urllib.parse.quote(str(p['ref']))}"
    data = t.request("GET", path)
    assert isinstance(data, dict)
    result: dict[str, Any] = {"path": data.get("path"), "sha": data.get("sha")}
    if data.get("encoding") == "base64" and isinstance(data.get("content"), str):
        raw = base64.b64decode(data["content"])
        try:
            result["content"] = raw.decode("utf-8")
            result["binary"] = False
        except UnicodeDecodeError:
            result["content"] = base64.b64encode(raw).decode()
            result["binary"] = True
    else:
        result["content"] = data.get("content")
        result["binary"] = False
    return result


def _status_create(t: Transport, p: dict[str, Any]) -> JsonValue:
    _require(p, "repo", "sha", "state")
    body = {
        "state": p["state"],
        "context": p.get("context", "sbxloop"),
    }
    if p.get("description"):
        body["description"] = p["description"]
    if p.get("target_url"):
        body["target_url"] = p["target_url"]
    data = t.request("POST", f"/repos/{p['repo']}/statuses/{p['sha']}", body)
    assert isinstance(data, dict)
    return {"id": data.get("id"), "state": data.get("state")}


def _repo_get(t: Transport, p: dict[str, Any]) -> JsonValue:
    _require(p, "repo")
    return t.request("GET", f"/repos/{p['repo']}")


def _search_issues(t: Transport, p: dict[str, Any]) -> JsonValue:
    _require(p, "query")
    query = urllib.parse.quote(str(p["query"]))
    per_page = int(p.get("per_page", 30))
    data = t.request("GET", f"/search/issues?q={query}&per_page={per_page}")
    assert isinstance(data, dict)
    items = data.get("items", [])
    return items if isinstance(items, list) else []


def _raw_api(t: Transport, p: dict[str, Any]) -> JsonValue:
    _require(p, "method", "path")
    return t.request(str(p["method"]).upper(), str(p["path"]), p.get("body"))


# One progress event per this many blobs keeps a multi-hundred-file delivery
# visibly alive without flooding the event stream.
BLOB_PROGRESS_EVERY = 10

ProgressFn = Callable[..., None]


def _blobs_create_many(t: Transport, p: dict[str, Any], progress: ProgressFn | None) -> JsonValue:
    """Create git blobs for a whole file manifest inside one job.

    The delivery path used to submit one worker job per blob POST; the fixed
    per-job overhead (sbx cp + fresh worker process) dominated delivery time
    for large workspaces (#66). Here the loop over files runs inside the
    sandbox, so N files cost N REST calls but only one job cycle.
    """
    _require(p, "repo", "files")
    files = p["files"]
    if not isinstance(files, list):
        raise GithubOpError("files must be a list of {path, content_b64} entries")
    total = len(files)
    blobs: list[dict[str, Any]] = []
    for index, entry in enumerate(files):
        path = entry.get("path") if isinstance(entry, dict) else None
        content = entry.get("content_b64") if isinstance(entry, dict) else None
        if not path or not isinstance(content, str):
            raise GithubOpError(f"files[{index}] must have a path and base64 content_b64")
        try:
            data = t.request(
                "POST",
                f"/repos/{p['repo']}/git/blobs",
                {"content": content, "encoding": "base64"},
            )
        except GithubOpError as exc:
            # Name the failing file: this message rides the JobResult error all
            # the way into the host's run.deliver event.
            raise GithubOpError(
                f"blob create failed for {path!r} (file {index + 1}/{total}): {exc}",
                http_status=exc.http_status,
            ) from exc
        sha = data.get("sha") if isinstance(data, dict) else None
        if not sha:
            raise GithubOpError(f"GitHub returned no sha for blob {path!r}: {data!r}")
        blobs.append({"path": path, "sha": sha})
        done = index + 1
        if progress is not None and (done % BLOB_PROGRESS_EVERY == 0 or done == total):
            progress(done=done, total=total)
    return {"blobs": blobs}


OPS: dict[str, OpImpl] = {
    "issue.create": _issue_create,
    "issue.comment": _issue_comment,
    "pr.create": _pr_create,
    "pr.comment": _issue_comment,  # PR comments go through the issues API
    "contents.read": _contents_read,
    "status.create": _status_create,
    "repo.get": _repo_get,
    "search.issues": _search_issues,
    "raw.api": _raw_api,
}

# Ops that stream progress while they run (they receive a progress callback in
# addition to the (transport, params) every op gets).
PROGRESS_OPS: dict[str, Callable[[Transport, dict[str, Any], ProgressFn | None], JsonValue]] = {
    "blobs.create_many": _blobs_create_many,
}


def execute_op(
    op: str,
    params: dict[str, Any],
    transport: Transport | None = None,
    progress: ProgressFn | None = None,
) -> JsonValue:
    progress_impl = PROGRESS_OPS.get(op)
    if progress_impl is not None:
        return progress_impl(transport or select_transport(), params, progress)
    impl = OPS.get(op)
    if impl is None:
        known = ", ".join(sorted({**OPS, **PROGRESS_OPS}))
        raise GithubOpError(f"unknown github op {op!r}; known: {known}")
    return impl(transport or select_transport(), params)
