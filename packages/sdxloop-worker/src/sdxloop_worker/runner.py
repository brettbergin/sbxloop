"""The worker job runner: dispatch one JobRequest, emit events, write the result."""

from __future__ import annotations

import subprocess
import threading
import traceback
from pathlib import Path

from sdxloop_worker.backends import get_backend
from sdxloop_worker.events import EventWriter
from sdxloop_worker.protocol import ErrorInfo, EventTypes, JobRequest, JobResult

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
    ) -> None:
        self.job = job
        self.events_path = events_path
        self.result_path = result_path
        self.heartbeat_s = heartbeat_s
        self.backend_name = backend_name

    def run(self) -> JobResult:
        """Execute the job and write the authoritative result file.

        Never raises for job-level failures — they become error results.
        """
        with EventWriter(self.events_path, self.job.run_id, self.job.job_id) as writer:
            heartbeat_stop = self._start_heartbeat(writer)
            try:
                writer.emit(EventTypes.WORKER_START, kind=self.job.kind)
                result = self._dispatch(writer)
            except subprocess.TimeoutExpired:
                result = self._error_result("timeout", "Timeout", "job timed out")
            except BaseException as exc:
                result = self._error_result(
                    "error",
                    type(exc).__name__,
                    str(exc) or repr(exc),
                    detail="".join(traceback.format_exception(exc))[-OUTPUT_TAIL_CHARS:],
                )
            finally:
                heartbeat_stop.set()

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
            artifacts=outcome.artifacts,
        )

    def _run_shell_check(self) -> JobResult:
        assert self.job.argv is not None
        proc = subprocess.run(
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

    def _run_github_op(self, writer: EventWriter) -> JobResult:
        from sdxloop_worker.githubops import execute_op

        assert self.job.op is not None
        writer.emit(EventTypes.GH_OP_START, op=self.job.op)
        output = execute_op(self.job.op, self.job.params)
        writer.emit(EventTypes.GH_OP_END, op=self.job.op)
        return JobResult(job_id=self.job.job_id, status="ok", output_json=output)

    # -- helpers -----------------------------------------------------------

    def _error_result(
        self,
        status: str,
        type_: str,
        message: str,
        detail: str | None = None,
    ) -> JobResult:
        return JobResult.model_validate(
            {
                "job_id": self.job.job_id,
                "status": status,
                "error": ErrorInfo(type=type_, message=message, detail=detail).model_dump(),
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
                except Exception:  # pragma: no cover - writer closed during shutdown
                    return

        thread = threading.Thread(target=beat, name="sdxloop-heartbeat", daemon=True)
        thread.start()
        return stop
