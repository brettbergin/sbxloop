# sbxloop

Read this first. It says what sbxloop is for, what it optimises for, and how
work lands here. `CLAUDE.md` is a symlink to this file.

## What this is

One run from an ask — a chat message or a labeled issue — to a merged pull
request on a repository that is **not this one**, executed inside a sandbox,
unattended, with a human able to watch and steer from chat at every step.

The target repository is the customer. Developer work (plan, build, verify,
deliver, land) is the first run kind, `code`; the second, `workload` (plan,
execute, judge, publish — a brief, a report, a set of files, delivered to a
sink rather than landed as a pull request), is real and rides the same run
shape. Nothing in this codebase may assume the task ends in code, and the
trail fixture `tests/unit/test_code_run_trail.py` holds a `code` run
byte-identical across every change the workload kind brings — read
`docs/architecture.md` "Workloads" before touching either. sbxloop's own
shape — Python, one maintainer with admin, an unprotected default branch, no
services — is an edge case, not the default to design for.

## Goals, in priority order

1. **Work on most software projects.** The default customer is an
   organisation repository: a review-protected base, a service-backed test
   suite, JS/TS, .NET, Ruby, Java or Go toolchains, private registries,
   submodules and LFS, CODEOWNERS, a PR template. Every default in this
   codebase should be the one that serves that repository.
2. **Autonomy with a human in the loop.** Every unattended path ships with a
   live chronology and a way to steer, hold and resume from chat. A design
   with no place for a person to intervene is incomplete.
3. **Spend scales with turns, not jobs.** Prefer one well-briefed phase to
   several cheap ones; every line the agent reads on every turn costs.

## Principles

Each of these has broken a real run when violated.

- **Fail closed on "could not tell".** A detection that cannot decide names
  what it needed and stops; it does not guess and continue.
- **Baseline comparison over absolute gating.** Judge a PR's checks against
  what the base branch already requires and already has — never against
  "everything green".
- **One round for bots.** Automated reviewers get a single answer, never a
  fix loop.
- **Prompts are domain-neutral.** No language-specific examples in neutral
  rules, no incidents from this repository, no bare `#N` from this tracker.
  `tests/unit/test_prompts.py` and `scripts/check_self_references.py`
  enforce this.
- **Every knob lands in three places:** the config model, the example config
  and the README table — with a per-repo override wherever `RepoConfig`
  already narrows.
- **Secrets never appear in events, logs or `sbx` argv.** Names travel;
  values ride the env-file path.
- **The target's conventions outrank ours.** When the repository says how it
  wants to be changed, the agent follows it.

## Non-goals

- Not a CI system, not a code host, not a general chat bot.
- Not a way to modify sbxloop with sbxloop. This repository is worked on by
  people and their coding agents directly.

## Where things live

`docs/architecture.md` is the map; this is the legend.

- `packages/sbxloop/src/sbxloop/` — the host orchestrator.
  - `cli/` — typer commands, `doctor`, `init`.
  - `daemon/` — the always-on outer loop: sources, store, control, chat
    bridges, the concierge.
  - `engine/` — one run: `engine.py` (stage machine), `phases.py`,
    `landing.py`, `review.py`, `checks.py`, `model.py`; `prompts/*.md` are
    what the agent is told.
  - `gh/` — GitHub REST/GraphQL ops, base-branch protection, App auth.
  - `sbx/` — sandbox pair provisioning, baking, conformance probes.
  - `toolchains.py`, `verifylint.py`, `policy.py`, `deliver.py`,
    `hostgit.py`, `config.py` — toolchain series, gate detection, network
    policy, delivery, host-side git, the config model.
  - `data/` — the example config and the `init` presets, shipped as package
    data.
- `packages/sbxloop-worker/src/sbxloop_worker/` — runs inside the sandbox.
  `protocol.py` is the host↔worker contract; `backends/` are the agents.
- `tests/fakes/fake_github.py` — the GitHub every test runs against.
  `tests/fixtures/ecosystems/` — one repository shape per toolchain family.

## Working here

- Python floor: the `requires-python` in `packages/*/pyproject.toml`. Set up
  with `make install` (`uv sync --all-packages`).
- Gates, in this order — CI runs all of them:
  1. `uv run ruff format .` then `uv run ruff check --fix .`
  2. `uv run mdformat <touched .md files>` — never the example toml
  3. `make lint` (format/lint checks, `mdformat --check`, the self-reference
     gate)
  4. `uv run mypy`
  5. `make security` (bandit)
  6. `make test-fast` (`pytest -m "not slow"`, ~2 min) on every commit;
     `make test` — the ~770 process-bound `slow` tests too, ~15 min — or
     CI, which runs all of it sharded, before a merge. Never a hand-picked
     subset in place of either.
- `sbxloop.toml.example` at the root is a symlink into
  `packages/sbxloop/src/sbxloop/data/`. Edit the target; update
  `tests/unit/test_examples.py` when keys change.
- Sandbox behaviour is verified on CI runners only — never against a
  maintainer's or a customer's machine.
- GitHub behaviour is tested against `tests/fakes/fake_github.py`. When a
  change needs a shape the fake lacks (a rule type, an error body, an
  endpoint), extend the fake in the same PR. Never stub the ops layer around
  it.
- Toolchain or gate-detection changes add a fixture under
  `tests/fixtures/ecosystems/` and a row to `tests/unit/test_ecosystems.py`.
- Prompt changes keep `build.md`'s per-language parity test green and pass
  the domain-neutrality gate.
- A claim about an external system you could not verify in this session is
  labelled **field-unverified** in the PR, not stated as fact.

## Pull requests

- Branch off `main`, or off the previous PR when working a stack. One
  concern per PR; a stack for a campaign.
- Title in the imperative. Body says what broke for a *target* repository,
  what changed, and how it was verified; `Closes #N` for the issue it
  resolves.
- Commits and PR bodies carry the trailers the project uses — see `git log`.
- A PR that adds a config key without the example, the README row and the
  test is not done. A PR that changes behaviour without a test that failed
  first is not done either.
