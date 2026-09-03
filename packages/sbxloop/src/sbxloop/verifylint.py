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

import configparser
import json
import re
import shlex
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml


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
# is what cost item gh:issue:478 both of its decompose attempts — twice over a
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
# Target names, never `test` and never `all`: `check`, `ci` and `verify`
# name the whole gate by convention (verify is Maven's lifecycle word and
# common in makefiles), whereas `test` is one part of it and demanding it
# would let a lint-failing PR through while looking satisfied, and `all` is
# the default build, not a check. Each detector carries its own list
# (#625): Rake's `default` is a gate where make's is not.
GATE_TARGETS = ("check", "ci", "verify")
RAKE_TARGETS = ("ci", "check", "default")
COMPOSER_TARGETS = ("check", "ci")
# `check` is a cargo built-in and a `[alias] check` is silently shadowed by
# it, so only `ci` names a gate here.
CARGO_ALIASES = ("ci",)


def _read(path: Path) -> str | None:
    """The file's text, or None when it is not a readable file."""
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _target_gate(
    workspace: Path,
    files: Sequence[str],
    pattern: str,
    command: str,
    targets: Sequence[str] = GATE_TARGETS,
) -> str | None:
    """First declared target in the first of ``files`` that exists.

    Only the first file is consulted: make, just and task each read one, so
    a target in a shadowed file is not the gate the tool would run.
    """
    for name in files:
        text = _read(workspace / name)
        if text is None:
            continue
        for target in targets:
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


def _rake_gate(workspace: Path) -> str | None:
    # `task :ci do`, `task ci: [...]`, `task "check" => ...`, `task default:`.
    return _target_gate(
        workspace,
        ("Rakefile", "rakefile", "Rakefile.rb"),
        r"^\s*task\s*\(?\s*(?::{target}\b|{target}\s*:|['\"]{target}['\"])",
        "bundle exec rake {target}",
        RAKE_TARGETS,
    )


def _json_object(path: Path, key: str) -> dict[str, Any] | None:
    """``key``'s object in a JSON file, or None for anything else."""
    text = _read(path)
    if text is None:
        return None
    try:
        value = json.loads(text).get(key)
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, dict) else None


