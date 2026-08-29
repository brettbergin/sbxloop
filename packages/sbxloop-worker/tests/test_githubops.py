"""githubops tests: op registry, gh CLI transport (fake gh), REST fallback."""

from __future__ import annotations

import base64
import email.message
import io
import json
import stat
import subprocess
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from sbxloop_worker import githubops
from sbxloop_worker.githubops import (
    GhCliTransport,
    GithubOpError,
    RestTransport,
    execute_op,
    select_transport,
)


class RecordingTransport:
    """In-memory transport recording requests and returning canned data.

    ``texts`` answers ``request_text`` by path prefix with a string, or
    raises the :class:`GithubOpError` stored there.
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        texts: dict[str, str | GithubOpError] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.text_calls: list[tuple[str, str]] = []
        self.responses = responses or {}
        self.texts = texts or {}

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        self.calls.append((method, path, body))
        for prefix, response in self.responses.items():
            if path.startswith(prefix):
                return response  # type: ignore[no-any-return]
        return {}

    def request_text(self, method: str, path: str) -> str:
        self.text_calls.append((method, path))
        for prefix, text in self.texts.items():
            if path.startswith(prefix):
                if isinstance(text, GithubOpError):
                    raise text
                return text
        return ""


class TestOpRegistry:
    def test_issue_create(self) -> None:
        t = RecordingTransport(
            {"/repos/o/r/issues": {"number": 7, "html_url": "https://x/7", "extra": 1}}
        )
        out = execute_op(
            "issue.create",
            {"repo": "o/r", "title": "T", "body": "B", "labels": ["sbxloop"]},
            t,
        )
        assert out == {"number": 7, "url": "https://x/7"}
        method, path, body = t.calls[0]
        assert (method, path) == ("POST", "/repos/o/r/issues")
        assert body == {"title": "T", "body": "B", "labels": ["sbxloop"]}

    def test_issue_create_missing_params(self) -> None:
        with pytest.raises(GithubOpError, match="missing required params: title"):
            execute_op("issue.create", {"repo": "o/r"}, RecordingTransport())

    def test_issue_and_pr_comment_share_issues_api(self) -> None:
        t = RecordingTransport({"/repos/o/r/issues/5/comments": {"html_url": "https://c/1"}})
        out = execute_op("pr.comment", {"repo": "o/r", "number": 5, "body": "hi"}, t)
        assert out == {"url": "https://c/1"}
        assert t.calls[0][1] == "/repos/o/r/issues/5/comments"

    def test_pr_create_with_draft(self) -> None:
        t = RecordingTransport({"/repos/o/r/pulls": {"number": 2, "html_url": "https://p/2"}})
        out = execute_op(
            "pr.create",
            {"repo": "o/r", "base": "main", "head": "dev", "title": "T", "draft": True},
            t,
        )
        assert out == {"number": 2, "url": "https://p/2"}
        assert t.calls[0][2] is not None
        assert t.calls[0][2]["draft"] is True

    def test_contents_read_decodes_base64(self) -> None:
        content = base64.b64encode(b"hello world").decode()
        t = RecordingTransport(
            {
                "/repos/o/r/contents/f.txt": {
                    "path": "f.txt",
                    "sha": "abc",
                    "encoding": "base64",
                    "content": content,
                }
            }
        )
        out = execute_op("contents.read", {"repo": "o/r", "path": "f.txt", "ref": "main"}, t)
        assert isinstance(out, dict)
        assert out["content"] == "hello world"
        assert out["binary"] is False
        assert "ref=main" in t.calls[0][1]

    def test_contents_read_binary_stays_base64(self) -> None:
        payload = bytes([0xFF, 0xFE, 0x00, 0x01])
        t = RecordingTransport(
            {
                "/repos/o/r/contents/blob": {
                    "path": "blob",
                    "sha": "abc",
                    "encoding": "base64",
                    "content": base64.b64encode(payload).decode(),
                }
            }
        )
        out = execute_op("contents.read", {"repo": "o/r", "path": "blob"}, t)
        assert isinstance(out, dict)
        assert out["binary"] is True
        assert base64.b64decode(out["content"]) == payload

    def test_status_create(self) -> None:
        t = RecordingTransport({"/repos/o/r/statuses/deadbeef": {"id": 9, "state": "success"}})
        out = execute_op(
            "status.create",
            {"repo": "o/r", "sha": "deadbeef", "state": "success", "description": "ok"},
            t,
        )
        assert out == {"id": 9, "state": "success"}

    def test_search_issues_returns_items(self) -> None:
        t = RecordingTransport({"/search/issues": {"items": [{"number": 1}], "total_count": 1}})
        out = execute_op("search.issues", {"query": "is:open repo:o/r"}, t)
        assert out == [{"number": 1}]

    def test_raw_api_passthrough(self) -> None:
        t = RecordingTransport({"/anything": {"ok": True}})
        out = execute_op("raw.api", {"method": "patch", "path": "/anything", "body": {"a": 1}}, t)
        assert out == {"ok": True}
        assert t.calls[0] == ("PATCH", "/anything", {"a": 1})

    def test_unknown_op(self) -> None:
        with pytest.raises(GithubOpError, match="unknown github op"):
            execute_op("teleport.repo", {}, RecordingTransport())

    def test_unknown_op_lists_progress_ops(self) -> None:
        with pytest.raises(GithubOpError, match=r"blobs\.create_many"):
            execute_op("teleport.repo", {}, RecordingTransport())


def check_run(
    name: str,
    conclusion: str | None,
    *,
    run_id: int = 1,
    app: str = "github-actions",
    output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "conclusion": conclusion,
        "details_url": f"https://ci/{name}",
        "app": {"slug": app},
        "output": output or {},
    }


def check_runs(*runs: dict[str, Any]) -> dict[str, Any]:
    return {"/repos/o/r/commits/abc/check-runs": {"check_runs": list(runs)}}


class TestChecksFailedLogs:
    """``checks.failed_logs``: the text behind each red check, clipped."""

    def test_actions_job_logs_fetched_and_clipped_head_tail(self) -> None:
        log = "H" * 2000 + "M" * 3000 + "T" * 2000
        t = RecordingTransport(
            check_runs(check_run("unit", "failure", run_id=77)),
            texts={"/repos/o/r/actions/jobs/77/logs": log},
        )
        out = execute_op("checks.failed_logs", {"repo": "o/r", "sha": "abc", "max_chars": 4000}, t)
        assert isinstance(out, dict)
        assert t.calls == [("GET", "/repos/o/r/commits/abc/check-runs", None)]
        assert t.text_calls == [("GET", "/repos/o/r/actions/jobs/77/logs")]
        (check,) = out["checks"]
        assert (check["name"], check["conclusion"]) == ("unit", "failure")
        assert check["details_url"] == "https://ci/unit"
        excerpt = check["excerpt"]
        # 1500 head + 2500 tail; the 3000 in between are cut and counted.
        assert excerpt.startswith("H" * 1500 + "\n...(clipped 3000 chars)...\n")
        assert excerpt.endswith("T" * 2000)
        assert excerpt.count("M") == 500
        assert len(excerpt) == 4000 + len("\n...(clipped 3000 chars)...\n")

    def test_short_log_is_not_clipped(self) -> None:
        t = RecordingTransport(
            check_runs(check_run("unit", "failure", run_id=77)),
            texts={"/repos/o/r/actions/jobs/77/logs": "short log\n"},
        )
        out = execute_op("checks.failed_logs", {"repo": "o/r", "sha": "abc"}, t)
        assert isinstance(out, dict)
        assert out["checks"][0]["excerpt"] == "short log\n"

    def test_non_actions_check_uses_its_output(self) -> None:
        t = RecordingTransport(
            check_runs(
                check_run(
                    "lint",
                    "failure",
                    app="some-bot",
                    output={"title": "3 problems", "summary": "", "text": "a.py:1: E501"},
                )
            )
        )
        out = execute_op("checks.failed_logs", {"repo": "o/r", "sha": "abc"}, t)
        assert isinstance(out, dict)
        assert t.text_calls == []
        assert out["checks"][0]["excerpt"] == "3 problems\n\na.py:1: E501"

    def test_logs_error_falls_back_to_output_with_a_note(self) -> None:
        t = RecordingTransport(
            check_runs(
                check_run(
                    "unit",
                    "timed_out",
                    run_id=5,
                    output={"title": "Job timed out", "summary": "after 6h"},
                )
            ),
            texts={"/repos/o/r/actions/jobs/5/logs": GithubOpError("gone", http_status=410)},
        )
        out = execute_op("checks.failed_logs", {"repo": "o/r", "sha": "abc"}, t)
        assert isinstance(out, dict)
        (check,) = out["checks"]
        assert check["conclusion"] == "timed_out"
        lines = check["excerpt"].splitlines()
        assert lines[0] == "(logs unavailable: HTTP 410)"
        assert "Job timed out" in check["excerpt"]
        assert "after 6h" in check["excerpt"]

    def test_logs_error_without_status_quotes_the_message(self) -> None:
        t = RecordingTransport(
            check_runs(check_run("unit", "failure", run_id=5)),
            texts={"/repos/o/r/actions/jobs/5/logs": GithubOpError("proxy refused")},
        )
        out = execute_op("checks.failed_logs", {"repo": "o/r", "sha": "abc"}, t)
        assert isinstance(out, dict)
        assert out["checks"][0]["excerpt"].startswith("(logs unavailable: proxy refused)")

    def test_only_failing_conclusions_are_listed(self) -> None:
        t = RecordingTransport(
            check_runs(
                check_run("ok", "success", run_id=1),
                check_run("Neutral", "NEUTRAL", run_id=2),
                check_run("skipped", "skipped", run_id=3),
                check_run("pending", None, run_id=4),
                check_run("cancelled", "cancelled", run_id=5),
                check_run("failed", "failure", run_id=6),
            ),
            texts={"/repos/o/r/actions/jobs/": "log"},
        )
        out = execute_op("checks.failed_logs", {"repo": "o/r", "sha": "abc"}, t)
        assert isinstance(out, dict)
        assert [c["name"] for c in out["checks"]] == ["cancelled", "failed"]
        assert [p for _, p in t.text_calls] == [
            "/repos/o/r/actions/jobs/5/logs",
            "/repos/o/r/actions/jobs/6/logs",
        ]

    def test_no_failures_is_an_empty_list(self) -> None:
        t = RecordingTransport(check_runs(check_run("ok", "success")))
        assert execute_op("checks.failed_logs", {"repo": "o/r", "sha": "abc"}, t) == {"checks": []}
        assert execute_op(
            "checks.failed_logs", {"repo": "o/r", "sha": "abc"}, RecordingTransport()
        ) == {"checks": []}

    def test_requires_repo_and_sha(self) -> None:
        with pytest.raises(GithubOpError, match="missing required params: sha"):
            execute_op("checks.failed_logs", {"repo": "o/r"}, RecordingTransport())

    def test_clip_head_tail_marker(self) -> None:
        assert githubops._clip_head_tail("abcdef", 2, 2) == "ab\n...(clipped 2 chars)...\nef"
        assert githubops._clip_head_tail("abcd", 2, 2) == "abcd"
        # A zero tail must not slice back to the whole string.
        assert githubops._clip_head_tail("abcdef", 2, 0) == "ab\n...(clipped 4 chars)...\n"


class SequencedBlobTransport:
    """Returns a distinct sha per call; can be told to fail on one call."""

    def __init__(self, fail_on_call: int | None = None, no_sha_on_call: int | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.fail_on_call = fail_on_call
        self.no_sha_on_call = no_sha_on_call

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        self.calls.append((method, path, body))
        n = len(self.calls)
        if n == self.fail_on_call:
            raise GithubOpError(f"POST {path} -> HTTP 502: bad gateway")
        if n == self.no_sha_on_call:
            return {"message": "no sha here"}
        return {"sha": f"sha{n}"}


def blob_manifest(count: int) -> list[dict[str, str]]:
    return [
        {"path": f"f{i}.txt", "content_b64": base64.b64encode(f"c{i}".encode()).decode()}
        for i in range(count)
    ]


class TestBlobsCreateMany:
    def test_one_rest_call_per_file_shas_in_order(self) -> None:
        t = SequencedBlobTransport()
        out = execute_op("blobs.create_many", {"repo": "o/r", "files": blob_manifest(3)}, t)
        assert out == {
            "blobs": [
                {"path": "f0.txt", "sha": "sha1"},
                {"path": "f1.txt", "sha": "sha2"},
                {"path": "f2.txt", "sha": "sha3"},
            ]
        }
        assert [(m, p) for m, p, _ in t.calls] == [("POST", "/repos/o/r/git/blobs")] * 3
        assert all(b is not None and b["encoding"] == "base64" for _, _, b in t.calls)

    def test_progress_every_n_and_final(self) -> None:
        seen: list[dict[str, Any]] = []
        execute_op(
            "blobs.create_many",
            {"repo": "o/r", "files": blob_manifest(23)},
            SequencedBlobTransport(),
            progress=lambda **data: seen.append(data),
        )
        assert seen == [
            {"done": 10, "total": 23},
            {"done": 20, "total": 23},
            {"done": 23, "total": 23},
        ]

    def test_transport_failure_names_file(self) -> None:
        with pytest.raises(GithubOpError, match=r"'f1\.txt' \(file 2/3\)"):
            execute_op(
                "blobs.create_many",
                {"repo": "o/r", "files": blob_manifest(3)},
                SequencedBlobTransport(fail_on_call=2),
            )

    def test_missing_sha_names_file(self) -> None:
        with pytest.raises(GithubOpError, match=r"no sha for blob 'f2\.txt'"):
            execute_op(
                "blobs.create_many",
                {"repo": "o/r", "files": blob_manifest(3)},
                SequencedBlobTransport(no_sha_on_call=3),
            )

    def test_malformed_entry_rejected(self) -> None:
        with pytest.raises(GithubOpError, match=r"files\[1\]"):
            execute_op(
                "blobs.create_many",
                {"repo": "o/r", "files": [blob_manifest(1)[0], {"path": "x"}]},
                SequencedBlobTransport(),
            )

    def test_empty_manifest_rejected(self) -> None:
        with pytest.raises(GithubOpError, match="missing required params: files"):
            execute_op("blobs.create_many", {"repo": "o/r", "files": []}, SequencedBlobTransport())


class TestGhCliTransport:
    @pytest.fixture
    def fake_gh(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A fake `gh` executable: records argv+stdin, replies from a file."""
        record = tmp_path / "gh-calls.jsonl"
        reply = tmp_path / "gh-reply.json"
        reply.write_text(json.dumps({"number": 3, "html_url": "https://x/3"}))
        script = tmp_path / "gh"
        script.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{record}"\n'
            f'cat >> "{record}.stdin" 2>/dev/null || true\n'
            f'cat "{reply}"\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def test_request_via_gh(self, fake_gh: Path, tmp_path: Path) -> None:
        transport = GhCliTransport(gh=str(fake_gh))
        out = transport.request("POST", "/repos/o/r/issues", {"title": "T"})
        assert out == {"number": 3, "html_url": "https://x/3"}
        recorded = (tmp_path / "gh-calls.jsonl").read_text()
        assert "api -X POST" in recorded
        assert "/repos/o/r/issues" in recorded
        assert "--input -" in recorded
        assert json.loads((tmp_path / "gh-calls.jsonl.stdin").read_text()) == {"title": "T"}

    def test_gh_failure_raises(self, tmp_path: Path) -> None:
        script = tmp_path / "gh"
        script.write_text('#!/bin/sh\necho "boom" >&2\nexit 1\n')
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        transport = GhCliTransport(gh=str(script))
        with pytest.raises(GithubOpError, match="rc=1"):
            transport.request("GET", "/repos/o/r")

    def test_request_text_returns_stdout_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="line 1\n{not json\n", stderr="")

        monkeypatch.setattr(githubops.subprocess, "run", fake_run)
        out = GhCliTransport(gh="gh").request_text("GET", "/repos/o/r/actions/jobs/7/logs")
        assert out == "line 1\n{not json\n"
        assert seen == [
            [
                "gh",
                "api",
                "-X",
                "GET",
                "-H",
                f"X-GitHub-Api-Version: {githubops.API_VERSION}",
                "/repos/o/r/actions/jobs/7/logs",
            ]
        ]

    def test_request_text_failure_carries_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gh: Gone (HTTP 410)")

        monkeypatch.setattr(githubops.subprocess, "run", fake_run)
        with pytest.raises(GithubOpError, match="rc=1") as info:
            GhCliTransport(gh="gh").request_text("GET", "/repos/o/r/actions/jobs/7/logs")
        assert info.value.http_status == 410


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class RedirectingOpener:
    """Stands in for ``build_opener(...)``: records every request's headers,
    answers the first with a 302 to ``location`` and the rest with ``body``."""

    def __init__(self, location: str, body: bytes = b"log body") -> None:
        self.location = location
        self.body = body
        self.requests: list[tuple[str, dict[str, str]]] = []

    def open(self, request: Any, timeout: float = 0) -> Any:
        headers = {k.lower(): v for k, v in request.header_items()}
        self.requests.append((request.full_url, headers))
        if len(self.requests) == 1:
            hdrs = email.message.Message()
            hdrs["Location"] = self.location
            raise urllib.error.HTTPError(request.full_url, 302, "Found", hdrs, io.BytesIO(b""))
        return FakeResponse(self.body)


