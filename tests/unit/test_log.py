"""The structlog pipeline: level, third-party quieting, redaction, renderers."""

from __future__ import annotations

import io
import json
import logging

import pytest

from sbxloop.log import (
    REDACTED,
    THIRD_PARTY_LOGGERS,
    bind_run,
    clear_run,
    configure_logging,
    get_logger,
    redact_secrets,
)


@pytest.fixture
def restore_logging() -> None:
    """Every test here reconfigures the root logger; put it back on DEBUG
    (the session default from conftest) so later tests see what they expect."""
    yield
    configure_logging("DEBUG")
    clear_run()


class TestConfigure:
    def test_level_applies_to_root(self, restore_logging: None) -> None:
        configure_logging("WARNING")
        assert logging.getLogger().level == logging.WARNING
        configure_logging("debug")
        assert logging.getLogger().level == logging.DEBUG

    def test_unknown_level_rejected(self, restore_logging: None) -> None:
        with pytest.raises(ValueError, match="unknown log level"):
            configure_logging("LOUD")

    def test_third_party_quiet_at_info_noisy_at_debug(self, restore_logging: None) -> None:
        configure_logging("INFO")
        for name in THIRD_PARTY_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING
        configure_logging("DEBUG")
        for name in THIRD_PARTY_LOGGERS:
            # Their DEBUG firehose is never wanted; INFO is the ceiling.
            assert logging.getLogger(name).level == logging.INFO

    def test_reconfigure_replaces_only_its_own_handler(self, restore_logging: None) -> None:
        root = logging.getLogger()
        foreign = logging.NullHandler()
        root.addHandler(foreign)
        try:
            configure_logging("INFO")
            configure_logging("INFO")
            ours = [h for h in root.handlers if getattr(h, "_sbxloop_log_handler", False)]
            assert len(ours) == 1
            assert foreign in root.handlers
        finally:
            root.removeHandler(foreign)

    def test_console_renders_event_and_fields(self, restore_logging: None) -> None:
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        get_logger("sbxloop.test").info("run.dispatch", item="gh:12", run="r1")
        line = stream.getvalue()
        assert "run.dispatch" in line
        assert "item=gh:12" in line
        assert "run=r1" in line
        assert "[sbxloop.test]" in line

    def test_none_valued_fields_are_dropped(self, restore_logging: None) -> None:
        """Field: `worker.job_done … error=None exit_code=None cwd=None` on
        every line — an absent optional fact must not render at all."""
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        get_logger("sbxloop.test").info(
            "worker.job_done", job="j1", error=None, exit_code=None, status="ok"
        )
        line = stream.getvalue()
        assert "job=j1" in line and "status=ok" in line
        assert "None" not in line
        # JSON output drops them too (absence is the record)
        stream2 = io.StringIO()
        configure_logging("INFO", fmt="json", stream=stream2)
        get_logger("sbxloop.test").info("x", cwd=None, keep=0)
        record = json.loads(stream2.getvalue().strip().splitlines()[-1])
        assert "cwd" not in record and record["keep"] == 0

    def test_json_renders_one_object_per_line(self, restore_logging: None) -> None:
        stream = io.StringIO()
        configure_logging("INFO", fmt="json", stream=stream)
        get_logger("sbxloop.test").info("run.dispatch", item="gh:12")
        get_logger("sbxloop.test").warning("run.failed", reason="boom")
        lines = stream.getvalue().strip().splitlines()
        assert len(lines) == 2
        first, second = (json.loads(line) for line in lines)
        assert first["event"] == "run.dispatch"
        assert first["item"] == "gh:12"
        assert first["level"] == "info"
        assert second["level"] == "warning"
        assert "timestamp" in first

    def test_stdlib_records_render_through_the_same_pipeline(self, restore_logging: None) -> None:
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        logging.getLogger("some.library").warning("legacy %s message", "positional")
        assert "legacy positional message" in stream.getvalue()
        assert "[some.library]" in stream.getvalue()

    def test_positional_args_still_format(self, restore_logging: None) -> None:
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        get_logger("sbxloop.test").info("old %s style", "percent")
        assert "old percent style" in stream.getvalue()

    def test_exc_info_renders_traceback(self, restore_logging: None) -> None:
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        try:
            raise RuntimeError("kaboom")
        except RuntimeError:
            get_logger("sbxloop.test").warning("thing.failed", exc_info=True)
        assert "RuntimeError: kaboom" in stream.getvalue()


class TestRedaction:
    def test_processor_masks_credential_keys(self) -> None:
        event = {
            "event": "x",
            "github_token": "ghp_secret",
            "api_key": "k",
            "authorization": "Bearer y",
            "tokens": 12,  # a count, not a credential
            "item": "gh:1",
        }
        out = redact_secrets(None, "info", event)
        assert out["github_token"] == REDACTED
        assert out["api_key"] == REDACTED
        assert out["authorization"] == REDACTED
        assert out["tokens"] == 12
        assert out["item"] == "gh:1"

    def test_masks_end_to_end(self, restore_logging: None) -> None:
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        get_logger("sbxloop.test").info("secret.set", copilot_token="ghu_abc123")
        assert "ghu_abc123" not in stream.getvalue()
        assert f"copilot_token={REDACTED}" in stream.getvalue()


class TestContext:
    def test_bind_run_stamps_every_record_until_cleared(self, restore_logging: None) -> None:
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        log = get_logger("sbxloop.test")
        bind_run("r1", "gh:12", source="github")
        log.info("first")
        clear_run()
        log.info("second")
        first, second = stream.getvalue().strip().splitlines()
        assert "run=r1" in first and "item=gh:12" in first and "source=github" in first
        assert "run=r1" not in second

    def test_bind_run_does_not_cross_threads(self, restore_logging: None) -> None:
        import threading

        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        bind_run("r-main")

        def other() -> None:
            get_logger("sbxloop.test").info("from.thread")

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        assert "run=r-main" not in stream.getvalue()