# The client a package.json's scripts run under (#626), strongest signal
# first: corepack's `packageManager` declaration, then the lockfile. `npm
# run` in a pnpm workspace fails outright on `workspace:` dependencies, so
# it would be a gate the executor cannot satisfy.
_LOCKFILE_CLIENTS: tuple[tuple[str, str], ...] = (
    ("pnpm-lock.yaml", "pnpm run"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun run"),
    ("bun.lock", "bun run"),
)
_PACKAGE_MANAGER_CLIENTS = {"pnpm": "pnpm run", "yarn": "yarn", "bun": "bun run", "npm": "npm run"}


def node_script_runner(workspace: Path) -> str:
    """How to run a script declared in ``workspace``'s package.json."""
    text = _read(workspace / "package.json")
    if text is not None:
        try:
            declared = json.loads(text).get("packageManager")
        except (ValueError, AttributeError):
            declared = None
        if isinstance(declared, str):
            name = declared.split("@", 1)[0].strip()
            if name in _PACKAGE_MANAGER_CLIENTS:
                return _PACKAGE_MANAGER_CLIENTS[name]
    for lockfile, client in _LOCKFILE_CLIENTS:
        if (workspace / lockfile).is_file():
            return client
    return "npm run"


def _npm_gate(workspace: Path) -> str | None:
    scripts = _json_object(workspace / "package.json", "scripts")
    if scripts is None:
        return None
    for target in GATE_TARGETS:
        if target in scripts:
            return f"{node_script_runner(workspace)} {target}"
    return None


def _composer_gate(workspace: Path) -> str | None:
    scripts = _json_object(workspace / "composer.json", "scripts")
    if scripts is None:
        return None
    for target in COMPOSER_TARGETS:
        if target in scripts:
            return f"composer run {target}"
    return None


def _tox_gate(workspace: Path) -> str | None:
    # tox and nox declare the whole matrix in one file; running the bare
    # command IS the gate, so presence is the whole signal.
    return "tox" if (workspace / "tox.ini").is_file() else None


def _nox_gate(workspace: Path) -> str | None:
    return "nox" if (workspace / "noxfile.py").is_file() else None


def _gradle_gate(workspace: Path) -> str | None:
    # `check` is Gradle's built-in lifecycle gate; the wrapper script is the
    # declaration of how to run it (and the only Gradle the sandbox has —
    # the java toolchain ships Maven, not Gradle).
    if not (workspace / "gradlew").is_file():
        return None
    if any((workspace / name).is_file() for name in ("build.gradle", "build.gradle.kts")):
        return "./gradlew check"
    return None


def _maven_gate(workspace: Path) -> str | None:
    # `verify` is Maven's lifecycle gate; a wrapper is preferred when the
    # project ships one, else the toolchain's mvn.
    if not (workspace / "pom.xml").is_file():
        return None
    return "./mvnw -q verify" if (workspace / "mvnw").is_file() else "mvn -q verify"


def _cargo_alias_gate(workspace: Path) -> str | None:
    for name in (".cargo/config.toml", ".cargo/config"):
        text = _read(workspace / name)
        if text is None:
            continue
        try:
            aliases = tomllib.loads(text).get("alias")
        except (tomllib.TOMLDecodeError, AttributeError):
            return None
        if not isinstance(aliases, dict):
            return None
        for alias in CARGO_ALIASES:
            if alias in aliases:
                return f"cargo {alias}"
        return None
    return None


# Language-native gates for the ecosystems whose build tool IS the gate
# (#625): nothing to declare, so nothing to read — the manifest at the root
# is the declaration, and the command is satisfiable by construction on the
# toolchain that manifest resolved. Go has no task-runner convention at all,
# which is why it is here and not above.
def _go_gate(workspace: Path) -> str | None:
    return "go vet ./... && go test ./..." if (workspace / "go.mod").is_file() else None


def _cargo_gate(workspace: Path) -> str | None:
    return "cargo test" if (workspace / "Cargo.toml").is_file() else None


def _dotnet_gate(workspace: Path) -> str | None:
    # `dotnet test` needs exactly one solution or project file in the
    # directory to know what to build.
    try:
        names = [p.name for p in workspace.iterdir() if p.is_file()]
    except OSError:
        return None
    solutions = [n for n in names if n.endswith(".sln")]
    projects = [n for n in names if n.endswith((".csproj", ".fsproj"))]
    if len(solutions) == 1 or (not solutions and len(projects) == 1):
        return "dotnet test"
    return None


@dataclass(frozen=True)
class GateDetector:
    """One convention: the detector, and the toolchain its command needs.

    ``language`` is None for a task runner (make, just, task) that any
    sandbox has; otherwise the registry name whose toolchain must be in
    the run's resolved set (#624) for the command to be runnable at all.
    Rule: a detector may only emit a command the resolved toolchain can
    run — a gate the executor cannot invoke is unsatisfiable, not strict.
    """

    detect: Callable[[Path], str | None]
    language: str | None = None


# Order matters only where a repo declares more than one; the first is taken.
# Task runners come before language runners because a repo carrying both has
# usually made the task runner the front door; the language-native
# fallbacks come last, after every declaration.
GATE_DETECTORS: tuple[GateDetector, ...] = (
    GateDetector(_make_gate),
    GateDetector(_just_gate),
    GateDetector(_task_gate),
    GateDetector(_npm_gate, "javascript"),
    GateDetector(_tox_gate, "python"),
    GateDetector(_nox_gate, "python"),
    GateDetector(_rake_gate, "ruby"),
    GateDetector(_composer_gate, "php"),
    GateDetector(_gradle_gate, "java"),
    GateDetector(_maven_gate, "java"),
    GateDetector(_cargo_alias_gate, "rust"),
    GateDetector(_go_gate, "go"),
    GateDetector(_cargo_gate, "rust"),
    GateDetector(_dotnet_gate, "dotnet"),
)


def project_gate(
    workspace: Path | None,
    override: str | None = None,
    *,
    languages: Sequence[str] | None = None,
) -> str | None:
    """The command running this project's whole gate, or None if it has none.

    ``override`` is the operator's answer (``[sandbox] gate_command``) and
    wins over every detector — the escape hatch for a project whose gate no
    convention describes, and, set to an empty string, the way to say "this
    project has no gate" and switch the requirement off.

    ``languages`` is the run's resolved toolchain set (#624): a detector
    whose command needs a toolchain outside it is not consulted, because
    the command could not run. None (no resolution at hand — embedders,
    tests) consults every detector.

    A project that declares nothing gets nothing required of it. A guessed
    requirement is worse than none: the executor cannot edit verify commands,
    so a gate we invented is unsatisfiable.
    """
    if override is not None:
        return override.strip() or None
    if workspace is None:
        return None
    for detector in GATE_DETECTORS:
        if (
            languages is not None
            and detector.language is not None
            and detector.language not in languages
        ):
            continue
        gate = detector.detect(workspace)
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


@dataclass(frozen=True)
class ConfigOverrideExample:
    """The prompts' worked example of a config-override, for one ecosystem.

    The decomposer and the reviewer each read exactly one of these — the one
    for the run's resolved toolchain (#634) — so the anchor they pattern-
    match against is correct for the repository in front of them rather than
    a story about some other project's build. ``config`` is the excerpt of
    the project file; ``story`` says what the gate runs, what the verify
    command ran instead, what that dragged in, and the remedy.
    """

    language: str
    fence: str
    config: str
    story: str

    def render(self) -> str:
        return f"```{self.fence}\n{self.config}\n```\n\n{self.story}"


# Config-driven tools whose *file set* lives in the project's config. Field
# failure rrhb28j7n (#387): the plan's verify command was `uv run mypy
# packages`. The repo pins `[tool.mypy] files = [...]` in pyproject.toml, and
# passing an explicit path OVERRIDES that list — dragging in a build hook
# that imports `hatchling`, absent from the sandbox. `uv run mypy` (bare)
# passes; `uv run mypy packages` can never pass. The executor may not edit
# verify commands, so the run burned two revisions and a replan on three
# 208-second mypy invocations of a check that was unpassable by construction.
#
# The rule only fires when the project actually declares the key: a tool with
# no configured file set is *supposed* to be given paths, and flagging that
# would reject the only correct shape. The same test decides which tools get
# an entry at all (#628): each one below was checked against the tool's own
# behaviour, and the ones an explicit path merely *narrows* stay out —
# eslint's flat-config `files`/`ignores` still apply to a directory or file
# named on the command line (an ignored file is skipped with a warning),
# and golangci-lint v2 applies `linters.exclusions.paths` whatever paths
# `run` is given (the v1 skip-dirs carve-out for explicitly named
# directories is gone from v2's exclusion_paths processor). Flagging
# `eslint src` or `golangci-lint run ./pkg/...` would reject a correct
# narrowing with a rule that is false for the tool.
#
# Three shapes of override:
#   include — the config names the file set; a path outside it is dragged
#             in (mypy `files`, ruff `include`/`src`, pytest `testpaths`).
#   exclude — the config names what to skip; a *file* named explicitly is
#             inspected anyway (rubocop `AllCops/Exclude`: only
#             `--force-exclusion` stops that; a directory argument is still
#             filtered, so it is a narrowing).
#   whole   — any input file on the command line makes the tool ignore its
#             project file entirely (tsc and tsconfig.json: `include`,
#             `compilerOptions`, `paths` alike).
OverrideMode = Literal["include", "exclude", "whole"]


@dataclass(frozen=True)
class ConfigScopedTool:
    """A tool whose configured file set explicit path arguments override."""

    name: str
    # (config file, section, keys) triples consulted in order. Empty for a
    # tool the lint does not read yet: the entry then only carries the
    # ecosystem's worked example for the prompts. Empty *keys* mean the
    # file's presence is the configuration (tsconfig.json for `tsc`).
    sources: tuple[tuple[str, str, tuple[str, ...]], ...]
    # Flags that consume the next word, so it is not a positional path.
    value_flags: frozenset[str]
    # Words that are subcommands rather than paths (`ruff check src`).
    subcommands: frozenset[str] = frozenset()
    # Arguments that are not a narrowing: `ruff check .` is the idiomatic
    # whole-tree invocation and ruff still applies its own include/exclude
    # underneath it, so flagging it would reject the canonical shape.
    benign_args: frozenset[str] = frozenset()
    # The prompts' worked example of this tool's override, if it is the one
    # that stands for its ecosystem.
    example: ConfigOverrideExample | None = None
    mode: OverrideMode = "include"
    # Flags under which the invocation is a different shape altogether and
    # the rule does not apply (`tsc -b <project>` names projects, not files).
    disarm_flags: frozenset[str] = frozenset()


_REMEDY = (
    "Nothing in the diff caused it and nothing in a diff can fix it; the remedy "
    "is re-authoring the command to the bare form"
)
_PYPROJECT = "pyproject.toml"
CONFIG_SCOPED_TOOLS: dict[str, ConfigScopedTool] = {
    "mypy": ConfigScopedTool(
        name="mypy",
        example=ConfigOverrideExample(
            language="python",
            fence="toml",
            config='[tool.mypy]\nfiles = ["src"]',
            story=(
                'The project gate runs `uv run mypy` and reports "Success: no issues '
                "found in 118 source files\". The task's verify command runs "
                "`uv run mypy .`; the explicit path overrides `files` and pulls in "
                "`docs/conf.py`, which imports `sphinx` — a docs-only dependency "
                'absent from the sandbox — so the command exits 1 with "Cannot find '
                'implementation or library stub for module named sphinx" on every '
                f"attempt. {_REMEDY}. The same shape reaches ruff (`include`/`src`) "
                "and pytest (`testpaths`)."
            ),
        ),
        sources=(
            (_PYPROJECT, "tool.mypy", ("files",)),
            ("setup.cfg", "mypy", ("files",)),
            ("mypy.ini", "mypy", ("files",)),
            (".mypy.ini", "mypy", ("files",)),
        ),
        value_flags=frozenset(
            {
                "--config-file",
                "--python-version",
                "--cache-dir",
                "--exclude",
                "--follow-imports",
                "--platform",
                "-p",
                "-m",
                "-c",
                "--module",
                "--package",
                "--command",
            }
        ),
    ),
    "ruff": ConfigScopedTool(
        name="ruff",
        sources=(
            (_PYPROJECT, "tool.ruff", ("include", "src")),
            ("ruff.toml", "", ("include", "src")),
            (".ruff.toml", "", ("include", "src")),
        ),
        value_flags=frozenset(
            {
                "--config",
                "--select",
                "--ignore",
                "--extend-select",
                "--extend-ignore",
                "--target-version",
                "--line-length",
                "--per-file-ignores",
                "--cache-dir",
                "-e",
            }
        ),
        subcommands=frozenset({"check", "format", "rule", "linter", "clean", "version"}),
        benign_args=frozenset({"."}),
    ),
    "pytest": ConfigScopedTool(
        name="pytest",
        sources=(
            (_PYPROJECT, "tool.pytest.ini_options", ("testpaths",)),
            ("pytest.ini", "pytest", ("testpaths",)),
            ("tox.ini", "pytest", ("testpaths",)),
            ("tox.ini", "tool:pytest", ("testpaths",)),
            ("setup.cfg", "tool:pytest", ("testpaths",)),
        ),
        value_flags=frozenset(
            {
                "-k",
                "-m",
                "-p",
                "-n",
                "-o",
                "-c",
                "-W",
                "--rootdir",
                "--deselect",
                "--ignore",
                "--junitxml",
                "--maxfail",
                "--cov",
                "--cov-report",
                "--override-ini",
                "--import-mode",
            }
        ),
    ),
    "tsc": ConfigScopedTool(
        name="tsc",
        # Presence is the configuration: tsc reads tsconfig.json for the
        # whole program, and input files on the command line drop it.
        sources=(("tsconfig.json", "", ()),),
        value_flags=frozenset(
            {
                "--project",
                "-p",
                "--outDir",
                "--outFile",
                "--rootDir",
                "--target",
                "-t",
                "--module",
                "-m",
                "--moduleResolution",
                "--lib",
                "--types",
                "--typeRoots",
                "--baseUrl",
                "--jsx",
                "--declarationDir",
                "--tsBuildInfoFile",
            }
        ),
        mode="whole",
        disarm_flags=frozenset({"-b", "--build"}),
        example=ConfigOverrideExample(
            language="typescript",
            fence="json",
            config=(
                "{\n"
                '  "compilerOptions": { "strict": true, "paths": { "@/*": ["src/*"] } },\n'
                '  "include": ["src"]\n'
                "}"
            ),
            story=(
                "The project gate runs `npx tsc --noEmit` and is clean. The task's "
                "verify command runs `npx tsc --noEmit src/index.ts`; naming input "
                "files on the command line makes `tsc` ignore `tsconfig.json` "
                "entirely — `include`, `paths` and `strict` alike — so every "
                '`import ... from "@/lib/x"` fails with "Cannot find module '
                "'@/lib/x' or its corresponding type declarations\" on every "
                f"attempt. {_REMEDY}, which reads the project file."
            ),
        ),
    ),
    "rubocop": ConfigScopedTool(
        name="rubocop",
        sources=((".rubocop.yml", "AllCops", ("Exclude",)),),
        value_flags=frozenset(
            {
                "--config",
                "-c",
                "--only",
                "--except",
                "--format",
                "-f",
                "--out",
                "-o",
                "--require",
                "-r",
                "--cache",
                "--cache-root",
                "--fail-level",
                "--init",
                "--regenerate-todo",
                "--stdin",
                "-s",
                "--server",
                "--parallel-workers",
            }
        ),
        mode="exclude",
        example=ConfigOverrideExample(
            language="ruby",
            fence="yaml",
            config="AllCops:\n  Exclude:\n    - db/schema.rb",
            story=(
                'The project gate runs `bundle exec rubocop` and reports "no '
                "offenses detected\". The task's verify command runs "
                "`bundle exec rubocop app/models/order.rb db/schema.rb`; a file "
                "named explicitly on the command line is inspected even when "
                "`Exclude` lists it (that is what `--force-exclusion` exists to "
                "switch off), so the generated `db/schema.rb` — rewritten by the "
                "next `db:migrate` — reports offenses on every attempt. "
                f"{_REMEDY} and letting `.rubocop.yml` choose the files."
            ),
        ),
    ),
    # No config sources: Go's own tools have no configured file set (the
    # override there is a build tag, a flag) and golangci-lint honours its
    # exclusions whatever paths it is given — see the module notes. The
    # entry carries the ecosystem's worked example.
    "go": ConfigScopedTool(
        name="go",
        sources=(),
        value_flags=frozenset({"-tags", "-run", "-p", "-o", "-coverprofile"}),
        subcommands=frozenset({"test", "vet", "build"}),
        example=ConfigOverrideExample(
            language="go",
            fence="go",
            config=(
                "//go:build integration\n\n"
                "package store_test // internal/store/postgres_integration_test.go"
            ),
            story=(
                "The project gate runs `go test ./...` and passes: the build "
                "constraint keeps that file out unless the tag is set. The task's "
                "verify command runs `go test -tags integration ./...`, which pulls "
                "the constrained files in; they dial a database the sandbox does "
                'not have, so the command fails with "dial tcp 127.0.0.1:5432: '
                f'connect: connection refused" on every attempt. {_REMEDY}. The '
                "same shape reaches `go vet -tags` and `golangci-lint run "
                "--build-tags`."
            ),
        ),
    ),
}


def config_override_example(languages: Sequence[str] | None = None) -> str:
    """The rendered config-override example for a run's resolved toolchains.

    The first language in ``languages`` that has an example wins, so a mixed
    repository reads the example for its primary toolchain; a run whose
    languages carry none (or no languages at all) reads the Python one, the
    ecosystem the lint itself checks.
    """
    by_language = {
        tool.example.language: tool.example
        for tool in CONFIG_SCOPED_TOOLS.values()
        if tool.example is not None
    }
    for language in languages or ():
        if language in by_language:
            return by_language[language].render()
    return by_language["python"].render()


# Runner prefixes that stand in front of the tool word without changing it.
_RUNNER_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("uv", "run"),
    ("poetry", "run"),
    ("pdm", "run"),
    ("hatch", "run"),
    ("pipenv", "run"),
    ("npx",),
    ("npm", "exec"),
    ("pnpm", "exec"),
    ("pnpm",),
    ("yarn", "run"),
    ("yarn",),
    ("bundle", "exec"),
    ("dotnet", "tool", "run"),
    ("dotnet",),
)
# Directory names that read as a path even without a separator or extension.
_PATHY_WORDS = frozenset(
    {
        "src",
        "tests",
        "test",
        "packages",
        "lib",
        "app",
        "docs",
        "scripts",
        "cmd",
        "internal",
        "pkg",
        "spec",
        "crates",
        ".",
    }
)
_PATH_SUFFIXES = (
    ".py",
    ".pyi",
    ".toml",
    ".cfg",
    ".txt",
    ".go",
    ".rs",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".rb",
    ".rake",
    ".java",
    ".kt",
    ".cs",
    ".php",
    ".yml",
    ".yaml",
    ".json",
)
_GLOB_CHARS = ("/", "\\", "*", "?", "[")


