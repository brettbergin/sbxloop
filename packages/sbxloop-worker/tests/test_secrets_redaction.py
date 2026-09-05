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

    def test_credential_word_must_be_a_whole_name_segment(self) -> None:
        # A pytest summary or usage counter must not be masked just because
        # a credential word appears inside an ordinary word (PR #420 review).
        for text in (
            "tokens: 5",
            "input_tokens: 1234",
            "2131 passed, 1 skipped",
            "compat=1",
            "patch: 3",
            "secrets_found=0",
        ):
            assert redact_secrets(text) == text

    def test_delimited_credential_segments_still_masked(self) -> None:
        for text, literal in (
            ("GITHUB_TOKEN=ghx-abc123", "ghx-abc123"),
            ("aws.credentials: /root/.aws", "/root/.aws"),
            ('{"api_key": "k3y"}', "k3y"),
            ("MY_PASSWORD=hunter2", "hunter2"),
        ):
            out = redact_secrets(text)
            assert literal not in out
            assert REDACTED in out


class TestRedactionIsLinear:
    def test_long_alphanumeric_run_is_not_quadratic(self) -> None:
        # ``_MAPPING`` had no left anchor, so a 50 KB alphanumeric line made
        # the engine retry the greedy name group from every offset: 33 s for
        # this input, an hour for a minified bundle (#749). The anchor keeps
        # it linear; the bound is generous so a loaded CI runner cannot flake.
        import time

        text = "BEGIN" + "x" * 50_000 + "END"
        start = time.perf_counter()
        out = redact_secrets(text)
        assert time.perf_counter() - start < 0.5
        assert out == text

    def test_anchor_keeps_the_mapping_contract(self) -> None:
        # The anchor must not cost any of the shapes ``_MAPPING`` exists for:
        # quoted JSON keys, bare YAML keys, dotted names, and a leading indent.
        for text, literal in (
            ('{"api_key": "k3y"}', "k3y"),
            ("token: abc", "abc"),
            ("  my.secret: v", "v"),
            ('"aws.credentials": "/root/.aws"', "/root/.aws"),
        ):
            out = redact_secrets(text)
            assert literal not in out
            assert REDACTED in out