class TestRestTransportText:
    def test_redirect_is_followed_without_the_bearer_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The logs endpoint 302s to a signed storage URL. The token must not
        travel there — the signature is the credential, and the storage host
        rejects a request carrying both."""
        opener = RedirectingOpener("https://blob.example/signed?sig=abc", b"log \xff line\n")
        monkeypatch.setattr(githubops.urllib.request, "build_opener", lambda *handlers: opener)
        out = RestTransport(token="tok123").request_text("GET", "/repos/o/r/actions/jobs/7/logs")
        assert out == "log � line\n"
        assert len(opener.requests) == 2
        first_url, first_headers = opener.requests[0]
        assert first_url == "https://api.github.com/repos/o/r/actions/jobs/7/logs"
        assert first_headers["authorization"] == "Bearer tok123"
        second_url, second_headers = opener.requests[1]
        assert second_url == "https://blob.example/signed?sig=abc"
        assert "authorization" not in second_headers
        assert second_headers == {"user-agent": githubops.USER_AGENT}

    def test_redirect_handler_is_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[Any] = []

        def fake_build_opener(*handlers: Any) -> RedirectingOpener:
            seen.extend(handlers)
            return RedirectingOpener("https://blob.example/x")

        monkeypatch.setattr(githubops.urllib.request, "build_opener", fake_build_opener)
        RestTransport(token="tok").request_text("GET", "/repos/o/r/actions/jobs/7/logs")
        assert seen == [githubops._NoRedirect]
        # and the handler really does refuse to follow
        assert githubops._NoRedirect().redirect_request(None, None, 302, "", None, "x") is None  # type: ignore[arg-type]

    def test_redirect_to_plain_http_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        opener = RedirectingOpener("http://blob.example/signed")
        monkeypatch.setattr(githubops.urllib.request, "build_opener", lambda *handlers: opener)
        with pytest.raises(GithubOpError, match="non-HTTPS redirect"):
            RestTransport(token="tok").request_text("GET", "/repos/o/r/actions/jobs/7/logs")
        assert len(opener.requests) == 1

    def test_direct_body_without_redirect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class DirectOpener:
            def open(self, request: Any, timeout: float = 0) -> Any:
                return FakeResponse(b"plain body")

        monkeypatch.setattr(githubops.urllib.request, "build_opener", lambda *h: DirectOpener())
        assert RestTransport(token="tok").request_text("GET", "/repos/o/r/x") == "plain body"

    def test_http_error_mapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FailingOpener:
            def open(self, request: Any, timeout: float = 0) -> Any:
                raise urllib.error.HTTPError(
                    request.full_url, 404, "not found", None, io.BytesIO(b"nope")
                )

        monkeypatch.setattr(githubops.urllib.request, "build_opener", lambda *h: FailingOpener())
        with pytest.raises(GithubOpError, match="HTTP 404") as info:
            RestTransport(token="tok").request_text("GET", "/repos/o/r/actions/jobs/7/logs")
        assert info.value.http_status == 404

    def test_non_https_refused(self) -> None:
        with pytest.raises(GithubOpError, match="non-HTTPS"):
            RestTransport(token="tok").request_text("GET", "http://api.github.com/x")


class TestRestTransport:
    def test_requires_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(GithubOpError, match="no GitHub token"):
            RestTransport()

    def test_request_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        class FakeResponse(io.BytesIO):
            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *args: object) -> None:
                self.close()

        def fake_urlopen(request: Any, timeout: float = 0) -> FakeResponse:
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["auth"] = request.get_header("Authorization")
            captured["data"] = request.data
            return FakeResponse(b'{"number": 11}')

        monkeypatch.setattr(githubops.urllib.request, "urlopen", fake_urlopen)
        transport = RestTransport(token="tok123")
        out = transport.request("POST", "/repos/o/r/issues", {"title": "T"})
        assert out == {"number": 11}
        assert captured["url"] == "https://api.github.com/repos/o/r/issues"
        assert captured["method"] == "POST"
        assert captured["auth"] == "Bearer tok123"
        assert json.loads(captured["data"]) == {"title": "T"}

    def test_http_error_mapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(request: Any, timeout: float = 0) -> Any:
            raise urllib.error.HTTPError(
                request.full_url, 404, "not found", None, io.BytesIO(b'{"message": "nope"}')
            )

        monkeypatch.setattr(githubops.urllib.request, "urlopen", fake_urlopen)
        transport = RestTransport(token="tok")
        with pytest.raises(GithubOpError, match="HTTP 404"):
            transport.request("GET", "/repos/o/r")

    def test_url_error_mapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(request: Any, timeout: float = 0) -> Any:
            raise urllib.error.URLError("proxy refused")

        monkeypatch.setattr(githubops.urllib.request, "urlopen", fake_urlopen)
        transport = RestTransport(token="tok")
        with pytest.raises(GithubOpError, match="proxy refused"):
            transport.request("GET", "/repos/o/r")


class TestSelectTransport:
    def test_prefers_gh_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(githubops.shutil, "which", lambda _: "/usr/bin/gh")
        assert isinstance(select_transport(), GhCliTransport)

    def test_falls_back_to_rest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(githubops.shutil, "which", lambda _: None)
        monkeypatch.setenv("GH_TOKEN", "tok")
        assert isinstance(select_transport(), RestTransport)


class TestRunnerIntegration:
    def test_github_op_through_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """github.op jobs flow through JobRunner with gh.op_start/end events."""
        from sbxloop_worker.protocol import EventTypes, JobRequest
        from sbxloop_worker.runner import JobRunner

        reply = {"number": 4, "html_url": "https://x/4"}
        gh = tmp_path / "gh"
        gh.write_text(f"#!/bin/sh\necho '{json.dumps(reply)}'\n")
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", str(tmp_path), prepend=":")

        job = JobRequest(
            job_id="j9",
            run_id="r1",
            kind="github.op",
            op="issue.create",
            params={"repo": "o/r", "title": "T"},
        )
        result = JobRunner(
            job,
            events_path=tmp_path / "e.jsonl",
            result_path=tmp_path / "r.json",
            heartbeat_s=0,
        ).run()
        assert result.status == "ok"
        assert result.output_json == {"number": 4, "url": "https://x/4"}
        lines = (tmp_path / "e.jsonl").read_text().splitlines()
        types = [json.loads(line)["type"] for line in lines]
        assert EventTypes.GH_OP_START in types
        assert EventTypes.GH_OP_END in types

    def test_blob_batch_emits_progress_events(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """blobs.create_many streams gh.op_progress through the runner."""
        from sbxloop_worker.protocol import EventTypes, JobRequest
        from sbxloop_worker.runner import JobRunner

        gh = tmp_path / "gh"
        gh.write_text('#!/bin/sh\necho \'{"sha": "s"}\'\n')
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", str(tmp_path), prepend=":")

        job = JobRequest(
            job_id="j10",
            run_id="r1",
            kind="github.op",
            op="blobs.create_many",
            params={"repo": "o/r", "files": blob_manifest(10)},
        )
        result = JobRunner(
            job,
            events_path=tmp_path / "e.jsonl",
            result_path=tmp_path / "r.json",
            heartbeat_s=0,
        ).run()
        assert result.status == "ok"
        events = [json.loads(line) for line in (tmp_path / "e.jsonl").read_text().splitlines()]
        progress = [e["data"] for e in events if e["type"] == EventTypes.GH_OP_PROGRESS]
        assert progress == [{"op": "blobs.create_many", "done": 10, "total": 10}]


if sys.platform == "win32":  # pragma: no cover
    pytest.skip("fake gh shims are POSIX shell scripts", allow_module_level=True)


def test_gh_failure_keeps_the_api_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """gh prints its verdict on stderr and the API's error body on stdout;
    a caller matching on the body's wording needs both (#387 field run)."""
    import subprocess as sp

    def fake_run(argv: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
        return sp.CompletedProcess(
            argv,
            1,
            stdout='{"message":"Validation Failed","errors":[{"message":'
            '"A pull request already exists for o:sbxloop/r42."}]}\n',
            stderr="gh: Validation Failed (HTTP 422)\n",
        )

    monkeypatch.setattr(sp, "run", fake_run)
    with pytest.raises(GithubOpError) as info:
        GhCliTransport().request("POST", "/repos/o/r/pulls", {"head": "sbxloop/r42"})
    assert info.value.http_status == 422
    assert "Validation Failed (HTTP 422)" in str(info.value)
    assert "A pull request already exists" in str(info.value)
