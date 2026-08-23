"""Unit tests for the worker's free-text secret redaction."""

from __future__ import annotations

from sbxloop_worker.secrets import REDACTED, redact_secrets


class TestRedactSecrets:
    def test_ordinary_text_untouched(self) -> None:
        text = "ruff check . -- 12 files checked, no issues found"
        assert redact_secrets(text) == text

    def test_empty_is_total(self) -> None:
        assert redact_secrets("") == ""

    def test_github_pat_masked(self) -> None:
        token = "ghp_" + "a1b2c3d4" * 4 + "efgh"
        out = redact_secrets(f"remote: using {token} for auth")
        assert token not in out
        assert REDACTED in out

    def test_fine_grained_pat_masked(self) -> None:
        token = "github_pat_" + "X" * 30
        assert token not in redact_secrets(token)

    def test_sentinel_masked(self) -> None:
        assert "sbx-cs-abcd1234" not in redact_secrets("TOK=sbx-cs-abcd1234")

    def test_aws_key_masked(self) -> None:
        assert "AKIAIOSFODNN7EXAMPLE" not in redact_secrets("id AKIAIOSFODNN7EXAMPLE here")

    def test_bearer_header_masked(self) -> None:
        out = redact_secrets("Authorization: Bearer abcdef0123456789")
        assert "abcdef0123456789" not in out
        assert "Bearer" in out

    def test_env_assignment_masked(self) -> None:
        out = redact_secrets("API_KEY=supersecretvalue")
        assert "supersecretvalue" not in out
        assert out.startswith("API_KEY=")

    def test_json_mapping_masked(self) -> None:
        out = redact_secrets('{"password": "hunter2", "user": "bob"}')
        assert "hunter2" not in out
        assert "bob" in out

    def test_non_secret_assignment_kept(self) -> None:
        assert redact_secrets("PATH=/usr/bin") == "PATH=/usr/bin"
