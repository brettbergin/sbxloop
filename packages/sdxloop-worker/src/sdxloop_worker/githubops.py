"""GitHub operations executed inside the github-ops sandbox.

Every operation is expressed as a GitHub REST call and executed through one
of two transports:

- **gh CLI** (preferred when present): ``gh api`` — inherits `GH_TOKEN`
  handling, retries, and pagination behavior from gh.
- **urllib REST** (mandatory fallback): the sandbox `shell` template does not
  guarantee gh, so a pure-stdlib client using ``GH_TOKEN``/``GITHUB_TOKEN``
  directly is required, not optional polish.

The op registry maps stable sdxloop op names (``issue.create``, ...) to
request builders + response shapers, so both transports produce identical
results for the host.
"""

from __future__ import annotations

import base64
import json
import os
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
    """A GitHub operation failed (bad op, transport error, HTTP error)."""


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
        proc = subprocess.run(
            argv, capture_output=True, text=True, input=stdin, timeout=120, check=False
        )
        if proc.returncode != 0:
            raise GithubOpError(
                f"gh api {method} {path} failed (rc={proc.returncode}): "
                f"{proc.stderr.strip()[:2000]}"
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
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "sdxloop-worker",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:2000]
            raise GithubOpError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc
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
        "context": p.get("context", "sdxloop"),
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


def execute_op(
    op: str,
    params: dict[str, Any],
    transport: Transport | None = None,
) -> JsonValue:
    impl = OPS.get(op)
    if impl is None:
        raise GithubOpError(f"unknown github op {op!r}; known: {', '.join(sorted(OPS))}")
    return impl(transport or select_transport(), params)
