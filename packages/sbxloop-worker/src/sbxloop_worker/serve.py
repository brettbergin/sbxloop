"""A resident worker: one process per sandbox, jobs in on stdin, events and
results out on stdout.

Every ``sbx exec`` and ``sbx cp`` costs about a second of round trip through
the sandbox backend, whatever it runs (field, db 2026-09-19: ``exec true``
1.15s, ``cp`` of a one-line file 1.1s). The one-process-per-job protocol
(``python -m sbxloop_worker run``) paid three of them per job, and one more
per host-tool response. This server pays the exec once per sandbox: the
host starts it with one ``sbx exec``, writes job requests to its stdin and
reads the jobs' events and results back on its stdout.

The host still initiates everything. The server listens on nothing and
dials nowhere; it reads the pipe the host opened and writes the pipe the
host reads (docs/architecture.md, host-initiated directionality). Each job
runs in a forked child of its own, exactly as ``run`` would have run it
(same :class:`JobRunner`, same events file, same result file), so a job's
crash, timeout or cancel never takes the server with it.

Wire format, one JSON object per line.

stdin (host to server):

- ``{"t": "env", "exports": {NAME: VALUE, ...}}`` the delivered credentials,
  applied to the server's environment so every job forked afterwards sees
  them; sent before the first job and again whenever they change (a
  rotating installation token). Values transit the pipe and process memory
  only, never the sandbox filesystem.
- ``{"t": "job", "job": {...}}`` run one :class:`JobRequest`. The server
  places the job's events, result and host-tool files under its own
  ``~/.sbxloop`` (the layout ``run`` is given on its argv), so the host
  never spells an in-VM path on this channel.
- ``{"t": "tool", "job_id": J, "response": {...}}`` a :class:`HostToolResponse`
  for a call the job's session made; written atomically where
  ``sbxloop_worker.hosttools`` polls for it.
- ``{"t": "cancel", "job_id": J}`` end the job (its process group gets
  SIGTERM, then SIGKILL after a grace period).
- end of file: cancel what is left and exit once every child is reaped.

stdout (server to host):

- ``{"t": "ready", "pid": N, "version": V}`` first, before anything is read
  from stdin.
- the jobs' event lines, byte for byte what each child appends to its
  events file (so every line carries its ``job_id``).
- ``{"t": "result", "job_id": J, "result": {...}}`` the job's
  :class:`JobResult`, once its child exited and left the result file.
- ``{"t": "lost", "job_id": J, "exit_code": N, "stderr": S}`` the child
  exited without a result file (killed, crashed): the host fails the job.
- ``{"t": "error", "message": S}`` a line the server could not act on; it
  keeps serving.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import signal
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import sbxloop_worker
from sbxloop_worker.protocol import HostToolResponse, JobRequest, JobResult
from sbxloop_worker.runner import JobRunner

# How often the server looks for new event bytes and exited children when
# nothing arrives on stdin; the ceiling on event latency to the host.
POLL_S = 0.05
# A cancelled job's process group gets this long to honour SIGTERM.
CANCEL_GRACE_S = 2.0
STDERR_TAIL_CHARS = 1500


@dataclass
class _Child:
    job_id: str
    pid: int
    events_path: Path
    result_path: Path
    stderr_path: Path
    tools_dir: Path | None
    # How far the events file has been forwarded, and the bytes of an
    # unfinished last line.
    offset: int = 0
    partial: bytes = b""
    cancelled_at: float | None = None


class ResidentServer:
    def __init__(
        self,
        *,
        apply_env: Callable[[], None],
        heartbeat_s: float = 15.0,
        limits: dict[str, float] | None = None,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._apply_env = apply_env
        self.heartbeat_s = heartbeat_s
        self.limits = dict(limits or {})
        self._in = sys.stdin if stdin is None else stdin
        self._out = sys.stdout if stdout is None else stdout
        self._clock = clock
        self._children: dict[str, _Child] = {}

    # -- the loop ----------------------------------------------------------

    def run(self) -> int:
        self._emit({"t": "ready", "pid": os.getpid(), "version": sbxloop_worker.__version__})
        fd = self._in.fileno()
        os.set_blocking(fd, False)
        buffer = b""
        closing = False
        while True:
            if closing:
                time.sleep(POLL_S)
            else:
                readable, _, _ = select.select([fd], [], [], POLL_S)
                if readable:
                    try:
                        chunk = os.read(fd, 1 << 16)
                    except BlockingIOError:
                        chunk = None
                    if chunk == b"":
                        closing = True
                        self._cancel_all()
                    elif chunk:
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            self._handle(line.decode("utf-8", "replace"))
            self._pump()
            self._reap()
            if closing and not self._children:
                return 0

    def _handle(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("not an object")
            kind = message.get("t")
            if kind == "env":
                self._env(message)
            elif kind == "job":
                job_id = (
                    message.get("job", {}).get("job_id")
                    if isinstance(message.get("job"), dict)
                    else None
                )
                try:
                    self._start(message)
                except Exception as exc:
                    if not isinstance(job_id, str) or not job_id:
                        raise
                    self._emit(
                        {
                            "t": "lost",
                            "job_id": job_id,
                            "exit_code": None,
                            "stderr": f"could not start: {type(exc).__name__}: {exc}"[
                                :STDERR_TAIL_CHARS
                            ],
                        }
                    )
            elif kind == "tool":
                self._tool(message)
            elif kind == "cancel":
                self._cancel(str(message.get("job_id", "")))
            else:
                raise ValueError(f"unknown message type {kind!r}")
        except Exception as exc:
            self._emit({"t": "error", "message": f"{type(exc).__name__}: {exc}"[:500]})

    # -- messages ----------------------------------------------------------

    def _env(self, message: dict[str, Any]) -> None:
        exports = message.get("exports")
        if not isinstance(exports, dict):
            raise ValueError("env.exports must be an object")
        for key, value in exports.items():
            if isinstance(key, str) and isinstance(value, str):
                os.environ[key] = value

    def _start(self, message: dict[str, Any]) -> None:
        job = JobRequest.model_validate(message["job"])
        if job.job_id in self._children:
            raise ValueError(f"job {job.job_id} is already running")
        # The same in-VM layout `run` is handed on its argv, rooted at this
        # process's home rather than spelt by the host.
        base = Path.home() / ".sbxloop"
        events_path = base / "events" / f"{job.job_id}.jsonl"
        result_path = base / "results" / f"{job.job_id}.json"
        stderr_path = base / "events" / f"{job.job_id}.stderr"
        cwd = job.cwd
        tools_dir = (
            str(base / "tools" / job.job_id) if job.host_tools or job.host_tools_dir else None
        )
        for parent in {events_path.parent, result_path.parent}:
            parent.mkdir(parents=True, exist_ok=True)
        # Everything buffered in this process must be out before the fork,
        # or the child flushes a copy of it into its own stdout.
        self._out.flush()
        pid = os.fork()
        if pid == 0:
            code = 70
            try:
                self._child(job, events_path, result_path, stderr_path, cwd, tools_dir)
                code = 0
            except BaseException:
                with contextlib.suppress(Exception):
                    sys.stderr.write(traceback.format_exc())
                    sys.stderr.flush()
            finally:
                os._exit(code)
        self._children[job.job_id] = _Child(
            job_id=job.job_id,
            pid=pid,
            events_path=events_path,
            result_path=result_path,
            stderr_path=stderr_path,
            tools_dir=Path(str(tools_dir)) if tools_dir else None,
        )

    def _child(
        self,
        job: JobRequest,
        events_path: Path,
        result_path: Path,
        stderr_path: Path,
        cwd: str | None,
        tools_dir: str | None,
    ) -> None:
        # Its own session and group: a cancel signals the job and whatever
        # it started, never the server or a sibling job.
        os.setsid()
        # The parent's stdout is the host's pipe. The child must never
        # write there (its lines would interleave with the parent's
        # forwarding), so fds 1 and 2 go to a per-job file the parent
        # quotes when the child dies without a result, and stdin is closed.
        sink = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(sink, 1)
        os.dup2(sink, 2)
        os.close(sink)
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
        # The env files as `run` reads them at startup: the env-file
        # credential tier rewrites them under the server's feet.
        self._apply_env()
        if cwd:
            os.chdir(cwd)
            job = job.model_copy(update={"cwd": str(Path.cwd())})
        if tools_dir:
            job = job.model_copy(update={"host_tools_dir": str(tools_dir)})
        JobRunner(
            job,
            events_path=events_path,
            result_path=result_path,
            heartbeat_s=self.heartbeat_s,
            disk_warn=self.limits.get("disk_warn", 0.0),
            disk_abort=self.limits.get("disk_abort", 0.0),
            mem_warn=self.limits.get("mem_warn", 0.0),
            mem_abort=self.limits.get("mem_abort", 0.0),
        ).run()

    def _tool(self, message: dict[str, Any]) -> None:
        job_id = str(message.get("job_id", ""))
        child = self._children.get(job_id)
        if child is None:
            raise ValueError(f"no running job {job_id!r} for the tool response")
        if child.tools_dir is None:
            raise ValueError(f"job {job_id} has no host tools directory")
        response = HostToolResponse.model_validate(message["response"])
        child.tools_dir.mkdir(parents=True, exist_ok=True)
        final = child.tools_dir / f"{response.call_id}.json"
        # A rename is atomic where a copy is not: the polling side never
        # sees a half-written document.
        tmp = child.tools_dir / f".{response.call_id}.json.part"
        tmp.write_text(response.model_dump_json())
        tmp.replace(final)

    def _cancel(self, job_id: str) -> None:
        child = self._children.get(job_id)
        if child is None:
            raise ValueError(f"no running job {job_id!r} to cancel")
        if child.cancelled_at is None:
            child.cancelled_at = self._clock()
            self._signal(child, signal.SIGTERM)

    def _cancel_all(self) -> None:
        for child in self._children.values():
            if child.cancelled_at is None:
                child.cancelled_at = self._clock()
                self._signal(child, signal.SIGTERM)

    @staticmethod
    def _signal(child: _Child, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, sig)

    # -- forwarding --------------------------------------------------------

    def _pump(self) -> None:
        wrote = False
        for child in self._children.values():
            wrote = self._forward(child) or wrote
        if wrote:
            self._out.flush()

    def _forward(self, child: _Child) -> bool:
        """Forward the complete lines the child appended since last time."""
        try:
            with child.events_path.open("rb") as handle:
                handle.seek(child.offset)
                chunk = handle.read()
        except OSError:
            return False
        if not chunk:
            return False
        child.offset += len(chunk)
        data = child.partial + chunk
        lines = data.split(b"\n")
        child.partial = lines.pop()
        wrote = False
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._out.write(line + "\n")
                wrote = True
        return wrote

    def _reap(self) -> None:
        for job_id, child in list(self._children.items()):
            if (
                child.cancelled_at is not None
                and self._clock() - child.cancelled_at > CANCEL_GRACE_S
            ):
                self._signal(child, signal.SIGKILL)
            try:
                pid, status = os.waitpid(child.pid, os.WNOHANG)
            except ChildProcessError:
                pid, status = child.pid, 0
            if pid == 0:
                continue
            # Whatever the child wrote in its last moments goes first.
            self._forward(child)
            del self._children[job_id]
            result = self._read_result(child)
            # The tools directory goes before the result is announced, so
            # the host never sees a job "done" with its files still there.
            if child.tools_dir is not None:
                shutil.rmtree(child.tools_dir, ignore_errors=True)
            if result is not None and result.job_id == job_id:
                self._emit(
                    {"t": "result", "job_id": job_id, "result": result.model_dump(mode="json")}
                )
            else:
                self._emit(
                    {
                        "t": "lost",
                        "job_id": job_id,
                        "exit_code": os.waitstatus_to_exitcode(status),
                        "stderr": self._stderr_tail(child),
                    }
                )

    @staticmethod
    def _read_result(child: _Child) -> JobResult | None:
        try:
            return JobResult.model_validate_json(child.result_path.read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _stderr_tail(child: _Child) -> str:
        try:
            return child.stderr_path.read_text(errors="replace")[-STDERR_TAIL_CHARS:]
        except OSError:
            return ""

    def _emit(self, message: dict[str, Any]) -> None:
        self._out.write(json.dumps(message, separators=(",", ":")) + "\n")
        self._out.flush()
