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
from http.client import HTTPMessage
from typing import IO, Any, Protocol

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "sbxloop-worker"

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

    def request_text(self, method: str, path: str) -> str:
        """Like :meth:`request`, for endpoints that answer with a text body
        (Actions job logs) rather than JSON."""
        ...


class GhCliTransport:
    """Executes REST calls through ``gh api``."""

    def __init__(self, gh: str = "gh") -> None:
        self.gh = gh

    def _run(self, method: str, path: str, body: dict[str, Any] | None = None) -> str:
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
            # gh prints its one-line verdict ("gh: Validation Failed (HTTP
            # 422)") on stderr and the API's error body — the part that
            # says *what* failed — on stdout. The body is what a caller
            # matching "pull request already exists" needs (#387 field run).
            error_body = " ".join(proc.stdout.split())[:1000]
            detail = f"{stderr} — {error_body}" if error_body else stderr
            raise GithubOpError(
                f"gh api {method} {path} failed (rc={proc.returncode}): {detail}",
                http_status=parse_gh_http_status(stderr),
            )
        return proc.stdout

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> JsonValue:
        stdout = self._run(method, path, body)
        if not stdout.strip():
            return {}
        parsed: JsonValue = json.loads(stdout)
        return parsed

    def request_text(self, method: str, path: str) -> str:
        # gh follows the logs endpoint's redirect to blob storage itself and
        # prints the body verbatim, so nothing beyond "don't parse it" differs.
        return self._run(method, path)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into an HTTPError so the caller sees the 302.

    The Actions logs endpoint answers 302 to a *signed* blob-storage URL.
    urllib's default handler would replay the original headers there —
    bearer token included — and the storage host rejects a request carrying
    both its signature and an Authorization header (and would be handed the
    token either way). The caller follows the hop itself, unauthenticated.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


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

    def request_text(self, method: str, path: str) -> str:
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        if not url.startswith("https://"):
            raise GithubOpError(f"refusing non-HTTPS GitHub API URL: {url}")
        opener = urllib.request.build_opener(_NoRedirect)
        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
        )
        body, location = self._open_text(opener, request)
        if location is None:
            return body
        # The hop to storage: the URL carries its own signature, so the only
        # header it gets is a User-Agent. Same https rule — that signature is
        # a credential too, if a short-lived one.
        if not location.startswith("https://"):
            raise GithubOpError(f"refusing non-HTTPS redirect target: {location}")
        follow = urllib.request.Request(location, method="GET", headers={"User-Agent": USER_AGENT})
        body, location = self._open_text(opener, follow)
        if location is not None:
            raise GithubOpError(f"{method} {url}: storage redirected again, to {location}")
        return body

    @staticmethod
    def _open_text(
        opener: urllib.request.OpenerDirector, request: urllib.request.Request
    ) -> tuple[str, str | None]:
        """``(body, None)`` for a response, ``("", location)`` for a redirect.

        Job logs are whatever the build printed, so the body is decoded with
        replacement: a stray byte must not turn "here is your failure" into
        a decode error.
        """
        method, url = request.get_method(), request.full_url
        try:
            with opener.open(request, timeout=120) as response:  # nosec B310 - https enforced by caller
                return response.read().decode("utf-8", errors="replace"), None
        except urllib.error.HTTPError as exc:
            if exc.code in _REDIRECT_CODES:
                location = exc.headers.get("Location")
                if location:
                    return "", str(location)
            detail = exc.read().decode(errors="replace")[:2000]
            raise GithubOpError(
                f"{method} {url} -> HTTP {exc.code}: {detail}", http_status=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise GithubOpError(f"{method} {url} failed: {exc.reason}") from exc


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
    """Fetch a repository; with ``allow_missing`` a 404 is an *answer*.

    The existence probe behind ``--create-repo`` asks "is it there?" and
    "no" is expected. Raising here would emit the worker's error event
    before the host gets to say the miss is fine, and the transcript would
    show a red panel for a routine question (#222) — so the miss comes back
    as ``{"missing": true}`` on an ok result instead.
    """
    _require(p, "repo")
    try:
        return t.request("GET", f"/repos/{p['repo']}")
    except GithubOpError as exc:
        if p.get("allow_missing") and exc.http_status == 404:
            return {"missing": True, "http_status": exc.http_status}
        raise


# Statuses a ref lookup answers with when there is no base to build on: 404
# for an absent ref on a non-empty repository, 409 for a repository with no
# commits at all (field-verified: run rgwp5z40x got ``HTTP 409 "Git
# Repository is empty."`` where the stubs had modeled a 404).
REF_MISSING_STATUSES = frozenset({404, 409})


def _ref_get(t: Transport, p: dict[str, Any]) -> JsonValue:
    """Resolve a git ref (``heads/main``) to its object sha.

    Delivery uses this to find the base commit; an empty repository or an
    absent branch is a normal state it bootstraps around, so with
    ``allow_missing`` those statuses come back as ``{"missing": true}`` on
    an ok result rather than as an error event (#222).
    """
    _require(p, "repo", "ref")
    try:
        data = t.request("GET", f"/repos/{p['repo']}/git/ref/{p['ref']}")
    except GithubOpError as exc:
        if p.get("allow_missing") and exc.http_status in REF_MISSING_STATUSES:
            return {"missing": True, "http_status": exc.http_status}
        raise
    if not isinstance(data, dict):
        raise GithubOpError(f"GitHub returned no object sha for ref {p['ref']!r}: {data!r}")
    obj = data.get("object")
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not sha:
        raise GithubOpError(f"GitHub returned no object sha for ref {p['ref']!r}: {data!r}")
    return {"ref": data.get("ref"), "sha": sha}


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


# Check-run conclusions that are not failures — the same set the host's
# ``fold_check_runs`` uses to decide a PR is red (the worker cannot import
# sbxloop, so the set is repeated here; keep the two aligned). ``neutral``
# and ``skipped`` are not red builds; everything else, including conclusions
# GitHub adds later, fails closed.
PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

# Per-check excerpt budget, and how much of it goes to the head of the log.
# The tail is where a failing job says *why* (the assertion, the traceback,
# the non-zero exit); the head names what was being run. Both matter to a
# fix agent, the middle rarely does.
DEFAULT_LOG_CHARS = 6000
LOG_HEAD_CHARS = 1500


def _clip_head_tail(text: str, head: int, tail: int) -> str:
    """Keep the first ``head`` and last ``tail`` characters, marking the cut."""
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n...(clipped {dropped} chars)...\n{text[len(text) - tail :]}"


def _check_output_excerpt(run: dict[str, Any]) -> str:
    """What a non-Actions check (or one whose logs cannot be read) said."""
    output = run.get("output") or {}
    if not isinstance(output, dict):
        return ""
    parts = [str(output.get(key) or "").strip() for key in ("title", "summary", "text")]
    return "\n\n".join(part for part in parts if part)


def _checks_failed_logs(t: Transport, p: dict[str, Any]) -> JsonValue:
    """The failing check runs on a commit, each with the text that explains it.

    A fix round needs more than the name of a red check: it needs the
    assertion or the compiler error. For GitHub Actions the check-run id is
    the job id, so the job's log is fetched; other checks (and Actions jobs
    whose logs are gone — expired, or a token without ``actions:read``)
    fall back to what the check itself reported. Each excerpt is clipped
    head+tail to ``max_chars`` so a chatty build cannot flood the brief.
    """
    _require(p, "repo", "sha")
    repo, sha = p["repo"], p["sha"]
    max_chars = int(p.get("max_chars") or DEFAULT_LOG_CHARS)
    head = min(LOG_HEAD_CHARS, max_chars)
    tail = max_chars - head
    data = t.request("GET", f"/repos/{repo}/commits/{sha}/check-runs")
    runs = data.get("check_runs") if isinstance(data, dict) else None
    checks: list[dict[str, Any]] = []
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict):
            continue
        conclusion = run.get("conclusion")
        if conclusion is None or str(conclusion).lower() in PASSING_CONCLUSIONS:
            continue
        excerpt = ""
        if (run.get("app") or {}).get("slug") == "github-actions" and run.get("id") is not None:
            try:
                excerpt = t.request_text("GET", f"/repos/{repo}/actions/jobs/{run['id']}/logs")
            except GithubOpError as exc:
                reason = f"HTTP {exc.http_status}" if exc.http_status else str(exc)[:200]
                excerpt = f"(logs unavailable: {reason})\n{_check_output_excerpt(run)}"
        if not excerpt.strip():
            excerpt = _check_output_excerpt(run)
        checks.append(
            {
                "name": str(run.get("name") or "check"),
                "conclusion": str(conclusion),
                "details_url": str(run.get("details_url") or ""),
                "excerpt": _clip_head_tail(excerpt, head, tail),
            }
        )
    return {"checks": checks}


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
    "ref.get": _ref_get,
    "search.issues": _search_issues,
    "raw.api": _raw_api,
    "checks.failed_logs": _checks_failed_logs,
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
