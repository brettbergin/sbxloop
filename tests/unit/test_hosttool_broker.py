"""HostToolBroker unit tests against a recording sandbox stand-in."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from sbxloop.errors import SbxError
from sbxloop.worker.hosttools import MAX_RESPONSE_CHARS, HostToolBroker
from sbxloop_worker.protocol import (
    Event,
    EventTypes,
    HostToolCall,
    HostToolResponse,
    HostToolSpec,
    JobRequest,
)


class RecordingSandbox:
    name = "boxa"

    def __init__(self, *, fail_writes: bool = False) -> None:
        self.written: dict[str, str] = {}
        self.execs: list[list[str]] = []
        self.fail_writes = fail_writes
        self._lock = threading.Lock()

    def write_text(self, sb_path: str, text: str) -> None:
        if self.fail_writes:
            raise SbxError("sandbox is gone")
        with self._lock:
            self.written[sb_path] = text

    def exec(self, cmd: list[str], *, timeout: float | None = None) -> Any:
        self.execs.append(list(cmd))


def job() -> JobRequest:
    return JobRequest(
        job_id="j1",
        run_id="r1",
        kind="agent.session",
        prompt="hi",
        host_tools=[HostToolSpec(name="echo_back", description="x")],
        host_tools_dir="/home/agent/.sbxloop/tools/j1",
    )


def request(call_id: str = "c1", job_id: str = "j1", **data: Any) -> Event:
    payload = {"call_id": call_id, "name": "echo_back", "arguments": {"x": 1}, **data}
    return Event.now(EventTypes.AGENT_TOOL_REQUEST, "r1", job_id, **payload)


def wait_for(pred: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def parse(sandbox: RecordingSandbox, call_id: str) -> HostToolResponse:
    return HostToolResponse.model_validate_json(
        sandbox.written[f"/home/agent/.sbxloop/tools/j1/{call_id}.json"]
    )


class TestBroker:
    def test_dispatch_writes_response_file(self) -> None:
        sandbox = RecordingSandbox()

        def handler(call: HostToolCall) -> HostToolResponse:
            return HostToolResponse(call_id="ignored", ok=True, text=json.dumps(call.arguments))

        broker = HostToolBroker(sandbox, job(), handler)  # type: ignore[arg-type]
        broker.dispatch(request())
        wait_for(lambda: sandbox.written)
        response = parse(sandbox, "c1")
        assert response.ok and response.text == '{"x": 1}'
        assert response.call_id == "c1"  # the broker pins the call id
        assert broker.calls == 1

    def test_persona_stamp_on_the_event_is_not_part_of_the_call(self) -> None:
        """WorkerClient stamps agent.* events with ``agent=<persona>``; the
        broker must still parse the call (HostToolCall is extra=forbid)."""
        sandbox = RecordingSandbox()
        broker = HostToolBroker(
            sandbox,
            job(),
            lambda c: HostToolResponse(call_id=c.call_id, ok=True, text=c.name),  # type: ignore[arg-type]
        )
        broker.dispatch(request(agent="concierge"))
        wait_for(lambda: sandbox.written)
        assert parse(sandbox, "c1").ok

    def test_other_jobs_requests_are_ignored(self) -> None:
        sandbox = RecordingSandbox()
        broker = HostToolBroker(
            sandbox, job(), lambda c: HostToolResponse(call_id=c.call_id, ok=True)
        )  # type: ignore[arg-type]
        broker.dispatch(request(job_id="other"))
        assert broker.calls == 0 and not sandbox.written

    def test_handler_exception_writes_error_response(self) -> None:
        sandbox = RecordingSandbox()

        def handler(call: HostToolCall) -> HostToolResponse:
            raise KeyError("no such run")

        broker = HostToolBroker(sandbox, job(), handler)  # type: ignore[arg-type]
        broker.dispatch(request())
        wait_for(lambda: sandbox.written)
        response = parse(sandbox, "c1")
        assert not response.ok and response.error == "KeyError: 'no such run'"

    def test_malformed_request_writes_error_when_call_id_known(self) -> None:
        sandbox = RecordingSandbox()
        broker = HostToolBroker(
            sandbox, job(), lambda c: HostToolResponse(call_id=c.call_id, ok=True)
        )  # type: ignore[arg-type]
        bad = Event.now(EventTypes.AGENT_TOOL_REQUEST, "r1", "j1", call_id="c9", arguments="?")
        broker.dispatch(bad)
        response = parse(sandbox, "c9")
        assert not response.ok and response.error == "malformed tool request"
        # No call id at all: nothing to answer, nothing written.
        broker.dispatch(Event.now(EventTypes.AGENT_TOOL_REQUEST, "r1", "j1", name="x"))
        assert len(sandbox.written) == 1

    def test_oversize_text_is_truncated(self) -> None:
        sandbox = RecordingSandbox()
        big = "y" * (MAX_RESPONSE_CHARS + 10)
        broker = HostToolBroker(
            sandbox,
            job(),
            lambda c: HostToolResponse(call_id=c.call_id, ok=True, text=big),  # type: ignore[arg-type]
        )
        broker.dispatch(request())
        wait_for(lambda: sandbox.written)
        assert parse(sandbox, "c1").text.endswith("(truncated)")

    def test_write_failure_is_logged_not_raised(self) -> None:
        sandbox = RecordingSandbox(fail_writes=True)
        done = threading.Event()

        def handler(call: HostToolCall) -> HostToolResponse:
            done.set()
            return HostToolResponse(call_id=call.call_id, ok=True)

        broker = HostToolBroker(sandbox, job(), handler)  # type: ignore[arg-type]
        broker.dispatch(request())
        assert done.wait(5)
        broker.close()  # in-flight write fails silently; close still cleans up
        assert ["rm", "-rf", "/home/agent/.sbxloop/tools/j1"] in sandbox.execs

    def test_close_cancels_pending_and_removes_tools_dir(self) -> None:
        sandbox = RecordingSandbox()
        release = threading.Event()

        def handler(call: HostToolCall) -> HostToolResponse:
            release.wait(5)
            return HostToolResponse(call_id=call.call_id, ok=True)

        broker = HostToolBroker(sandbox, job(), handler, max_workers=1)  # type: ignore[arg-type]
        broker.dispatch(request("c1"))
        broker.dispatch(request("c2"))  # queued behind c1 on the single worker
        broker.close()
        release.set()
        wait_for(lambda: "/home/agent/.sbxloop/tools/j1/c1.json" in sandbox.written)
        time.sleep(0.05)
        assert "/home/agent/.sbxloop/tools/j1/c2.json" not in sandbox.written
        assert sandbox.execs[-1] == ["rm", "-rf", "/home/agent/.sbxloop/tools/j1"]

    def test_requires_tools_dir(self) -> None:
        plain = JobRequest(job_id="j1", run_id="r1", kind="agent.session", prompt="hi")
        with pytest.raises(ValueError, match="host_tools_dir"):
            HostToolBroker(
                RecordingSandbox(), plain, lambda c: HostToolResponse(call_id=c.call_id, ok=True)
            )  # type: ignore[arg-type]