def _config_tables(workspace: Path, filename: str) -> dict[str, dict[str, object]] | None:
    """Section -> key map for a config file, or None when it is unreadable.

    TOML sections are dotted paths (``tool.mypy``); INI sections are their
    literal header (``tool:pytest``). A bare ``""`` section means the file's
    top level (``ruff.toml``).
    """
    path = workspace / filename
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if filename.endswith((".toml", ".yml", ".yaml")):
        data: object
        try:
            data = tomllib.loads(text) if filename.endswith(".toml") else yaml.safe_load(text)
        except (tomllib.TOMLDecodeError, yaml.YAMLError):
            return None
        if not isinstance(data, dict):
            return None
        tables: dict[str, dict[str, object]] = {}

        def walk(node: dict[str, object], prefix: str) -> None:
            tables[prefix] = node
            for key, value in node.items():
                if isinstance(value, dict) and isinstance(key, str):
                    walk(value, f"{prefix}.{key}" if prefix else key)

        walk(data, "")
        return tables
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    return {section: dict(parser[section]) for section in parser.sections()}


def _configured_file_set(
    workspace: Path, tool: ConfigScopedTool
) -> tuple[str, str, tuple[str, ...]] | None:
    """``(config file, key, declared entries)`` for ``tool``, or None.

    A source with no keys is satisfied by the file's presence alone (the
    ``whole`` mode: tsc drops tsconfig.json whatever it says).
    """
    for filename, section, keys in tool.sources:
        if not keys:
            if (workspace / filename).is_file():
                return filename, "", ()
            continue
        tables = _config_tables(workspace, filename)
        if tables is None:
            continue
        table = tables.get(section)
        if not isinstance(table, dict):
            continue
        for key in keys:
            if key in table:
                return filename, key, _declared_entries(table[key])
    return None


