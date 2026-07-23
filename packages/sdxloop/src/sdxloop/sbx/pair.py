"""SandboxPair — the core sdxloop primitive — and its cleanup guarantees.

A run's pair consists of the agent sandbox (COPILOT_GITHUB_TOKEN only) and
the github-ops sandbox (GH_TOKEN only). The pair is a context manager whose
exit stops and removes both sandboxes unless ``keep`` is set; a process-wide
registry additionally cleans up on interpreter exit and on SIGINT/SIGTERM,
so aborted runs do not leak microVMs.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import signal
import threading
import types
from typing import Any

from sdxloop.sbx.sandbox import Sandbox

logger = logging.getLogger(__name__)


class SandboxPair:
    def __init__(
        self,
        run_id: str,
        agent: Sandbox,
        github: Sandbox,
        *,
        keep: bool = False,
    ) -> None:
        self.run_id = run_id
        self.agent = agent
        self.github = github
        self.keep = keep
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
        """Stop and remove both sandboxes. Idempotent, best-effort per sandbox."""
        if self._cleaned:
            return
        self._cleaned = True
        cleanup_registry.unregister(self)
        for sandbox in (self.agent, self.github):
            try:
                sandbox.stop()
            except Exception:
                logger.warning("failed to stop sandbox %s", sandbox.name, exc_info=True)
            try:
                sandbox.rm()
            except Exception:
                logger.warning("failed to remove sandbox %s", sandbox.name, exc_info=True)


class CleanupRegistry:
    """Process-wide safety net: cleans registered pairs on exit and signals."""

    def __init__(self) -> None:
        self._pairs: list[SandboxPair] = []
        self._lock = threading.Lock()
        self._installed = False
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
            try:
                pair.cleanup()
            except Exception:
                logger.warning("cleanup failed for run %s", pair.run_id, exc_info=True)

    def _install_handlers(self) -> None:
        if self._installed:
            return
        self._installed = True
        atexit.register(self.cleanup_all)
        if threading.current_thread() is not threading.main_thread():
            return  # signal handlers can only be installed from the main thread
        for signum in (signal.SIGINT, signal.SIGTERM):
            # ValueError/OSError: not installable in this context (e.g. no tty)
            with contextlib.suppress(ValueError, OSError):
                self._previous[signum] = signal.signal(signum, self._handle_signal)

    def _handle_signal(self, signum: int, frame: types.FrameType | None) -> None:
        logger.info("signal %s received; cleaning up sandboxes", signum)
        self.cleanup_all()
        previous = self._previous.get(signum)
        if callable(previous):
            previous(signum, frame)
        elif signum == signal.SIGINT:
            raise KeyboardInterrupt
        else:
            raise SystemExit(128 + signum)


cleanup_registry = CleanupRegistry()
