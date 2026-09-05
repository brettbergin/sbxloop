"""The repository's working agreement for agents and contributors.

`AGENTS.md` is what a coding agent reads first when it clones this repository
(Copilot reads `AGENTS.md`, Claude reads `CLAUDE.md`); the two names must be
one file, the way `sbxloop.toml.example` aliases the shipped example config.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "AGENTS.md"
CLAUDE = REPO_ROOT / "CLAUDE.md"

REQUIRED_SECTIONS = (
    "## What this is",
    "## Goals, in priority order",
    "## Principles",
    "## Non-goals",
    "## Where things live",
    "## Working here",
    "## Pull requests",
)


def test_claude_md_is_a_symlink_to_agents_md() -> None:
    assert AGENTS.is_file() and not AGENTS.is_symlink()
    assert CLAUDE.is_symlink()
    assert CLAUDE.resolve() == AGENTS.resolve()


def test_agents_md_keeps_its_sections_and_stays_short() -> None:
    text = AGENTS.read_text(encoding="utf-8")
    for heading in REQUIRED_SECTIONS:
        assert heading in text, heading
    # Read on every agent turn, so every line costs (#717 caps it).
    assert text.count("\n") <= 150


def test_agents_md_points_at_real_paths() -> None:
    text = AGENTS.read_text(encoding="utf-8")
    for rel in (
        "docs/architecture.md",
        "packages/sbxloop/src/sbxloop/",
        "packages/sbxloop-worker/src/sbxloop_worker/",
        "tests/fakes/fake_github.py",
        "tests/fixtures/ecosystems/",
        "tests/unit/test_examples.py",
        "tests/unit/test_ecosystems.py",
        "tests/unit/test_prompts.py",
        "scripts/check_self_references.py",
        "sbxloop.toml.example",
    ):
        assert rel in text, rel
        assert (REPO_ROOT / rel).exists(), rel


def test_readme_links_to_agents_md() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "[`AGENTS.md`](AGENTS.md)" in readme
