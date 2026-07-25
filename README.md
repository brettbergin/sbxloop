# sbxloop

[![CI](https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml/badge.svg)](https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sbxloop)](https://pypi.org/project/sbxloop/)
[![Python](https://img.shields.io/pypi/pyversions/sbxloop)](https://pypi.org/project/sbxloop/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Agentic loop orchestration on [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`), with hard credential isolation.**

sbxloop turns a large outcome ("migrate this service to async", "add coverage to every untested module") into a supervised agentic loop: it **decomposes** the outcome into a task graph, then for each task **plans → executes → scrutinizes → verifies → validates**, with revision/replan budgets, checkpointing, and resume.

## The primitive: a sandbox pair

Every run gets an isolated microVM agent sandbox — plus, when the GitHub integration is configured, a second github-ops sandbox, so no single environment ever holds both credentials:

| Sandbox                | Credential                                                               | Purpose                                                                                                                                             |
| ---------------------- | ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sbxloop-<run>-agent`  | `COPILOT_GITHUB_TOKEN` (fine-grained PAT, *Copilot Requests* permission) | Runs the [GitHub Copilot SDK](https://github.com/github/copilot-sdk) agentic layer. All model calls and tool executions happen inside this VM.      |
| `sbxloop-<run>-github` | `GH_TOKEN` (fine-grained PAT: issues write, contents read, …)            | Performs user-facing GitHub operations (issues, PRs, statuses) against the one configured repository. Only provisioned when `[github] repo` is set. |

Both sandboxes run under sbx's **balanced network policy** (default-deny egress plus a curated allowlist), and tokens are injected through sbx's secret proxy — **credential values never enter the VM**; the host proxy substitutes them only on egress to their declared domains.

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
```

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

- **Scrutinize** — a fresh, read-only critic session reviews the diff and artifacts.
- **Verify** — mechanical: the task's `verify_commands` must exit 0. No LLM.
- **Validate** — a fresh read-only session judges the acceptance criteria.
- Budgets bound revisions, replans, tasks, and wall clock. State is checkpointed to
  SQLite after every transition; `sbxloop resume <run>` re-provisions sandboxes
  (they're cattle) and continues where it left off.

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
Provisioning *discovers* the in-VM mount point (marker file + bounded search) rather
than assuming one; when the mount can't be found, jobs run in a fallback dir that is
**harvested** to `.sbxloop/runs/<run>/artifacts` with `sbx cp` at each task end and at
run finalize. Either way the files an agent produces survive the sandbox:

```bash
sbxloop run "write a fib.py with tests"   # summary ends with an artifact tree
sbxloop artifacts <run>                   # list a past run's files (--tree for a tree)
cat "$(sbxloop artifacts <run> --path)/fib.py"
```

To publish a completed run's artifacts as a GitHub pull request, configure the
GitHub integration (see Setup) and pass `--deliver` (or set `deliver = true` under
`[github]`). Delivery goes to the one configured `[github] repo`, through the
github-ops sandbox — `GH_TOKEN` only, one atomic commit via the git data API, branch
`sbxloop/<run>` — and needs `contents:write` + `pull_requests:write` on that repo.
Delivery failures are reported loudly (`run.deliver` event) but never fail a
completed run. Without the integration configured, `--deliver` refuses to run.

## Debugging failed runs

By default sandboxes are torn down at run end — including failed runs, which is
exactly when the in-sandbox evidence (worker stderr, install leftovers, workspace
state) matters most. Two levers:

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

Attaching to an in-flight run is meant as observation — the worker owns its env
files and workspace, so avoid mutating them mid-phase. Kept runs are marked in the
state DB (`kept_reason`) and stay exempt from `sandbox prune` until you pass
`--include-kept`, so debugging convenience cannot become a permanent leak.

## Sandbox hygiene

Sandboxes are torn down at run end, and an in-process registry also cleans up on
Ctrl-C/SIGTERM — but a host crash or `kill -9` can still leak a run's microVM pair.
`sbxloop sandbox prune` garbage-collects those orphans by cross-referencing
`sbx ls` against the state DB:

```bash
sbxloop sandbox prune            # dry run: classify every sbxloop sandbox
sbxloop sandbox prune --force    # actually remove the orphan candidates
```

A sandbox counts as an orphan candidate when its run is terminal
(completed/failed/cancelled), unknown to this working copy's state DB, or
non-terminal but silent past `--min-age` (default 1 hour — the persisted event
stream, heartbeats included, is the liveness signal). Sandboxes deliberately kept
for debugging are excluded unless you pass `--include-kept`. `sbxloop doctor`
reports the current orphan-candidate count.

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

Unit and contract tests run against a **fake sbx CLI** — no Docker Sandboxes install
is required for development. The real-sbx end-to-end suite runs in CI via a manually
dispatched workflow.

## Setup

1. Install [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/), then `sbx login` and `sbx policy init balanced`.

2. Create a fine-grained GitHub PAT:

   - `COPILOT_GITHUB_TOKEN` — personal account, **Copilot Requests** permission. Used *only* by the agent sandbox.

   Export it, or put it in a `.env` file (loaded automatically from the working directory; real environment variables always win):

   ```bash
   cp .env.example .env   # then fill in the token(s)
   ```

3. **Optional — the GitHub integration.** sbxloop has no GitHub capability until you configure the one repository it may work with:

   ```toml
   # sbxloop.toml
   [github]
   repo = "you/your-repo"   # the ONE repo sbxloop may act on
   report = false           # post run progress as a tracking issue (or `--report`)
   deliver = false          # PR the run's artifacts to the repo (or `--deliver`)
   ```

   With `repo` set, runs provision the github-ops sandbox and require a second PAT, `GH_TOKEN`, with the repository permissions you want sbxloop to act with (e.g. issues: write, contents: read) — used *only* by that sandbox. Without it, no github sandbox exists, `GH_TOKEN` is not needed, and repo-facing features refuse to run.

4. `sbxloop doctor` verifies all of it and prints remediation for anything missing. It also runs the **sbx conformance suite**: every empirically-learned assumption about sbx semantics (secret visibility under `exec`, `cp` directory semantics, workspace-mount discovery, ...) is a named probe whose verdict is cached per `sbx` version — `sbxloop doctor --deep` runs the full suite in a scratch sandbox, and doctor warns loudly when an sbx upgrade flips a verdict that sbxloop's behavior depends on.

### Secret registration hygiene

sbx keys custom secrets by env var name (one registration per var, whatever the
scope), so leftover registrations from old runs or old versions surface as
`already exists in scope …` collisions. Provisioning recovers automatically,
and `sbxloop secrets` manages the same state proactively:

```bash
sbxloop secrets list             # registrations + pre-collision warnings
sbxloop secrets clean            # dry-run removal of stale entries (--apply to execute)
sbxloop secrets rotate           # replace the COPILOT_GITHUB_TOKEN registration
                                 # (token from env/.env or --prompt, never argv)
```

`rotate` also reports which secret strategy (proxy vs plain-env fallback) the
next run will use. None of these commands touch the built-in `github` service
secret or registrations owned by other tools.

Configuration lives in `sbxloop.toml` / `pyproject.toml [tool.sbxloop]` / `SBXLOOP_*` env vars (`sbxloop init` writes a commented starter file; `sbxloop config show` shows the resolved values and their sources).

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
