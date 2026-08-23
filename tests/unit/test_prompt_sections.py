"""The SDK ``system_message`` kwarg the Copilot backend builds for a job.

Per-phase system-message trimming was removed (see CHANGELOG, Unreleased):
the static prefix it targeted is the most-cached region of the context. What
survives is the append-only contract the concierge relies on.
"""

from __future__ import annotations

from sbxloop_worker.backends.copilot import system_message_config


class TestSystemMessageConfig:
    def test_no_content_leaves_the_sdk_prompt_alone(self) -> None:
        assert system_message_config(None) is None
        assert system_message_config("") is None

    def test_content_appends(self) -> None:
        assert system_message_config("extra") == {"mode": "append", "content": "extra"}
