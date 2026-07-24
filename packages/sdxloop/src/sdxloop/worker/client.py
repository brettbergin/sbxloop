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
import threading
import time
from pathlib import Path

import sdxloop
from sdxloop.config import WorkerTransport
from sdxloop.errors import SbxError, WorkerError, WorkerTimeoutError
from sdxloop.events import EventBus
from sdxloop.sbx.models import ExecResult
from sdxloop.sbx.sandbox import ENV_FILE, EVENTS_DIR, JOBS_DIR, RESULTS_DIR, VENV_DIR, Sandbox
from sdxloop.sbx.sandbox import VENV_PYTHON as DEFAULT_PYTHON
from sdxloop.worker.wheel import resolve_worker_wheel
from sdxloop_worker.protocol import Event, EventTypes, JobRequest, JobResult

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
    ) -> None:
        self.sandbox = sandbox
        self.bus = bus or EventBus()
        self.transport = transport
        self.python = python
        self.poll_interval = poll_interval
        self.grace_s = grace_s

    # -- install -----------------------------------------------------------

    def install(
        self,
        *,
        extras: str = "copilot",
        wheel: Path | None = None,
        timeout: float = 600.0,
        no_deps: bool = False,
        system_site_packages: bool = False,
    ) -> None:
        """Install sdxloop-worker into the sandbox, venv-first with fallbacks.

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
        """
        wheel = wheel if wheel is not None else resolve_worker_wheel()
        if wheel is not None:
            staged = f"{STAGED_WHEEL_DIR}/{wheel.name}"
            self.sandbox.cp_in(wheel, staged)
            base_target = staged
        else:
            base_target = f"sdxloop-worker=={sdxloop.__version__}"
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
            [self.python, "-c", "import sdxloop_worker; print(sdxloop_worker.__version__)"]
        )
        self._check(verify, "worker import check")
        installed = verify.stdout.strip()
        if installed != sdxloop.__version__:
            raise WorkerError(
                f"worker version {installed!r} does not match host {sdxloop.__version__!r}"
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
            "sdxloop_worker",
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
        deadline = time.monotonic() + job.timeout_s + self.grace_s
        if self.transport == "poll":
            self._run_poll(job, argv, events_path, result_path, deadline)
        else:
            self._run_stream(job, argv, deadline)
        return self._fetch_result(job, result_path)

    # -- stream transport --------------------------------------------------

    def _run_stream(self, job: JobRequest, argv: list[str], deadline: float) -> None:
        proc = self.sandbox.exec_stream(argv)
        lines: queue.Queue[str | None] = queue.Queue()

        def reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        thread = threading.Thread(target=reader, name="sdxloop-stream-reader", daemon=True)
        thread.start()

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
        quoted = " ".join(argv)
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
            self.sandbox.exec(["pkill", "-f", f"sdxloop_worker.*{job.job_id}"])
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.kill()  # type: ignore[attr-defined]

    def _fetch_result(self, job: JobRequest, result_path: str) -> JobResult:
        try:
            raw = self.sandbox.read_text(result_path)
        except SbxError as exc:
            raise WorkerError(
                f"worker for job {job.job_id} produced no result file ({result_path})"
            ) from exc
        try:
            result = JobResult.model_validate_json(raw)
        except ValueError as exc:
            raise WorkerError(f"invalid result file for job {job.job_id}: {exc}") from exc
        if result.job_id != job.job_id:
            raise WorkerError(f"result job_id mismatch: expected {job.job_id}, got {result.job_id}")
        return result