def _declared_entries(value: object) -> tuple[str, ...]:
    """The configured paths, from a TOML list or an INI whitespace/comma list."""
    if isinstance(value, str):
        raw: list[str] = re.split(r"[,\s]+", value)
    elif isinstance(value, (list, tuple)):
        raw = [item for item in value if isinstance(item, str)]
    else:
        return ()
    return tuple(entry.strip() for entry in raw if entry.strip())


def _normalise_path_arg(word: str) -> str:
    """A path argument reduced to the file part: pytest node ids carry a
    ``::test_name`` suffix, and a trailing slash is not a difference."""
    return word.split("::", 1)[0].rstrip("/") or word


def _is_inside(path: str, entry: str) -> bool:
    """True when ``path`` cannot reach outside ``entry``.

    A narrowing argument (``pytest tests/unit`` under ``testpaths =
    ["tests"]``) selects a subset of what the config already selects, so it
    can never drag in a file the project excludes — the failure mode this
    rule exists for. Glob patterns are not resolved; an entry containing one
    is treated as not containing anything, which keeps the rule's default
    conservative.
    """
    if any(char in entry for char in ("*", "?", "[")):
        return False
    left = PurePosixPath(_normalise_path_arg(path).lstrip("./") or ".")
    right = PurePosixPath(entry.rstrip("/").lstrip("./") or ".")
    if right == PurePosixPath("."):
        return True
    return left == right or right in left.parents


