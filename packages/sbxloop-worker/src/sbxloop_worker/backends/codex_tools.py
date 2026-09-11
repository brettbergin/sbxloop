"""Worker-owned Codex tools whose every invocation passes the session governor.

Codex's native tools are disabled by the adapter. These functions run only in
the agent worker sandbox; the read-only set confines resolved paths to its
workspace. The host-only concierge receives none of these tools.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from sbxloop_worker.protocol import HostToolSpec, JobRequest

_OUTPUT_LIMIT = 32_768
_FILE_LIMIT = 1_048_576
_SCAN_LIMIT = 4_194_304
_FILE_COUNT_LIMIT = 2_000
_READ_TOOLS = frozenset({"read_file", "list_files", "search_files"})


@dataclass(frozen=True)
class LocalTool:
    spec: HostToolSpec
    invoke: Callable[[dict[str, Any]], str]


def _text(arguments: dict[str, Any], key: str, default: str | None = None) -> str:
    value = arguments.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _integer(arguments: dict[str, Any], key: str, default: int, maximum: int) -> int:
    value = arguments.get(key, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{key} must be an integer between 1 and {maximum}")
    return value


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Codex tool exceeded the session deadline")
    return remaining


def _path(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("path resolves outside the workspace")
    return path


def _files(root: Path, start: Path, deadline: float) -> Iterator[Path]:
    if start.is_file():
        yield start
        return
    if not start.is_dir():
        raise ValueError("path must name an existing file or directory")
    for directory, dirs, files in os.walk(start, followlinks=False):
        _remaining(deadline)
        dirs[:] = sorted(
            name
            for name in dirs
            if name != ".git" and (Path(directory) / name).resolve().is_relative_to(root)
        )
        for name in sorted(files):
            _remaining(deadline)
            candidate = Path(directory) / name
            if name != ".git" and candidate.resolve().is_relative_to(root) and candidate.is_file():
                yield candidate


def _read(path: Path, limit: int = _FILE_LIMIT) -> tuple[str, bool]:
    if not path.is_file():
        raise ValueError("path must name an existing regular file")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if b"\x00" in data:
        raise ValueError("path contains binary data; a text file is required")
    return data[:limit].decode("utf-8", errors="replace"), len(data) > limit


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill descendants as well as the command on the Linux worker runtime."""
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        process.kill()


