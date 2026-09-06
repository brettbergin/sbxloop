"""The worker job runner: dispatch one JobRequest, emit events, write the result."""

from __future__ import annotations

import contextlib
import os
import subprocess
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
        if self.job.kind == "service.http":
            return self._run_service_http(writer)
        if self.job.kind == "service.fetch":
            return self._run_service_fetch(writer)
        return self._run_github_op(writer)

    def _run_agent_session(self, writer: EventWriter) -> JobResult:
        backend = get_backend(self.backend_name)
        outcome = backend.run_session(self.job, writer.emit)
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
            # nosec below: executing the job's commands inside the sandbox IS
            # this worker's contract, same as shell.check's argv.
            proc = subprocess.run(  # nosec B603 B607
                ["sh", "-c", command],
                capture_output=True,
                text=True,
                cwd=self.job.cwd,
                timeout=min(per_command, remaining),
                check=False,
            )
            output = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
            results.append(
                BatchCommandResult(
                    command=command,
                    exit_code=proc.returncode,
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
        """One dependency fetch in the service sandbox (#766): the argv the
        host composed, in the workspace, with the sandbox's own environment
        (the registry credential and the cache location are in it). Exit
        code and output tail come back like shell.check's; the host decides
        what a non-zero exit means."""
        assert self.job.argv is not None
        summary = {
            "ecosystem": self.job.params.get("ecosystem"),
            "verb": self.job.params.get("verb"),
            "argv": list(self.job.argv),
        }
        writer.emit(EventTypes.SERVICE_FETCH_START, **summary)
        started = time.monotonic()
        # nosec below: running the host-authored fetch argv inside the
        # sandbox IS this job kind's contract; list argv, never shell=True.
        proc = subprocess.run(  # nosec B603
            self.job.argv,
            capture_output=True,
            text=True,
            cwd=self.job.cwd,
            timeout=self.job.timeout_s,
            check=False,
        )
        output = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
        # The output goes back to the host and into the ledger; a package
        # manager is free to echo a URL with the token in it. The host names
        # the variables holding the secrets (names, not values); their
        # values are blanked out here, where they are.
        for name in self.job.params.get("scrub_env") or ():
            value = os.environ.get(str(name), "")
            if len(value) >= 8:
                output = output.replace(value, "***")
        writer.emit(
            EventTypes.SERVICE_FETCH_END,
            exit_code=proc.returncode,
            duration_s=round(time.monotonic() - started, 2),
            **summary,
        )
        return JobResult(
            job_id=self.job.job_id,
            status="ok",
            exit_code=proc.returncode,
            output_text=output[-OUTPUT_TAIL_CHARS:],
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
