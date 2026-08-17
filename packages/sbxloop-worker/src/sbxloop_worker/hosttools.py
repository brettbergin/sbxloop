"""Host-tool round trip, worker side.

An agent session may be given tools the HOST implements (``JobRequest.host_tools``):
daemon control, run inspection, work enqueueing — things that live in the
host's state store and process, not in the sandbox. The transport is the
same file-and-event contract as everything else in the protocol:

1. The worker emits ``agent.tool_request`` (data = :class:`HostToolCall`) on
   its event stream, which the host is already tailing.
2. The host runs the tool and copies a :class:`HostToolResponse` JSON file to
   ``<job.host_tools_dir>/<call_id>.json`` inside the sandbox (``sbx cp``).
3. :func:`request_tool` polls for that file and returns the parsed response.

``sbx cp`` is not atomic and the sandbox offers no rename primitive, so a
file that exists but does not yet validate is treated as "still being
written" and polled again — a truncated JSON document never validates.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from sbxloop_worker.backends import EmitFn
from sbxloop_worker.protocol import EventTypes, HostToolCall, HostToolResponse

POLL_INTERVAL_S = 0.25
# call ids become file names; anything outside this alphabet is replaced.
_SAFE_CALL_ID = re.compile(r"[A-Za-z0-9_\-.:]{1,128}")


class HostToolTimeout(TimeoutError):
    """The host did not answer a tool call within ``host_tool_timeout_s``."""


def safe_call_id(call_id: str | None) -> str:
    if call_id and _SAFE_CALL_ID.fullmatch(call_id):
        return call_id
    return uuid.uuid4().hex


def response_path(tools_dir: str | Path, call_id: str) -> Path:
    return Path(tools_dir) / f"{call_id}.json"


def request_tool(
    emit: EmitFn,
    tools_dir: str | Path,
    call: HostToolCall,
    timeout_s: float,
    *,
    poll_s: float = POLL_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> HostToolResponse:
    """Emit the request event and wait for the host's response file.

    Raises :class:`HostToolTimeout` (after emitting a failed
    ``agent.tool_response``) when the deadline passes.
    """
    path = response_path(tools_dir, call.call_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = clock()
    emit(EventTypes.AGENT_TOOL_REQUEST, **call.model_dump(mode="json"))
    deadline = started + timeout_s
    while True:
        response = _read_response(path)
        if response is not None:
            emit(
                EventTypes.AGENT_TOOL_RESPONSE,
                call_id=call.call_id,
                name=call.name,
                ok=response.ok,
                elapsed_s=round(clock() - started, 3),
                error=response.error,
            )
            return response
        if clock() >= deadline:
            emit(
                EventTypes.AGENT_TOOL_RESPONSE,
                call_id=call.call_id,
                name=call.name,
                ok=False,
                elapsed_s=round(clock() - started, 3),
                error="timeout",
            )
            raise HostToolTimeout(f"host tool {call.name!r} was not answered within {timeout_s:g}s")
        sleep(poll_s)


def _read_response(path: Path) -> HostToolResponse | None:
    try:
        raw = path.read_text()
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        return HostToolResponse.model_validate_json(raw)
    except ValueError:
        # Partial write in flight (sbx cp is not atomic); poll again.
        return None
