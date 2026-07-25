"""WorkerClient: install the worker into a sandbox and run jobs through it.

Two transports:

- **stream** (default): one blocking ``sbx exec`` per job; the worker mirrors
  its JSONL events to stdout, which the host parses line-by-line and
  republishes on the EventBus. The result file is fetched afterwards with
  ``cp`` — stdout is telemetry, the result file is the outcome.
- **poll**: the worker is launched detached (``nohup ... &``); the host tails
  the in-sandbox events file by byte offset every ``poll_interval`` seconds.
  Fallback for environments where long-running exec streams are unreliable.

Host-side timeouts are ``job.timeout_s`` plus a grace period; on expiry the
worker process is killed inside the sandbox (pattern-scoped pkill) and
WorkerTimeoutError is raised.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import shlex
import threading
import time
from collections import deque
from pathlib import Path

import sbxloop
from sbxloop.config import Limits, WorkerTransport
from sbxloop.errors import SbxError, WorkerError, WorkerTimeoutError
from sbxloop.events import EventBus
from sbxloop.sbx.models import ExecResult
from sbxloop.sbx.sandbox import ENV_FILE, EVENTS_DIR, JOBS_DIR, RESULTS_DIR, VENV_DIR, Sandbox
from sbxloop.sbx.sandbox import VENV_PYTHON as DEFAULT_PYTHON
from sbxloop.worker.wheel import resolve_worker_wheel
from sbxloop_worker.protocol import Event, EventTypes, JobRequest, JobResult

# Wheels must keep their canonical filename when staged: pip validates the
# name-version-python-abi-platform structure of the FILENAME itself and
# refuses to install a renamed wheel ("Invalid wheel filename").
STAGED_WHEEL_DIR = "/tmp"

logger = logging.getLogger(__name__)


def _output_tail(result: ExecResult, limit: int = 2000) -> str:
    """Combined stderr+stdout tail: sbx exec surfaces some in-sandbox errors
    on stdout, so stderr alone can be empty exactly when it matters."""
    combined = "\n".join(part.strip() for part in (result.stderr, result.stdout) if part.strip())
    return combined[-limit:] if combined else "(no output)"


class WorkerClient:
    def __init__(
        self,
        sandbox: Sandbox,
        bus: EventBus | None = None,
        *,
        transport: WorkerTransport = "stream",
        python: str = DEFAULT_PYTHON,
        poll_interval: float = 2.0,
        grace_s: float = 60.0,
        role: str | None = None,
        limits: Limits | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.bus = bus or EventBus()
        self.transport = transport
        self.python = python
        self.poll_interval = poll_interval
        self.grace_s = grace_s
        # Sandbox role for enriching resource telemetry (the worker doesn't
        # know which sandbox it lives in), and guardrail thresholds to pass
        # through to the worker's heartbeat sampler.
        self.role = role
        self.limits = limits

    # -- install -----------------------------------------------------------

    def install(
        self,
        *,
        extras: str = "copilot",
        wheel: Path | None = None,
        timeout: float = 600.0,
        no_deps: bool = False,
        system_site_packages: bool = False,
        ensure_dev_tools: bool = False,
    ) -> None:
        """Install sbxloop-worker into the sandbox, venv-first with fallbacks.

        Sandbox templates ship python3 but often lack python3-venv
        (Debian/Ubuntu split ensurepip out). The ladder:

        1. ``python3 -m venv`` — the clean path.
        2. On a venv/ensurepip failure: ``sudo -n apt-get install
           python3-venv python3-pip`` (the template's agent user has sudo;
           apt hosts are on the balanced allowlist), then retry the venv.
        3. Still no venv: **user-site fallback** — ``python3 -m pip install
           --user`` (adding ``--break-system-packages`` when pip reports an
           externally-managed environment), and the worker runs under the
           system ``python3``. ``self.python`` is updated so submit() uses
           the right interpreter either way.

        ``no_deps``/``system_site_packages`` are test seams for hermetic
        installs; production uses full dependency resolution (PyPI is
        reachable under the balanced network policy).

        ``ensure_dev_tools`` additionally makes the sandbox dev-ready for
        the AGENT's own work (see _ensure_dev_tools) — the engine sets it
        for the agent sandbox only.
        """
        if ensure_dev_tools:
            self._ensure_dev_tools(timeout)
        wheel = wheel if wheel is not None else resolve_worker_wheel()
        if wheel is not None:
            staged = f"{STAGED_WHEEL_DIR}/{wheel.name}"
            self.sandbox.cp_in(wheel, staged)
            base_target = staged
        else:
            base_target = f"sbxloop-worker=={sbxloop.__version__}"
        target = f"{base_target}[{extras}]" if extras else base_target

        if self._create_venv(timeout, system_site_packages):
            self.python = DEFAULT_PYTHON
            pip = [f"{VENV_DIR}/bin/pip", "install", "--quiet"]
            if no_deps:
                pip.append("--no-deps")
            self._check(self.sandbox.exec([*pip, target], timeout=timeout), "worker install")
        else:
            self.python = "python3"
            self._pip_user_install(target, timeout=timeout, no_deps=no_deps)

        verify = self.sandbox.exec(
            [self.python, "-c", "import sbxloop_worker; print(sbxloop_worker.__version__)"]
        )
        self._check(verify, "worker import check")
        installed = verify.stdout.strip()
        if installed != sbxloop.__version__:
            raise WorkerError(
                f"worker version {installed!r} does not match host {sbxloop.__version__!r}"
            )

        # Entrypoint smoke check: importing the package proves nothing about
        # `python -m sbxloop_worker` actually executing under sbx exec. A run
        # against a missing job file must exit 64 (the worker's usage-error
        # code) — anything else means jobs would die with no result file,
        # so fail HERE with full output instead of at the first real job.
        smoke = self.sandbox.exec(
            [
                self.python,
                "-m",
                "sbxloop_worker",
                "run",
                "--job",
                "/tmp/sbxloop-smoke-missing.json",
                "--events",
                "/tmp/sbxloop-smoke.events.jsonl",
                "--result",
                "/tmp/sbxloop-smoke.result.json",
            ]
        )
        if smoke.returncode != 64:
            raise WorkerError(
                "worker entrypoint check failed "
                f"(rc={smoke.returncode}, expected 64): {_output_tail(smoke)}"
            )

    def _ensure_dev_tools(self, timeout: float) -> None:
        """Best-effort: make the sandbox dev-ready for the agent's own work.

        Field failure (0.4.0): templates ship a system python without
        ensurepip. The worker self-heals its OWN venv (the ladder below),
        but when that apt heal silently fails the worker still succeeds via
        the user-site fallback — leaving python3-venv missing, so the
        AGENT's `python3 -m venv` for the project it is building dies with
        "ensurepip is not available" on every revision until the budget
        exhausts. Install the venv/pip packages up front, unconditionally
        (a fast no-op when the template already has them), and WARN loudly
        on failure instead of ignoring it. Never fatal: worker installation
        has its own ladder, and the agent may not need venvs at all.
        """
        result = self.sandbox.exec(
            [
                "sh",
                "-c",
                "sudo -n apt-get update -q && "
                "sudo -n apt-get install -y -q python3-venv python3-pip",
            ],
            timeout=timeout,
        )
        if not result.ok:
            logger.warning(
                "dev-tools ensure failed (rc=%s) — the agent's own venv/pip use "
                "may fail with 'ensurepip is not available': %s",
                result.returncode,
                _output_tail(result),
            )

    def _create_venv(self, timeout: float, system_site_packages: bool) -> bool:
        venv_cmd = ["python3", "-m", "venv"]
        if system_site_packages:
            venv_cmd.append("--system-site-packages")
        venv_cmd.append(VENV_DIR)

        result = self.sandbox.exec(venv_cmd, timeout=timeout)
        if result.ok:
            return True
        output = f"{result.stdout} {result.stderr}".lower()
        if "ensurepip" in output or "venv" in output:
            # Self-heal: the official templates run Ubuntu with a sudo-capable
            # agent user, and apt hosts are on the balanced allowlist.
            self.sandbox.exec(
                [
                    "sh",
                    "-c",
                    "sudo -n apt-get update -q && "
                    "sudo -n apt-get install -y -q python3-venv python3-pip",
                ],
                timeout=timeout,
            )
            result = self.sandbox.exec(venv_cmd, timeout=timeout)
            if result.ok:
                return True
        logger.warning(
            "venv creation failed (rc=%s): %s — falling back to a user-site install "
            "with the system python3",
            result.returncode,
            _output_tail(result),
        )
        return False

    def _pip_user_install(self, target: str, *, timeout: float, no_deps: bool) -> None:
        pip = ["python3", "-m", "pip", "install", "--quiet", "--user"]
        if no_deps:
            pip.append("--no-deps")
        result = self.sandbox.exec([*pip, target], timeout=timeout)
        if not result.ok and "externally-managed" in f"{result.stdout} {result.stderr}".lower():
            # PEP 668 (Ubuntu 24.04+): system pip refuses --user without an
            # explicit opt-out.
            result = self.sandbox.exec([*pip, "--break-system-packages", target], timeout=timeout)
        self._check(result, "worker install (user-site fallback)")

    @staticmethod
    def _check(result: ExecResult, step: str) -> None:
        if not result.ok:
            raise WorkerError(f"{step} failed (rc={result.returncode}): {_output_tail(result)}")

    # -- submit ------------------------------------------------------------

    def submit(self, job: JobRequest) -> JobResult:
        job_path = f"{JOBS_DIR}/{job.job_id}.json"
        events_path = f"{EVENTS_DIR}/{job.job_id}.jsonl"
        result_path = f"{RESULTS_DIR}/{job.job_id}.json"
        self.sandbox.write_text(job_path, job.model_dump_json())

        argv = [
            self.python,
            "-m",
            "sbxloop_worker",
            "run",
            "--job",
            job_path,
            "--events",
            events_path,
            "--result",
            result_path,
            "--env-file",
            ENV_FILE,
        ]
        # cwd travels on argv (not only in the job JSON) so the worker
        # process itself chdirs there — agent SDK sessions inherit it.
        if job.cwd:
            argv += ["--cwd", job.cwd]
        if self.limits is not None:
            argv += [
                "--disk-warn",
                str(self.limits.disk_warn),
                "--disk-abort",
                str(self.limits.disk_abort),
                "--mem-warn",
                str(self.limits.mem_warn),
            ]
        # sbx injects secrets through the sandbox session/profile machinery;
        # a bare exec'd process may not see them. Run the worker under a
        # login shell so the sandbox environment is fully loaded.
        wrapped = ["sh", "-lc", shlex.join(argv)]
        deadline = time.monotonic() + job.timeout_s + self.grace_s
        if self.transport == "poll":
            self._run_poll(job, wrapped, events_path, result_path, deadline)
            diagnostics = ""
        else:
            diagnostics = self._run_stream(job, wrapped, deadline)
        return self._fetch_result(job, result_path, events_path, diagnostics)

    # -- stream transport --------------------------------------------------

    def _run_stream(self, job: JobRequest, argv: list[str], deadline: float) -> str:
        """Run the worker via a blocking exec; returns diagnostics (exit code
        + stderr tail) for the no-result failure path."""
        proc = self.sandbox.exec_stream(argv)
        lines: queue.Queue[str | None] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=50)

        def reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        def err_reader() -> None:
            # stderr must be drained: an unread PIPE deadlocks a chatty
            # worker once the 64KB buffer fills — and its content is the
            # only clue when the process dies before writing a result.
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_tail.append(line.rstrip())

        threading.Thread(target=reader, name="sbxloop-stream-reader", daemon=True).start()
        threading.Thread(target=err_reader, name="sbxloop-stderr-reader", daemon=True).start()

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill(job, proc)
                    raise WorkerTimeoutError(
                        f"job {job.job_id} exceeded {job.timeout_s}s (+{self.grace_s}s grace)"
                    )
                try:
                    line = lines.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    continue
                if line is None:
                    break
                self._handle_line(job, line)
        finally:
            with contextlib.suppress(Exception):
                proc.wait(timeout=self.grace_s)
        parts = [f"exec rc={proc.returncode}"]
        if stderr_tail:
            parts.append("stderr: " + " | ".join(stderr_tail)[-1500:])
        return "; ".join(parts)

    def _handle_line(self, job: JobRequest, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = Event.from_json_line(line)
        except ValueError:
            self.bus.publish(
                Event.now(EventTypes.WORKER_STDOUT, job.run_id, job_id=job.job_id, line=line)
            )
            return
        if self.role is not None and event.type in (
            EventTypes.SANDBOX_RESOURCES,
            EventTypes.SANDBOX_RESOURCES_WARNING,
        ):
            event.data.setdefault("role", self.role)
        self.bus.publish(event)

    # -- poll transport ----------------------------------------------------

    def _run_poll(
        self,
        job: JobRequest,
        argv: list[str],
        events_path: str,
        result_path: str,
        deadline: float,
    ) -> None:
        quoted = shlex.join(argv)
        launch = self.sandbox.exec(["sh", "-c", f"nohup {quoted} >/dev/null 2>&1 & echo $!"])
        if not launch.ok:
            raise WorkerError(f"failed to launch worker: {launch.stderr.strip()[:2000]}")
        pid = launch.stdout.strip().splitlines()[-1] if launch.stdout.strip() else ""

        offset = 0
        buffer = ""

        def drain() -> bool:
            """Read new event bytes; return True once worker.end is seen."""
            nonlocal offset, buffer
            chunk = self.sandbox.exec(
                ["sh", "-c", f"tail -c +{offset + 1} {events_path} 2>/dev/null || true"]
            )
            finished = False
            if chunk.stdout:
                offset += len(chunk.stdout.encode())
                *complete, buffer = (buffer + chunk.stdout).split("\n")
                for line in complete:
                    self._handle_line(job, line)
                    if '"worker.end"' in line:
                        finished = True
            return finished

        while True:
            if time.monotonic() > deadline:
                self._kill(job, None)
                raise WorkerTimeoutError(
                    f"job {job.job_id} exceeded {job.timeout_s}s (+{self.grace_s}s grace)"
                )
            time.sleep(self.poll_interval)
            if drain():
                break
            if pid:
                alive = self.sandbox.exec(
                    ["sh", "-c", f"kill -0 {pid} 2>/dev/null && echo alive || echo dead"]
                )
                if "dead" in alive.stdout:
                    # Worker exited between polls: drain whatever remains.
                    drain()
                    break

    # -- helpers -----------------------------------------------------------

    def _kill(self, job: JobRequest, proc: object) -> None:
        # Pattern is job-id scoped so concurrent workers are never collateral.
        with contextlib.suppress(Exception):
            self.sandbox.exec(["pkill", "-f", f"sbxloop_worker.*{job.job_id}"])
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.kill()  # type: ignore[attr-defined]

    def _events_tail(self, events_path: str, lines: int = 5) -> str:
        if not events_path:
            return ""
        with contextlib.suppress(Exception):
            result = self.sandbox.exec(
                ["sh", "-c", f"tail -n {lines} {events_path} 2>/dev/null || true"]
            )
            return result.stdout.strip().replace("\n", " | ")[-1500:]
        return ""

    def _fetch_result(
        self,
        job: JobRequest,
        result_path: str,
        events_path: str = "",
        diagnostics: str = "",
    ) -> JobResult:
        try:
            raw = self.sandbox.read_text(result_path)
        except SbxError as exc:
            detail = [f"worker for job {job.job_id} produced no result file ({result_path})"]
            if diagnostics:
                detail.append(diagnostics)
            tail = self._events_tail(events_path)
            if tail:
                detail.append(f"last events: {tail}")
            raise WorkerError("; ".join(detail)) from exc
        try:
            result = JobResult.model_validate_json(raw)
        except ValueError as exc:
            raise WorkerError(f"invalid result file for job {job.job_id}: {exc}") from exc
        if result.job_id != job.job_id:
            raise WorkerError(f"result job_id mismatch: expected {job.job_id}, got {result.job_id}")
        return result