def _looks_like_path(word: str) -> bool:
    if any(char in word for char in _GLOB_CHARS):
        return True
    if word in _PATHY_WORDS:
        return True
    return word.endswith(_PATH_SUFFIXES)


def _drop_dot_slash(path: str) -> str:
    while path.startswith("./"):
        path = path[2:]
    return path or "."


def _matches_exclusion(path: str, pattern: str, workspace: Path) -> bool:
    """Whether an explicitly named *file* falls under an exclusion glob.

    Rubocop's ``Exclude`` globs are relative to the config's directory and
    use ``**``; an entry with ERB in it cannot be evaluated here and is
    skipped. A directory argument is not an override — the tool expands it
    and applies the exclusions inside — so it never matches.
    """
    if "<%" in pattern:
        return False
    clean = _drop_dot_slash(_normalise_path_arg(path))
    if clean == "." or (workspace / clean).is_dir():
        return False
    target = PurePosixPath(clean)
    glob = _drop_dot_slash(pattern.rstrip("/"))
    try:
        return target.full_match(glob) or target == PurePosixPath(glob)
    except ValueError:
        return False


def _explicit_path_arguments(command: str, tool: ConfigScopedTool) -> list[str]:
    """Positional path-ish arguments given to ``tool`` in ``command``."""
    found: list[str] = []
    for segment in _SEGMENT_SPLIT.split(command):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        words = [word for word in words if not _ENV_ASSIGNMENT.match(word)]
        index = _tool_index(words, tool.name)
        if index is None:
            continue
        rest = words[index + 1 :]
        if any(
            word in tool.disarm_flags or word.split("=", 1)[0] in tool.disarm_flags for word in rest
        ):
            continue
        i = 0
        while i < len(rest):
            word = rest[i]
            if word.startswith("-"):
                if "=" not in word and word in tool.value_flags:
                    i += 2
                    continue
                i += 1
                continue
            if word in tool.subcommands or word in tool.benign_args:
                i += 1
                continue
            if _looks_like_path(word):
                found.append(word)
            i += 1
    return found


