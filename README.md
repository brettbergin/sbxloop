# sbxloop

[![CI](https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml/badge.svg)](https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sbxloop)](https://pypi.org/project/sbxloop/)
[![Python](https://img.shields.io/pypi/pyversions/sbxloop)](https://pypi.org/project/sbxloop/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Agentic loop orchestration on [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`), with hard credential isolation.**

sbxloop turns a large outcome ("migrate this service to async", "add coverage
to every untested module") into a supervised agentic loop: it **decomposes**
the outcome into a task graph, then for each task
**plans → executes → scrutinizes → verifies → validates**, with
revision/replan budgets, checkpointing, resume, artifact harvesting, and
optional delivery of the results as a GitHub pull request.

## The primitive: a sandbox pair

Every run gets an isolated microVM agent sandbox — plus, when the GitHub integration is configured, a second github-ops sandbox, so no single environment ever holds both credentials:

| Sandbox                | Credential                                                               | Purpose                                                                                                                                             |
| ---------------------- | ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sbxloop-<run>-agent`  | `COPILOT_GITHUB_TOKEN` (fine-grained PAT, *Copilot Requests* permission) | Runs the [GitHub Copilot SDK](https://github.com/github/copilot-sdk) agentic layer. All model calls and tool executions happen inside this VM.      |
| `sbxloop-<run>-github` | `GH_TOKEN` (fine-grained PAT: issues write, contents read, …)            | Performs user-facing GitHub operations (issues, PRs, statuses) against the one configured repository. Only provisioned when `[github] repo` is set. |

Both sandboxes run under sbx's **balanced network policy** (default-deny
egress plus a curated allowlist), and tokens are injected through sbx's secret
proxy — **credential values never enter the VM**; the host proxy substitutes
them only on egress to their declared domains. Sandboxes are cattle: they are
torn down at run end and re-provisioned on resume, while all durable state
(workspace, SQLite checkpoints, event log) lives on the host.

## Quickstart

```bash
pip install sbxloop

# one-time host setup
sbx login
sbx policy init balanced
sbxloop doctor          # verifies sbx, policy, tokens, worker wheel
sbxloop doctor --deep   # + full sbx conformance suite in a scratch sandbox

# go
sbxloop run "Add mypy strict typing to every module in ./src and fix all findings"

# while it runs / afterwards
sbxloop status                  # all runs; `sbxloop status <run>` for one run's tasks
sbxloop logs <run>              # the persisted event stream
sbxloop artifacts <run> --tree  # what the run produced
```

`run` opens a live chat-style dashboard by default: agent messages as
markdown panels, tool calls as compact lines, lifecycle events as dim
one-liners. `--no-tui` prints the same transcript sequentially (good for CI
logs); the full raw event stream is always available via `sbxloop logs`.

Optional, but cuts provisioning latency a lot: bake a sandbox template with
the worker preinstalled once, instead of installing it on every run.

```bash
sbxloop bake            # installs the worker + Copilot runtime into a template
# then set in sbxloop.toml:
#   [sandbox]
#   template = "sbxloop-baked:latest"
```

Runs verify the baked worker with fast probes and fall back to the normal
install if the template is stale (`sbxloop doctor` will tell you to re-bake
after upgrading sbxloop).

Wondering what to put in `model = "..."` (or `--model`)? Ask the Copilot SDK
which models your subscription can actually use:

```bash
pip install 'sbxloop[copilot]'   # the SDK is optional on the host
sbxloop list-models              # id, billing multiplier, context, reasoning, policy
sbxloop list-models --json       # machine-readable, for scripting
```

Or as a library:

```python
from sbxloop import LoopEngine, load_config

engine = LoopEngine(config=load_config())
result = engine.start(outcome="Add mypy strict typing to ./src and fix all findings")
print(result.state, result.run_id)
```

## How a run works

```
outcome ──▶ DECOMPOSE (task DAG) ──▶ for each task:
              PLAN ─▶ EXECUTE ─▶ SCRUTINIZE ─▶ VERIFY ─▶ VALIDATE ─▶ done
                        ▲             │revise            │fail        │reject
                        └─────────────┴──────────────────┘            │
                        └── replan ◀──────────────────────────────────┘
```

- **Plan** — produces the task's approach, its `verify_commands`, and
  (optionally) declared network egress needs (see
  [Network egress](#network-egress-least-privilege-by-plan)).
- **Execute** — the Copilot agent session does the work in the sandbox
  workspace.
- **Scrutinize** — a fresh, read-only critic session reviews the diff and
  artifacts. The read-only barrier is allowlist + default-deny: critic
  sessions may only use known-read capabilities, so an SDK change can never
  silently hand a critic write access to the workspace it reviews.
- **Verify** — mechanical: the task's `verify_commands` must exit 0, run from
  the workspace root. No LLM. The full command transcript is persisted with
  the attempt, so a resumed run judges with the real evidence.
- **Validate** — a fresh read-only session judges the acceptance criteria.

**Budgets, not vibes.** Revisions, replans, task count, and wall clock are all
bounded (`[budgets]` in config; defaults: 2 revisions and 1 replan per task,
20 tasks, 2 h wall clock, 15 min per job). Budget exhaustion fails the *task*;
its dependents are skipped and the run continues, finishing `failed` if any
task failed. One deliberate exception: when revisions are exhausted by
*verify-command* failures, the task spends a replan first when budget
remains — the executor cannot edit verify commands, so only a fresh plan can
unstick a broken check.

**Checkpointing and resume.** State is committed to SQLite after every
transition. `sbxloop resume <run>` re-provisions a fresh sandbox pair and
continues from the last committed transition — under the **run's persisted
config**, not whatever is on disk at resume time. The workspace is pinned from
the state DB (a mismatch refuses to resume), and any difference from the
current on-disk config is surfaced as a `run.config_drift` event. The one
exception: the debug toggles (`keep_sandboxes` / `keep_on_failure`) stay
resume-time choices, so a crashing run can be resumed with keep flipped on in
config or env.

**Guardrails.** The worker heartbeat samples in-VM disk and memory
(`[limits]`; defaults warn at 85 % disk / 90 % memory and abort the task at
95 % disk), so a runaway task fails with "sandbox disk exhausted" instead of
letting in-VM tooling fail confusingly on a full disk.

## CLI reference

| Command                               | What it does                                                                                                   |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `sbxloop run "OUTCOME"`               | Start a run. Options: `--report`, `--deliver`, `--model`, `--keep-sandboxes`, `--keep-on-failure`, `--no-tui`. |
| `sbxloop resume RUN`                  | Re-provision sandboxes and continue a checkpointed run under its persisted config.                             |
| `sbxloop cancel RUN`                  | Cancel an in-flight run.                                                                                       |
| `sbxloop status [RUN]`                | List runs, or show one run's task/phase detail.                                                                |
| `sbxloop logs RUN`                    | The persisted event stream. `--type` filters by prefix (e.g. `--type policy.`), `--task` by task id.           |
| `sbxloop artifacts RUN`               | List a run's harvested files. `--tree` renders a tree; `--path` prints just the directory (for scripting).     |
| `sbxloop shell RUN`                   | Interactive shell in a run's sandbox. `--role agent\|github` picks the pair member; `-c CMD` runs one command. |
| `sbxloop init`                        | Write a commented starter `sbxloop.toml` (`--force` overwrites).                                               |
| `sbxloop bake`                        | Bake a sandbox template with the worker preinstalled (`--ref`, `--from`, `--keep`).                            |
| `sbxloop doctor [--deep]`             | Verify the host setup; `--deep` boots a scratch sandbox for the full sbx conformance suite.                    |
| `sbxloop sandbox ls\|rm\|prune`       | Inspect, remove (`--run`, `--all`), or garbage-collect orphaned sbxloop sandboxes.                             |
| `sbxloop secrets list\|clean\|rotate` | Manage the sbx custom-secret registrations sbxloop owns.                                                       |
| `sbxloop config show\|policy`         | Resolved configuration with per-key sources; the effective egress policy.                                      |

## Network egress: least privilege, by plan

Sandboxes start with only the baseline allowlist (Copilot/GitHub hosts, PyPI,
apt mirrors). Well-known package registries — RubyGems, npm/yarn, crates.io,
the Go module proxy — are one notch wider: not reachable by default, but a
plan may declare them in `egress` with no configuration, so `bundle install`
or `npm install` works out of the box while every grant still lands in the
audit log. Anything else the PLAN phase declares — each domain with a
justification — is validated against operator-set bounds:

```toml
# sbxloop.toml
[policy]
allow = ["repo.maven.apache.org", "repo1.maven.org"]  # what plans MAY request
deny  = []                                # never grantable, even if allowed
```

Patterns are exact domains, `*.example.com` wildcards, or `*`. Empty `allow`
(the default) means plans may only use the baseline and the well-known
registries. In-bounds grants are
applied **grant-late** — `sbx policy allow network` runs at EXECUTE entry, so
resumed runs re-grant on their fresh sandboxes — and every grant and refusal
is a `policy.allow` / `policy.deny` run event, making the persisted event log
an egress audit trail:

```bash
sbxloop logs <run> --type policy.   # who asked for what, and what was granted
sbxloop config policy               # the effective per-phase policy
```

Out-of-bounds requests fail plan validation with a remediation hint. Static
extras that every run should have go in `[sandbox] extra_allow_domains`.

### Chatting with a running loop

A run is not read-only: type a message into the TUI's input line (Enter to send)
and the agent pauses at the next checkpoint — the same phase boundary
cancellation uses — to answer it in a fresh **read-only STEER session** that can
inspect the workspace. The reply lands in the transcript, and the agent decides
what your message means for the work:

- **continue** — a question or status check; it answers and carries on.
- **steer task** — the current task is re-planned immediately with your guidance
  as feedback (user direction spends no revision/replan budget).
- **steer run** — your guidance becomes a standing instruction injected into
  every later planning/execution prompt, persisted so `sbxloop resume` keeps it.

Messages queue while a phase is in flight (the status panel shows them), every
chat turn is a persisted event (`sbxloop logs <run> --type chat.`), and
`--no-chat` disables the input entirely. With `--no-tui`, plain line input on
stdin does the same job.

## Artifacts

Every job in a run executes in the run's **workspace** — a host directory
(`.sbxloop/runs/<run>/workspace`) that sbx mounts into the agent microVM.
Provisioning *discovers* the in-VM mount point (marker file + bounded search)
rather than assuming one; when the mount can't be found, jobs run in a
fallback dir that is **harvested** to `.sbxloop/runs/<run>/artifacts` with
`sbx cp` at each task end and at run finalize. Either way the files an agent
produces survive the sandbox:

```bash
sbxloop run "write a fib.py with tests"   # summary ends with an artifact tree
sbxloop artifacts <run>                   # list a past run's files (--tree for a tree)
cat "$(sbxloop artifacts <run> --path)/fib.py"
```

## GitHub integration

sbxloop has **no** GitHub capability until you configure the one repository it
may work with:

```toml
# sbxloop.toml
[github]
repo = "you/your-repo"   # the ONE repo sbxloop may act on
report = false           # post run progress as a tracking issue (or `--report`)
deliver = false          # PR the run's artifacts to the repo (or `--deliver`)
deliver_base = ""        # base branch for delivery PRs (default: repo default)
deliver_draft = false    # open delivery PRs as drafts
```

With `repo` set, runs provision the github-ops sandbox and require a second
PAT, `GH_TOKEN`, with the repository permissions you want sbxloop to act with
— used *only* by that sandbox. Without it, no github sandbox exists,
`GH_TOKEN` is not needed, and repo-facing features refuse to run.

- **`--report`** opens a tracking issue at run start, comments as tasks
  finish, and posts the final summary before teardown. A resumed run re-finds
  its existing issue instead of opening a duplicate.
- **`--deliver`** publishes a completed run's artifacts as a pull request:
  one atomic commit via the git data API, branch `sbxloop/<run>`, through the
  github-ops sandbox (`GH_TOKEN` only). Needs `contents:write` +
  `pull_requests:write` on the repo. Delivery runs after the run has already
  succeeded; delivery failures are reported loudly (`run.deliver` event) but
  never fail a completed run.

## Debugging failed runs

By default sandboxes are torn down at run end — including failed runs, which
is exactly when the in-sandbox evidence (worker stderr, install leftovers,
workspace state) matters most. Two levers:

```toml
# sbxloop.toml
keep_on_failure = true   # keep the pair alive only when a run fails (or --keep-on-failure)
keep_sandboxes = true    # keep it always (or --keep-sandboxes)
```

A failed run then ends with a prominent hint naming the kept sandboxes, and
`sbxloop shell` drops you inside — kept, in-flight, or leaked:

```bash
sbxloop shell <run>                    # interactive shell in the agent sandbox
sbxloop shell <run> --role github      # ... or the github-ops sandbox
sbxloop shell <run> -c 'cat ~/.sbxloop/env.sh'   # one-off command
```

Attaching to an in-flight run is meant as observation — the worker owns its
env files and workspace, so avoid mutating them mid-phase. Kept runs are
marked in the state DB (`kept_reason`) and stay exempt from `sandbox prune`
until you pass `--include-kept`, so debugging convenience cannot become a
permanent leak.

One transcript signature worth knowing: agent `glob`/`grep` calls failing
with `<jemalloc>: Unsupported system page size` mean the guest's page size
is not the 4 KiB the Copilot CLI's bundled ripgrep was compiled for (16 KiB
guests are common on Apple-silicon hosts). sbxloop handles this
automatically — the worker reroutes glob/grep to a system ripgrep
(`USE_BUILTIN_RIPGREP=false`) and provisioning apt-installs `ripgrep` on
such guests — so seeing the abort means the fallback had no `rg` to land
on: look for a `sandbox.tooling_warning` event in `sbxloop logs`, and check
the `page-size` probe under `sbxloop doctor --deep`.

## Language toolchains

The agent builds a project inside its sandbox, so whatever that project needs
to compile has to be there. `[sandbox] languages` says which toolchains get
installed before the agent's first turn, instead of the agent discovering a
missing compiler on its first build and spending revision budget on it:

```toml
[sandbox]
languages = ["python"]   # the default when the key is unset
```

| Value    | Also accepts               | Installs                                                      |
| -------- | -------------------------- | ------------------------------------------------------------- |
| `python` | `py`, `python3`            | `python3-venv`, `python3-pip` (apt)                           |
| `cpp`    | `c`, `c++`, `cxx`, `c-cpp` | `build-essential`, `cmake`, `ninja-build`, `pkg-config` (apt) |

Three rules apply to every entry. Provisioning is **probe-first** — a template
that already ships the toolchain costs no install and no network. It is
**never fatal** — a failure warns with the toolchain named and the run
continues, since the agent has passwordless `sudo apt-get` as an escape
hatch. And it is **opt-in** — setting `languages` replaces the default rather
than adding to it, so nothing is installed for a language you did not ask
for. Heavier toolchains are better baked into a template (`sbxloop bake`)
than downloaded per run.

## Sandbox hygiene

Sandboxes are torn down at run end, and an in-process registry also cleans up
on Ctrl-C/SIGTERM — but a host crash or `kill -9` can still leak a run's
microVM pair. `sbxloop sandbox prune` garbage-collects those orphans by
cross-referencing `sbx ls` against the state DB:

```bash
sbxloop sandbox prune            # dry run: classify every sbxloop sandbox
sbxloop sandbox prune --force    # actually remove the orphan candidates
```

A sandbox counts as an orphan candidate when its run is terminal
(completed/failed/cancelled), unknown to this working copy's state DB, or
non-terminal but silent past `--min-age` (default 1 hour — the persisted event
stream, heartbeats included, is the liveness signal). Sandboxes deliberately
kept for debugging are excluded unless you pass `--include-kept`. `sbxloop doctor` reports the current orphan-candidate count.

## Setup

1. Install [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/), then
   `sbx login` and `sbx policy init balanced`.

2. Create a fine-grained GitHub PAT:

   - `COPILOT_GITHUB_TOKEN` — personal account, **Copilot Requests**
     permission. Used *only* by the agent sandbox.

   Export it, or put it in a `.env` file (loaded automatically from the
   working directory; real environment variables always win):

   ```bash
   cp .env.example .env   # then fill in the token(s)
   ```

3. **Optional** — configure the [GitHub integration](#github-integration)
   (adds the second PAT, `GH_TOKEN`).

4. `sbxloop doctor` verifies all of it and prints remediation for anything
   missing.

### Doctor and the sbx conformance suite

Every empirically-learned assumption sbxloop makes about sbx semantics
(secret visibility under `exec`, `cp` directory semantics, workspace-mount
discovery, custom-secret keying, whether `secret set-custom` has grown a
stdin path yet, …) is a named probe with a machine-checkable verdict, cached
per `sbx` version. `sbxloop doctor` runs the cheap probes and serves
live-sandbox verdicts from the cache; `sbxloop doctor --deep` boots one
scratch sandbox for the full suite. When an sbx upgrade flips a verdict that
sbxloop's behavior depends on, doctor warns loudly and names the dependent
behavior. Ordinary runs feed the same cache, so verdicts stay fresh for free.
Doctor also checks the installed Copilot SDK's permission-kind vocabulary
against the field-verified snapshot backing the read-only critic barrier.

### Secret registration hygiene

sbx keys custom secrets by env var name (one registration per var, whatever
the scope), so leftover registrations from old runs or old versions surface
as `already exists in scope …` collisions. Provisioning recovers
automatically, and `sbxloop secrets` manages the same state proactively:

```bash
sbxloop secrets list             # registrations + pre-collision warnings
sbxloop secrets clean            # dry-run removal of stale entries (--apply to execute)
sbxloop secrets rotate           # replace the COPILOT_GITHUB_TOKEN registration
                                 # (token from env/.env or --prompt, never argv)
```

`rotate` also reports which secret strategy (proxy vs plain-env fallback) the
next run will use. None of these commands touch the built-in `github` service
secret or registrations owned by other tools.

## Configuration

Configuration resolves, in order, from `SBXLOOP_*` environment variables,
`sbxloop.toml`, and `pyproject.toml [tool.sbxloop]`. `sbxloop init` writes a
commented starter file; `sbxloop config show` prints every resolved value and
where it came from. The notable knobs:

| Key                                    | Default            | Meaning                                                                                                 |
| -------------------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------- |
| `model`                                | `auto`             | Copilot model id (`--model` overrides per run).                                                         |
| `state_dir`                            | `.sbxloop`         | Runs, workspaces, artifacts, SQLite state, event logs.                                                  |
| `keep_sandboxes` / `keep_on_failure`   | `false`            | Sandbox retention for debugging (see above).                                                            |
| `secret_strategy`                      | `proxy`            | `proxy` keeps token values out of the VM; `plain-env` writes an in-VM env file.                         |
| `[sandbox] template`                   | unset              | Baked template ref from `sbxloop bake`.                                                                 |
| `[sandbox] extra_allow_domains`        | `[]`               | Static egress allows applied to every run.                                                              |
| `[sandbox] languages`                  | `["python"]`       | Toolchains pre-installed in the agent sandbox (see below).                                              |
| `[policy] allow` / `deny`              | `[]`               | Bounds for plan-declared egress.                                                                        |
| `[github] repo` / `report` / `deliver` | unset / `false`    | The GitHub integration gate and toggles.                                                                |
| `[budgets]`                            | see above          | `max_revisions_per_task`, `max_replans_per_task`, `max_tasks`, `max_wall_clock_s`, `per_job_timeout_s`. |
| `[limits]`                             | `85` / `95` / `90` | `disk_warn`, `disk_abort`, `mem_warn` percentages (0 disables).                                         |

## Repository layout

This repo is a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) with two distributions:

- [`packages/sbxloop`](packages/sbxloop) — the host orchestrator: sbx CLI wrapper, sandbox pair provisioning, worker transport, loop engine, typer CLI + rich TUI.
- [`packages/sbxloop-worker`](packages/sbxloop-worker) — the in-sandbox runtime: shared protocol models, job runner, Copilot backend. Installed into sandboxes automatically (the host package embeds the worker wheel, so this works before anything is on PyPI).

## Development

```bash
make install    # uv sync --all-packages
make check      # ruff format --check + ruff check + mypy --strict + pytest --cov
make build      # build both wheels
```

Unit and contract tests run against a **fake sbx CLI** — no Docker Sandboxes
install is required for development. The suite runs parallel by default
(pytest-xdist; pass `-n0` for a serial run when debugging with `-s`/`--pdb`).
The real-sbx end-to-end suite runs in CI via a manually dispatched workflow.

## Documentation

- [Architecture](docs/architecture.md) — layers, the sandbox-pair security model, the loop, persistence/resume
- [Worker protocol](docs/worker-protocol.md) — the host↔worker contract: job kinds, events, transports
- [Spike: agent-session backend](docs/spikes/46-agent-session-backend.md) — feasibility study for proxy-held secrets via sbx native sessions (issue #46)
- [Changelog](CHANGELOG.md)

## Requirements

- Python ≥ 3.13
- [Docker Sandboxes (`sbx`)](https://docs.docker.com/ai/sandboxes/) on the host (macOS Apple silicon, Windows 11, or Ubuntu 24.04+/KVM)
- A GitHub Copilot subscription (any plan) + a fine-grained PAT (a second one only if the GitHub integration is configured — see above)

## Releasing

Releases are fully automated — just merge to `main`. Every merge runs the full check suite, auto-bumps the patch version via a new `vX.Y.Z` git tag ([hatch-vcs](https://github.com/ofek/hatch-vcs) derives both package versions from the tag), and publishes both distributions to PyPI via [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC, no token secrets). See [RELEASING.md](RELEASING.md) for details, including how to cut a minor/major release. The manually-dispatched `e2e.yml` workflow installs real sbx on a GitHub runner for end-to-end validation.

## License

MIT
