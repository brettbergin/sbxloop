"""Unit tests for command display formatting."""

from __future__ import annotations

import pytest

from sbxloop.cli.cmdfmt import COMMAND_DISPLAY_CLIP, collapse_run_prefix, format_command

RUN_ROOT = "/home/bergs/.local/state/sbxloop/sbxloop-work/runs/rfxm7ad23/workspace"
REAL_CALL = f"cd {RUN_ROOT} && git diff -- README.md docs/architecture.md CHANGELOG.md | head -120"


def test_real_call_keeps_verb_and_fits() -> None:
    out = format_command(REAL_CALL)
    assert out.startswith("cd $RUN && git diff")
    assert len(out) <= COMMAND_DISPLAY_CLIP
    assert RUN_ROOT not in out


def test_collapse_run_prefix_variants() -> None:
    assert collapse_run_prefix(f"cd {RUN_ROOT} && ls").startswith("cd $RUN && ls")
    assert collapse_run_prefix(f"cd {RUN_ROOT}; ls") == "cd $RUN && ls"
    assert collapse_run_prefix(f'cd "{RUN_ROOT}" && ls') == "cd $RUN && ls"
    assert collapse_run_prefix("cd /srv/x/y && ls", run_root="/srv/x") == "cd $RUN && ls"


def test_collapse_run_prefix_leaves_other_prefixes_alone() -> None:
    assert collapse_run_prefix("cd /tmp && ls") == "cd /tmp && ls"
    assert collapse_run_prefix("git status") == "git status"
    assert collapse_run_prefix("") == ""


LONG_CASES = [
    REAL_CALL,
    f"cd {RUN_ROOT} && uv run ruff check . 2>&1 | tail -2 && uv run mypy 2>&1 | tail -2",
    f"cd {RUN_ROOT} && grep -rn 'daemon_log' "
    + " ".join(f"docs/really-long-name-{i}.md" for i in range(20)),
    "python " + "-".join(["averyverylongflagvalue"] * 12),
    "grep " + "x" * 400,
]


@pytest.mark.parametrize("cmd", LONG_CASES)
def test_verb_preserved(cmd: str) -> None:
    out = format_command(cmd)
    assert len(out) <= COMMAND_DISPLAY_CLIP
    flat = " ".join(cmd.split())
    verb = collapse_run_prefix(flat).split(" ")
    verb_tok = verb[3] if verb[:3] == ["cd", "$RUN", "&&"] else verb[0]
    assert verb_tok in out


@pytest.mark.parametrize("cmd", LONG_CASES)
def test_no_silent_mid_token_split(cmd: str) -> None:
    flat = collapse_run_prefix(" ".join(cmd.split()))
    original = set(flat.split(" "))
    for tok in format_command(cmd).split(" "):
        assert tok in original or "…" in tok


@pytest.mark.parametrize("cmd", [*LONG_CASES, "ls -la", "", "  git   status  "])
def test_idempotent(cmd: str) -> None:
    once = format_command(cmd)
    assert format_command(once) == once


def test_short_commands_pass_through() -> None:
    assert format_command("ls -la") == "ls -la"


def test_whitespace_collapsing() -> None:
    assert format_command("git\n  status\t-s") == "git status -s"


def test_tiny_limit_still_marks_elision() -> None:
    out = format_command(REAL_CALL, limit=12)
    assert len(out) <= 12
    assert "…" in out