def _tool_index(words: Sequence[str], tool: str) -> int | None:
    """Index of ``tool`` when it heads the segment, after any runner prefix."""
    for start in range(len(words)):
        word = words[start]
        base = word.rsplit("/", 1)[-1]
        if base == tool and (start == 0 or _is_runner_prefix(words[:start])):
            return start
    return None


def _is_runner_prefix(words: Sequence[str]) -> bool:
    for prefix in _RUNNER_PREFIXES:
        if len(words) >= len(prefix) and tuple(words[-len(prefix) :]) == prefix:
            return True
    return False


def config_override_problems(command: str, workspace: Path | None) -> list[str]:
    """Violations where a path argument overrides a tool's configured files.

    ``workspace`` defaults to the current directory: the lint runs from the
    run's workspace root, and a missing root should not silently switch the
    rule off.
    """
    root = Path.cwd() if workspace is None else workspace
    problems: list[str] = []
    for tool in CONFIG_SCOPED_TOOLS.values():
        paths = _explicit_path_arguments(command, tool)
        if not paths:
            continue
        declared = _configured_file_set(root, tool)
        if declared is None:
            continue
        filename, key, entries = declared
        bare = _bare_form(command, tool.name)
        if tool.mode == "whole":
            shown = ", ".join(f"`{path}`" for path in paths)
            problems.append(
                f"verify command `{command}` names input file(s) {shown} for "
                f"`{tool.name}` — this project configures {tool.name} in "
                f"`{filename}`, and input files on the command line make "
                f"{tool.name} IGNORE that file entirely (its `include`, "
                f"`compilerOptions` and `paths` alike), so the command checks a "
                f"different program from the one the project builds and can fail "
                f"on work that is correct. Drop the path(s) and use the bare "
                f"form: `{bare}` (a different project file is `--project "
                f"<file>`, never a list of inputs)"
            )
            continue
        if tool.mode == "exclude":
            overridden = [
                path
                for path in paths
                if any(_matches_exclusion(path, entry, root) for entry in entries)
            ]
            if not overridden:
                continue
            shown = ", ".join(f"`{path}`" for path in overridden)
            excluded = ", ".join(f"`{entry}`" for entry in entries)
            problems.append(
                f"verify command `{command}` names {shown} explicitly for "
                f"`{tool.name}` — this project excludes it in `{filename}` "
                f"(`{key}` = {excluded}), and a file named on the command line "
                f"is inspected even when excluded (that is what "
                f"`--force-exclusion` exists to switch off), so the command "
                f"checks a file the project deliberately excludes — generated "
                f"code, usually — and can fail on work that is correct. Drop the "
                f"path and use the bare form: `{bare}` (a directory argument is "
                f"fine — the exclusions still apply inside it)"
            )
            continue
        # A path already inside the configured set only narrows the run: it
        # cannot pull in a file the project excludes, which is the whole
        # failure this rule guards. `uv run pytest tests/unit` and `uv run
        # mypy packages/sbxloop/src/...` are the ordinary faster-signal
        # invocations and must stay legal; `uv run mypy packages` (a parent
        # of the configured `packages/*/src`) still fires.
        outside = [path for path in paths if not any(_is_inside(path, entry) for entry in entries)]
        if not outside:
            continue
        shown = ", ".join(f"`{path}`" for path in outside)
        configured = ", ".join(f"`{entry}`" for entry in entries) or f"`{key}`"
        problems.append(
            f"verify command `{command}` passes explicit path(s) {shown} to "
            f"`{tool.name}` — this project configures {tool.name}'s file set in "
            f"`{filename}` (`{key}` = {configured}), an explicit path argument "
            f"OVERRIDES it, and {shown} is not inside the configured set, so the "
            f"command checks files the project deliberately excludes and can "
            f"fail on work that is correct. Drop the path and use the bare form: "
            f"`{bare}` (a path *inside* the configured set is fine — it only "
            f"narrows the run)"
        )
    return problems


