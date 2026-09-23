"""The resident transport, host side: one worker process per sandbox.

Every ``sbx exec`` and ``sbx cp`` costs about a second of round trip
through the sandbox backend, whatever it runs (field, db 2026-09-19:
``exec true`` 1.15s, ``cp`` of a one-line file 1.1s). The stream transport
paid three of them per job (cp the job in, exec the worker, cp the result
out) and one more per host-tool response. Here the host starts
``python -m sbxloop_worker serve`` once per sandbox with one exec, writes
each job to its stdin and reads the job's events and result back on its
stdout; a tool response rides the same stdin. The wire format is the
server's (``sbxloop_worker.serve``).

Host-initiated, still: the host opens the exec and owns both pipes; the
sandbox process listens on nothing. Credentials go the same road per-job
stdin delivery already uses, once before the first job and again when they
change, so they are never at rest in the VM.

A server that dies (the sandbox rebooted, the process was killed) fails
the jobs it was running the way a lost worker always has, and the next
submit starts a fresh one. A server that never reports ready is given up
on, and the client falls back to the stream transport.
"""

from __future__ import annotations

import contextlib
import json
import queue
import shlex
import subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sbxloop.errors import SbxError, WorkerError, WorkerTimeoutError
from sbxloop.log import get_logger
from sbxloop.sbx.sandbox import ENV_FILE
from sbxloop_worker.protocol import HostToolResponse, JobRequest, JobResult

if TYPE_CHECKING:
    from sbxloop.worker.client import WorkerClient

log = get_logger(__name__)

# A cold interpreter in a VM reports ready in a second or two; a login
# profile that hangs, or an sbx that does not pass stdin through, never does.
READY_TIMEOUT_S = 60.0


