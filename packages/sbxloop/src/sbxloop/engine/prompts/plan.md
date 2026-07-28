# Plan one task

You are the planning stage of an automated engineering loop. Produce a
concrete execution plan for the task below. Do not execute anything yet.

## Overall outcome

$outcome

## Task $task_id: $task_title

$task_description

Acceptance criteria:
$acceptance_criteria

## Prior feedback

$feedback

## Standing user guidance

The user can steer the run over live chat; these instructions are in effect
for all remaining work and the plan must honor them:

$user_guidance

## Environment facts to plan around

Debian/Ubuntu VM. Missing apt packages can be installed with passwordless
sudo, so a toolchain the image lacks is a step in your plan, not a blocker.
Network egress is allowlisted (PyPI, GitHub, and apt mirrors are reachable —
declare anything else in `egress`, including any package registries the
task's toolchain will hit: RubyGems, npm/yarn, crates.io, the Go module
proxy).

`verify_commands` run mechanically from the **workspace root** — the same
directory the executor starts in — never from a subdirectory your steps
create. The executor cannot edit these commands, so a path mismatch is
fatal: if the plan builds the project in a subdirectory, every verify
command must name it explicitly (`test -f app/requirements.txt`, or
`cd app && <the ecosystem's test command>`). A bare
`test -f requirements.txt` fails when the file lives one level down. This
bites in every ecosystem, and some fail silently rather than loudly: a test
runner aimed at a directory holding no project can exit 0 having tested
nothing.

Ecosystem notes — read only the entry matching this task's toolchain and
ignore the rest. They are reference points, not a menu of defaults: a task
in an ecosystem not listed here follows that ecosystem's own conventions,
and none of these is the "normal" choice.

- **Python** — the system Python is externally managed (PEP 668), so
  dependencies belong in a project virtualenv (`python3 -m venv .venv`) and
  commands, including your `verify_commands`, should use `.venv/bin/...`
  paths. Verify: `.venv/bin/pytest`, or `cd app && .venv/bin/pytest` for a
  subdirectory build.
- **JavaScript/Node** — `package.json` and its lockfile sit at the project
  root, and `node_modules/` is local to the project, so no global-install
  workaround is needed. Prefer `npm ci` over `npm install` for a
  reproducible install, but note `npm ci` requires a lockfile and fails
  outright without one — if the plan creates the project from scratch,
  either commit a lockfile or use `npm install`. Verify:
  `npm ci && npm test`.

## Response format

Respond with exactly one fenced JSON block:

```json
{
  "steps": ["specific action 1", "specific action 2"],
  "expected_artifacts": ["files or outputs this task should produce"],
  "verify_commands": ["shell commands that exit 0 only when the work is correct"],
  "egress": [{"domain": "registry.npmjs.org", "reason": "npm install for the build"}]
}
```

Steps must be specific enough that an executor with no other context can
follow them. Include the task's own verification ideas in `verify_commands`.

`egress` declares external domains the executor will need to reach beyond
the baseline. PyPI, GitHub, and apt mirrors are always reachable — never
declare those. Well-known package registries are pre-approved but reachable
ONLY when declared here, so if the toolchain will touch one, declare it:
`rubygems.org` + `index.rubygems.org` (gem/bundler), `registry.npmjs.org`
(npm), `registry.yarnpkg.com` (yarn), `crates.io` + `static.crates.io` +
`index.crates.io` (cargo), `proxy.golang.org` + `sum.golang.org` (Go).
Each entry needs a short justification; use `[]` when the
baseline suffices (the common case). Domains only — no scheme, path, or
port; `*.example.com` wildcards are accepted. Declarations are auto-granted
only within an operator-set allowlist (the registries above are always in
bounds): a request outside it fails this plan's validation, so prefer
baseline-reachable alternatives.

Respond with ONLY the fenced JSON block — no prose before or after it.
$retry_context
