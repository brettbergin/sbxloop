"""Mechanical validation of verify commands against toolchain conventions.

Field failure r12ygfd7t: the decomposer wrote ``python -m pytest`` verify
commands. The sandbox's system Python is externally managed (PEP 668) and
carries no project dependencies, so the mechanical verify phase failed and
the executor burned a revision apt-installing packages system-wide to
satisfy a command it is forbidden to edit. Prompt guidance alone is
probabilistic — the model complied at plan time and not at decompose time
in the very same run — so the convention is enforced here, at JSON
acceptance, where a violation costs one retry with the rule quoted instead
of a revision cycle.

The hazard is not the same across languages, so the rules are a table
keyed by the run's configured toolchains. Only ecosystems with a
project-local dependency prefix need entries: bare ``python``/``pytest``
bypasses ``.venv/bin/``, bare ``rspec`` bypasses ``bundle exec``, bare
``phpunit`` bypasses ``vendor/bin/``. Go, Rust, .NET, Java, Node, and
C/C++ are *correctly* invoked bare (``go test``, ``cargo test``,
``dotnet test``, ``npm test``) and deliberately have no entries — a
blanket "no bare commands" rule would break the languages that are fine.

Environment mutation (``sudo``/``apt``) is rejected for every language:
verification must check the work, not rebuild the environment it checks.
Project-local installs the ecosystem notes prescribe (``npm ci``,
``composer install``) stay legal.

The Python rule has two shapes (#250). A workspace carrying ``uv.lock`` is
a uv project — often a uv *workspace* with several members — and there
``python3 -m venv`` + ``pip install`` does not reproduce the locked
environment at all; ``uv run`` does (and syncs it first). So with a
lockfile present the convention flips: ``uv run pytest`` is required and
``.venv/bin/pytest`` is flagged. Without one, ``.venv/bin/...`` stays the
shape and ``uv run`` is not demanded.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageRule:
    """Bare command names that bypass the ecosystem's dependency prefix."""

    commands: frozenset[str]
    remedy: str


LANGUAGE_RULES: dict[str, LanguageRule] = {
    "python": LanguageRule(
        commands=frozenset({"python", "python3", "pip", "pip3", "pytest"}),
        remedy=(
            "use the project virtualenv's paths (.venv/bin/python, "
            ".venv/bin/pip, .venv/bin/pytest): the sandbox's system Python "
            "is externally managed (PEP 668) and has no project dependencies, "
            "and `python` (unversioned) does not exist on Debian at all. If "
            "this command was creating the venv or installing packages, move "
            "that into the plan's execution steps — verify commands run after "
            "execution and may assume the environment the steps built, so "
            "`.venv/bin/pytest -q` alone is the right shape"
        ),
    ),
    "ruby": LanguageRule(
        commands=frozenset({"rspec", "rake", "rubocop"}),
        remedy=(
            "prefix with `bundle exec` (e.g. `bundle exec rspec`) so gems "
            "resolve from the project bundle instead of system gems"
        ),
    ),
    "php": LanguageRule(
        commands=frozenset({"phpunit"}),
        remedy=(
            "use the project path `./vendor/bin/phpunit`: composer-installed "
            "binaries are not on PATH"
        ),
    ),
}

# The uv-project variant of the Python rule. Same bare names are wrong, the
# remedy is different: the executor's `uv sync` builds the environment from
# uv.lock and `uv run` is what reaches it. And `.venv/bin/...` is wrong here
# too — a hand-made venv beside a lockfile is exactly the environment drift
# uv exists to prevent (a uv workspace's members are only importable when
# uv installed them).
UV_LOCKFILE = "uv.lock"
UV_PYTHON_RULE = LanguageRule(
    commands=LANGUAGE_RULES["python"].commands,
    remedy=(
        f"this workspace has a `{UV_LOCKFILE}`, so run through uv: `uv run pytest -q` "
        "(`uv run python ...` for scripts). uv resolves the interpreter and the "
        "locked dependencies itself; `uv sync` belongs in the plan's execution "
        "steps, not in a verify command"
    ),
)
_VENV_PATH = re.compile(r"(^|/)\.venv/bin/")
_UV_VENV_REMEDY = (
    f"this workspace has a `{UV_LOCKFILE}`, so use `uv run <command>` (e.g. "
    "`uv run pytest -q`) rather than a `.venv/bin/...` path: uv builds and syncs "
    "the environment from the lockfile, and a hand-made venv beside it does not "
    "carry the workspace's own packages"
)