@dataclass
class _Capture:
    data: bytes = b""
    truncated: bool = False

    def drain(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(8192):
                available = _OUTPUT_LIMIT - len(self.data)
                self.data += chunk[:available]
                self.truncated |= len(chunk) > available
        finally:
            stream.close()


def _shell(root: Path, arguments: dict[str, Any], deadline: float) -> str:
    command = _text(arguments, "command")
    requested_timeout = arguments.get("timeout_s", 120)
    if (
        isinstance(requested_timeout, bool)
        or not isinstance(requested_timeout, int | float)
        or requested_timeout <= 0
    ):
        raise ValueError("timeout_s must be a positive number")
    call_deadline = min(deadline, time.monotonic() + requested_timeout)
    _remaining(call_deadline)
    process = subprocess.Popen(  # nosec B603 B607 — fixed shell argv, only in agent sandbox.
        ["sh", "-c", command],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout, stderr = _Capture(), _Capture()
    readers = [
        threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        process.wait(timeout=_remaining(call_deadline))
        for reader in readers:
            reader.join(timeout=_remaining(call_deadline))
            if reader.is_alive():
                raise TimeoutError("shell descendants did not finish before the deadline")
    except (subprocess.TimeoutExpired, TimeoutError) as error:
        _kill_process_group(process)
        process.wait()
        for reader in readers:
            reader.join(timeout=1)
        raise TimeoutError("shell command exceeded its time limit") from error
    return json.dumps(
        {
            "exit_code": process.returncode,
            "stdout": stdout.data.decode("utf-8", errors="replace"),
            "stderr": stderr.data.decode("utf-8", errors="replace"),
            "truncated": stdout.truncated or stderr.truncated,
        }
    )


def local_tools(job: JobRequest, *, deadline: float | None = None) -> list[LocalTool]:
    """Build the permitted local tools, rejecting unsupported allowlists."""
    if job.available_tools == []:
        return []
    root = Path(job.cwd or Path.cwd()).resolve()
    if not root.is_dir():
        raise ValueError("Codex workspace must be an existing directory")
    deadline = deadline if deadline is not None else time.monotonic() + job.timeout_s

    def read_file(arguments: dict[str, Any]) -> str:
        _remaining(deadline)
        path = _path(root, _text(arguments, "path"))
        start = _integer(arguments, "start_line", 1, 100_000)
        maximum = _integer(arguments, "max_lines", 200, 2_000)
        text, truncated = _read(path)
        lines = text.splitlines(keepends=True)
        selected = "".join(lines[start - 1 : start - 1 + maximum])
        return json.dumps(
            {
                "path": path.relative_to(root).as_posix(),
                "start_line": start,
                "text": selected[:_OUTPUT_LIMIT],
                "truncated": truncated
                or start - 1 + maximum < len(lines)
                or len(selected) > _OUTPUT_LIMIT,
            }
        )

    def list_files(arguments: dict[str, Any]) -> str:
        _remaining(deadline)
        start = _path(root, _text(arguments, "path", "."))
        found: list[str] = []
        size = 0
        truncated = False
        for path in _files(root, start, deadline):
            relative = path.relative_to(root).as_posix()
            size += len(relative)
            if len(found) >= _FILE_COUNT_LIMIT or size > _OUTPUT_LIMIT:
                truncated = True
                break
            found.append(relative)
        return json.dumps({"files": found, "truncated": truncated})

    def search_files(arguments: dict[str, Any]) -> str:
        _remaining(deadline)
        query = _text(arguments, "query")
        if not query:
            raise ValueError("query must not be empty")
        start = _path(root, _text(arguments, "path", "."))
        maximum = _integer(arguments, "max_results", 50, 200)
        matches: list[dict[str, Any]] = []
        scanned = 0
        size = 0
        truncated = False
        for count, path in enumerate(_files(root, start, deadline)):
            if count >= _FILE_COUNT_LIMIT or scanned >= _SCAN_LIMIT:
                truncated = True
                break
            try:
                text, cut = _read(path, min(_FILE_LIMIT, _SCAN_LIMIT - scanned))
            except ValueError:
                continue
            scanned += len(text.encode("utf-8"))
            truncated |= cut
            for line_number, line in enumerate(text.splitlines(), 1):
                _remaining(deadline)
                if query not in line:
                    continue
                relative = path.relative_to(root).as_posix()
                snippet_start = max(0, line.index(query) - 80)
                snippet = line[snippet_start : snippet_start + 512]
                size += len(relative) + len(snippet)
                if len(matches) >= maximum or size > _OUTPUT_LIMIT:
                    return json.dumps({"matches": matches, "truncated": True})
                matches.append({"path": relative, "line": line_number, "text": snippet})
        return json.dumps({"matches": matches, "truncated": truncated})

    def write_file(arguments: dict[str, Any]) -> str:
        _remaining(deadline)
        path = _path(root, _text(arguments, "path"))
        content = _text(arguments, "content")
        if len(content.encode("utf-8")) > _FILE_LIMIT:
            raise ValueError("content exceeds the 1 MiB write limit")
        if path.exists() and not path.is_file():
            raise ValueError("path must name a regular file")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return json.dumps({"path": path.relative_to(root).as_posix(), "written": True})

    string = {"type": "string"}
    path_property = {"path": string}
    definitions: list[
        tuple[str, str, dict[str, Any], list[str], Callable[[dict[str, Any]], str]]
    ] = [
        (
            "read_file",
            "Read UTF-8 text in the workspace; output is bounded. Line numbers start at 1.",
            {
                **path_property,
                "start_line": {"type": "integer", "minimum": 1, "maximum": 100_000},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 2_000},
            },
            ["path"],
            read_file,
        ),
        (
            "list_files",
            "List workspace files recursively, excluding .git and external symlinks.",
            path_property,
            [],
            list_files,
        ),
        (
            "search_files",
            "Find case-sensitive literal text in workspace files; returns bounded matching lines.",
            {
                **path_property,
                "query": string,
                "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ["query"],
            search_files,
        ),
        (
            "write_file",
            "Write UTF-8 text to a workspace file, creating parents; at most 1 MiB.",
            {**path_property, "content": string},
            ["path", "content"],
            write_file,
        ),
        (
            "shell",
            "Run a shell command in the agent sandbox workspace. "
            "Output is bounded; default timeout is 120 seconds.",
            {"command": string, "timeout_s": {"type": "number", "exclusiveMinimum": 0}},
            ["command"],
            lambda arguments: _shell(root, arguments, deadline),
        ),
    ]
    all_names = {name for name, *_ in definitions}
    selected = set(job.available_tools) if job.available_tools is not None else all_names
    if unknown := selected - all_names:
        raise ValueError(f"unknown Codex local tool(s): {', '.join(sorted(unknown))}")
    if job.permission_mode == "read_only":
        if job.available_tools is not None and selected - _READ_TOOLS:
            raise ValueError("configured Codex tools are not permitted in read-only sessions")
        selected &= _READ_TOOLS
    return [
        LocalTool(
            spec=HostToolSpec(
                name=name,
                description=description,
                parameters={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            ),
            invoke=invoke,
        )
        for name, description, properties, required, invoke in definitions
        if name in selected
    ]
