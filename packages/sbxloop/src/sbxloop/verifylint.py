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

import json
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


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

# So is the network. A verify command must judge the workspace, not remote
# state: `gh pr view | grep -q .` failed a review task whose deliverable — a
# local file — was present and valid, because the sandbox's anonymous GitHub
# quota was exhausted (#440). A rate limit or a network flake must never be
# able to fail work that is actually done. `curl`/`wget` stay legal against
# an address that is unambiguously local (probing a server the command
# itself started is a documented pattern); `gh` never is — it talks to
# GitHub by definition.
NETWORK_COMMANDS = frozenset({"gh", "curl", "wget"})
_LOCAL_ADDRESS = re.compile(r"localhost|127\.0\.0\.1|\[?::1\]?|0\.0\.0\.0|unix:")

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


# A nested shell is a hazard when it can change what runs. `bash`/`dash`/
# `zsh` may not be installed; a login or extra flag (`sh -lc`) changes the
# environment; and a double-quoted or unquoted payload is expanded by the
# OUTER shell before the inner one ever sees it (field failure r7ef26eht).
#
# `sh -c '...'` whose payload holds no `$` and no backtick is none of those:
# the outer shell hands the single-quoted span through verbatim, so it runs
# exactly as the payload would have run unwrapped. Rejecting that inert form
# is what cost item gh:478 both of its decompose attempts — twice over a
# `sh -c 'git diff --quiet && git diff --cached --quiet'` that would have
# behaved identically either way — and PR #476 went unreviewed for it.
_INERT_SHELL_WRAPPER = re.compile(r"^(?:/bin/|/usr/bin/)?sh\s+-c\s+'([^']*)'$")


def unwrap_inert_shell(command: str) -> str:
    """The payload of a provably-inert ``sh -c '...'``, else the command.

    Unwrapping rather than merely allowing is the point: the payload is what
    actually runs, so every other rule here has to keep seeing it. A wrapper
    waved through whole would hide its payload from the toolchain and
    mutating-command checks (single-quoted spans are blanked before those
    scans), which is a larger hole than the one it closes.
    """
    match = _INERT_SHELL_WRAPPER.match(command.strip())
    if match is None:
        return command
    payload = match.group(1)
    if "$" in payload or "`" in payload:
        # Expansion the wrapper's own quoting decides — out of scope for
        # "provably identical", so leave it to the nested-shell rule.
        return command
    return payload


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


# The project's own gate: the one command that runs everything the project
# holds itself to. Field failure: a delivered PR (#389) failed `mdformat` and
# `security` because the plan's verify commands were a *subset* of what the
# repository enforces. The run reported success, the PR sat red, and the
# review that followed cut five style issues without noticing the build was
# broken.
#
# Checked at JSON acceptance rather than after delivery: here it costs one
# retry with the rule quoted, there it costs a PR, a review round and a human
# noticing. Polling the PR's checks still catches what this cannot (anything
# that only fails in CI's environment); this is the cheap half.
#
# Detection is a table of conventions, like LANGUAGE_RULES above — a gate is
# per-ecosystem knowledge, and hardcoding one project's shape would silently
# switch the guarantee off for every repository that does something else.
# Each detector answers only when the project *declares* the gate itself.
# Nothing is inferred from CI workflow files: a requirement invented from a
# YAML we half-understood is unfixable by the executor, which cannot edit
# verify commands.
#
# `check` and `ci` only, never `test`: those two name the whole gate by
# convention, whereas `test` is one part of it and demanding it would let a
# lint-failing PR through while looking satisfied.
GATE_TARGETS = ("check", "ci")


def _target_gate(workspace: Path, files: Sequence[str], pattern: str, command: str) -> str | None:
    """First declared target in the first of ``files`` that exists.

    Only the first file is consulted: make, just and task each read one, so
    a target in a shadowed file is not the gate the tool would run.
    """
    for name in files:
        path = workspace / name
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for target in GATE_TARGETS:
            if re.search(pattern.format(target=re.escape(target)), text, re.M):
                return command.format(target=target)
        return None
    return None


def _make_gate(workspace: Path) -> str | None:
    # GNU make's own search order.
    return _target_gate(
        workspace, ("GNUmakefile", "makefile", "Makefile"), r"^{target}\s*:", "make {target}"
    )


def _just_gate(workspace: Path) -> str | None:
    return _target_gate(
        workspace, ("justfile", ".justfile", "Justfile"), r"^{target}\s*:", "just {target}"
    )


def _task_gate(workspace: Path) -> str | None:
    # Taskfile targets are YAML keys under `tasks:`, so a two-space indent is
    # the shape rather than a line start.
    return _target_gate(
        workspace, ("Taskfile.yml", "Taskfile.yaml"), r"^\s+{target}\s*:", "task {target}"
    )


def _npm_gate(workspace: Path) -> str | None:
    path = workspace / "package.json"
    try:
        scripts = json.loads(path.read_text(encoding="utf-8", errors="replace")).get("scripts")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(scripts, dict):
        return None
    for target in GATE_TARGETS:
        if target in scripts:
            return f"npm run {target}"
    return None


