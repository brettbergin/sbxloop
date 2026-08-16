"""TUI unit tests: the chat input form, chat event rendering, and the
dashboard's chat lifecycle."""

from __future__ import annotations

import os

import pytest
from rich.console import Console

from sbxloop.cli.tui import ChatInput, Dashboard, format_event, render_event
from sbxloop.events import HostEventTypes
from sbxloop_worker.protocol import Event


def make_event(type: str, **data: object) -> Event:
    return Event.now(type, "r1", **data)


def render_to_text(renderable: object) -> str:
    console = Console(record=True, width=100)
    console.print(renderable)
    return console.export_text()


class TestChatInput:
    def collect(self) -> tuple[ChatInput, list[str]]:
        submitted: list[str] = []
        return ChatInput(submitted.append), submitted

    def test_enter_submits_stripped_line(self) -> None:
        chat, submitted = self.collect()
        chat.feed(b"  fix the tests  \r")
        assert submitted == ["fix the tests"]
        assert chat.buffer == ""

    def test_empty_line_not_submitted(self) -> None:
        chat, submitted = self.collect()
        chat.feed(b"\r\n   \r")
        assert submitted == []

    def test_backspace_and_ctrl_u(self) -> None:
        chat, submitted = self.collect()
        chat.feed(b"abcd\x7f\x7f")
        assert chat.buffer == "ab"
        chat.feed(b"\x15")
        assert chat.buffer == ""
        chat.feed(b"xy\x08\n")
        assert submitted == ["x"]

    def test_escape_sequences_swallowed(self) -> None:
        chat, submitted = self.collect()
        # Up arrow (CSI A), Delete (CSI 3~), then real text.
        chat.feed(b"\x1b[A\x1b[3~hello\r")
        assert submitted == ["hello"]

    def test_bare_escape_costs_at_most_the_next_char(self) -> None:
        chat, submitted = self.collect()
        # Esc alone, later typing: only the char right after Esc is treated
        # as an Alt-combo and dropped; the rest goes through.
        chat.feed(b"\x1b")
        chat.feed(b"xhello\r")
        assert submitted == ["hello"]

    def test_utf8_split_across_feeds(self) -> None:
        chat, submitted = self.collect()
        encoded = "héllo ✓".encode()
        chat.feed(encoded[:3])
        chat.feed(encoded[3:])
        chat.feed(b"\r")
        assert submitted == ["héllo ✓"]

    def test_renderable_shows_hint_then_buffer(self) -> None:
        chat, _ = self.collect()
        assert "type to chat" in render_to_text(chat.renderable())
        chat.feed(b"do the thing")
        assert "do the thing" in render_to_text(chat.renderable())

    def test_pump_drains_everything_buffered_and_reports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # More than one os.read()'s worth queued at once (a paste, or keys
        # typed while the loop was busy) must be absorbed in a single pump
        # call, and pump must say it consumed input so the caller repaints.
        read_fd, write_fd = os.pipe()
        monkeypatch.setattr("sys.stdin", os.fdopen(read_fd, "r"))
        chat, submitted = self.collect()
        os.write(write_fd, b"a" * 1500 + b"\r")
        assert chat.pump(0.5) is True
        assert submitted == ["a" * 1500]

    def test_pump_times_out_quietly_without_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        read_fd, _write_fd = os.pipe()
        monkeypatch.setattr("sys.stdin", os.fdopen(read_fd, "r"))
        chat, submitted = self.collect()
        assert chat.pump(0.01) is False
        assert submitted == []
        assert chat.buffer == ""


class TestChatRendering:
    def test_chat_message_renders_as_user_panel(self) -> None:
        rendered = render_event(
            make_event(HostEventTypes.CHAT_MESSAGE, message_id="m1", text="switch to Go")
        )
        text = render_to_text(rendered)
        assert "you" in text
        assert "switch to Go" in text

    def test_chat_reply_renders_markdown(self) -> None:
        rendered = render_event(
            make_event(HostEventTypes.CHAT_REPLY, message_id="m1", reply="**on it**")
        )
        text = render_to_text(rendered)
        assert "agent" in text
        assert "on it" in text

    def test_chat_reply_error_renders_red_panel(self) -> None:
        rendered = render_event(
            make_event(HostEventTypes.CHAT_REPLY, message_id="m1", error="worker died")
        )
        assert "steering failed: worker died" in render_to_text(rendered)

    def test_chat_action_renders_one_liner(self) -> None:
        rendered = render_event(
            make_event(
                HostEventTypes.CHAT_ACTION,
                action="steer_run",
                guidance="g",
                message="user steering: standing guidance added — g",
            )
        )
        assert "standing guidance added" in render_to_text(rendered)

    def test_verify_suspect_ruling_renders_as_warning(self) -> None:
        # A critic ruling the check wrong (#231) is a course change, not
        # dim lifecycle noise.
        rendered = render_event(
            make_event(
                HostEventTypes.PHASE_END,
                task_id="t1",
                phase="scrutinize",
                status="verify_suspect",
                honored=True,
                message="scrutinizer passed the work but ruled the verify command wrong: od",
            )
        )
        assert rendered is not None
        assert rendered.style == "yellow"
        assert "ruled the verify command wrong" in render_to_text(rendered)

    def test_format_event_carries_chat_text(self) -> None:
        line = format_event(make_event(HostEventTypes.CHAT_MESSAGE, text="hello agent"))
        assert "chat.message" in line
        assert "hello agent" in line
        line = format_event(make_event(HostEventTypes.CHAT_REPLY, reply="hi"))
        assert "hi" in line


class TestDashboardChat:
    def test_pending_then_processing_then_cleared(self) -> None:
        dashboard = Dashboard()
        dashboard.post_chat("m1", "please hurry")
        assert "please hurry" in render_to_text(dashboard.renderable())
        assert "queued" in render_to_text(dashboard.renderable())

        dashboard.on_event(
            make_event(HostEventTypes.CHAT_MESSAGE, message_id="m1", text="please hurry")
        )
        assert dashboard.chat_pending == {}
        assert dashboard.chat_processing == "please hurry"
        assert "steering" in render_to_text(dashboard.renderable())

        dashboard.on_event(make_event(HostEventTypes.CHAT_REPLY, message_id="m1", reply="ok"))
        assert dashboard.chat_processing is None
        text = render_to_text(dashboard.renderable())
        assert "steering" not in text
        assert "queued" not in text

    def test_chat_line_rendered_inside_panel(self) -> None:
        dashboard = Dashboard()
        chat = ChatInput(lambda _: None)
        chat.feed(b"typing here")
        assert "typing here" in render_to_text(dashboard.renderable(chat.renderable()))