def _bare_form(command: str, tool: str) -> str:
    """The command's own invocation of ``tool`` with its path arguments gone
    — the flags stay (`npx tsc --noEmit src/index.ts` → `npx tsc --noEmit`),
    so the remedy is the command the author meant, minus the override."""
    scoped = CONFIG_SCOPED_TOOLS.get(tool)
    for segment in _SEGMENT_SPLIT.split(command):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        index = _tool_index(words, tool)
        if index is None:
            continue
        kept = words[: index + 1]
        rest = words[index + 1 :]
        i = 0
        while i < len(rest):
            word = rest[i]
            if word.startswith("-"):
                kept.append(word)
                if scoped is not None and "=" not in word and word in scoped.value_flags:
                    kept.extend(rest[i + 1 : i + 2])
                    i += 2
                    continue
            elif (
                scoped is not None and (word in scoped.subcommands or word in scoped.benign_args)
            ) or not _looks_like_path(word):
                kept.append(word)
            i += 1
        return shlex.join(kept)
    return tool


def lint_verify_commands(
    commands: Sequence[str],
    languages: Sequence[str],
    *,
    uv_project: bool = False,
    gate: str | None = None,
    workspace: Path | None = None,
) -> list[str]:
    """Violation messages for ``commands`` under the run's toolchains.

    Empty means clean. Messages are written to be fed back to the model
    verbatim: they name the offending command, the bare word, and the
    ecosystem's remedy. ``uv_project`` says the workspace carries a
    ``uv.lock``, which swaps the Python convention from ``.venv/bin/...``
    to ``uv run`` (see the module docstring). ``gate`` is the project's own
    full check (see :func:`project_gate`); when the project declares one,
    the verify commands must run it. ``workspace`` is the run's workspace
    root, read for the tools' configured file sets; it defaults to the
    current directory.
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
        problems.extend(config_override_problems(command, workspace))
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
    problems.extend(gate_problems(commands, gate))
    return problems


def gate_problems(commands: Sequence[str], gate: str | None) -> list[str]:
    """The "no command runs the project gate" violation, if it applies.

    Split out so a caller checking the gate across *all* tasks' commands can
    do that without re-running the per-command rules (which would report
    every other violation a second time).
    """
    if not gate or any(runs_gate(unwrap_inert_shell(command), gate) for command in commands):
        return []
    return [
        f"none of the verify commands runs `{gate}`, this project's own gate — "
        "the checks it bundles are what CI enforces on the pull request, so "
        "work that skips it lands red. Add it as a verify command; keep the "
        "narrower ones too if they give a faster signal."
    ]
