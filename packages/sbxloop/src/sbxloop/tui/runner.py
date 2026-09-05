"""Processes the console starts: ``systemctl``, ``journalctl``, the
upgrade command, a detached ``sbxloop resume``, a supervised ``sbxloop
daemon``, and an interactive shell with the terminal handed over.

Everything goes through :class:`CommandRunner` so the screens and the
actions are testable against a fake that records argv and answers with
scripted output; :class:`SubprocessRunner` is the real one."""

from __future__ import annotations

import os
import shlex
import subprocess  # nosec B404 - list argv only, never a shell
import sys
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol

#: How long a bounded command (``systemctl``, ``sbx ls`` …) may take.
DEFAULT_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class RunOutcome:
    """What one bounded command produced."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        """The output an operator reads: stdout, else stderr, stripped."""
        return (self.stdout.strip() or self.stderr.strip()) or ""

    @property
    def command(self) -> str:
        return shlex.join(self.argv)


class ChildHandle(Protocol):
    """A process the console started and may still own."""

    @property
    def pid(self) -> int: ...
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout_s: float) -> int | None: ...


class StreamHandle(Protocol):
    """A long-running command read line by line (``journalctl -f``);
    ``close`` ends it from another thread."""

    def lines(self) -> Iterator[str]: ...
    def close(self) -> None: ...


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], *, timeout_s: float = DEFAULT_TIMEOUT_S) -> RunOutcome: ...

    def spawn(
        self, argv: Sequence[str], *, cwd: Path | None = None, log_path: Path | None = None
    ) -> ChildHandle: ...

    def stream(self, argv: Sequence[str]) -> StreamHandle: ...

    def interactive(self, argv: Sequence[str]) -> int: ...


def sbxloop_argv() -> tuple[str, ...]:
    """How the console starts another ``sbxloop`` — this interpreter and
    this install, so a spawned daemon or resume runs the code the console
    runs, whatever the entry point on PATH happens to be."""
    return (sys.executable, "-c", "from sbxloop.cli.app import main; main()")


class _Child:
    def __init__(self, proc: subprocess.Popen[bytes], log: IO[Any] | None) -> None:
        self._proc = proc
        self._log = log

    @property
    def pid(self) -> int:
        return self._proc.pid

    def poll(self) -> int | None:
        code = self._proc.poll()
        if code is not None and self._log is not None:
            self._log.close()
            self._log = None
        return code

    def terminate(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()

    def wait(self, timeout_s: float) -> int | None:
        try:
            return self._proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None


class _Stream:
    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc
        self._closed = threading.Event()

    def lines(self) -> Iterator[str]:
        out = self._proc.stdout
        if out is None:
            return
        try:
            for line in out:
                if self._closed.is_set():
                    break
                yield line.rstrip("\n")
        finally:
            self.close()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class SubprocessRunner:
    """The real runner: list argv, never a shell."""

    def run(self, argv: Sequence[str], *, timeout_s: float = DEFAULT_TIMEOUT_S) -> RunOutcome:
        argv = tuple(argv)
        try:
            proc = subprocess.run(  # nosec B603 - list argv, no shell
                argv, capture_output=True, text=True, timeout=timeout_s, check=False
            )
        except FileNotFoundError:
            return RunOutcome(argv, 127, stderr=f"{argv[0]}: not found on PATH")
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode(errors="replace") if exc.stdout else ""
            return RunOutcome(argv, 124, stdout=out, stderr=f"timed out after {timeout_s:g}s")
        return RunOutcome(argv, proc.returncode, proc.stdout, proc.stderr)

    def spawn(
        self, argv: Sequence[str], *, cwd: Path | None = None, log_path: Path | None = None
    ) -> ChildHandle:
        """Start a process in its own session so it outlives the console,
        its output appended to ``log_path`` (else discarded)."""
        log: IO[Any] | None = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("ab")
        proc = subprocess.Popen(  # nosec B603 - list argv, no shell
            list(argv),
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=log if log is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        return _Child(proc, log)

    def stream(self, argv: Sequence[str]) -> StreamHandle:
        proc = subprocess.Popen(  # nosec B603 - list argv, no shell
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        return _Stream(proc)

    def interactive(self, argv: Sequence[str]) -> int:
        """Run with the terminal attached; the caller has suspended the UI."""
        try:
            return subprocess.run(list(argv), check=False).returncode  # nosec B603
        except FileNotFoundError:
            return 127


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "ChildHandle",
    "CommandRunner",
    "RunOutcome",
    "StreamHandle",
    "SubprocessRunner",
    "sbxloop_argv",
]
