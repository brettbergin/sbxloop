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
            "is externally managed (PEP 668) and has no project dependencies "
            "— and `python` (unversioned) does not exist on Debian at all"
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

# Environment mutation is out of bounds in verify commands regardless of
# language. Verify commands run mechanically after the work is done; a
# command that has to install something is doing the executor's job.
MUTATING_COMMANDS = frozenset({"sudo", "apt", "apt-get", "dnf", "yum", "apk"})

# Shell operators that start a new command position. Backtick / $( catch
# command substitutions so `echo $(pytest)` is still inspected.
_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||\$\(|`|\n")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


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


def lint_verify_commands(commands: Sequence[str], languages: Sequence[str]) -> list[str]:
    """Violation messages for ``commands`` under the run's toolchains.

    Empty means clean. Messages are written to be fed back to the model
    verbatim: they name the offending command, the bare word, and the
    ecosystem's remedy.
    """
    rules = [LANGUAGE_RULES[lang] for lang in languages if lang in LANGUAGE_RULES]
    problems: list[str] = []
    for command in commands:
        for head in command_heads(command):
            if head in MUTATING_COMMANDS:
                problems.append(
                    f"verify command `{command}` runs `{head}` — verify commands "
                    "must not modify the environment; anything that needs "
                    "installing is a plan step, not a verification"
                )
                continue
            for rule in rules:
                if head in rule.commands:
                    problems.append(
                        f"verify command `{command}` invokes bare `{head}` — {rule.remedy}"
                    )
                    break
    return problems
