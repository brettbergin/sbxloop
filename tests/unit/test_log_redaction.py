"""Ring-buffer redaction: rendered lines are value-scrubbed, not just key-redacted."""

from __future__ import annotations

import io
import logging

import pytest

from sbxloop.log import (
    REDACTED,
    THIRD_PARTY_LOGGERS,
    clear_run,
    configure_logging,
    get_logger,
    log_buffer,
    redact_text,
)

SECRET = "ghp_SECRET123"  # nosec B105 - fixture value, not a credential


@pytest.fixture
def restore_logging() -> None:
    yield
    configure_logging("DEBUG")
    clear_run()


class TestRingBufferRedaction:
    def test_issue_repro_leaks_nothing(self, restore_logging: None) -> None:
        """The exact repro from the issue: a third-party record with a token in
        the message, and a structlog event with a secret in an innocuously
        named field. Neither may reach the buffer intact."""
        assert "httpx" in THIRD_PARTY_LOGGERS
        configure_logging("DEBUG", fmt="console", stream=io.StringIO())
        log_buffer().clear()

        logging.getLogger("httpx").warning(f"HTTP Request failed url=https://x/?token={SECRET}")
        get_logger("sbxloop.t").info(
            "cmd.failed",
            stderr=f"could not read Password for 'https://{SECRET}@github.com'",
        )

        lines = [record.line for record in log_buffer().tail(10)]
        assert len(lines) >= 2
        blob = "\n".join(lines)
        assert SECRET not in blob
        assert REDACTED in blob

    def test_secret_in_innocuous_key_is_masked(self, restore_logging: None) -> None:
        configure_logging("DEBUG", fmt="json", stream=io.StringIO())
        log_buffer().clear()

        get_logger("sbxloop.t").warning("clone.failed", detail=f"token={SECRET}")

        blob = "\n".join(record.line for record in log_buffer().tail(5))
        assert SECRET not in blob
        assert REDACTED in blob

    def test_ordinary_lines_are_untouched(self, restore_logging: None) -> None:
        configure_logging("DEBUG", fmt="console", stream=io.StringIO())
        log_buffer().clear()

        get_logger("sbxloop.t").info("run.started", run_id="abc123", attempt=2)
        logging.getLogger("httpx").warning("HTTP Request: GET https://api/ok 200")

        blob = "\n".join(record.line for record in log_buffer().tail(5))
        assert REDACTED not in blob
        assert "run.started" in blob
        assert "abc123" in blob
        assert "GET https://api/ok 200" in blob


class TestRedactText:
    @pytest.mark.parametrize(
        "text",
        [
            f"could not read Password for 'https://{SECRET}@github.com'",
            f"url=https://x/?token={SECRET}",
            f"Authorization: Bearer {SECRET}",
            f"authorization=token {SECRET}",
            f"gh auth login --with-token {SECRET}",
            f"--api-key={SECRET}",
            f"GITHUB_TOKEN={SECRET}",
            f"my.secret: {SECRET}",
            "fine grained github_pat_11ABCDEFG0aBcDeFgHiJkL_mNoPqRsTuVwXyZ0123456789",
            "AWS key AKIAIOSFODNN7EXAMPLE in the env",
        ],
    )
    def test_credential_shapes_are_masked(self, text: str) -> None:
        out = redact_text(text)
        assert SECRET not in out
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "github_pat_11ABCDEFG0aBcDeFgHiJkL" not in out
        assert REDACTED in out

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "run finished ok in 3.2s",
            "PATH=/usr/bin:/bin",
            "git apply --patch fixes.diff",
            "compat=1 patch: 3",
            "https://github.com/brettbergin/sbxloop/pull/42",
            "cloning into /tmp/work",
        ],
    )
    def test_ordinary_text_unchanged(self, text: str) -> None:
        assert redact_text(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            f"Authorization: Bearer {SECRET}",
            f"GITHUB_TOKEN={SECRET}",
            f"https://user:{SECRET}@github.com",
            f"https://{SECRET}@github.com",
            f"plain {SECRET} token",
        ],
    )
    def test_idempotent(self, text: str) -> None:
        once = redact_text(text)
        assert redact_text(once) == once

    def test_never_raises_on_hostile_input(self) -> None:
        class Boom:
            def __str__(self) -> str:
                raise RuntimeError("nope")

        assert redact_text(Boom()) == ""  # type: ignore[arg-type]
        assert redact_text(12345) == "12345"  # type: ignore[arg-type]
