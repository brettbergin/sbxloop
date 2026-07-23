"""A handle to one live sandbox: exec, file transfer, lifecycle."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

from sdxloop.sbx.cli import SbxCLI
from sdxloop.sbx.models import ExecResult

# Canonical in-sandbox layout. The sandbox user is `agent` in the official
# Docker sandbox templates.
SANDBOX_HOME = "/home/agent"
SDXLOOP_DIR = f"{SANDBOX_HOME}/.sdxloop"
JOBS_DIR = f"{SDXLOOP_DIR}/jobs"
RESULTS_DIR = f"{SDXLOOP_DIR}/results"
EVENTS_DIR = f"{SDXLOOP_DIR}/events"
ENV_FILE = f"{SDXLOOP_DIR}/env.sh"
VENV_DIR = f"{SDXLOOP_DIR}/venv"
VENV_PYTHON = f"{VENV_DIR}/bin/python"


class Sandbox:
    """Operations on one existing sandbox. Does not own creation."""

    def __init__(self, cli: SbxCLI, name: str) -> None:
        self.cli = cli
        self.name = name

    def __repr__(self) -> str:
        return f"Sandbox({self.name!r})"

    def exec(self, cmd: Sequence[str], *, timeout: float | None = None) -> ExecResult:
        return self.cli.exec(self.name, cmd, timeout=timeout)

    def exec_stream(self, cmd: Sequence[str]) -> subprocess.Popen[str]:
        """Start a streaming exec; caller owns the returned process."""
        return self.cli.popen("exec", self.name, *cmd)

    def cp_in(self, host_path: Path, sb_path: str) -> None:
        self.cli.cp(str(host_path), f"{self.name}:{sb_path}")

    def cp_out(self, sb_path: str, host_path: Path) -> None:
        self.cli.cp(f"{self.name}:{sb_path}", str(host_path))

    def write_text(self, sb_path: str, text: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".sdxloop", delete=False) as f:
            f.write(text)
            tmp = Path(f.name)
        try:
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
