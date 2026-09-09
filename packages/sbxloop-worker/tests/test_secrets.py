"""Sentinel/credential classification — both sbx placeholder shapes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from sbxloop_worker.secrets import (
    is_sbx_sentinel,
    looks_like_github_token,
    redact_secrets,
    shell_sentinel_case,
    shell_token_case,
)

MIMIC = "gho_sbxproxymanagedAbc123"
CUSTOM_SENTINEL = "sbx-cs-Abc123"


class TestPredicates:
    def test_both_sentinel_shapes(self) -> None:
        assert is_sbx_sentinel(CUSTOM_SENTINEL)
        assert is_sbx_sentinel(MIMIC)
        assert not is_sbx_sentinel("gho_realusertoken123")
        assert not is_sbx_sentinel("ghs_installation123")

    def test_mimic_is_not_a_credential(self) -> None:
        assert looks_like_github_token("ghs_installation123")
        assert looks_like_github_token("github_pat_abc")
        assert not looks_like_github_token(MIMIC)
        assert not looks_like_github_token(CUSTOM_SENTINEL)


class TestShellCases:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (MIMIC, "sentinel"),
            (CUSTOM_SENTINEL, "sentinel"),
            ("ghs_installation123", "token"),
            ("github_pat_abc", "token"),
            ("sk-proj-example_inference_key", "token"),
            ("sk-svcacct-example_inference_key", "token"),
            ("sk-example_inference_key", "token"),
            ("sk-sbxproxymanagedAbc123", "sentinel"),
            ("not-a-token", "other"),
        ],
    )
    def test_sentinel_case_wins_over_token_case(self, value: str, expected: str) -> None:
        """Probes interpolate sentinel-case BEFORE token-case; the mimic
        matches both, so ordering is what keeps it classified correctly."""
        script = (
            f'case "$1" in {shell_sentinel_case()}) echo sentinel ;; '
            f"{shell_token_case()}) echo token ;; *) echo other ;; esac"
        )
        out = subprocess.run(
            ["sh", "-c", script, "_", value], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == expected


@pytest.mark.parametrize("prefix", ["sk-", "sk-proj-", "sk-svcacct-"])
def test_openai_keys_are_redacted_without_a_label(prefix: str) -> None:
    token = prefix + "A1b2C3d4E5f6G7h8I9j0K1l2"
    assert redact_secrets(f"response included {token} here") == "response included *** here"


class TestApplyEnvFile:
    def test_env_file_beats_both_sentinel_shapes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The db 2026-08-31 field failure: a template-stamped mimic must
        lose to the token the provisioner wrote."""
        from sbxloop_worker.__main__ import apply_env_file

        env_file = tmp_path / "env.sh"
        env_file.write_text("export GH_TOKEN=ghs_fresh_installation\nexport OTHER=from_file\n")
        monkeypatch.setenv("GH_TOKEN", MIMIC)
        monkeypatch.setenv("OTHER", "already_set")
        apply_env_file(env_file)
        assert os.environ["GH_TOKEN"] == "ghs_fresh_installation"
        assert os.environ["OTHER"] == "already_set"  # real values still win
