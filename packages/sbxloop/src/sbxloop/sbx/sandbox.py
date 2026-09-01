"""A handle to one live sandbox: exec, file transfer, lifecycle."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

from sbxloop.sbx.cli import SbxCLI
from sbxloop.sbx.models import ExecResult

# Canonical in-sandbox layout. The sandbox user is `agent` in the official
# Docker sandbox templates.
SANDBOX_HOME = "/home/agent"
# Fallback in-VM working directory when the host-workspace mount cannot be
# discovered (harvest mode copies it out with `sbx cp` instead).
WORK_DIR = f"{SANDBOX_HOME}/work"
SBXLOOP_DIR = f"{SANDBOX_HOME}/.sbxloop"
JOBS_DIR = f"{SBXLOOP_DIR}/jobs"
RESULTS_DIR = f"{SBXLOOP_DIR}/results"
EVENTS_DIR = f"{SBXLOOP_DIR}/events"
# Host-tool responses land under here, one directory per job
# (``<TOOLS_DIR>/<job_id>/<call_id>.json``; see sbxloop.worker.hosttools).
TOOLS_DIR = f"{SBXLOOP_DIR}/tools"
ENV_FILE = f"{SBXLOOP_DIR}/env.sh"
# Per-job env delivery (#592): the host pipes `export KEY=VALUE` lines into a
# job launch's stdin; the launch shell captures them into this variable and
# the inner login shell evals it AFTER its profile ran — so the delivered
# values beat anything the profile stamped (a stale sbx sentinel included)
# and the credential is never at rest on the sandbox filesystem.
JOB_ENV_VAR = "SBXLOOP_JOB_ENV"
VENV_DIR = f"{SBXLOOP_DIR}/venv"
VENV_PYTHON = f"{VENV_DIR}/bin/python"
# Written by `sbxloop bake` into the template; provisioning reads it to
# decide whether the worker install ladder can be skipped.
BAKE_MANIFEST = f"{SBXLOOP_DIR}/bake.json"


class Sandbox:
    """Operations on one existing sandbox. Does not own creation."""

    def __init__(self, cli: SbxCLI, name: str) -> None:
        self.cli = cli
        self.name = name

    def __repr__(self) -> str:
        return f"Sandbox({self.name!r})"

    def exec(
        self, cmd: Sequence[str], *, timeout: float | None = None, stdin: str | None = None
    ) -> ExecResult:
        return self.cli.exec(self.name, cmd, timeout=timeout, stdin=stdin)

    def exec_stream(self, cmd: Sequence[str], *, stdin_pipe: bool = False) -> subprocess.Popen[str]:
        """Start a streaming exec; caller owns the returned process (and its
        stdin pipe, when ``stdin_pipe`` requests one: write, then close)."""
        return self.cli.popen("exec", self.name, *cmd, stdin_pipe=stdin_pipe)

    def cp_in(self, host_path: Path, sb_path: str) -> None:
        self.cli.cp(str(host_path), f"{self.name}:{sb_path}")

    def cp_out(self, sb_path: str, host_path: Path) -> None:
        self.cli.cp(f"{self.name}:{sb_path}", str(host_path))

    def write_text(self, sb_path: str, text: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".sbxloop", delete=False) as f:
            f.write(text)
            tmp = Path(f.name)
        try:
            # NamedTemporaryFile creates 0600 files and sbx cp preserves the
            # mode into the VM, where the in-sandbox `agent` user (not the
            # owner) must read them — job files were arriving unreadable
            # (Errno 13). Everything staged in must be world-readable.
            tmp.chmod(0o644)
            self.cp_in(tmp, sb_path)
        finally:
            tmp.unlink(missing_ok=True)

    def read_text(self, sb_path: str) -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "out"
            self.cp_out(sb_path, target)
            return target.read_text()

    def mkdirs(self, *sb_paths: str) -> None:
        self.exec(["mkdir", "-p", *sb_paths])

    def stop(self) -> None:
        self.cli.stop(self.name)

    def rm(self) -> None:
        self.cli.rm(self.name, force=True)
