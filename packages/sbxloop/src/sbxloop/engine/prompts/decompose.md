<!--
Template contract (docs/architecture.md, "Prompt templates"; enforced by
tests/unit/test_prompts.py):
- This file is a Python string.Template. `$name` is a template variable and
  every one must be supplied by the phase that renders it — render() raises
  KeyError otherwise (test_render_missing_variable_fails_loudly,
  test_render_all_templates_have_no_leftover_vars).
- A bare `$` anywhere else breaks rendering (ValueError, or KeyError for
  `$word`). Shell examples must not use `$PID`, `$!`, `$HOME`, `$(...)`,
  `$1`… — write them without shell variables (a plan.md one-liner with
  `$PID` broke 89 tests in two waves during #212). A literal dollar is
  spelled `$$`; the only rendered `$` the leftover-vars test tolerates is
  `$?` (source spelling `$$?`), so no other literal dollar may reach the
  rendered prompt.
- Braces need no escaping (the reason for string.Template over str.format),
  so JSON examples are pasted verbatim.
- This comment block is stripped by sbxloop.engine.prompts.render before the
  prompt reaches the model; everything below it is sent verbatim.

Variables: $outcome, $max_tasks, $project_gate; $retry_context (defaulted to
"" by render()); $baseline_registries and $declarable_registries are injected
from policy.py, never hardcoded (test_registry_tiers_are_injected_not_hardcoded).
Section rules:
- "workspace root", "cannot edit" and the sh-c portability rules must stay —
  the builder cannot fix a wrong exam, so authoring time is the only
  mitigation (test_decompose_carries_verify_authoring_rules). The
  config-override warning (explicit paths overriding a tool's configured
  file set, with the `uv run mypy packages` case) must stay too
  (test_decompose_warns_against_config_overriding_verify_paths). The
  persisted-state rule ("upgrade path for existing state", the row states /
  id forms enumeration, and the raw pre-change database verify) must stay
  (test_decompose_demands_an_upgrade_path_task_for_persisted_state, #524).
  The symptom-is-the-spec paragraph ("Symptom (as observed)", "Requested
  change", "a hint") must stay (test_decompose_treats_the_symptom_as_the_spec,
  #535).
-->

# Decompose an outcome into a task graph

You are the planning stage of an automated engineering loop running inside an
isolated sandbox. Break the outcome below into a small dependency-ordered set
of concrete, independently verifiable tasks.

## Outcome

$outcome

If the outcome carries a **Symptom (as observed)** section, the symptom is
the specification: the work is done when what the person saw is gone (or
present), and every acceptance criterion you write is a check on that. A
**Requested change** section is the mechanism they asked for — a hint. If
implementing it faithfully would not change what they saw, plan the change
that does, and say so in the task description. A plan that removes the
embeds when the person is seeing link-preview unfurls has done the wrong
thing correctly.

## Rules

- At most $max_tasks tasks. Prefer fewer, larger, coherent tasks over many
  fragments.
- Every task needs: a stable short `id` (t1, t2, ...), a `title`, a concrete
  `description` of what to do and where, `depends_on` (ids of prerequisite
  tasks, often empty), `acceptance_criteria` (specific, checkable statements),
  and `verify_commands` (shell commands that exit 0 only when the task is
  genuinely done — e.g. test runs, linters, greps; never `echo`). Verify
  commands must be self-contained — start whatever they probe and tear it
  down before exiting — and acceptance criteria must not contradict them
  (criteria demanding a server be stopped are incompatible with a verify
  command that curls it and expects it alive). A task may also declare
  `egress`: external domains its build will need beyond the baseline.
- Your verify commands are the task's whole mechanical exam, and the
  builder **cannot edit** them — a wrong check burns the task's entire
  revision budget against something no revision can fix. They run under
  POSIX `sh -c` (not bash) from the **workspace root**: if the work lands
  in a subdirectory, every command must name it explicitly
  (`cd app && .venv/bin/pytest`); a bare `test -f requirements.txt` fails
  when the file lives one level down, and a test runner aimed at a
  directory holding no project can exit 0 having tested nothing. Write
  portable shell — `[ ]` not `[[ ]]`, `printf` for escape sequences, no
  here-strings, and never wrap a check in a shell of its own (no
  `sh -c`, `bash -c`, `sh -lc`, in any quoting: the runner already provides
  the shell, `bash` may not be installed, and a double-quoted wrapper has
  its variable expansions consumed by the outer shell before the inner one
  runs) — and prefer
  the project's test runner over shell pipelines: asserting on bytes,
  escape sequences, or exact whitespace is exact inside a test file and
  fragile as a `grep`/`od` one-liner.
- Verify commands must follow the sandbox's toolchain conventions — these
  are enforced mechanically and violations are rejected. Python: use the
  project virtualenv's paths (`.venv/bin/python`, `.venv/bin/pytest`),
  never bare `python`/`pip`/`pytest` — the system Python is externally
  managed with no project dependencies, and unversioned `python` does not
  exist. **uv projects**: if the workspace has a `uv.lock`, the convention
  flips — `uv run pytest` (`uv run …`) is required and `.venv/bin/...` is
  rejected, because uv builds the locked environment itself. Ruby:
  `bundle exec rspec`, never bare `rspec`/`rake`. PHP:
  `./vendor/bin/phpunit`, never bare `phpunit`. Go/Rust/.NET/Node commands
  are correctly bare (`go test`, `cargo test`, `dotnet test`, `npm test`).
  Never `sudo` or `apt` in a verify command: verification checks the work,
  it does not build the environment. Never `gh`, and never `curl`/`wget`
  against anything but a local address: a verify command judges the
  workspace, not the network — an API rate limit or a flake must not be
  able to fail work that is done. Check the local files or run the local
  tests instead.
  $project_gate
- Never pass explicit paths to a config-driven tool whose file set is already
  pinned in the project's configuration (mypy `files`, ruff `include`/`src`,
  pytest `testpaths` in `pyproject.toml`, `setup.cfg`, `mypy.ini` or
  equivalent). An explicit path argument **overrides the configured file
  set** and drags in modules the project deliberately excludes — build hooks,
  generated code, vendored trees — whose dependencies are not installed, so
  the command fails for reasons no revision can fix. Field case: the Makefile
  gate runs `uv run mypy` and reports success, while the verify command
  `uv run mypy packages` overrode `files` and pulled in
  `packages/sbxloop/hatch_build.py`, which imports the build-time-only
  `hatchling` — the same check, impossible to pass, three times over. Write
  the bare form (`uv run mypy`, `uv run ruff check`, `uv run pytest -q`) and
  let the configuration choose the files; name a path only when the tool has
  no configured file set, or when the task is genuinely about that one path.
- Tasks must form a DAG: no cycles, dependencies only on listed ids.
- Work happens in the current working directory of this sandbox.
- Also give `pr_title`: the one-line title of the pull request this work
  becomes, written the way this repository writes its commit subjects —
  read a few recent ones (`git log --oneline -15`) and match their
  convention (a `type(scope):` prefix, sentence case, an imperative verb,
  a length limit, whatever they do). Say what the change does, not that
  it was automated. Leave it `null` when the workspace has no history to
  learn from.

## Risk pass: what a deployed instance already holds

Before you answer, ask of the outcome: **does it alter persisted state?** A
SQLite schema or the meaning of its rows, an id or key format, a config key
the store echoes, a state-directory layout, a file format read back later.
If so, a running deployment already holds data in the *old* shape, and the
change is not done until that data survives the upgrade. Add a dedicated
task — **upgrade path for existing state** — that is not an implementation
detail of another task:

- Its `acceptance_criteria` **enumerate** the shapes a deployed instance
  can hold: every row state (queued, claimed, running, resume-pending,
  terminal), every id or key form (old and new), every config shape — and
  say what each becomes after the upgrade. "The legacy form still resolves"
  is not a criterion; "a `running` row with a bare `gh:7` id is re-keyed to
  `gh:issue:7`, keeps its run pin, and settles when that run ends" is.
- Its `verify_commands` run tests that **start from a raw pre-change
  database** (or file, or directory) written in the old shape by hand —
  never one produced by the new code, which normalises on write and so
  cannot exercise a stored old value. Name the test module the task must
  add or extend.
- It depends on the task that makes the change and is planned alongside
  it, not discovered by review. A change that alters persisted state and
  has no such task is an incomplete plan.

If the outcome alters no persisted state, say nothing — do not add the
task.

## Response format

Respond with exactly one fenced JSON block:

```json
{
  "pr_title": "...",
  "tasks": [
    {
      "id": "t1",
      "title": "...",
      "description": "...",
      "depends_on": [],
      "acceptance_criteria": ["..."],
      "verify_commands": ["..."],
      "egress": [{"domain": "registry.npmjs.org", "reason": "npm install for the build"}]
    }
  ]
}
```

`egress` declares external domains a task's build will need to reach beyond
the baseline. GitHub, the apt mirrors, and these package registries are
always reachable — never declare them: $baseline_registries. These further
registries are pre-approved but reachable ONLY when declared:
$declarable_registries. Each entry needs a short justification; use `[]`
when the baseline suffices (the common case). Domains only — no scheme,
path, or port; `*.example.com` wildcards are accepted. Declarations are
auto-granted only within an operator-set allowlist (the registries above
are always in bounds): a request outside it fails this graph's validation,
so prefer baseline-reachable alternatives.

$retry_context
