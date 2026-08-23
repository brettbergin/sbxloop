"""The per-run event → log mirror (``sbxloop.run``)."""

from __future__ import annotations

import logging

import pytest

from sbxloop.cli.tui import format_event
from sbxloop.daemon.logsink import RUN_LOGGER_NAME, event_log_subscriber, level_for
from sbxloop.events import Event, EventBus, HostEventTypes, summarize_event
from sbxloop_worker.protocol import EventTypes


class TestLevelFor:
    @pytest.mark.parametrize(
        ("event_type", "level"),
        [
            (HostEventTypes.RUN_START, logging.INFO),
            (HostEventTypes.RUN_END, logging.INFO),
            ("run.anything_new", logging.INFO),  # the whole run.* family
            (HostEventTypes.TASK_STATE, logging.INFO),
            (HostEventTypes.PHASE_END, logging.INFO),
            (HostEventTypes.PHASE_PLAN, logging.INFO),
            (HostEventTypes.PHASE_VERDICT, logging.INFO),
            (HostEventTypes.SANDBOX_READY, logging.INFO),
            (HostEventTypes.CHAT_MESSAGE, logging.INFO),
            (HostEventTypes.POLICY_DENY, logging.INFO),
            (EventTypes.GH_OP_START, logging.INFO),
            (EventTypes.WORKER_ERROR, logging.WARNING),
            (EventTypes.SANDBOX_TOOLING_WARNING, logging.WARNING),
            (EventTypes.AGENT_PERMISSION_DENIED, logging.WARNING),
            (HostEventTypes.RUN_CONFIG_DRIFT, logging.WARNING),
            (EventTypes.AGENT_TOOL_START, logging.DEBUG),
            (EventTypes.AGENT_MESSAGE_DELTA, logging.DEBUG),
            (EventTypes.WORKER_HEARTBEAT, logging.DEBUG),
            (EventTypes.SANDBOX_RESOURCES, logging.DEBUG),
            (HostEventTypes.POLICY_ALLOW, logging.DEBUG),
            (EventTypes.GH_OP_PROGRESS, logging.DEBUG),
        ],
    )
    def test_tiers(self, event_type: str, level: int) -> None:
        assert level_for(event_type) == level


class TestSubscriber:
    def test_mirrors_bus_events_at_tiered_levels(self, caplog: pytest.LogCaptureFixture) -> None:
        bus = EventBus()
        bus.subscribe(event_log_subscriber)
        with caplog.at_level(logging.DEBUG, logger=RUN_LOGGER_NAME):
            bus.emit(HostEventTypes.RUN_START, "r1", outcome="ship it")
            bus.emit(EventTypes.AGENT_TOOL_START, "r1", job_id="j1", tool="bash", args="ls")
            bus.emit(EventTypes.WORKER_ERROR, "r1", job_id="j1", error="boom")
        records = [r for r in caplog.records if r.name == RUN_LOGGER_NAME]
        assert [r.levelno for r in records] == [logging.INFO, logging.DEBUG, logging.WARNING]
        texts = [r.getMessage() for r in records]
        assert "run.start" in texts[0] and "r1" in texts[0] and "ship it" in texts[0]
        assert "agent.tool_start" in texts[1] and "bash" in texts[1] and "j1" in texts[1]
        assert "worker.error" in texts[2] and "boom" in texts[2]

    def test_summary_fields_match_format_event(self) -> None:
        event = Event.now(
            EventTypes.AGENT_TOOL_END,
            "r1",
            job_id="j1",
            task_id="t1",
            agent="executor",
            tool="bash",
            args="pytest -q",
            error="exit 1",
        )
        fields = summarize_event(event)
        assert fields["task"] == "t1"
        assert fields["agent"] == "executor"
        assert fields["summary_key"] == "tool"
        assert fields["summary"] == "bash"
        assert fields["args"] == "pytest -q"
        assert fields["error"] == "exit 1"
        line = format_event(event)
        for value in ("agent.tool_end", "[t1]", "[executor]", "bash", "pytest -q", "exit 1"):
            assert value in line

    def test_resource_sample_summary(self) -> None:
        event = Event.now(
            EventTypes.SANDBOX_RESOURCES,
            "r1",
            disk_used_pct=91,
            mem_used_pct=40,
            load1=1.5,
            level="warn",
        )
        fields = summarize_event(event)
        assert fields == {"disk": "91%", "mem": "40%", "load": 1.5, "resource_level": "warn"}
        assert "disk=91% mem=40% load=1.5 warn" in format_event(event)

    def test_long_text_is_clipped(self) -> None:
        event = Event.now(EventTypes.AGENT_MESSAGE, "r1", content="x" * 500 + "\nnext line")
        summary = summarize_event(event)["summary"]
        assert len(summary) == 160
        assert "\n" not in summary


OLD_TOOL_END = {
    "tool": "bash",
    "args": "cd /home/x/.local/state/sbxloop/sbxloop-work/runs/rfxm7ad23/workspace && uv run mypy",
    "success": False,
    "exit_code": 1,
    "error": "error: bad type",
}
NEW_TOOL_END = {**OLD_TOOL_END, "tool_call_id": "c1", "output_lines": 120, "duration_ms": 1500}


class TestToolEventSchemaCompatibility:
    """`output_lines`/`duration_ms`/`tool_call_id` are additive: a stored
    stream from an older worker still summarises, renders and replays, and a
    stream with them renders the richer form (#403 t7)."""

    @pytest.mark.parametrize("data", [OLD_TOOL_END, NEW_TOOL_END], ids=["old", "new"])
    def test_summary_and_render_tolerate_both_shapes(self, data: dict[str, object]) -> None:
        event = Event.now(EventTypes.AGENT_TOOL_END, "r1", job_id="j1", **data)
        fields = summarize_event(event)
        # The run path collapses; the verb survives; the log line renders.
        assert fields["args"] == "cd $RUN && uv run mypy"
        assert fields["summary"] == "bash"
        line = format_event(event)
        assert "agent.tool_end" in line and "cd $RUN && uv run mypy" in line

    def test_stored_payload_keeps_the_full_command(self) -> None:
        event = Event.now(EventTypes.AGENT_TOOL_END, "r1", **NEW_TOOL_END)
        # Rendering is display-only: the event's own data is untouched.
        assert event.data["args"] == NEW_TOOL_END["args"]
        assert event.data["output_lines"] == 120
        assert event.data["duration_ms"] == 1500

    def test_bus_mirrors_both_shapes_without_error(self, caplog: pytest.LogCaptureFixture) -> None:
        bus = EventBus()
        bus.subscribe(event_log_subscriber)
        with caplog.at_level(logging.DEBUG, logger=RUN_LOGGER_NAME):
            bus.emit(EventTypes.AGENT_TOOL_START, "r1", tool="bash", args="ls")
            bus.emit(EventTypes.AGENT_TOOL_END, "r1", **OLD_TOOL_END)
            bus.emit(EventTypes.AGENT_TOOL_START, "r1", tool="bash", args="ls", tool_call_id="c1")
            bus.emit(EventTypes.AGENT_TOOL_END, "r1", **NEW_TOOL_END)
        records = [r for r in caplog.records if r.name == RUN_LOGGER_NAME]
        assert len(records) == 4
        assert all(r.levelno == logging.DEBUG for r in records)
