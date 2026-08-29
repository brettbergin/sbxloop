"""Worker github.op tests: HTTP status as a structured field (#221) and
probes answering "missing" as data instead of raising (#222).

Field failure rgwp5z40x: the host matched "HTTP 404" in the error prose while
real GitHub (via gh) said "(HTTP 409)". These tests pin that both transports
put the status on ``GithubOpError.http_status``, that the runner carries it
onto ``ErrorInfo``, and that ``repo.get`` / ``ref.get`` under
``allow_missing`` turn the expected "no" into an ok result. The op registry
runs against a scripted Transport (no gh, no network).
"""

from __future__ import annotations

import io
import subprocess
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from sbxloop_worker import githubops
from sbxloop_worker.githubops import (
    GhCliTransport,
    GithubOpError,
    JsonValue,
    RestTransport,
    execute_op,
    parse_gh_http_status,
)
from sbxloop_worker.protocol import JobRequest
from sbxloop_worker.runner import JobRunner


class TestParseGhHttpStatus:
    @pytest.mark.parametrize(
        ("stderr", "expected"),
        [
            ("gh: Git Repository is empty. (HTTP 409)", 409),
            ("gh: Not Found (HTTP 404)", 404),
            ("HTTP 502: Bad Gateway", 502),
            # gh's parenthesized status is authoritative over a status quoted
            # inside the server message; the last parenthesized one wins.
            ("gh: upstream said HTTP 404 (HTTP 500)", 500),
            ("gh: retried after (HTTP 502) then failed (HTTP 409)", 409),
            ("gh: could not connect", None),
            ("gh: 12345 items", None),
        ],
    )
    def test_cases(self, stderr: str, expected: int | None) -> None:
        assert parse_gh_http_status(stderr) == expected


class ScriptedTransport:
    """Answers each request from a path -> (json | GithubOpError) table."""

    def __init__(self, table: dict[str, Any]) -> None:
        self.table = table
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> JsonValue:
        self.calls.append((method, path))
        answer = self.table[path]
        if isinstance(answer, Exception):
            raise answer
        result: JsonValue = answer
        return result


def http_error(status: int, message: str = "nope") -> GithubOpError:
    return GithubOpError(f"gh: {message} (HTTP {status})", http_status=status)


class TestRepoGet:
    def test_missing_is_data_only_when_asked(self) -> None:
        t = ScriptedTransport({"/repos/o/r": http_error(404, "Not Found")})
        # Default: a 404 is still an error (callers that did not opt in must
        # not silently get an empty dict).
        with pytest.raises(GithubOpError, match="404"):
            execute_op("repo.get", {"repo": "o/r"}, transport=t)
        assert execute_op("repo.get", {"repo": "o/r", "allow_missing": True}, transport=t) == {
            "missing": True,
            "http_status": 404,
        }

    def test_other_statuses_still_raise_with_allow_missing(self) -> None:
        t = ScriptedTransport({"/repos/o/r": http_error(403, "rate limited")})
        with pytest.raises(GithubOpError, match="403"):
            execute_op("repo.get", {"repo": "o/r", "allow_missing": True}, transport=t)

    def test_present_repo_passes_through(self) -> None:
        t = ScriptedTransport({"/repos/o/r": {"full_name": "o/r"}})
        assert execute_op("repo.get", {"repo": "o/r", "allow_missing": True}, transport=t) == {
            "full_name": "o/r"
        }


class TestRefGet:
    def test_resolves_sha(self) -> None:
        t = ScriptedTransport(
            {"/repos/o/r/git/ref/heads/main": {"ref": "refs/heads/main", "object": {"sha": "abc"}}}
        )
        assert execute_op("ref.get", {"repo": "o/r", "ref": "heads/main"}, transport=t) == {
            "ref": "refs/heads/main",
            "sha": "abc",
        }

    @pytest.mark.parametrize("status", [404, 409])
    def test_missing_and_empty_repo_are_data(self, status: int) -> None:
        """404 (absent branch) and 409 (empty repository — the shape real
        GitHub gave run rgwp5z40x) both mean 'no base to build on'."""
        t = ScriptedTransport({"/repos/o/r/git/ref/heads/main": http_error(status)})
        params = {"repo": "o/r", "ref": "heads/main", "allow_missing": True}
        assert execute_op("ref.get", params, transport=t) == {
            "missing": True,
            "http_status": status,
        }
        with pytest.raises(GithubOpError, match=str(status)):
            execute_op("ref.get", {"repo": "o/r", "ref": "heads/main"}, transport=t)

    def test_unrelated_status_raises(self) -> None:
        t = ScriptedTransport({"/repos/o/r/git/ref/heads/main": http_error(403)})
        with pytest.raises(GithubOpError, match="403"):
            execute_op(
                "ref.get", {"repo": "o/r", "ref": "heads/main", "allow_missing": True}, transport=t
            )

    def test_malformed_response_raises(self) -> None:
        t = ScriptedTransport({"/repos/o/r/git/ref/heads/main": {"message": "weird"}})
        with pytest.raises(GithubOpError, match="no object sha"):
            execute_op("ref.get", {"repo": "o/r", "ref": "heads/main"}, transport=t)


