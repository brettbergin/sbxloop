from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from sbxloop_worker.backends.codex_tools import local_tools
from sbxloop_worker.protocol import JobRequest


def _job(tmp_path: Path, **changes: object) -> JobRequest:
    return JobRequest.model_validate(
        {
            "job_id": "job",
            "run_id": "run",
            "kind": "agent.session",
            "prompt": "Inspect the workspace.",
            "cwd": str(tmp_path),
            **changes,
        }
    )


def _invoke(job: JobRequest, name: str, **arguments: object) -> dict[str, object]:
    tools = {tool.spec.name: tool for tool in local_tools(job)}
    return json.loads(tools[name].invoke(arguments))


def test_local_tool_permissions_and_explicit_allowlist(tmp_path: Path) -> None:
    assert {t.spec.name for t in local_tools(_job(tmp_path))} == {
        "shell",
        "read_file",
        "list_files",
        "search_files",
        "write_file",
    }
    assert {t.spec.name for t in local_tools(_job(tmp_path, permission_mode="read_only"))} == {
        "read_file",
        "list_files",
        "search_files",
    }
    assert local_tools(_job(tmp_path, available_tools=[])) == []
    assert local_tools(_job(tmp_path / "unused", available_tools=[])) == []
    assert [t.spec.name for t in local_tools(_job(tmp_path, available_tools=["read_file"]))] == [
        "read_file"
    ]
    with pytest.raises(ValueError, match=r"unknown.*tool"):
        local_tools(_job(tmp_path, available_tools=["mystery"]))
    with pytest.raises(ValueError, match=r"read.only"):
        local_tools(_job(tmp_path, permission_mode="read_only", available_tools=["shell"]))


def test_read_list_search_and_write(tmp_path: Path) -> None:
    tmp_path = tmp_path / "workspace"
    tmp_path.mkdir()
    _invoke(_job(tmp_path), "write_file", path="src/report.txt", content="First\nNeedle\nLast\n")
    read = _invoke(_job(tmp_path), "read_file", path="src/report.txt", start_line=2, max_lines=1)
    assert read["text"] == "Needle\n"
    assert read["truncated"] is True
    listed = _invoke(_job(tmp_path), "list_files", path=".")
    assert listed["files"] == ["src/report.txt"]
    searched = _invoke(_job(tmp_path), "search_files", query="Needle")
    assert searched["matches"] == [{"path": "src/report.txt", "line": 2, "text": "Needle"}]


@pytest.mark.parametrize("tool", ["read_file", "list_files", "search_files", "write_file"])
def test_paths_cannot_escape_workspace(tmp_path: Path, tool: str) -> None:
    arguments = {"path": "../outside", "content": "x", "query": "x"}
    with pytest.raises(ValueError, match=r"outside.*workspace"):
        _invoke(_job(tmp_path), tool, **arguments)


def test_symlinks_cannot_expose_or_overwrite_outside_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("private needle")
    try:
        (workspace / "link.txt").symlink_to(external)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    job = _job(workspace)
    with pytest.raises(ValueError, match=r"outside.*workspace"):
        _invoke(job, "read_file", path="link.txt")
    with pytest.raises(ValueError, match=r"outside.*workspace"):
        _invoke(job, "write_file", path="link.txt", content="changed")
    assert _invoke(job, "list_files")["files"] == []
    assert _invoke(job, "search_files", query="needle")["matches"] == []
    assert external.read_text() == "private needle"


def test_outputs_are_bounded_and_report_truncation(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("x" * 200_000)
    result = _invoke(_job(tmp_path), "read_file", path="large.txt")
    assert len(result["text"]) <= 32_768
    assert result["truncated"] is True
    for index in range(5):
        (tmp_path / f"match-{index}.txt").write_text("needle")
    result = _invoke(_job(tmp_path), "search_files", query="needle", max_results=2)
    assert len(result["matches"]) == 2
    assert result["truncated"] is True


def test_expired_deadline_prevents_file_mutation(tmp_path: Path) -> None:
    tools = {
        tool.spec.name: tool for tool in local_tools(_job(tmp_path), deadline=time.monotonic() - 1)
    }
    with pytest.raises(TimeoutError):
        tools["write_file"].invoke({"path": "late.txt", "content": "late"})
    assert not (tmp_path / "late.txt").exists()


def test_search_is_literal_and_binary_files_are_not_text(tmp_path: Path) -> None:
    (tmp_path / "text.txt").write_text("first.*match\nother text")
    (tmp_path / "binary").write_bytes(b"\x00first.*match")
    result = _invoke(_job(tmp_path), "search_files", query=".*")
    assert result["matches"] == [{"path": "text.txt", "line": 1, "text": "first.*match"}]
    with pytest.raises(ValueError, match="binary"):
        _invoke(_job(tmp_path), "read_file", path="binary")


def test_read_and_write_reject_nonregular_files(tmp_path: Path) -> None:
    import os

    if not hasattr(os, "mkfifo"):
        pytest.skip("named pipes require POSIX")
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(ValueError, match="regular file"):
        _invoke(_job(tmp_path), "read_file", path="pipe")
    with pytest.raises(ValueError, match="regular file"):
        _invoke(_job(tmp_path), "write_file", path="pipe", content="no blocking write")


def test_shell_capture_and_timeout_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import subprocess

    import sbxloop_worker.backends.codex_tools as module

    class Process:
        pid = 12345
        returncode = 0
        stdout = io.BytesIO(b"x" * 100_000)
        stderr = io.BytesIO(b"diagnostic")

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

    calls = []
    monkeypatch.setattr(
        module.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)) or Process()
    )
    result = _invoke(_job(tmp_path), "shell", command="a command")
    assert result["exit_code"] == 0
    assert result["stdout"] == "x" * 32_768
    assert result["stderr"] == "diagnostic"
    assert result["truncated"] is True
    assert calls[0][0] == ["sh", "-c", "a command"]
    assert calls[0][1]["cwd"] == str(tmp_path.resolve())

    class TimedOutProcess(Process):
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired("sh", timeout)
            return -9

    killed = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: TimedOutProcess())
    monkeypatch.setattr(module, "_kill_process_group", lambda process: killed.append(process.pid))
    with pytest.raises(TimeoutError):
        _invoke(_job(tmp_path, timeout_s=0.1), "shell", command="a long command")
    assert killed == [12345]
