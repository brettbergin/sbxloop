# sbxloop

[![CI](https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml/badge.svg)](https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sbxloop)](https://pypi.org/project/sbxloop/)
[![Python](https://img.shields.io/pypi/pyversions/sbxloop)](https://pypi.org/project/sbxloop/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Agentic loop orchestration on [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`), with hard credential isolation.**

sbxloop turns a large outcome ("migrate this service to async", "add coverage to every untested module") into a supervised agentic loop: it **decomposes** the outcome into a task graph, then for each task **plans → executes → scrutinizes → verifies → validates**, with revision/replan budgets, checkpointing, and resume.

## The primitive: a sandbox pair

Every run gets an isolated microVM agent sandbox — plus, when the GitHub integration is configured, a second github-ops sandbox, so no single environment ever holds both credentials:

| Sandbox | Credential | Purpose |
|---|---|---|
| `sbxloop-<run>-agent` | `COPILOT_GITHUB_TOKEN` (fine-grained PAT, *Copilot Requests* permission) | Runs the [GitHub Copilot SDK](https://github.com/github/copilot-sdk) agentic layer. All model calls and tool executions happen inside this VM. |
| `sbxloop-<run>-github` | `GH_TOKEN` (fine-grained PAT: issues write, contents read, …) | Performs user-facing GitHub operations (issues, PRs, statuses) against the one configured repository. Only provisioned when `[github] repo` is set. |

Both sandboxes run under sbx's **balanced network policy** (default-deny egress plus a curated allowlist), and tokens are injected through sbx's secret proxy — **credential values never enter the VM**; the host proxy substitutes them only on egress to their declared domains.

## Quickstart

```bash
pip install sbxloop

# one-time host setup
sbx login
sbx policy init balanced
sbxloop doctor          # verifies sbx, policy, tokens, worker wheel

# go
sbxloop run "Add mypy strict typing to every module in ./src and fix all findings"
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

- **Scrutinize** — a fresh, read-only critic session reviews the diff and artifacts.
- **Verify** — mechanical: the task's `verify_commands` must exit 0. No LLM.
- **Validate** — a fresh read-only session judges the acceptance criteria.
- Budgets bound revisions, replans, tasks, and wall clock. State is checkpointed to
  SQLite after every transition; `sbxloop resume <run>` re-provisions sandboxes
  (they're cattle) and continues where it left off.

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
4. `sbxloop doctor` verifies all of it and prints remediation for anything missing.

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
