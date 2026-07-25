"""SandboxPair — the core sbxloop primitive — and its cleanup guarantees.

A run's pair consists of the agent sandbox (COPILOT_GITHUB_TOKEN only) and,
when the GitHub integration is configured (``[github].repo``), the
github-ops sandbox (GH_TOKEN only) — otherwise ``pair.github`` is None and
no GitHub capability exists anywhere in the run. The pair is a context
manager whose
exit stops and removes both sandboxes unless ``keep`` is set; a process-wide
registry additionally cleans up on interpreter exit, and turns SIGINT/SIGTERM
into the ordinary Python exceptions so cleanup happens by unwinding (context
managers, the CLI's interrupt path, the atexit hook) — aborted runs do not
leak microVMs, and no sandbox teardown ever runs inside a signal handler.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import signal
import threading
import types
from pathlib import Path
from typing import Any

from sbxloop.sbx.sandbox import WORK_DIR, Sandbox

logger = logging.getLogger(__name__)


class SandboxPair:
    def __init__(
        self,
        run_id: str,
        agent: Sandbox,
        github: Sandbox | None = None,
        *,
        keep: bool = False,
        workspace: Path | None = None,
        agent_workdir: str = WORK_DIR,
        mounted: bool = False,
    ) -> None:
        self.run_id = run_id
        self.agent = agent
        self.github = github
        self.keep = keep
        # The host directory sbx was given as the run workspace, the in-VM
        # working directory agent jobs run in, and whether the two are the
        # same filesystem (mount discovered) — if not, artifacts must be
        # harvested out with `sbx cp`.
        self.workspace = workspace
        self.agent_workdir = agent_workdir
        self.mounted = mounted
        self._cleaned = False

    def __enter__(self) -> SandboxPair:
        cleanup_registry.register(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        if self.keep:
            cleanup_registry.unregister(self)
        else:
            self.cleanup()

    def cleanup(self) -> None:
        """Stop and remove the pair's sandboxes. Idempotent, best-effort each."""
        if self._cleaned:
            return
        self._cleaned = True
        cleanup_registry.unregister(self)
        for sandbox in (self.agent, self.github):
            if sandbox is None:
                continue
            try:
                sandbox.stop()
            except Exception:
                logger.warning("failed to stop sandbox %s", sandbox.name, exc_info=True)
            try:
                sandbox.rm()
            except Exception:
                logger.warning("failed to remove sandbox %s", sandbox.name, exc_info=True)


class CleanupRegistry:
    """Process-wide safety net: cleans registered pairs on interpreter exit.

    The signal handlers only convert SIGINT/SIGTERM into their ordinary
    Python exceptions; the actual sandbox teardown happens by unwinding —
    pair context managers, the CLI's interrupt path, and the atexit hook.
    Tearing sandboxes down inside a signal handler would block Ctrl+C for
    seconds of ``sbx`` subprocess work and re-enter on a second signal.
    """

    def __init__(self) -> None:
        self._pairs: list[SandboxPair] = []
        self._lock = threading.Lock()
        self._atexit_installed = False
        self._signals_installed = False
        self._previous: dict[int, Any] = {}

    def register(self, pair: SandboxPair) -> None:
        with self._lock:
            if pair not in self._pairs:
                self._pairs.append(pair)
            self._install_handlers()

    def unregister(self, pair: SandboxPair) -> None:
        with self._lock:
            if pair in self._pairs:
                self._pairs.remove(pair)

    def cleanup_all(self) -> None:
        with self._lock:
            pairs = list(self._pairs)
        for pair in pairs:
            if pair.keep:
                # Deliberately kept pairs (--keep-sandboxes) survive aborts
                # too; their kept marker is already in the run DB.
                self.unregister(pair)
                continue
            try:
                pair.cleanup()
            except Exception:
                logger.warning("cleanup failed for run %s", pair.run_id, exc_info=True)

    def install_signal_handlers(self) -> None:
        """Install the SIGINT/SIGTERM handlers (idempotent, main thread only).

        ``register()`` installs them as a side effect, but only when it runs
        on the main thread — the CLI drives runs on a worker thread in TUI
        mode, so it calls this from the main thread before starting one.
        """
        with self._lock:
            self._install_handlers()

    def _install_handlers(self) -> None:
        if not self._atexit_installed:
            self._atexit_installed = True
            atexit.register(self.cleanup_all)
        if self._signals_installed:
            return
        if threading.current_thread() is not threading.main_thread():
            return  # signal handlers can only be installed from the main thread
        self._signals_installed = True
        for signum in (signal.SIGINT, signal.SIGTERM):
            # ValueError/OSError: not installable in this context (e.g. no tty)
            with contextlib.suppress(ValueError, OSError):
                self._previous[signum] = signal.signal(signum, self._handle_signal)

    def _handle_signal(self, signum: int, frame: types.FrameType | None) -> None:
        logger.info("signal %s received", signum)
        previous = self._previous.get(signum)
        if callable(previous):
            previous(signum, frame)
        elif signum == signal.SIGINT:
            raise KeyboardInterrupt
        else:
            raise SystemExit(128 + signum)


cleanup_registry = CleanupRegistry()
