"""The listener: uvicorn on a thread inside ``sbxloop daemon``.

One daemon owns execution; the API is a second way in, not a second
scheduler. The server runs on its own thread with its own event loop —
uvicorn only captures signals on the main thread, so the daemon's own
handlers stand — and every store or loop call the routes make goes
through the context's executor, never the event loop.

Uvicorn's own log records go through the daemon's logging pipeline like
every other library's; its access log is off (the request id middleware
and the operations record are the audit).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from sbxloop.api.context import ApiContext
from sbxloop.api.projector import Projector
from sbxloop.config import ApiConfig
from sbxloop.daemon.controls.steering import SteeringStore
from sbxloop.log import get_logger

log = get_logger(__name__)

GRACEFUL_SHUTDOWN_S = 5


class ApiServer:
    def __init__(self, app: Any, config: ApiConfig, *, ctx: ApiContext) -> None:
        import uvicorn

        self.config = config
        self.ctx = ctx
        proxies = list(config.trusted_proxies)
        self._uv = uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.bind,
                port=config.port,
                loop="asyncio",
                lifespan="on",
                log_config=None,
                access_log=False,
                proxy_headers=bool(proxies),
                forwarded_allow_ips=proxies or None,
                timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
            )
        )
        self._thread: threading.Thread | None = None
        self._failed: BaseException | None = None
        # The projection thread rides with the listener: it keeps the
        # public chronology current for the streams and prunes history.
        if ctx.projector is None:
            ctx.projector = Projector(
                ctx.chronology,
                ctx.hub,
                clock=ctx.clock,
                retention_s=float(config.replay_retention_s),
            )
            ctx.projector.catalog_with(self._catalog_run)
        # uvicorn's loggers are noisy at INFO; the daemon narrates the start.
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).setLevel(logging.WARNING)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="sbxloop-api", daemon=True)
        self._thread.start()
        if not self.wait_started(10.0):
            if self._failed is not None:
                raise RuntimeError(f"the API listener did not start: {self._failed}")
            raise RuntimeError("the API listener did not start within 10 s")
        # The listener starts before recovery: no run is in flight, so a
        # steering instruction still waiting from the last process will
        # never be answered — its receipt says so.
        try:
            SteeringStore(self.ctx.loop.dstore).settle_orphans(None, self.ctx.clock())
        except Exception:
            log.warning("api.steering_settle_failed", exc_info=True)
        self.ctx.projector.start()
        log.info("api.started", bind=self.config.bind, port=self.port)

    def _catalog_run(self, run_id: str) -> None:
        """Catalog a finished run's artifacts on the projector thread."""
        from sbxloop.errors import SbxloopError

        try:
            record = self.ctx.loop.store.get_run(run_id)
        except SbxloopError:
            return
        self.ctx.artifacts.catalog_run(record)

    def _run(self) -> None:
        try:
            self._uv.run()
        except BaseException as exc:  # a dead listener must be reported, not hidden
            self._failed = exc
            log.error("api.listener_failed", exc_info=True)

    def wait_started(self, timeout_s: float) -> bool:
        deadline = threading.Event()
        waited = 0.0
        while waited < timeout_s:
            if self._uv.started:
                return True
            if self._failed is not None or (self._thread and not self._thread.is_alive()):
                return False
            deadline.wait(0.05)
            waited += 0.05
        return bool(self._uv.started)

    @property
    def port(self) -> int:
        """The bound port — what a ``port = 0`` bind actually got."""
        servers = getattr(self._uv, "servers", None) or []
        for server in servers:
            for sock in getattr(server, "sockets", None) or []:
                return int(sock.getsockname()[1])
        return int(self.config.port)

    def close(self) -> None:
        self.ctx.stopping.set()
        self.ctx.hub.notify()
        if self.ctx.projector is not None:
            self.ctx.projector.stop()
        self._uv.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=GRACEFUL_SHUTDOWN_S + 5)
            if self._thread.is_alive():
                log.warning("api.close_timeout")
        self.ctx.close()
        log.debug("api.closed")
