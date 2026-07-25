"""SandboxPair — the core sbxloop primitive — and its cleanup guarantees.

A run's pair consists of the agent sandbox (COPILOT_GITHUB_TOKEN only) and,
when the GitHub integration is configured (``[github].repo``), the
github-ops sandbox (GH_TOKEN only) — otherwise ``pair.github`` is None and
no GitHub capability exists anywhere in the run. The pair is a context
manager whose
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
from collections.abc import Callable
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
    """Process-wide safety net: cleans registered pairs on exit and signals."""

    def __init__(self) -> None:
        self._pairs: list[SandboxPair] = []
        self._lock = threading.Lock()
        self._installed = False
        self._atexit_registered = False
        self._previous: dict[int, Any] = {}
        self._quiesce: Callable[[], None] | None = None

    def register(self, pair: SandboxPair) -> None:
        with self._lock:
            if pair not in self._pairs:
                self._pairs.append(pair)
            self._install_handlers()

    def install_handlers(self) -> None:
        """Install the atexit hook and signal handlers now.

        Signal handlers can only be installed from the main thread; call
        this from it before handing the engine to a background thread
        (the TUI does), or registration there would silently skip them —
        and SIGTERM's default disposition kills the process without
        running atexit, leaking the sandboxes.
        """
        with self._lock:
            self._install_handlers()

    def set_quiesce(self, fn: Callable[[], None] | None) -> None:
        """Callback run before signal-triggered cleanup (None clears it).

        Lets a driver stop the work that is still using the sandboxes —
        the TUI signals its engine thread and joins it briefly — so
        teardown does not race an engine mid-``sbx exec``. Best-effort:
        a raising callback never blocks cleanup.
        """
        self._quiesce = fn

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
        if not self._atexit_registered:
            self._atexit_registered = True
            atexit.register(self.cleanup_all)
        if threading.current_thread() is not threading.main_thread():
            # Signal handlers can only be installed from the main thread.
            # Do NOT latch _installed here: a later register (or an explicit
            # install_handlers) from the main thread must still install them.
            return
        self._installed = True
        for signum in (signal.SIGINT, signal.SIGTERM):
            # ValueError/OSError: not installable in this context (e.g. no tty)
            with contextlib.suppress(ValueError, OSError):
                self._previous[signum] = signal.signal(signum, self._handle_signal)

    def _handle_signal(self, signum: int, frame: types.FrameType | None) -> None:
        logger.info("signal %s received; cleaning up sandboxes", signum)
        quiesce = self._quiesce
        if quiesce is not None:
            try:
                quiesce()
            except Exception:
                logger.warning("quiesce before signal cleanup failed", exc_info=True)
        self.cleanup_all()
        previous = self._previous.get(signum)
        if callable(previous):
            previous(signum, frame)
        elif signum == signal.SIGINT:
            raise KeyboardInterrupt
        else:
            raise SystemExit(128 + signum)


cleanup_registry = CleanupRegistry()
