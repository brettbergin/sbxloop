"""githubops tests: op registry, gh CLI transport (fake gh), REST fallback."""

from __future__ import annotations

import base64
import io
import json
import stat
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
    """In-memory transport recording requests and returning canned data."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.responses = responses or {}

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        self.calls.append((method, path, body))
        for prefix, response in self.responses.items():
            if path.startswith(prefix):
                return response  # type: ignore[no-any-return]
        return {}


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


if sys.platform == "win32":  # pragma: no cover
    pytest.skip("fake gh shims are POSIX shell scripts", allow_module_level=True)
