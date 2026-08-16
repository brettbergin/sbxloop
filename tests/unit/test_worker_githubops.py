"""Worker github.op error plumbing: HTTP status as a structured field (#221).

Field failure rgwp5z40x: the host matched "HTTP 404" in the error prose while
real GitHub (via gh) said "(HTTP 409)". These tests pin that both transports
put the status on ``GithubOpError.http_status`` and that the runner carries it
onto ``ErrorInfo`` so host code never has to grep messages again.
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
