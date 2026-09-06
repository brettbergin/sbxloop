#!/usr/bin/env python3
"""Fail on sbxloop self-references in user-facing surfaces.

The audit behind this gate found the same drift over and over: a bare `#N`
from sbxloop's own issue tracker in an error message, a personal path in a
shipped file, an sbxloop incident quoted into a prompt. Each one reads as
noise — or as a reference into the *user's* repository, which has its own
#N namespace — to anyone who is not developing sbxloop. Nothing mechanical
stopped it, so this does.

Surfaces and rules:

- bare `#N` (``#`` + digits, not part of a URL or an identifier) in
  * prompt bodies — ``engine/prompts/*.md`` below the stripped contract
    header;
  * exception messages — every string literal inside a ``raise`` in both
    packages;
  * console text — every string literal (docstrings excluded) in the CLI
    package and the conformance table doctor renders;
  * the files ``sbxloop init`` writes — both templates and the presets;
- sbxloop source paths (``packages/sbxloop``, ``src/sbxloop``) in prompt
  bodies;
- personal and host identifiers (the maintainer's login outside the
  canonical repository URL, the deploy host, its user and home) anywhere
  outside ``contrib/``, ``docs/``, ``.github/``, package metadata and tests.

Comments and docstrings are not surfaces: they keep their references.

Deliberate exceptions live in ONE reviewed file, ``scripts/self-references.allow``
(``<path> <text>`` per line, ``#`` comments); an entry that no longer
matches anything fails the gate too, so the list cannot go stale.

Exit 0 when clean; otherwise every finding as ``path:line: rule: text`` and
exit 1. Stdlib only — runs as ``make lint`` does, without the package.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "scripts" / "self-references.allow"
SRC = ROOT / "packages" / "sbxloop" / "src" / "sbxloop"
WORKER_SRC = ROOT / "packages" / "sbxloop-worker" / "src" / "sbxloop_worker"

# `#` + digits that is neither a URL fragment/path (`/#12`) nor part of a
# word (`x#1`), nor a hex colour (`#0e8a16`, which is also `#0`… + letters).
BARE_ISSUE = re.compile(r"(?<![\w/&])#(\d+)(?![\w-])")
SOURCE_PATH = re.compile(r"packages/sbxloop|src/sbxloop\b")
# The maintainer's login is fine inside the project's own URL; anywhere else
# it is a host detail leaking. `bergco` catches the deploy host's domain.
PERSONAL = re.compile(
    r"brettbergin(?!/sbxloop\b)|/home/bergs|\bbergs\b|bergco|project-mountain-dew"
)

PROMPTS = SRC / "engine" / "prompts"
TEMPLATES = [
    SRC / "data" / "sbxloop.toml.example",
    *(SRC / "data" / "presets").glob("*.toml"),
    ROOT / ".env.example",
]
CONSOLE_MODULES = [
    *(SRC / "cli").rglob("*.py"),
    *(SRC / "tui").rglob("*.py"),
    SRC / "sbx" / "conformance.py",
]

# Where personal identifiers are allowed to live: operator-facing material
# about sbxloop's own deployment, package metadata, and test fixtures.
PERSONAL_EXCLUDED_DIRS = {"contrib", "docs", ".github", "tests", ".git", "__pycache__", "_vendor"}
PERSONAL_EXCLUDED_FILES = {"pyproject.toml", "uv.lock", "CHANGELOG.md", "RELEASING.md"}
PERSONAL_SKIP_SUFFIXES = {".pyc", ".whl", ".png", ".gif", ".lock"}
# The gate has to spell the patterns it hunts for.
GATE_FILES = {Path(__file__).resolve(), ALLOWLIST}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    rule: str
    text: str

    def render(self) -> str:
        return f"{self.path.relative_to(ROOT)}:{self.line}: {self.rule}: {self.text}"


def _bare_issue_findings(path: Path, text: str, first_line: int, rule: str) -> list[Finding]:
    found: list[Finding] = []
    for offset, line in enumerate(text.splitlines()):
        for match in BARE_ISSUE.finditer(line):
            found.append(Finding(path, first_line + offset, rule, match.group(0)))
    return found


def check_prompt_bodies() -> list[Finding]:
    """Prompt bodies below the contract header; the header is stripped
    before the model sees the file and is written for humans."""
    found: list[Finding] = []
    for path in sorted(PROMPTS.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        body_start = 0
        if raw.startswith("<!--"):
            end = raw.find("-->")
            body_start = raw[: end + 3].count("\n") if end >= 0 else 0
        body = "\n".join(raw.splitlines()[body_start:])
        found += _bare_issue_findings(path, body, body_start + 1, "issue-ref in prompt")
        for offset, line in enumerate(body.splitlines()):
            for match in SOURCE_PATH.finditer(line):
                found.append(
                    Finding(path, body_start + 1 + offset, "sbxloop path in prompt", match.group(0))
                )
    return found


def check_templates() -> list[Finding]:
    """Everything `sbxloop init` writes into the user's project, comments
    included — a comment in their sbxloop.toml is theirs to read."""
    found: list[Finding] = []
    for path in TEMPLATES:
        found += _bare_issue_findings(
            path, path.read_text(encoding="utf-8"), 1, "issue-ref in init-written file"
        )
    return found


def _string_parts(node: ast.AST) -> list[tuple[int, str]]:
    """Every literal string piece under `node` with its line: constants,
    the constant parts of f-strings, and both sides of concatenations."""
    parts: list[tuple[int, str]] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            parts.append((sub.lineno, sub.value))
    return parts


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def check_exception_messages() -> list[Finding]:
    """Every string literal inside a `raise` in both packages."""
    found: list[Finding] = []
    for root in (SRC, WORKER_SRC):
        for path in sorted(root.rglob("*.py")):
            if "_vendor" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Raise) and node.exc is not None:
                    for line, text in _string_parts(node.exc):
                        found += _bare_issue_findings(path, text, line, "issue-ref in exception")
    return found


def check_console_text() -> list[Finding]:
    """Every non-docstring string literal in the CLI package and the
    conformance table: what `sbxloop` prints or `doctor` renders."""
    found: list[Finding] = []
    for path in sorted(set(CONSOLE_MODULES)):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                found += _bare_issue_findings(
                    path, node.value, node.lineno, "issue-ref in console text"
                )
    return found


def _tracked_files() -> list[Path]:
    """What the repository ships: git's index, not whatever sits in the
    working tree (a venv, build output, editor state)."""
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], check=True, capture_output=True
    ).stdout
    return [ROOT / name for name in listing.decode("utf-8").split("\0") if name]


def _personal_scope() -> list[Path]:
    paths: list[Path] = []
    for path in _tracked_files():
        rel = path.relative_to(ROOT)
        if set(rel.parts[:-1]) & PERSONAL_EXCLUDED_DIRS:
            continue
        if rel.name in PERSONAL_EXCLUDED_FILES or path.suffix in PERSONAL_SKIP_SUFFIXES:
            continue
        if path.is_symlink() or not path.is_file() or path in GATE_FILES:
            continue
        paths.append(path)
    return paths


def check_personal_identifiers() -> list[Finding]:
    found: list[Finding] = []
    for path in _personal_scope():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for offset, line in enumerate(text.splitlines()):
            for match in PERSONAL.finditer(line):
                found.append(Finding(path, offset + 1, "personal identifier", match.group(0)))
    return found


def load_allowlist() -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    if not ALLOWLIST.exists():
        return entries
    for raw in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path, _, text = line.partition(" ")
        entries.append((path, text.strip()))
    return entries


def run() -> tuple[list[Finding], list[tuple[str, str]]]:
    """(findings not covered by the allowlist, allowlist entries nothing used)."""
    findings = (
        check_prompt_bodies()
        + check_templates()
        + check_exception_messages()
        + check_console_text()
        + check_personal_identifiers()
    )
    allow = load_allowlist()
    used: set[tuple[str, str]] = set()
    kept: list[Finding] = []
    for finding in findings:
        # POSIX separators on both sides: the allowlist is written with "/"
        # and str() of a relative path yields "\" on Windows, so a developer
        # running the gate there saw every allowed entry reported twice —
        # once as an unallowed finding, once as a stale allowlist line.
        rel = finding.path.relative_to(ROOT).as_posix()
        hit = next(((p, t) for p, t in allow if p == rel and t == finding.text), None)
        if hit is None:
            kept.append(finding)
        else:
            used.add(hit)
    stale = [entry for entry in allow if entry not in used]
    return kept, stale


def main() -> int:
    findings, stale = run()
    for finding in findings:
        print(finding.render())
    for path, text in stale:
        print(f"{ALLOWLIST.relative_to(ROOT)}: stale allowlist entry: {path} {text}")
    if findings or stale:
        print(
            f"\n{len(findings)} self-reference(s), {len(stale)} stale allowlist entr(y/ies) — "
            "strip the reference, expand it to a full URL, or add a reviewed line to "
            f"{ALLOWLIST.relative_to(ROOT)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
