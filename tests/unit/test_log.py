"""The structlog pipeline: level, third-party quieting, redaction, renderers."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from sbxloop.log import (
    LOG_BUFFER_MAXLEN,
    REDACTED,
    THIRD_PARTY_LOGGERS,
    LogRecordLine,
    _RingBufferHandler,
    bind_run,
    clear_run,
    configure_logging,
    get_logger,
    log_buffer,
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
            assert len(ours) == 2  # one stderr handler + one ring buffer handler
            assert len([h for h in ours if isinstance(h, _RingBufferHandler)]) == 1
            assert foreign in root.handlers
        finally:
            root.removeHandler(foreign)

    def test_console_renders_event_and_fields(self, restore_logging: None) -> None:
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        get_logger("sbxloop.test").info("run.dispatch", item="gh:issue:12", run="r1")
        line = stream.getvalue()
        assert "run.dispatch" in line
        assert "item=gh:issue:12" in line
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
        get_logger("sbxloop.test").info("run.dispatch", item="gh:issue:12")
        get_logger("sbxloop.test").warning("run.failed", reason="boom")
        lines = stream.getvalue().strip().splitlines()
        assert len(lines) == 2
        first, second = (json.loads(line) for line in lines)
        assert first["event"] == "run.dispatch"
        assert first["item"] == "gh:issue:12"
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
            "item": "gh:issue:1",
        }
        out = redact_secrets(None, "info", event)
        assert out["github_token"] == REDACTED
        assert out["api_key"] == REDACTED
        assert out["authorization"] == REDACTED
        assert out["tokens"] == 12
        assert out["item"] == "gh:issue:1"

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
        bind_run("r1", "gh:issue:12", source="github")
        log.info("first")
        clear_run()
        log.info("second")
        first, second = stream.getvalue().strip().splitlines()
        assert "run=r1" in first and "item=gh:issue:12" in first and "source=github" in first
        assert "run=r1" not in second

    def test_bind_run_normalizes_legacy_item_id(self, restore_logging: None) -> None:
        """#508: a legacy `gh:<n>` id read from an old checkpoint must be
        logged in the typed form — the boundary canonicalises."""
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        bind_run("r1", "gh:12")
        get_logger("sbxloop.test").info("run.dispatch")
        clear_run()
        line = stream.getvalue()
        assert "item=gh:issue:12" in line
        assert "item=gh:12 " not in line

    def test_bind_run_leaves_non_github_item_ids_alone(self, restore_logging: None) -> None:
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        bind_run("r1", "inbox:todo.md")
        get_logger("sbxloop.test").info("run.dispatch")
        clear_run()
        assert "item=inbox:todo.md" in stream.getvalue()

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


class TestLogBuffer:
    @pytest.fixture(autouse=True)
    def _clean_buffer(self) -> None:
        log_buffer().clear()
        yield
        log_buffer().clear()

    def test_records_land_in_buffer(self, restore_logging: None) -> None:
        configure_logging("DEBUG")
        get_logger("sbxloop.test").info("x.event", foo=1)
        records = log_buffer().tail(10)
        assert records
        last = records[-1]
        assert last.level == "INFO"
        assert last.logger == "sbxloop.test"
        assert "x.event" in last.line and "foo=1" in last.line
        assert "\x1b[" not in last.line

    def test_bounded(self) -> None:
        buffer = log_buffer()
        for i in range(2500):
            buffer.append(LogRecordLine("t", "INFO", "l", f"line {i}"))
        assert len(buffer) == LOG_BUFFER_MAXLEN == 2000
        lines = [r.line for r in buffer.tail(LOG_BUFFER_MAXLEN)]
        assert "line 2499" in lines
        assert "line 0" not in lines

    def test_level_filter(self) -> None:
        buffer = log_buffer()
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            buffer.append(LogRecordLine("t", level, "l", f"a {level}"))
        kept = [r.level for r in buffer.tail(level="warning")]
        assert kept == ["WARNING", "ERROR"]

    def test_unknown_level_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown log level"):
            log_buffer().tail(level="bogus")

    def test_grep_is_plain_substring(self) -> None:
        buffer = log_buffer()
        buffer.append(LogRecordLine("t", "INFO", "l", "daemon.idle a.b here"))
        assert log_buffer().tail(grep="A.B")
        assert log_buffer().tail(grep="a[.]b") == []
        assert log_buffer().tail(grep=".*") == []
        buffer.append(LogRecordLine("t", "INFO", "l", "literal .* match"))
        assert len(log_buffer().tail(grep=".*")) == 1

    def test_tail_limit_and_order(self) -> None:
        buffer = log_buffer()
        for i in range(10):
            buffer.append(LogRecordLine("t", "INFO", "l", f"line {i}"))
        got = [r.line for r in buffer.tail(3)]
        assert got == ["line 7", "line 8", "line 9"]
        assert buffer.tail(0) == []

    def test_emit_never_raises(self, restore_logging: None) -> None:
        configure_logging("DEBUG")
        root = logging.getLogger()
        ring = [h for h in root.handlers if isinstance(h, _RingBufferHandler)]
        assert len(ring) == 1

        class Boom(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                raise RuntimeError("nope")

        ring[0].setFormatter(Boom())
        get_logger("sbxloop.test").info("still.fine")
        assert log_buffer().tail(10) == []

    def test_level_filter_end_to_end(self, restore_logging: None) -> None:
        configure_logging("DEBUG")
        log_buffer().clear()
        log = get_logger("sbxloop.test")
        log.debug("x.debug")
        log.info("x.info")
        log.warning("x.warn")
        log.error("x.error")
        kept = log_buffer().tail(level="WARNING")
        assert [r.level for r in kept] == ["WARNING", "ERROR"]
        blob = "\n".join(r.line for r in kept)
        assert "x.debug" not in blob and "x.info" not in blob
        assert "x.warn" in blob and "x.error" in blob

    def test_grep_matches_metacharacters_literally(self, restore_logging: None) -> None:
        configure_logging("DEBUG")
        log_buffer().clear()
        get_logger("sbxloop.test").info("daemon.idle", reason="a.b")
        assert log_buffer().tail(grep="a.b")
        assert log_buffer().tail(grep="a[.]b") == []
        assert log_buffer().tail(grep="a.*b") == []

    def test_configure_twice_does_not_duplicate_records(self, restore_logging: None) -> None:
        configure_logging("DEBUG")
        configure_logging("DEBUG")
        log_buffer().clear()
        get_logger("sbxloop.test").info("dup.check", marker="once")
        assert len(log_buffer().tail(50, grep="dup.check")) == 1

    def test_configure_twice_installs_one_ring_handler(self, restore_logging: None) -> None:
        root = logging.getLogger()
        foreign = logging.NullHandler()
        root.addHandler(foreign)
        try:
            configure_logging("INFO")
            configure_logging("DEBUG")
            ring = [h for h in root.handlers if isinstance(h, _RingBufferHandler)]
            assert len(ring) == 1
            assert foreign in root.handlers
        finally:
            root.removeHandler(foreign)


class TestRedactText:
    """Free-form text scrubbing: credential shapes with no key name (#403 t6)."""

    PAT = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    FINE = "github_pat_11ABCDEFG0" + "abcdefghijklmnopqrstuvwxyz012345"
    JWT = "eyJhbGciOiJIUzI1NiJ9.payload.signature"

    def test_github_token_shapes_masked(self) -> None:
        from sbxloop.log import redact_text

        for token in (self.PAT, self.FINE, "gho_" + "z" * 30, "ghs_" + "q" * 24):
            out = redact_text(f"gh auth login --with-token {token} now")
            assert token not in out
            assert "***" in out

    def test_authorization_header_masked_but_named(self) -> None:
        from sbxloop.log import redact_text

        out = redact_text(f"curl -H 'Authorization: Bearer {self.JWT}' https://api")
        assert self.JWT not in out
        assert "Authorization:" in out
        assert "https://api" in out

    def test_assignments_and_flags_masked(self) -> None:
        from sbxloop.log import redact_text

        out = redact_text("env API_KEY=sk-live-abc123 PASSWORD: hunter2 --token=t0psecret")
        for literal in ("sk-live-abc123", "hunter2", "t0psecret"):
            assert literal not in out
        assert out.count("***") == 3

    def test_ordinary_text_untouched(self) -> None:
        from sbxloop.log import redact_text

        for text in (
            "uv run pytest -q tests/unit",
            "grep -rn 'daemon_log' README.md | head -40",
            "cd /home/x && git diff -- README.md docs/architecture.md",
            "",
        ):
            assert redact_text(text) == text

    def test_credential_word_must_be_a_whole_name_segment(self) -> None:
        # `pat` inside PATH/patch/compat is not a credential; masking these
        # made rendered commands unreadable (PR #420 review).
        from sbxloop.log import redact_text

        for text in (
            "PATH=/usr/local/bin:/usr/bin",
            "export PATH=$HOME/bin",
            "git apply --patch=foo",
            "ls --path /tmp",
            "patch: 3",
            "compat=1",
            "dispatch=async",
        ):
            assert redact_text(text) == text

    def test_delimited_credential_segments_still_masked(self) -> None:
        from sbxloop.log import redact_text

        for text, literal in (
            ("GITHUB_PAT=ghx-abc123", "ghx-abc123"),
            ("MY_PAT: sekrit", "sekrit"),
            ("gh --api-key k3y", "k3y"),
            ("app.token=t0k", "t0k"),
            ("--access-token=abc123", "abc123"),
        ):
            out = redact_text(text)
            assert literal not in out
            assert "***" in out

    def test_idempotent_and_never_raises(self) -> None:
        from sbxloop.log import redact_text

        once = redact_text(f"API_KEY=abc {self.PAT} Authorization: Bearer {self.JWT}")
        assert redact_text(once) == once
        assert redact_text(None) == "None"  # type: ignore[arg-type]


class TestDaemonLogFile:
    """The daemon's second copy: ``logs/daemon.log`` under the home.

    Both tests end on ``configure_logging("WARNING")`` to prove the file
    handler is gone, so both take ``restore_logging``: without it the root
    logger — and the process-wide ring buffer's handler — stay at WARNING
    for every test that runs after this file in the same worker, and a
    later test asserting on its own INFO records finds an empty buffer.
    """

    def test_records_land_in_the_file_plain_and_rotated(
        self, tmp_path: Path, restore_logging: None
    ) -> None:
        import logging

        from sbxloop.log import configure_logging, get_logger

        path = tmp_path / "logs" / "daemon.log"
        configure_logging("INFO", file=path, file_max_bytes=400, file_backups=2)
        try:
            log = get_logger("sbxloop.test.file")
            for i in range(40):
                log.info("daemon.tick", n=i, token="hunter2")
            for handler in logging.getLogger().handlers:
                handler.flush()
            text = path.read_text()
            assert "daemon.tick" in text and "\x1b[" not in text  # plain, never coloured
            assert "hunter2" not in text and "***" in text  # redacted like every sink
            rotated = sorted(p.name for p in path.parent.iterdir())
            assert "daemon.log" in rotated and "daemon.log.1" in rotated
            assert "daemon.log.3" not in rotated  # backups capped
        finally:
            configure_logging("WARNING")  # drop the file handler
        assert not any(
            getattr(h, "baseFilename", "") == str(path) for h in logging.getLogger().handlers
        )

    def test_json_format_reaches_the_file_too(self, tmp_path: Path, restore_logging: None) -> None:
        import json
        import logging

        from sbxloop.log import configure_logging, get_logger

        path = tmp_path / "daemon.log"
        configure_logging("INFO", fmt="json", file=path)
        try:
            get_logger("sbxloop.test.file").info("daemon.starting", home=str(tmp_path))
            for handler in logging.getLogger().handlers:
                handler.flush()
            (line,) = path.read_text().splitlines()
            assert json.loads(line)["event"] == "daemon.starting"
        finally:
            configure_logging("WARNING")