class ResidentWorker:
    """One running ``sbxloop_worker serve`` and the jobs waiting on it."""

    def __init__(self, client: WorkerClient) -> None:
        self.client = client
        self.proc: subprocess.Popen[str] | None = None
        # job_id -> the queue its submit thread drains: ("line", raw event
        # line), ("result", JobResult json), ("lost", server message),
        # ("dead", diagnostics).
        self.pending: dict[str, queue.Queue[tuple[str, Any]]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._ready = threading.Event()
        self._dead = threading.Event()
        self._stderr_tail: deque[str] = deque(maxlen=50)
        self._env_sent: tuple[tuple[str, str], ...] | None = None
        self.pid: int | None = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and not self._dead.is_set()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        client = self.client
        argv = [
            client.python,
            *(["-I"] if client.role == "service" else []),
            "-m",
            "sbxloop_worker",
            "serve",
            "--env-file",
            ENV_FILE,
        ]
        if client.limits is not None:
            argv += [
                "--disk-warn",
                str(client.limits.disk_warn),
                "--disk-abort",
                str(client.limits.disk_abort),
                "--mem-warn",
                str(client.limits.mem_warn),
                "--mem-abort",
                str(client.limits.mem_abort),
            ]
        # The same login shell a job launch uses, so the sandbox's profile
        # (PATH, sbx's own exports) is loaded once for every job to come.
        wrapped = ["sh", "-lc", shlex.join(argv)]
        try:
            proc = client.sandbox.exec_stream(wrapped, stdin_pipe=True)
        except SbxError as exc:
            raise WorkerError(f"resident worker could not be started: {exc}") from exc
        self.proc = proc
        threading.Thread(
            target=self._read_stdout, name="sbxloop-resident-reader", daemon=True
        ).start()
        threading.Thread(
            target=self._read_stderr, name="sbxloop-resident-stderr", daemon=True
        ).start()
        if not self._ready.wait(READY_TIMEOUT_S) or self._dead.is_set():
            self._terminate()
            raise WorkerError(
                f"resident worker in {client.sandbox.name} did not report ready"
                f"{self._diagnostics()}"
            )
        log.info(
            "worker.resident_started",
            sandbox=client.sandbox.name,
            role=client.role,
            pid=self.pid,
        )
        self.refresh_env()

    def close(self) -> None:
        """End the server: close its stdin (it cancels what is left and
        exits), then make sure of it."""
        proc = self.proc
        if proc is None:
            return
        with contextlib.suppress(OSError):
            if proc.stdin is not None:
                proc.stdin.close()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._terminate()
        self._dead.set()

    def _terminate(self) -> None:
        proc = self.proc
        if proc is None:
            return
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5.0)

    # -- the pipe ------------------------------------------------------------

    def _send(self, message: Mapping[str, Any]) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None or not self.alive:
            raise WorkerError(f"resident worker in {self.client.sandbox.name} is gone")
        line = json.dumps(message, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self._dead.set()
                raise WorkerError(
                    f"resident worker in {self.client.sandbox.name} stopped taking work: {exc}"
                ) from exc

    def refresh_env(self) -> None:
        """Send the delivered credentials when they are new or changed."""
        if self.client.job_env is None:
            return
        exports = tuple(sorted(self.client.job_env().items()))
        if exports == self._env_sent:
            return
        self._send({"t": "env", "exports": dict(exports)})
        self._env_sent = exports

    def deliver_tool(self, job_id: str, response: HostToolResponse) -> None:
        self._send({"t": "tool", "job_id": job_id, "response": response.model_dump(mode="json")})

    def _read_stdout(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                # A login profile's greeting, a stray print: nothing to
                # attribute to a job.
                log.debug(
                    "worker.resident_noise", sandbox=self.client.sandbox.name, line=line[:200]
                )
                continue
            if not isinstance(obj, dict):
                continue
            kind = obj.get("t")
            if kind is not None:
                self._control(str(kind), obj)
                continue
            job_id = obj.get("job_id")
            with self._lock:
                waiting = self.pending.get(job_id) if isinstance(job_id, str) else None
            if waiting is not None:
                waiting.put(("line", line))
            else:
                log.debug(
                    "worker.resident_orphan_event",
                    sandbox=self.client.sandbox.name,
                    job=job_id,
                    event_type=str(obj.get("type", ""))[:100],
                )
        self._dead.set()
        note = self._diagnostics()
        with self._lock:
            waiting_all = list(self.pending.values())
        for waiting in waiting_all:
            waiting.put(("dead", note))
        log.info("worker.resident_exited", sandbox=self.client.sandbox.name, detail=note[:300])

    def _control(self, kind: str, obj: dict[str, Any]) -> None:
        if kind == "ready":
            pid = obj.get("pid")
            self.pid = pid if isinstance(pid, int) else None
            self._ready.set()
            return
        if kind == "error":
            log.warning(
                "worker.resident_error",
                sandbox=self.client.sandbox.name,
                message=str(obj.get("message", ""))[:500],
            )
            return
        job_id = obj.get("job_id")
        with self._lock:
            waiting = self.pending.get(job_id) if isinstance(job_id, str) else None
        if waiting is None:
            # A cancelled job's tail, or a result nobody waits on any more.
            log.debug("worker.resident_late_message", sandbox=self.client.sandbox.name, kind=kind)
            return
        if kind == "result":
            waiting.put(("result", obj.get("result")))
        elif kind == "lost":
            waiting.put(("lost", obj))

    def _read_stderr(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stderr is not None
        for line in proc.stderr:
            self._stderr_tail.append(line.rstrip())

    def _diagnostics(self) -> str:
        parts = []
        proc = self.proc
        if proc is not None and proc.poll() is not None:
            parts.append(f"exec rc={proc.returncode}")
        if self._stderr_tail:
            parts.append("stderr: " + " | ".join(self._stderr_tail)[-1500:])
        return ("; " + "; ".join(parts)) if parts else ""

    # -- a job -----------------------------------------------------------------

    def submit(
        self,
        job: JobRequest,
        *,
        events_path: str,
        result_path: str,
        deadline: float,
    ) -> JobResult:
        """Run ``job`` on the server and wait for its result, publishing its
        events through the client as they arrive."""
        waiting: queue.Queue[tuple[str, Any]] = queue.Queue()
        with self._lock:
            self.pending[job.job_id] = waiting
        try:
            # The server roots the job's files at its own home; the paths
            # here only name them in diagnostics.
            self._send({"t": "job", "job": job.model_dump(mode="json")})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning(
                        "worker.job_timeout",
                        job=job.job_id,
                        sandbox=self.client.sandbox.name,
                        transport="resident",
                        timeout_s=job.timeout_s,
                        grace_s=self.client.grace_s,
                    )
                    with contextlib.suppress(WorkerError):
                        self._send({"t": "cancel", "job_id": job.job_id})
                    grace = self.client.grace_s
                    raise WorkerTimeoutError(
                        f"job {job.job_id} exceeded {job.timeout_s}s (+{grace}s grace)"
                    )
                try:
                    kind, payload = waiting.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    # The exec can end without its stdout closing (a child
                    # of the launch shell may hold the pipe): the process
                    # itself is the liveness, not the stream.
                    if not self.alive:
                        kind, payload = "dead", self._diagnostics()
                    else:
                        continue
                if kind == "line":
                    self.client._handle_line(job, payload)
                elif kind == "result":
                    try:
                        result = JobResult.model_validate(payload)
                    except ValueError as exc:
                        raise WorkerError(f"invalid result for job {job.job_id}: {exc}") from exc
                    if result.job_id != job.job_id:
                        raise WorkerError(
                            f"result job_id mismatch: expected {job.job_id}, got {result.job_id}"
                        )
                    return result
                elif kind == "lost":
                    detail = [
                        f"worker for job {job.job_id} produced no result file ({result_path})",
                        f"resident worker child exit {payload.get('exit_code')}",
                    ]
                    tail = str(payload.get("stderr") or "").strip().replace("\n", " | ")
                    if tail:
                        detail.append(f"stderr: {tail[-1500:]}")
                    raise WorkerError("; ".join(detail))
                elif kind == "dead":
                    raise WorkerError(
                        f"resident worker in {self.client.sandbox.name} exited before job "
                        f"{job.job_id} finished{payload}"
                    )
        finally:
            with self._lock:
                self.pending.pop(job.job_id, None)
