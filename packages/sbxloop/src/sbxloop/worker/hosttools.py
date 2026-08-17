"""Host-tool round trip, host side.

The worker relays an in-sandbox agent's host-tool calls as
``agent.tool_request`` events (see ``sbxloop_worker.hosttools``); the
:class:`HostToolBroker` answers them. It runs the caller's handler on its
own small thread pool — never on the thread draining the worker's event
stream, which must keep publishing and watching the deadline, and because
the SDK issues tool calls concurrently — and drops the
:class:`HostToolResponse` into the sandbox at
``<job.host_tools_dir>/<call_id>.json`` via ``Sandbox.write_text`` (a
``sbx cp``, the same primitive that stages job files).

Failure handling is "the model always gets an answer": a handler exception
becomes an ``ok=False`` response carrying the error text; a request that
cannot even be parsed is answered the same way when it has a call id
(otherwise it is logged and the worker's own timeout takes over). Writing
into a sandbox that has meanwhile died is logged, not raised.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from sbxloop.errors import SbxError
from sbxloop.log import get_logger
from sbxloop.sbx.sandbox import Sandbox
from sbxloop_worker.protocol import Event, HostToolCall, HostToolResponse, JobRequest

log = get_logger(__name__)

HostToolHandler = Callable[[HostToolCall], HostToolResponse]

# A response is what the model reads back; anything past this is noise
# that only makes the cp slower.
MAX_RESPONSE_CHARS = 200_000


class HostToolBroker:
    """Answers one job's host-tool requests through the sandbox filesystem."""

    def __init__(
        self,
        sandbox: Sandbox,
        job: JobRequest,
        handler: HostToolHandler,
        *,
        max_workers: int = 4,
    ) -> None:
        if not job.host_tools_dir:
            raise ValueError("HostToolBroker needs a job with host_tools_dir")
        self.sandbox = sandbox
        self.job = job
        self.handler = handler
        self.tools_dir = job.host_tools_dir
        self._max_workers = max_workers
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._futures: list[Future[None]] = []
        self.calls = 0

    def dispatch(self, event: Event) -> None:
        """Take one ``agent.tool_request`` event and answer it asynchronously."""
        if event.job_id != self.job.job_id:
            return
        # The client stamps agent.* events with the persona (and may grow
        # other annotations); only the call's own fields are the call.
        payload = {k: v for k, v in event.data.items() if k in HostToolCall.model_fields}
        try:
            call = HostToolCall.model_validate(payload)
        except ValueError as exc:
            call_id = event.data.get("call_id")
            log.warning(
                "hosttool.malformed_request",
                job=self.job.job_id,
                sandbox=self.sandbox.name,
                error=str(exc)[:200],
            )
            if isinstance(call_id, str) and call_id:
                self._write(
                    HostToolResponse(call_id=call_id, ok=False, error="malformed tool request")
                )
            return
        with self._lock:
            self.calls += 1
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=self._max_workers, thread_name_prefix="sbxloop-hosttool"
                )
            self._futures.append(self._pool.submit(self._serve, call))

    def _serve(self, call: HostToolCall) -> None:
        try:
            response = self.handler(call)
        except Exception as exc:
            log.warning(
                "hosttool.handler_failed",
                job=self.job.job_id,
                tool=call.name,
                call_id=call.call_id,
                error=f"{type(exc).__name__}: {exc}"[:500],
                exc_info=True,
            )
            response = HostToolResponse(
                call_id=call.call_id, ok=False, error=f"{type(exc).__name__}: {exc}"
            )
        if response.call_id != call.call_id:
            response = response.model_copy(update={"call_id": call.call_id})
        if len(response.text) > MAX_RESPONSE_CHARS:
            response = response.model_copy(
                update={"text": response.text[:MAX_RESPONSE_CHARS] + "\n… (truncated)"}
            )
        self._write(response)

    def _write(self, response: HostToolResponse) -> None:
        path = f"{self.tools_dir}/{response.call_id}.json"
        try:
            self.sandbox.write_text(path, response.model_dump_json())
        except SbxError as exc:
            # The job may have been killed (timeout) and the sandbox torn
            # down; the worker side times out on its own.
            log.warning(
                "hosttool.response_write_failed",
                job=self.job.job_id,
                sandbox=self.sandbox.name,
                call_id=response.call_id,
                error=str(exc)[:200],
            )

    def close(self) -> None:
        """Stop taking work; queued calls are cancelled, in-flight ones finish
        on their own thread. The job's tools directory is removed best-effort."""
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        with contextlib.suppress(SbxError):
            self.sandbox.exec(["rm", "-rf", self.tools_dir])
