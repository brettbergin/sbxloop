"""The worker job runner: dispatch one JobRequest, emit events, write the result."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from sbxloop_worker.backends import get_backend
from sbxloop_worker.events import EventWriter
from sbxloop_worker.protocol import (
    BatchCommandResult,
    ErrorInfo,
    EventTypes,
    JobRequest,
    JobResult,
)
from sbxloop_worker.resources import LEVEL_SEVERITY, classify_level, sample_resources

OUTPUT_TAIL_CHARS = 20_000

# How long anything a command leaves running gets to honour SIGTERM before
# the group is killed outright. A dev server or a database shutting down
# cleanly takes milliseconds; nothing a verify command starts deserves
# longer, and the wait ends as soon as the group is empty.
GROUP_TERM_GRACE_S = 2.0


def run_isolated_command(command: str, *, cwd: str | None, timeout_s: float) -> tuple[int, str]:
    """Run one shell command so it can neither see nor outlive itself.

    Verify commands are model-authored text, and running them the obvious
    way — ``sh -c '<the whole command>'`` sharing the worker's process
    group — gave that text two ways to break a check that was otherwise
    correct.

    It could **see itself**. ``pkill``/``pgrep`` match against the full
    command line, and ``sh -c`` puts the entire command *on* the command
    line, so any pattern drawn from the command's own text also matches the
    shell running it. A check that started a dev server on a port and
    cleaned up with ``pkill -f <port>`` signalled its own shell: it died
    with a SIGTERM exit and no output, identically on every attempt, with
    the work correct and every other gate green (field failure rkbgkf32a,
    a run abandoned as unverifiable). Passing the command as a *script
    file* keeps the text out of the process table, so a pattern kill
    reaches what the command started and nothing else.

    It could **outlive itself**. Anything backgrounded and not reaped kept
    running after the command returned — holding its port against the next
    attempt, and holding the captured pipe open so reading the output
    blocked until the whole job timed out. Each command gets a session of
    its own, which makes its leftovers a process group this can signal, and
    output goes to a file rather than to a pipe an orphan can hold open.

    Stdin is ``/dev/null`` for the same reason: a check that reads it is
    already wrong, and inheriting the worker's would let it block there
    until the job's timeout instead of failing in front of the builder.

    Returns the exit code and the combined stdout/stderr. Raises
    :class:`subprocess.TimeoutExpired` when ``timeout_s`` passes, after
    tearing the group down.
    """
    with tempfile.TemporaryDirectory(prefix="sbxloop-cmd-") as tmp:
        script = Path(tmp) / "command.sh"
        script.write_text(command, encoding="utf-8")
        output_path = Path(tmp) / "output"
        with output_path.open("wb") as sink:
            # nosec below: executing the job's command inside the sandbox IS
            # this worker's contract. `sh <file>` runs the command exactly as
            # `sh -c` did, without publishing it to every reader of the
            # process table; start_new_session makes the command and its
            # children a group of their own, so the reap below can never
            # reach the worker or a sibling command.
            proc = subprocess.Popen(  # nosec B603 B607
                ["sh", str(script)],
                stdout=sink,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                start_new_session=True,
            )
            try:
                exit_code = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                # Signal the group while the leader is still in it, then
                # collect the leader itself.
                _reap_group(proc.pid)
                proc.wait()
                raise
            _reap_group(proc.pid)
        # Explicit UTF-8: the sandbox's locale is not guaranteed, and a
        # check whose output carries a non-ASCII byte must still be readable
        # rather than raising or arriving mangled.
        return exit_code, output_path.read_text(encoding="utf-8", errors="replace")


def _reap_group(pgid: int) -> None:
    """SIGTERM, then SIGKILL, whatever is left in the command's process group.

    The group id is the leader's pid. ``ProcessLookupError`` is the normal
    case and the fast path: the command left nothing behind, so the group is
    already gone.
    """
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + GROUP_TERM_GRACE_S
    while time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


class JobRunner:
    def __init__(
        self,
        job: JobRequest,
        events_path: Path,
        result_path: Path,
        *,
        heartbeat_s: float = 15.0,
        backend_name: str | None = None,
        disk_warn: float = 0.0,
        disk_abort: float = 0.0,
        mem_warn: float = 0.0,
        mem_abort: float = 0.0,
    ) -> None:
        self.job = job
        self.events_path = events_path
        self.result_path = result_path
        self.heartbeat_s = heartbeat_s
        self.backend_name = backend_name
        self.disk_warn = disk_warn
        self.disk_abort = disk_abort
        self.mem_warn = mem_warn
        self.mem_abort = mem_abort
        self._resource_level = "ok"
        self._resource_abort: str | None = None

    def run(self) -> JobResult:
        """Execute the job and write the authoritative result file.

        Never raises for job-level failures — they become error results.
        """
        with EventWriter(self.events_path, self.job.run_id, self.job.job_id) as writer:
            heartbeat_stop = self._start_heartbeat(writer)
            try:
                writer.emit(EventTypes.WORKER_START, kind=self.job.kind)
                # Baseline resource sample: even a job that finishes inside
                # one heartbeat gets a datapoint, and a sandbox already past
                # a threshold is flagged before work starts.
                if self.heartbeat_s > 0:
                    self._sample_and_emit(writer)
                result = self._dispatch(writer)
            except subprocess.TimeoutExpired:
                result = self._error_result("timeout", "Timeout", "job timed out")
            except BaseException as exc:
                result = self._error_result(
                    "error",
                    type(exc).__name__,
                    str(exc) or repr(exc),
                    detail="".join(traceback.format_exception(exc))[-OUTPUT_TAIL_CHARS:],
                    http_status=getattr(exc, "http_status", None),
                )
            finally:
                heartbeat_stop.set()

            if self._resource_abort and result.status != "ok":
                # The sandbox blew past disk_abort/mem_abort while this job
                # ran: name the real cause instead of whatever confusing
                # failure the in-VM tooling produced on a full disk or under
                # the OOM killer.
                original = ""
                if result.error is not None:
                    original = f"underlying failure: {result.error.type}: {result.error.message}"
                    if result.error.detail:
                        original += f"\n{result.error.detail}"
                result = self._error_result(
                    "error",
                    "SandboxResourcesExhausted",
                    self._resource_abort,
                    detail=original[-OUTPUT_TAIL_CHARS:] or None,
                )

            self.result_path.parent.mkdir(parents=True, exist_ok=True)
            self.result_path.write_text(result.model_dump_json())
            if result.status == "ok":
                writer.emit(EventTypes.WORKER_RESULT, status=result.status)
            else:
                assert result.error is not None
                writer.emit(
                    EventTypes.WORKER_ERROR,
                    status=result.status,
                    error_type=result.error.type,
                    message=result.error.message,
                )
            writer.emit(EventTypes.WORKER_END)
            return result

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self, writer: EventWriter) -> JobResult:
        if self.job.kind == "agent.rate_limits":
            from sbxloop_worker.rate_limits import failure

            backend = self.job.params["backend"]
            try:
                report = get_backend(backend).rate_limits(timeout_s=self.job.timeout_s)
            except Exception as exc:
                report = failure(backend, exc)
            return JobResult(
                job_id=self.job.job_id,
                status="ok",
                output_json=report.bounded().model_dump(mode="json"),
            )
        if self.job.kind == "agent.session":
            return self._run_agent_session(writer)
        if self.job.kind == "shell.check":
            return self._run_shell_check()
        if self.job.kind == "shell.batch":
            return self._run_shell_batch()
        if self.job.kind == "git.merge":
            from sbxloop_worker.gitops import merge_from_base

            assert self.job.cwd is not None
            bundle = (
                Path(self.job.params["bundle_path"]) if self.job.params.get("bundle_path") else None
            )
            try:
                merged = merge_from_base(
                    Path(self.job.cwd),
                    self.job.params["base_branch"],
                    timeout_s=self.job.timeout_s,
                    base_sha=self.job.params["base_sha"],
                    bundle_path=bundle,
                )
            finally:
                if bundle is not None:
                    with contextlib.suppress(OSError):
                        bundle.unlink(missing_ok=True)
            return JobResult(job_id=self.job.job_id, status="ok", output_json=asdict(merged))
        if self.job.kind == "service.mcp":
            from sbxloop_worker.mcpops import execute

            output = execute(
                self.job.params,
                self.result_path.parent / "mcp-sessions",
                timeout_s=self.job.timeout_s,
            )
            return JobResult(job_id=self.job.job_id, status="ok", output_json=output)
        if self.job.kind == "service.http":
            return self._run_service_http(writer)
        if self.job.kind == "service.fetch":
            return self._run_service_fetch(writer)
        return self._run_github_op(writer)

    def _run_agent_session(self, writer: EventWriter) -> JobResult:
        backend = get_backend(self.backend_name)
        outcome = backend.run_session(self.job, writer.emit)
        if outcome.failure is not None:
            return JobResult(
                job_id=self.job.job_id,
                status="error",
                error=ErrorInfo(
                    type="ProviderFailure",
                    message=outcome.failure.reason,
                    http_status=outcome.failure.http_status,
                    provider=outcome.failure,
                ),
                output_text=outcome.output_text,
                session_id=outcome.session_id,
                usage=outcome.usage,
                turns=outcome.turns,
                health=outcome.health,
            )
        if self.job.expect == "json" and outcome.output_json is None:
            return self._error_result(
                "error",
                "ExpectedJsonMissing",
                "agent response contained no parseable JSON",
                detail=outcome.output_text[-OUTPUT_TAIL_CHARS:],
            )
        return JobResult(
            job_id=self.job.job_id,
            status="ok",
            output_text=outcome.output_text,
            output_json=outcome.output_json,
            session_id=outcome.session_id,
            usage=outcome.usage,
            turns=outcome.turns,
            health=outcome.health,
            artifacts=outcome.artifacts,
        )

    def _run_shell_check(self) -> JobResult:
        assert self.job.argv is not None
        # nosec below: executing the job's argv inside the sandbox IS this
        # worker's contract; list argv, never shell=True.
        proc = subprocess.run(  # nosec B603
            self.job.argv,
            capture_output=True,
            text=True,
            cwd=self.job.cwd,
            timeout=self.job.timeout_s,
            check=False,
        )
        output = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
        # A nonzero inner exit code is still a successful *job*: the engine
        # inspects exit_code to decide whether verification passed.
        return JobResult(
            job_id=self.job.job_id,
            status="ok",
            exit_code=proc.returncode,
            output_text=output[-OUTPUT_TAIL_CHARS:],
        )

    def _run_shell_batch(self) -> JobResult:
        assert self.job.commands is not None
        deadline = time.monotonic() + self.job.timeout_s
        per_command = self.job.command_timeout_s or self.job.timeout_s
        results: list[BatchCommandResult] = []
        for command in self.job.commands:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, self.job.timeout_s)
            exit_code, output = run_isolated_command(
                command,
                cwd=self.job.cwd,
                timeout_s=min(per_command, remaining),
            )
            results.append(
                BatchCommandResult(
                    command=command,
                    exit_code=exit_code,
                    output=output[-OUTPUT_TAIL_CHARS:],
                )
            )
        # Job-level exit_code is the first nonzero (0 when everything
        # passed) so a result is glanceable without parsing output_json.
        return JobResult(
            job_id=self.job.job_id,
            status="ok",
            exit_code=next((r.exit_code for r in results if r.exit_code != 0), 0),
            output_json=[r.model_dump() for r in results],
        )

    def _run_github_op(self, writer: EventWriter) -> JobResult:
        from sbxloop_worker.githubops import execute_op

        assert self.job.op is not None
        writer.emit(EventTypes.GH_OP_START, op=self.job.op)

        def progress(**data: object) -> None:
            writer.emit(EventTypes.GH_OP_PROGRESS, op=self.job.op, **data)

        output = execute_op(self.job.op, self.job.params, progress=progress)
        writer.emit(EventTypes.GH_OP_END, op=self.job.op)
        return JobResult(job_id=self.job.job_id, status="ok", output_json=output)

    def _run_service_http(self, writer: EventWriter) -> JobResult:
        from sbxloop_worker.serviceops import execute_http

        params = self.job.params
        summary = {
            "credential": params.get("credential"),
            "method": str(params.get("method", "")).upper(),
            "path": params.get("path"),
        }
        writer.emit(EventTypes.SERVICE_HTTP_START, **summary)
        output = execute_http(params)
        writer.emit(EventTypes.SERVICE_HTTP_END, status=output["status"], **summary)
        return JobResult(job_id=self.job.job_id, status="ok", output_json=output)

    def _run_service_fetch(self, writer: EventWriter) -> JobResult:
        """Read registry bytes with fixed operations; never evaluate the project."""
        from sbxloop_worker.registryops import execute_fetch

        summary = {
            "registry": self.job.params.get("registry"),
            "operation": self.job.params.get("operation", "download"),
            "path": self.job.params.get("path"),
        }
        writer.emit(EventTypes.SERVICE_FETCH_START, **summary)
        started = time.monotonic()
        output = execute_fetch(
            self.job.params,
            self.result_path.with_suffix(".artifact"),
            timeout_s=self.job.timeout_s,
        )
        writer.emit(
            EventTypes.SERVICE_FETCH_END,
            bytes=output["bytes"],
            sha256=output["sha256"],
            duration_s=round(time.monotonic() - started, 2),
            **summary,
        )
        return JobResult(
            job_id=self.job.job_id,
            status="ok",
            output_json=output,
        )

    # -- helpers -----------------------------------------------------------

    def _error_result(
        self,
        status: str,
        type_: str,
        message: str,
        detail: str | None = None,
        http_status: int | None = None,
    ) -> JobResult:
        return JobResult.model_validate(
            {
                "job_id": self.job.job_id,
                "status": status,
                "error": ErrorInfo(
                    type=type_, message=message, detail=detail, http_status=http_status
                ).model_dump(),
            }
        )

    def _start_heartbeat(self, writer: EventWriter) -> threading.Event:
        stop = threading.Event()
        if self.heartbeat_s <= 0:
            stop.set()
            return stop

        def beat() -> None:
            while not stop.wait(self.heartbeat_s):
                try:
                    writer.emit(EventTypes.WORKER_HEARTBEAT)
                    self._sample_and_emit(writer)
                except Exception:  # pragma: no cover - writer closed during shutdown
                    return

        thread = threading.Thread(target=beat, name="sbxloop-heartbeat", daemon=True)
        thread.start()
        return stop

    def _sample_and_emit(self, writer: EventWriter) -> None:
        """Emit one ``sandbox.resources`` sample; escalations additionally
        emit a prominent warning event (edge-triggered, so a long run at 90%
        disk produces one warning, not one per beat)."""
        sample = sample_resources()
        if not sample:
            return
        level = classify_level(
            sample,
            disk_warn=self.disk_warn,
            disk_abort=self.disk_abort,
            mem_warn=self.mem_warn,
            mem_abort=self.mem_abort,
        )
        writer.emit(EventTypes.SANDBOX_RESOURCES, level=level, **sample)
        if LEVEL_SEVERITY[level] > LEVEL_SEVERITY[self._resource_level]:
            writer.emit(
                EventTypes.SANDBOX_RESOURCES_WARNING,
                level=level,
                message=self._level_message(level, sample),
                **sample,
            )
        if level == "abort":
            self._latch_abort(sample)
        self._resource_level = level

    def _latch_abort(self, sample: dict[str, object]) -> None:
        """Remember the abort diagnosis that rewrites a failed result.

        The first abort sample latches; a later sample only replaces it when
        disk has since crossed its threshold and the latched diagnosis was
        memory. Disk wins because it is the non-transient resource — a run
        that first spiked memory and then filled the filesystem failed for
        the disk, and reporting "memory exhausted" would send the operator
        chasing the wrong cause."""
        message = self._level_message("abort", sample)
        if self._resource_abort is None or (
            self._disk_tripped(sample) and not self._resource_abort.startswith("sandbox disk")
        ):
            self._resource_abort = message

    def _disk_tripped(self, sample: dict[str, object]) -> bool:
        disk = sample.get("disk_used_pct")
        return isinstance(disk, (int, float)) and self.disk_abort > 0 and disk >= self.disk_abort

    def _level_message(self, level: str, sample: dict[str, object]) -> str:
        disk = sample.get("disk_used_pct")
        mem = sample.get("mem_used_pct")
        if level == "abort":
            # Disk wins when both tripped: it is the non-transient one.
            if self._disk_tripped(sample):
                return (
                    f"sandbox disk exhausted: {disk}% of the workspace filesystem is used "
                    f"(disk_abort threshold: {self.disk_abort}%)"
                )
            return (
                f"sandbox memory exhausted: {mem}% of memory is used "
                f"(mem_abort threshold: {self.mem_abort}%)"
            )
        parts = []
        if isinstance(disk, (int, float)) and self.disk_warn > 0 and disk >= self.disk_warn:
            parts.append(f"disk {disk}% used (disk_warn: {self.disk_warn}%)")
        if isinstance(mem, (int, float)) and self.mem_warn > 0 and mem >= self.mem_warn:
            parts.append(f"memory {mem}% used (mem_warn: {self.mem_warn}%)")
        return "sandbox resources under pressure: " + ", ".join(parts)
