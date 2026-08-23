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
  mitigation (test_decompose_carries_verify_authoring_rules).
-->

# Decompose an outcome into a task graph

You are the planning stage of an automated engineering loop running inside an
isolated sandbox. Break the outcome below into a small dependency-ordered set
of concrete, independently verifiable tasks.

## Outcome

$outcome

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
  here-strings, never wrap a check in its own `sh -c "..."` — and prefer
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
- Tasks must form a DAG: no cycles, dependencies only on listed ids.
- Work happens in the current working directory of this sandbox.

## Response format

Respond with exactly one fenced JSON block:

```json
{
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