# Environment mutation is out of bounds in verify commands regardless of
# language. Verify commands run mechanically after the work is done; a
# command that has to install something is doing the executor's job.
MUTATING_COMMANDS = frozenset({"sudo", "apt", "apt-get", "dnf", "yum", "apk"})

# Shell operators that start a new command position. Backtick / $( catch
# command substitutions so `echo $(pytest)` is still inspected.
_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||\$\(|`|\n")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Verify commands run under `sh -c` (POSIX sh, dash on Debian), NOT bash.
# Bash-only syntax fails there in one of two ways, and neither is a check
# the executor can fix (it may not edit verify commands): ANSI-C quoting
# `$'...'` is *silently reinterpreted* — `grep -q $'\033[31m'` searches for
# the literal text "$\033[31m" and never matches (field failure re59gj4vq:
# correct code, unrunnable check, revisions burned) — while `[[`, `source`,
# `declare`/`local`, `pushd`/`popd` fail as *unknown commands* and `<<<` is
# a *syntax error* in dash. Command-like bashisms are matched only in
# command position (so `grep -F 'source' file` is data, not syntax);
# operator-like ones only outside quotes.
_BASHISM_COMMANDS: dict[str, tuple[str, str]] = {
    "[[": ("`[[ ... ]]` (bash test)", "use POSIX `[ ... ]` / `test`"),
    "source": ("`source` (bash builtin)", "use POSIX `.` to source a file"),
    "declare": ("`declare` (bash builtin)", "use plain POSIX assignment"),
    "local": ("`local` (bash builtin)", "use plain assignment or a subshell"),
    "pushd": ("`pushd` (bash builtin)", "use `cd` in a subshell: `(cd dir && ...)`"),
    "popd": ("`popd` (bash builtin)", "use `cd` in a subshell: `(cd dir && ...)`"),
}
_BASHISM_OPERATORS: tuple[tuple[str, str, str], ...] = (
    (
        "$'",
        "ANSI-C quoting `$'...'`, which POSIX sh silently reinterprets as literal text",
        "use `printf` for escapes, e.g. `printf '\\033[31m'` or "
        "`grep -q \"$(printf '\\033')\\[31m\"`",
    ),
    (
        "<<<",
        "a here-string `<<<`, which is a syntax error in POSIX sh",
        "pipe with `printf '%s' ... |` instead",
    ),
)


def _strip_quoted(command: str, *, keep_ansi_c: bool = False) -> str:
    """The command with single- and double-quoted spans blanked out, so
    operator scans see shell syntax rather than string data. With
    ``keep_ansi_c`` the ``$`` opening an ANSI-C quote survives (the quote
    body is still blanked), so ``$'`` can be detected."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
                if keep_ansi_c and ch == "'" and out and out[-1] == "$":
                    out.append("'")
                else:
                    out.append(" ")
            else:
                out.append(ch)
        else:
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                out.append("  ")
                i += 2
                continue
            if ch == quote:
                quote = None
            out.append(" ")
        i += 1
    return "".join(out)


# `sh -c "..."` / `bash -c '...'` at command position: a shell inside the
# shell the runner already provides. Matched on the raw command so quoting
# does not hide it.
_NESTED_SHELL = re.compile(
    r"(?:^|[|&;(]\s*)(?:/bin/|/usr/bin/)?(?:sh|bash|dash|zsh)\s+-[a-zA-Z]*c\b"
)


def bashisms(command: str) -> list[str]:
    """Portable-shell violations in one verify command (empty = clean)."""
    problems: list[str] = []
    # ANSI-C quoting is `$` immediately followed by an opening single quote
    # *outside* any quote — so it must be detected before quoted spans are
    # blanked (the `'...'` part looks like an ordinary string otherwise).
    # A `$'` inside double quotes is just a dollar sign and a quote.
    if "$'" in _strip_quoted(command, keep_ansi_c=True):
        what, rewrite = _BASHISM_OPERATORS[0][1], _BASHISM_OPERATORS[0][2]
        problems.append(f"uses {what} — verify commands run under `sh -c`; {rewrite}")
    unquoted = _strip_quoted(command)
    for token, what, rewrite in _BASHISM_OPERATORS[1:]:
        if token in unquoted:
            problems.append(f"uses {what} — verify commands run under `sh -c`; {rewrite}")
    heads = command_heads(command)
    for head in heads:
        if head in _BASHISM_COMMANDS:
            what, rewrite = _BASHISM_COMMANDS[head]
            problems.append(
                f"invokes {what}, which fails as an unknown command under `sh -c`; {rewrite}"
            )
    if _NESTED_SHELL.search(command):
        # Field failure r7ef26eht (first sbxloop-on-sbxloop run): the plan
        # wrapped a pipeline as `sh -c "... awk '{print $2}' ..."`. The
        # runner already executes each verify command under `sh -c`, so the
        # OUTER shell expanded the double-quoted `$2` to nothing, awk printed
        # whole lines, the allowlist grep matched none of them, and a correct
        # change failed verification three revisions and a replan in a row.
        problems.append(
            "wraps the check in a nested `sh -c`/`bash -c` string — verify commands "
            "already run under `sh -c`, and `$` expansions inside the wrapper's double "
            "quotes are consumed by the outer shell; write the pipeline directly, "
            "unwrapped"
        )
    return problems


def command_heads(command: str) -> list[str]:
    """The command-position words of a shell command line.

    Splits on operators, skips env-var prefixes (``FOO=1 cmd``), and
    returns the first real word of each segment. Bare names only carry
    meaning for the rules: a pathed invocation (``.venv/bin/pytest``)
    contains a slash and never matches a bare-name rule.
    """
    heads: list[str] = []
    for segment in _SEGMENT_SPLIT.split(command):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        for word in words:
            word = word.lstrip("(!{ ")
            if not word or _ENV_ASSIGNMENT.match(word):
                continue
            heads.append(word)
            break
    return heads


def lint_verify_commands(
    commands: Sequence[str], languages: Sequence[str], *, uv_project: bool = False
) -> list[str]:
    """Violation messages for ``commands`` under the run's toolchains.

    Empty means clean. Messages are written to be fed back to the model
    verbatim: they name the offending command, the bare word, and the
    ecosystem's remedy. ``uv_project`` says the workspace carries a
    ``uv.lock``, which swaps the Python convention from ``.venv/bin/...``
    to ``uv run`` (see the module docstring).
    """
    uv_python = uv_project and "python" in languages
    rules = [
        UV_PYTHON_RULE if uv_python and lang == "python" else LANGUAGE_RULES[lang]
        for lang in languages
        if lang in LANGUAGE_RULES
    ]
    problems: list[str] = []
    for command in commands:
        for problem in bashisms(command):
            problems.append(f"verify command `{command}` {problem}")
        for head in command_heads(command):
            if head in MUTATING_COMMANDS:
                problems.append(
                    f"verify command `{command}` runs `{head}` — verify commands "
                    "must not modify the environment; anything that needs "
                    "installing is a plan step, not a verification"
                )
                continue
            if uv_python and _VENV_PATH.search(head):
                problems.append(f"verify command `{command}` invokes `{head}` — {_UV_VENV_REMEDY}")
                continue
            for rule in rules:
                if head in rule.commands:
                    problems.append(
                        f"verify command `{command}` invokes bare `{head}` — {rule.remedy}"
                    )
                    break
    return problems