class TestGhCliTransport:
    def test_http_failure_carries_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="gh: Git Repository is empty. (HTTP 409)\n"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(GithubOpError) as info:
            GhCliTransport().request("GET", "/repos/o/r/git/ref/heads/main")
        assert info.value.http_status == 409
        assert "HTTP 409" in str(info.value)

    def test_non_http_failure_has_no_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 4, stdout="", stderr="gh: not logged in")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(GithubOpError) as info:
            GhCliTransport().request("GET", "/user")
        assert info.value.http_status is None


class TestRestTransport:
    def test_http_error_carries_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(request: Any, timeout: float = 0) -> Any:
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {}, io.BytesIO(b'{"message":"Not Found"}')
            )

        monkeypatch.setattr(githubops.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(GithubOpError) as info:
            RestTransport(token="t").request("GET", "/repos/o/r")
        assert info.value.http_status == 404

    def test_url_error_has_no_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_urlopen(request: Any, timeout: float = 0) -> Any:
            raise urllib.error.URLError("dns down")

        monkeypatch.setattr(githubops.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(GithubOpError) as info:
            RestTransport(token="t").request("GET", "/repos/o/r")
        assert info.value.http_status is None


class TestBlobsCreateMany:
    def test_wrapped_error_keeps_status(self) -> None:
        class Failing:
            def request(self, method: str, path: str, body: Any = None) -> Any:
                raise GithubOpError("POST blobs -> HTTP 403: nope", http_status=403)

        with pytest.raises(GithubOpError) as info:
            execute_op(
                "blobs.create_many",
                {"repo": "o/r", "files": [{"path": "a.txt", "content_b64": "YQ=="}]},
                transport=Failing(),
            )
        assert info.value.http_status == 403
        assert "'a.txt'" in str(info.value)


class TestRunnerErrorInfo:
    def _run(self, tmp_path: Path, exc: BaseException) -> Any:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise exc

        job = JobRequest(job_id="j1", run_id="r1", kind="github.op", op="raw.api", params={})
        runner = JobRunner(job, tmp_path / "events.jsonl", tmp_path / "result.json", heartbeat_s=0)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(githubops, "execute_op", boom)
            return runner.run()

    def test_github_op_error_status_lands_on_error_info(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, GithubOpError("gh: empty (HTTP 409)", http_status=409))
        assert result.status == "error"
        assert result.error is not None
        assert result.error.type == "GithubOpError"
        assert result.error.http_status == 409

    def test_other_exceptions_leave_status_unset(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, RuntimeError("boom"))
        assert result.error is not None
        assert result.error.http_status is None


class RecordingTransport:
    """Records (method, path, body) and answers with a fixed payload."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> JsonValue:
        self.calls.append((method, path, body))
        result: JsonValue = self.answer
        return result


class TestPrUpdate:
    def test_patches_only_supplied_fields(self) -> None:
        t = RecordingTransport({"number": 7, "html_url": "https://p/7"})
        out = execute_op(
            "pr.update",
            {"repo": "o/r", "number": 7, "title": "T", "body": "", "base": "main"},
            t,
        )
        assert t.calls == [("PATCH", "/repos/o/r/pulls/7", {"title": "T", "base": "main"})]
        assert out == {"number": 7, "url": "https://p/7", "state": None, "head_ref": None}

    def test_falls_back_to_requested_number(self) -> None:
        t = RecordingTransport({"html_url": "https://p/7"})
        out = execute_op("pr.update", {"repo": "o/r", "number": 7, "body": "B"}, t)
        assert out == {"number": 7, "url": "https://p/7", "state": None, "head_ref": None}

    def test_surfaces_state_and_head_ref(self) -> None:
        t = RecordingTransport(
            {"number": 7, "html_url": "https://p/7", "state": "open", "head": {"ref": "b"}}
        )
        out = execute_op("pr.update", {"repo": "o/r", "number": 7, "body": "B"}, t)
        assert out == {"number": 7, "url": "https://p/7", "state": "open", "head_ref": "b"}

    def test_requires_repo_and_number(self) -> None:
        with pytest.raises(GithubOpError):
            execute_op("pr.update", {"repo": "o/r"}, RecordingTransport({}))

    def test_registered_in_ops(self) -> None:
        assert "pr.update" in githubops.OPS