def _tox_gate(workspace: Path) -> str | None:
    # tox and nox declare the whole matrix in one file; running the bare
    # command IS the gate, so presence is the whole signal.
    return "tox" if (workspace / "tox.ini").is_file() else None


def _nox_gate(workspace: Path) -> str | None:
    return "nox" if (workspace / "noxfile.py").is_file() else None


# Order matters only where a repo declares more than one; the first is taken.
# Task runners come before language runners because a repo carrying both has
# usually made the task runner the front door.
GATE_DETECTORS: tuple[Callable[[Path], str | None], ...] = (
    _make_gate,
    _just_gate,
    _task_gate,
    _npm_gate,
    _tox_gate,
    _nox_gate,
)


def project_gate(workspace: Path | None, override: str | None = None) -> str | None:
    """The command running this project's whole gate, or None if it has none.

    ``override`` is the operator's answer (``[sandbox] gate_command``) and
    wins over every detector — the escape hatch for a project whose gate no
    convention describes, and, set to an empty string, the way to say "this
    project has no gate" and switch the requirement off.

    A project that declares nothing gets nothing required of it. A guessed
    requirement is worse than none: the executor cannot edit verify commands,
    so a gate we invented is unsatisfiable.
    """
    if override is not None:
        return override.strip() or None
    if workspace is None:
        return None
    for detect in GATE_DETECTORS:
        gate = detect(workspace)
        if gate:
            return gate
    return None


def gate_rule(gate: str | None) -> str:
    """The decompose prompt's gate paragraph, naming *this* project's gate.

    Rendered rather than written into the template because the gate is not
    a constant: `make check` here, `just ci` or `npm run check` elsewhere,
    and nothing at all in a project that declares none. A template that
    named one convention would be wrong everywhere else — and would teach
    the model to invent that convention where it does not exist.
    """
    if not gate:
        return (
            "- This project declares no single gate command, so none is required. "
            "Choose verify commands that actually check each task."
        )
    return (
        f"- One task's `verify_commands` MUST run this project's own gate: "
        f"`{gate}`. That gate is what CI enforces on the pull request, so work "
        f"that never runs it lands red. One task carrying it is enough — the "
        f"last task is usually the right one — and narrower commands on other "
        f"tasks are still worth having for a faster signal. This is enforced "
        f"mechanically, like the conventions above."
    )


def runs_gate(command: str, gate: str) -> bool:
    """Whether ``command`` invokes ``gate``.

    Token-exact, so ``make check-fast`` and ``make precheck`` do not pass for
    ``make check``. A command may carry flags and extra arguments — ``make
    -j4 check lint`` and ``npm run check --silent`` both still run the gate.
    A single-word gate (``tox``, ``nox``) needs only its program.
    """
    try:
        words = shlex.split(command)
        wanted = shlex.split(gate)
    except ValueError:
        return False
    if not wanted:
        return False
    program, rest = wanted[0], wanted[1:]
    for index, word in enumerate(words):
        if word != program:
            continue
        after = words[index + 1 :]
        if all(token in after for token in rest):
            return True
    return False


def lint_verify_commands(
    commands: Sequence[str],
    languages: Sequence[str],
    *,
    uv_project: bool = False,
    gate: str | None = None,
) -> list[str]:
    """Violation messages for ``commands`` under the run's toolchains.

    Empty means clean. Messages are written to be fed back to the model
    verbatim: they name the offending command, the bare word, and the
    ecosystem's remedy. ``uv_project`` says the workspace carries a
    ``uv.lock``, which swaps the Python convention from ``.venv/bin/...``
    to ``uv run`` (see the module docstring). ``gate`` is the project's own
    full check (see :func:`project_gate`); when the project declares one,
    the verify commands must run it.
    """
    uv_python = uv_project and "python" in languages
    rules = [
        UV_PYTHON_RULE if uv_python and lang == "python" else LANGUAGE_RULES[lang]
        for lang in languages
        if lang in LANGUAGE_RULES
    ]
    problems: list[str] = []
    for raw in commands:
        # An inert `sh -c '...'` runs exactly as its payload does, so lint
        # the payload — and name it in the messages, since it is what the
        # runner will execute.
        command = unwrap_inert_shell(raw)
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
            if head in NETWORK_COMMANDS and (head == "gh" or not _LOCAL_ADDRESS.search(command)):
                problems.append(
                    f"verify command `{command}` runs `{head}` — verify commands "
                    "must judge the workspace, not the network: an API rate "
                    "limit or a flake would fail work that is done. Check the "
                    "local files or run the local tests instead"
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
    if gate and not any(runs_gate(unwrap_inert_shell(command), gate) for command in commands):
        problems.append(
            f"none of the verify commands runs `{gate}`, this project's own gate — "
            "the checks it bundles are what CI enforces on the pull request, so "
            "work that skips it lands red. Add it as a verify command; keep the "
            "narrower ones too if they give a faster signal."
        )
    return problems
