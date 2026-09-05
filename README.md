# sbxloop

[![CI](https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml/badge.svg)](https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sbxloop)](https://pypi.org/project/sbxloop/)
[![Python](https://img.shields.io/pypi/pyversions/sbxloop)](https://pypi.org/project/sbxloop/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Agentic loop orchestration on [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`), with hard credential isolation.**

sbxloop turns a large outcome ("migrate this service to async", "add coverage
to every untested module") into a supervised agentic loop: it **decomposes**
the outcome into a task graph, **builds → verifies** each task under
revision/replan budgets, and — with a GitHub repository configured — carries
the work the rest of the way itself: a draft pull request, its own
adversarial review of the diff, bounded fix rounds, CI, and the merge.
Checkpointing, resume and artifact harvesting throughout.

## The primitive: a sandbox pair

Every run gets an isolated microVM agent sandbox — plus, when the GitHub integration is configured, a second github-ops sandbox, so no single environment ever holds both credentials:

| Sandbox                 | Credential                                                                                                                                                                                                  | Purpose                                                                                                                                                                                                                                    |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `sbxloop-<run>-agent`   | `COPILOT_GITHUB_TOKEN` (fine-grained PAT, *Copilot Requests* permission) — or `ANTHROPIC_API_KEY` with `[agent] backend = "claude"` ([Agent backends](#agent-backends-copilot-or-claude))                   | Runs the configured agent SDK — [GitHub Copilot SDK](https://github.com/github/copilot-sdk) by default, or the Claude Agent SDK. All model calls and tool executions happen inside this VM.                                                |
| `sbxloop-<run>-github`  | `GH_TOKEN` (fine-grained PAT with the permissions in [docs/permissions.md](docs/permissions.md)) — or a GitHub App installation token, host-minted and auto-refreshed ([GitHub App auth](#github-app-auth)) | Performs the GitHub operations (branch, PR, review, CI polling, merge, issue labels) against the one configured repository. Only provisioned when `[github] repo` is set.                                                                  |
| `sbxloop-<run>-service` | The `[[credentials]]` a run was granted by name — operator secrets, each bound to one host (#765)                                                                                                           | Makes the authenticated requests the agent asks for through its `call_service` tool, one fixed `service.http` op at a time, redacting the credential from what comes back. Only provisioned for a run granted a credential; none is today. |

The rule behind the table: **the only key in an agent sandbox is its inference
key.** Everything else that needs a secret happens in a separate sandbox that
runs no model and only the fixed ops the host submits; the agent dispatches
those ops and reads their results, never the credential.

Both sandboxes run under sbx's **balanced network policy** (default-deny
egress plus a curated allowlist), and tokens are injected through sbx's secret
proxy — **credential values never enter the VM**; the host proxy substitutes
them only on egress to their declared domains (where sbx's proxy cannot feed
exec'd workers, tokens are piped into each worker job over stdin — nothing at
rest in the VM — with a 0600 in-VM env file as the last-resort fallback; see
docs/architecture.md). To be honest about it: on current sbx the cached
exec-visibility verdict is negative, so that non-proxy / env-file fallback is the
**common case**, not an edge case — `proxy` names the strategy that is
*attempted*, not the one that usually runs (tracked operationally in #46;
interim hardening proposed in #592). Sandboxes are cattle: they are
torn down at run end and re-provisioned on resume, while all durable state
(workspace, SQLite checkpoints, event log) lives on the host.

### Agent backends: Copilot or Claude

The SDK that runs the agent personas is configurable (#533) — Copilot stays
the default with unchanged behaviour:

```toml
# sbxloop.toml
[agent]
backend = "claude"   # default: "copilot"
```

| backend   | host credential (agent sandbox only)                                         | in-sandbox runtime                                                                                                                   |
| --------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `copilot` | `COPILOT_GITHUB_TOKEN` (PAT, *Copilot Requests*)                             | `github-copilot-sdk` (worker `[copilot]` extra; wheels bundle the Copilot CLI)                                                       |
| `claude`  | `ANTHROPIC_API_KEY` ([console](https://console.anthropic.com/settings/keys)) | `claude-agent-sdk` (worker `[claude]` extra) + the Claude Code CLI, which provisioning installs (Node + `@anthropic-ai/claude-code`) |

Everything else is backend-agnostic: the top-level `model` key names the
model the chosen backend runs (`"auto"` lets it pick its default), every
persona (decompose, build, review, fix, the concierge) runs the same, token
usage is reported through the same `run_usage`/`usage_today` accounting, and
the credential split holds — the agent sandbox carries the chosen agent
credential and never a GitHub token. With the claude backend, provisioning
also allows `api.anthropic.com` egress and keeps the CLI hermetic
(telemetry/auto-update traffic disabled). Missing or invalid configuration
fails fast: an unknown backend fails config loading, and a missing
`ANTHROPIC_API_KEY` fails before any microVM boots. Re-run `sbxloop bake`
after switching backends so a baked template carries the right runtime; the
daemon's long-lived concierge sandbox rebuilds itself, since its reuse check
asks whether the box is equipped for the configured backend and not only
whether the worker version matches. Every host-side command that is *about*
the backend reads one descriptor (`sbxloop.backends`, #617): `sbxloop doctor` checks the configured credential and network hosts (and skips the
Copilot-SDK rows under claude), `sbxloop secrets list|clean|rotate` manage
that credential's registration, `sbxloop list-models` lists that backend's
models, and `--model` help says so.

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

`run` works on a checkout, and says which one before anything is
provisioned: `--workspace PATH`, else the checkout the config names
(`[sandbox] workspace` or a `[[github.repos]]` entry), else the git checkout
enclosing the directory you typed the command in. If the sandbox cannot see
that checkout the run stops there — it never quietly "succeeds" on an empty
directory. With no checkout anywhere the run says `workspace: none` and
works from an empty directory whose output is harvested as artifacts.

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
after upgrading sbxloop). The bake also installs the toolchains for the
configured `[sandbox] languages` (python by default) and records which ones
landed; a run whose languages the template lacks — a Go repo on a Python bake,
say — keeps the baked worker and provisions the missing toolchain on top, and
the `sandbox.prebaked` event and `sbxloop doctor` both say so, so you know
when a re-bake would stop paying for that per provision.

Wondering what to put in `model = "..."` (or `--model`)? Ask the configured
backend which models your credential can actually use:

```bash
pip install 'sbxloop[copilot]'   # copilot: the SDK is optional on the host
sbxloop list-models              # id, billing multiplier, context, reasoning, policy
sbxloop list-models --json       # machine-readable, for scripting
```

Under `[agent] backend = "claude"` the same command asks the Anthropic Models
API with `ANTHROPIC_API_KEY` (id, name, release date; no SDK needed on the
host).

Or as a library:

```python
from sbxloop import LoopEngine, load_config

engine = LoopEngine(config=load_config())
result = engine.start(outcome="Add mypy strict typing to ./src and fix all findings")
print(result.state, result.run_id)
```

## How a run works

```
outcome ──▶ DECOMPOSE (task DAG) ──▶ for each task, in dependency order:
              BUILD ─▶ VERIFY ─▶ done
                ▲        │fail (≤ revisions: same session resumes;
                └────────┘        exhausted: fresh session, one replan)

        ──▶ GATE ─▶ DELIVER (draft PR) ─▶ REVIEW ─▶ CI ─▶ LAND ─▶ merged
              ▲                            │changes requested / red / conflict
              └──────── FIX (one task) ◀───┘  (≤ max_review_rounds / max_ci_rounds)
```

- **Decompose** — produces the task DAG, and with it every task's
  `verify_commands` (the whole mechanical exam — the builder cannot edit
  them) and any declared network egress needs (see
  [Network egress](#network-egress-least-privilege-by-plan)).
- **Build** — one agent session plans and does the work in the sandbox
  workspace, narrating its approach first. It is told where it is — a
  feature branch of an existing repository that a human will review as a
  pull request, with the resolved toolchains and their versions named — and
  how to change code there: match the surrounding conventions, change only
  what the task requires (work beyond the outcome's scope is a defect, in
  the reviewer's own words), create no top-level files the task did not ask
  for. A revision resumes the same session; a replan (or a chat steer)
  starts a fresh one.
- **Verify** — mechanical: the task's `verify_commands` must exit 0, run from
  the workspace root. No LLM. The full command transcript is persisted with
  the attempt, so a resumed run judges with the real evidence. How much
  this decides is `[sandbox] verify_mode`: `full` (the default) gates;
  `advisory` runs the commands and reports; `ci-only` skips them and
  leaves the judging to the PR's checks — see "Suites that need services".
- **Gate** — the project's own check (`[sandbox] gate_command`, or the one
  the project declares — a `check`/`ci`/`verify` target in a makefile,
  justfile or Taskfile, a package.json script run under the client its
  lockfile names, tox, nox, a Rakefile task, a composer script, a Gradle
  wrapper, a `pom.xml`, a cargo alias — or, for Go, Rust and .NET, the tool
  itself) over the whole tree, mechanical. A detector only fires for a
  toolchain the sandbox was provisioned with — the task runners included:
  a Makefile, justfile or Taskfile selects `make`, `just` or `task` the
  way `go.mod` selects Go. The root is read first, then the same two
  levels of subdirectories language detection reads (test, fixture,
  example and docs directories excluded); a gate found below the root
  runs as `cd <dir> && <gate>`, a package.json script under the client
  the monorepo's root pins. A later task can break what an earlier one proved,
  so this is the last look at the tree exactly as it will be delivered. A run
  with no `[github] repo` ends **`completed`** here, its work in the
  workspace.
- **Deliver** — the tree becomes one commit on `sbxloop/<run>` and a draft
  pull request. Every later round re-delivers onto the same branch, so one
  run is one PR.
- **Review** — a fresh read-only session reads the PR's whole diff
  adversarially (concurrency, failure ordering, trust-boundary parsing,
  cross-module invariants, scope) and returns a verdict with line-anchored
  findings. The reviewer is told the project's gate as a result to weigh
  (or that the repository declares none), not as a step to run, and a diff
  the inline budget clipped says where the cut is rather than passing as
  unchanged. The verdict is the run's own and is authoritative; it is also
  posted to the PR for the record. There is no per-task critic: the old
  per-task review stages judged task completion and rubber-stamped it while
  diff-level defects leaked to the PR; one adversarial pass over the
  assembled diff is the critic that earns its turns.
- **Fix** — one seeded task (`fix-N`), built and verified like any other
  under the same revision/replan budgets, then back through the gate. Every
  round sees the earlier rounds' findings and the fixer's per-finding
  `addressed` / `refuted: <why>` list, and the next review may not re-raise
  a refuted finding without a rebuttal — the memory that stops a run arguing
  with itself. Every finding of the round is in the brief — blocking ones to
  address or refute, the rest to address, refute or `defer` to a follow-up —
  and a finding the fixer says nothing about is *unanswered*: it is carried
  into the next brief first, marked as such, and the reviewer keeps it at its
  severity, so a nit cannot be dropped on the floor round after round. Every
  blocking/major finding carries the reviewer's `repro`;
  the fix brief makes it a regression test that fails first, asks for the
  adjacent cases the same code path sees, and shows the fixer what earlier
  rounds decided — so rounds stop converging one case at a time.
- **CI** — the delivered head's check runs *and* commit statuses are
  polled (GitHub Actions and Checks-API apps alongside Jenkins, Buildkite,
  Travis, Codecov and anything else that reports through the Status API);
  red fetches the failing jobs' logs into the next fix brief, and a check
  whose log cannot be read from the sandbox (a commit status, or an
  Actions job the token cannot see) is briefed with its link and an
  instruction to reproduce with the project gate. For a red non-Actions
  check the worker also follows its `details_url` / `target_url`
  best-effort — unauthenticated, https only, text or JSON bodies, the
  same size clamp as an Actions log — and puts what it reads in the
  brief; the sandbox reaches only the hosts its policy allows, so a CI
  host worth reading goes in `[sandbox] extra_allow_domains`, and a
  failure to read leaves the brief at name, link and instruction.
  "Nothing has reported"
  on either API only counts as "no CI" once it has persisted for
  `ci_settle_s` — after a delivery, and again at landing for a head no
  poll has waited on (a resume at the landing stage, a merge-gate
  approve, the head an update-branch makes), so a slow CI's first run
  is never merged ahead of.
- **Land** — un-draft, update the branch if protection wants it current,
  merge with the head the review actually judged (a push that landed since
  loses the race rather than being merged over). Then, and only then, the
  review's out-of-scope notes (`followups`) and the fix rounds' deferred
  findings are filed as **follow-up issues** on the repository — labelled
  `sbxloop:follow-up`, never the trigger label, so a human decides whether
  they run; deduplicated by title within the run and by a body marker
  against the repository, capped by `max_followups_per_run`, and listed in
  one PR comment. `[landing] followups = "comment"` lists them on the PR
  instead of filing; `"off"` drops them. A repository with Issues disabled
  cannot take them: filing downgrades to that one PR comment and the
  `run.followups` event records the downgrade (`downgraded_from`,
  `reason = issues_disabled`) — nothing is dropped silently. The issue
  body names the trigger label only when a daemon dispatched the run;
  under `sbxloop run` nothing polls the repository, so it says only that
  the follow-up is not queued. A failed or blocked run files nothing.

**Budgets, not vibes.** Revisions, replans, task count and wall clock are
bounded by `[budgets]` (defaults: 2 revisions and 1 replan per task, 20
tasks, 2 h wall clock, 30 min per job); the fix loop by `[landing]` —
`max_review_rounds` (default 3) for verdicts that request changes,
`max_ci_rounds` (default 2) for the mechanical failures: a red gate, red CI,
a base conflict, a human requesting changes on the PR. A run past either
budget is one round short, not broken — its branch is green and its PR is
open — so under the daemon the item's retry **resumes that same run** with
`retry_rounds` (default 2) more, once, instead of planning from scratch and
opening a second PR; a second exhaustion hands it to a human, and
`sbxloop daemon ctl grant-rounds <run> <n>` (or `!sbx grant-rounds`, or
asking the concierge for "two more rounds") resumes it at once with more.
Exhausting a task's budget fails the *task*; its dependents are skipped and the run finishes
`failed` before anything is delivered. One deliberate exception: when
revisions are exhausted by *verify-command* failures, the task spends a
replan first when budget remains — the builder cannot edit verify commands,
so only a fresh session's fresh approach can unstick work that disagrees
with where a check looks. Time spent waiting on GitHub is not charged to
`max_wall_clock_s`; `[landing] ci_timeout_s` bounds each wait instead.

**How a run ends.** `merged` — the PR landed; the work is on the base
branch. `completed` — no repository was configured; the work passed the
gate and sits in the workspace. `failed` — a task or a round budget ran out
(any PR is still a draft, and nothing re-picks it). `blocked` — the run
cleared its own bar but GitHub would not let it finish: a protection rule
wanting an approval this identity cannot give, CI that never reported within
`ci_timeout_s`, an update-branch budget spent. Nothing another round would
change, so the PR is left open and out of draft for a human. (`cancelled` is
the fifth, and yours.)

**Checkpointing and resume.** State is committed to SQLite after every
transition, and every stage is a run state — `building`, `gating`,
`delivering`, `reviewing`, `fixing`, `awaiting_ci`, `landing` — with the last
one entered kept on the run. `sbxloop resume <run>` re-provisions a fresh
sandbox pair and re-enters *there*: a crash during a CI wait costs a re-poll,
not a rebuild; a re-delivery is idempotent (the branch is force-moved, the
open PR reused); a `blocked` run resumes at `landing` once a human has dealt
with the cause. The run continues under its **persisted config**, not
whatever is on disk at resume time: the workspace is pinned from the state
DB (a mismatch refuses to resume), and any difference from the current
on-disk config is surfaced as a `run.config_drift` event. The one exception:
the debug toggles (`keep_sandboxes` / `keep_on_failure`) stay resume-time
choices, so a crashing run can be resumed with keep flipped on in config or
env.

**Guardrails.** The worker heartbeat samples in-VM disk and memory
(`[limits]`; defaults warn at 85 % disk / 90 % memory and abort the task at
95 % disk), so a runaway task fails with "sandbox disk exhausted" instead of
letting in-VM tooling fail confusingly on a full disk.

## CLI reference

| Command                                         | What it does                                                                                                                                                                                                                                                                                        |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sbxloop run "OUTCOME"`                         | Start a run; with a repository it carries the work through to the merge. Options: `--workspace`, `--repo`, `--deliver-base`, `--create-repo`, `--create-public`, `--model`, `--keep-sandboxes`, `--keep-on-failure`, `--no-tui`, `--no-chat`.                                                       |
| `sbxloop daemon`                                | The always-on outer loop: claim labeled issues, run each one through to a merged PR, settle the issue, mirror to chat (Discord or Slack). Options: `--repo`, `--max-runs-per-day`, `--poll-interval`, `--discord-channel`, `--slack-channel`, `--once`, `--dry-run`, `--log-level`, `--log-format`. |
| `sbxloop daemon items\|abandon\|retry\|requeue` | Inspect and steer individual work items from another shell without stopping the daemon (see below).                                                                                                                                                                                                 |
| `sbxloop daemon ctl CMD`                        | Drive the running daemon from a script or cron: `status` (`--json` for one machine-readable object), `pause`, `resume`, `cancel`, `queue` — the same verbs as chat's `!sbx`, over a file queue in `state_dir/daemon/ctl/`.                                                                          |
| `sbxloop daemon notify TEXT`                    | Post one message to the control channel through the configured `[chat] backend` — from the host, without the daemon, for deploy scripts and cron.                                                                                                                                                   |
| `sbxloop resume RUN`                            | Re-provision sandboxes and continue a checkpointed run under its persisted config — at the task graph, or at the pipeline stage it stopped in (the retry path for a failed delivery or a `blocked` landing).                                                                                        |
| `sbxloop cancel RUN`                            | Cancel an in-flight run.                                                                                                                                                                                                                                                                            |
| `sbxloop status [RUN]`                          | List runs, or show one run's task/phase detail.                                                                                                                                                                                                                                                     |
| `sbxloop logs RUN`                              | The persisted event stream. `--type` filters by prefix (e.g. `--type policy.`), `--task` by task id.                                                                                                                                                                                                |
| `sbxloop artifacts RUN`                         | List a run's harvested files. `--tree` renders a tree; `--path` prints just the directory (for scripting).                                                                                                                                                                                          |
| `sbxloop shell RUN`                             | Interactive shell in a run's sandbox. `--role agent\|github` picks the pair member; `-c CMD` runs one command.                                                                                                                                                                                      |
| `sbxloop init`                                  | Write a commented starter `sbxloop.toml` from `sbxloop.toml.example` (`--force` overwrites, `--stdout` prints, `--preset large-repo` appends the packaged budget preset).                                                                                                                           |
| `sbxloop init-repo OWNER/NAME`                  | Create the labels the loop relies on in a repository — the six lifecycle labels (with that repository's renames applied) and the follow-up label, each colored and described. Idempotent; boots one github-ops sandbox; exits 1 when the token cannot write labels.                                 |
| `sbxloop bake`                                  | Bake a sandbox template with the worker preinstalled (`--ref`, `--from`, `--keep`).                                                                                                                                                                                                                 |
| `sbxloop doctor [--deep]`                       | Verify the host setup; `--deep` boots a scratch sandbox for the full sbx conformance suite.                                                                                                                                                                                                         |
| `sbxloop sandbox ls\|rm\|prune`                 | Inspect, remove (`--run`, `--all`), or garbage-collect orphaned sbxloop sandboxes.                                                                                                                                                                                                                  |
| `sbxloop gc`                                    | Remove old run directories (workspace clones, harvested artifacts) past the retention window; `--older-than DAYS`, `--dry-run`.                                                                                                                                                                     |
| `sbxloop secrets list\|clean\|rotate`           | Manage the sbx custom-secret registrations sbxloop owns.                                                                                                                                                                                                                                            |
| `sbxloop config show\|policy`                   | Resolved configuration with per-key sources; the effective egress policy.                                                                                                                                                                                                                           |

## Network egress: least privilege, by plan

Sandboxes start with only the baseline allowlist: the Copilot/GitHub hosts,
the apt mirrors, and the supported languages' package registries (issue
[#141](https://github.com/brettbergin/sbxloop/issues/141) — no language's
build should fail for a reason another language's build never encounters).
Well-known registries outside that set are one notch narrower: not reachable
by default, but the decomposer may declare them per task in `egress` with no
configuration,
and every grant lands in the audit log. Anything else the decomposer
declares — each domain with a justification — is validated against
operator-set bounds:

```toml
# sbxloop.toml
[policy]
allow = ["nexus.corp.example.com"]  # what tasks MAY request
deny  = []                          # never grantable, even if allowed
```

Patterns are exact domains, `*.example.com` wildcards, or `*`. Empty `allow`
(the default) means tasks may only use the baseline and the well-known
registries. `deny` wins over everything, including the always-reachable
baseline: a denied registry is never seeded into the sandbox in the first
place. In-bounds grants are
applied **grant-late** — `sbx policy allow network` runs at EXECUTE entry, so
resumed runs re-grant on their fresh sandboxes — and every grant and refusal
is a `policy.allow` / `policy.deny` run event, making the persisted event log
an egress audit trail:

```bash
sbxloop logs <run> --type policy.   # who asked for what, and what was granted
sbxloop config policy               # the effective per-phase policy
```

Out-of-bounds requests fail graph validation with a remediation hint. Static
extras that every run should have go in `[sandbox] extra_allow_domains`.

### Chatting with a running loop

A run is not read-only: type a message into the TUI's input line (Enter to send)
and the agent pauses at the next checkpoint — the same phase boundary
cancellation uses — to answer it in a fresh **read-only STEER session** that can
inspect the workspace. The reply lands in the transcript, and the agent decides
what your message means for the work:

- **continue** — a question or status check; it answers and carries on.
- **steer task** — the current task's build session is discarded and restarted
  immediately with your guidance as feedback (user direction spends no
  revision/replan budget).
- **steer run** — your guidance becomes a standing instruction injected into
  every later build prompt, persisted so `sbxloop resume` keeps it.

Messages queue while a phase is in flight (the status panel shows them), every
chat turn is a persisted event (`sbxloop logs <run> --type chat.`), and
`--no-chat` disables the input entirely. With `--no-tui`, plain line input on
stdin does the same job.

## Working against an existing checkout

Point `[sandbox] workspace` at a project and runs execute on that code. When
the workspace is a **git checkout**, each run is isolated in a per-run clone
(`workspace_isolation = "auto"`, the default): the run works in
`.sbxloop/runs/<run>/workspace` on branch `sbxloop/<run>`, and your checkout —
its working tree, branches, HEAD — is never touched. Pull the results back
with the command the finish summary prints:

```bash
git fetch .sbxloop/runs/<run>/workspace sbxloop/<run>
```

Dirty-tree rules: `auto` **refuses to start** when the checkout has
uncommitted changes (a clone takes committed HEAD, so they would silently not
travel — commit or stash first). `workspace_isolation = "clone"` isolates the
same way but proceeds from HEAD with a warning; `"in-place"` skips isolation
entirely and mutates the workspace directly. Clones hardlink git objects on
the same filesystem, so isolation is cheap; the working tree itself is
copied. If the agent commits inside the VM it needs `git config user.name` /
`user.email` — agents typically set these themselves.

Every run clone is cut `--single-branch --no-tags` (#632): it carries the
run's branch and its history, not every branch and tag the repository has
ever pushed. That is safe because the loop fetches the delivery base
explicitly before every merge-from-base and diff, so a base that is not the
clone's branch still resolves. Shallow clones are deliberately not used —
a `--depth 1` clone has no history to compute a merge base from, and a
wrong base is silently the wrong diff. For a very large repository without a
host checkout, `[sandbox] clone_filter = "blob:none"` opts the remote clone
into git's partial-clone filter: history and trees come down, file contents
are fetched lazily on first checkout. The hazard is that lazy fetch happens
wherever git next needs a blob, including inside the VM, which holds no git
credential (the run's token authenticates the host clone only) — fine for a
public repository, a mid-task failure on a private one, and the reason the
filter is opt-in and applies only to the remote clone. A git without
partial-clone support logs `workspace.clone_filter_unsupported` and clones
in full rather than failing.

**Submodules** are populated in every fresh run clone (#692), nested ones
included. A submodule comes from the host checkout's own copy of it when that
copy holds the commit the superproject records — no network, no credential —
and otherwise from its `.gitmodules` URL with the run's GitHub credential, the
same way the superproject's remote clone authenticates; the run's credential
must therefore be able to read the submodule's repository too. A submodule
neither route can populate fails provisioning naming it, rather than starting
the run on an empty directory where a dependency should be;
`[sandbox] clone_submodules = false` opts a repository whose submodules are
optional or unreadable out, leaving the directories empty. The hosts the
submodules fetch from join the agent sandbox's egress allow list, announced
as `sandbox.submodule_hosts` when they widen it, and
`sandbox.workspace_submodules` records what was populated from where. A
resumed run never re-populates: the submodule stays at whatever commit the
agent moved it to. At delivery, a submodule the run moved to a commit its
remote has is delivered as the moved pointer (a `160000` tree entry); changes
*inside* a submodule are never delivered — the pull request is against the
superproject — and are named in the PR body's **Not delivered** line instead,
as is a pointer at a commit the submodule's remote does not have.

**Git LFS** works the same way (#693). A run clone is cut with the pointer
files, and a fresh clone of a repository whose `.gitattributes` routes files
through `filter=lfs` is then populated from the host: every object the host
checkout's own LFS store holds is hard-linked into the clone — no network, no
credential — and whatever is still a pointer afterwards is fetched from the
repository's LFS endpoint (`<clone url>.git/info/lfs`) with the run's GitHub
credential, which must therefore be able to read the LFS store too. The host
needs `git-lfs` installed (`apt install git-lfs`; `sbxloop doctor` says so in
the `host git-lfs` row); without it, or when an object is missing and there
is no endpoint to fetch it from, provisioning fails naming the fix rather
than starting the run on pointer files. `[sandbox] clone_lfs = false` opts a
repository out and runs on the pointers. The clone's own config carries the
LFS filters, so a build that touches an asset's mtime does not turn it into a
change, and `sandbox.workspace_lfs` records how many objects came from where.
At delivery, an added or modified file that `.gitattributes` routes through
LFS is **not delivered** — the pull request API writes blobs, and committing
the asset's bytes where the repository expects a pointer would be worse than
refusing — and is named in the PR body's **Not delivered** line
(`deliver.lfs_change_skipped` in the log); deleting one delivers, since a
dropped pointer needs no object behind it.

**Tags** come back when the build needs them (#694). A `--no-tags` clone has
nothing for a build that derives its version from git tags — `setuptools_scm`,
`hatch-vcs`, `versioningit`, `poetry-dynamic-versioning`, `vergen`, Gradle's
`axion-release` / `nebula.release` / `git-version`, `MinVer`, `GitVersion`,
`Nerdbank.GitVersioning`, or a `git describe` in a Makefile — and such a
build fails, or quietly reports `0.0.0`. When a fresh clone's manifests
(`pyproject.toml`, `Cargo.toml`, `build.gradle`, `*.csproj`, `Makefile`,
`.goreleaser.yml`, …) name one of those, the loop fetches the repository's
tags into the clone: from the host checkout when it has tags (no network, no
credential), else with `git fetch --tags origin` under the run's GitHub
credential. A failed fetch fails provisioning by name rather than starting
a run whose version is wrong. `sandbox.workspace_tags` records what was
detected and how many tags came from where; `[sandbox] fetch_tags` is
`"auto"` by default, `"always"` for a build whose marker the loop does not
recognise, `"never"` to keep the clone tag-free.

### Environment for the agent sandbox

A project's test suite often reads its environment — `RAILS_ENV`,
`DATABASE_URL`, `GOFLAGS`. `[sandbox] env` (#679) puts that environment in
front of every command the agent sandbox's worker runs — the agent's own
turns and each task's verify commands alike:

```toml
[sandbox]
env = { RAILS_ENV = "test", DATABASE_URL = "postgres://localhost/app_test" }
```

`env` holds plain values, written into the config as given, and it has no
secret counterpart: the only credential the agent sandbox ever holds is its
own inference token, everything else lives in a sandbox the agent's commands
cannot read. A private registry's token goes on `[[registries]] auth_env`
(fetched from by the service sandbox; below), a service's key on
`[[credentials]]` (called through `call_service`; "Credentials a run may be
granted"), and a value a CI job needs goes to CI. An `env` value that names a
variable the loop delivers itself (`GH_TOKEN`, the agent credential,
anything `SBXLOOP_*`) or a registry's `auth_env` is refused at load. The
`[sandbox] secret_env` key of 1.0.x, which delivered a daemon secret into the
agent's sandbox, is gone (#766): a config that still carries it fails to
load by name — `sbxloop doctor` says so first — with the way forward in the
message. A `[[github.repos]]` entry can set `env` for its own runs; a set
value replaces the `[sandbox]` one.

### Private package registries

A repository whose `.npmrc` points at Artifactory, whose Python index is
private, or whose Go modules live on an internal host cannot install its
dependencies from the public baseline — so no gate can pass. `[[registries]]`
(#680, #766) declares each such registry once:

```toml
[[registries]]
kind = "npm"                 # npm | pypi | go | cargo | maven | nuget | gem | generic
host = "artifactory.example.com"
url = "https://artifactory.example.com/api/npm/npm-virtual/"
auth_env = "NPM_TOKEN"       # a daemon-environment variable — held by the service sandbox
scope = "@example"           # npm only: this scope; unset = the default registry

[[registries]]
kind = "go"
host = "github.example.com"  # → GOPRIVATE=github.example.com
```

An entry without `auth_env` is an **open** registry: its `host` joins the
agent sandbox's network allowlist (like `extra_allow_domains`, and in bounds
for a plan that names it) and the ecosystem's client configuration is
written into the agent sandbox before the worker installs, so the tooling
actually uses it.

An entry with `auth_env` is a **credentialed** registry, and the agent
sandbox never reaches it: the credential and the client file go to the run's
**service sandbox** (the same one `[[credentials]]` provisions), which fetches
the project's dependencies into a cache both sandboxes see — the workspace's
`.sbxloop/deps/` (kept out of git, reached as `~/.sbxloop/deps` in either
VM) — and the agent sandbox is configured **offline** for that ecosystem
(`npm_config_offline`, `PIP_NO_INDEX` + `PIP_FIND_LINKS` and the `UV_*`
pair, `GOPROXY=off`, `CARGO_NET_OFFLINE`, `MAVEN_ARGS=-o`, `NUGET_PACKAGES`,
`BUNDLE_LOCAL`) and installs, builds and tests from that cache. Provisioning runs
one fetch per ecosystem whose manifest the workspace carries (`package.json`,
`requirements.txt` / `pyproject.toml`, `go.mod`, `Cargo.toml`, `pom.xml`, a
`*.csproj`, `Gemfile`); a non-zero exit fails the run at provisioning with
the package manager's last lines. During the build the agent holds a
`fetch_dependencies(ecosystem, packages?)` tool: called after it edits the
manifest it re-fetches from it; called with package specs it adds them (npm,
pypi and go — the other ecosystems take the manifest only). The host authors
the fetch command from a fixed per-ecosystem recipe (`npm ci --ignore-scripts`, `pip download`, `go mod download`, `cargo fetch`, `mvn dependency:go-offline`, `dotnet restore`, `bundle cache`), so nothing the
model writes runs where the credential is readable; a package spec that is
not one, or an ecosystem the run has no credentialed registry for, is
refused before a job exists. Every fetch is a `sandbox.fetch` event with the
command and exit code, its output scrubbed of the credential. The service
sandbox needs the workspace mount the agent's has, and fails provisioning
naming it otherwise. The recipes are field-unverified past npm and pip as of
#766; Gradle, pnpm/yarn Berry and Poetry have no recipe yet and stay open
registries.

| kind      | writes                                                                                                                                                      |
| --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `npm`     | `~/.npmrc`: `@scope:registry=` (or `registry=`) and `//host/path/:_authToken=${AUTH_ENV}`; read by npm, pnpm and yarn classic                               |
| `pypi`    | `PIP_INDEX_URL` and `UV_DEFAULT_INDEX` — the registry *is* the index, so point it at a virtual/group repository that proxies PyPI; credential in `~/.netrc` |
| `go`      | `GOPRIVATE` naming the host (every `go` entry joins it); credential in `~/.netrc` for the git fetch                                                         |
| `cargo`   | `~/.cargo/config.toml` `[registries.NAME]` with a sparse index; token in `CARGO_REGISTRIES_<NAME>_TOKEN`                                                    |
| `maven`   | `~/.m2/settings.xml`: a `<mirror>` of `*` and a `<server>` whose password is `${env.AUTH_ENV}`; Gradle does not read it                                     |
| `nuget`   | `~/.nuget/NuGet/NuGet.Config`: a package source plus credentials referencing `%AUTH_ENV%`; nuget.org stays unless the repository clears it                  |
| `gem`     | `BUNDLE_<HOST>=user:token` for bundler; with `url`, `~/.gemrc` lists it as the gem source                                                                   |
| `generic` | nothing but the allowlist entry, plus `~/.netrc` when `auth_env` is set                                                                                     |

`auth_env` is read from the daemon's environment at provision time; unset
names fail provisioning before a sandbox boots (the `registry credentials`
row of `sbxloop doctor` lists them), and the value never rides an `sbx`
argument, an event, or a log line. Wherever the ecosystem expands environment
variables in its own config the client file names the variable and holds no
secret; the netrc kinds (`pypi`, `go`, `generic`) have no such form, so
`~/.netrc` holds the value at rest, 0600, in the service VM — those kinds
pair it with `auth_user`, the login the registry expects beside the token. A
derived variable a repository sets in `[sandbox] env` (its own `GOPRIVATE`)
wins over the registry's. A `[[github.repos]]` entry may carry its own
`registries` list, which replaces the top-level one.

### OS packages and setup commands

Toolchains carry the packages their own runtime needs; the library a
project links against (`libpq-dev`, `libjpeg-dev`), a compiler its build
shells out to (`protobuf-compiler`), a JDK beside a Python project, or a
one-off like `playwright install --with-deps` is the project's business —
and without a place to say so, the agent discovers it on its first failed
install and spends revision budget on `sudo apt-get`. `[sandbox] apt_packages` and `setup_commands` (#681) are that place:

```toml
[sandbox]
apt_packages = ["libpq-dev", "protobuf-compiler"]
setup_commands = [
  "npx playwright install --with-deps chromium",
  "pre-commit install-hooks",
]
```

`apt_packages` are ensured right after the toolchains, on the prebaked path
too: one `dpkg -s` pass names what the template lacks and the rest is one
`apt-get install`, so `sbxloop bake` with the list configured makes a run's
share a probe and no network. Unlike the toolchain ensure this is not
best-effort — the operator named the package because the project does not
build without it, so a failed install fails provisioning naming the package
and apt's last lines. `setup_commands` run in order, in the cloned workspace,
after the toolchains, the registries' client files and the sandbox
environment are in place and before the first agent phase; each runs under a
login shell with the same environment a job gets (per-job stdin delivery or
the in-VM env file) and under the
run's egress policy as already applied — a command that needs a host the
allowlist lacks fails here, not in a phase. Every command's exit code,
duration and output tail is a `sandbox.setup` event (delivered secret values
scrubbed); the first non-zero exit ends the run at provisioning with the
command in the error, and `keep_on_failure` keeps the sandbox for `sbxloop shell`. A `[[github.repos]]` entry may carry its own `apt_packages` or
`setup_commands`, which replaces the top-level list; a per-repo package list
is paid at that repository's provision, since the bake reads the global list
only.

### Suites that need services

Most backend suites want a database, a broker or a browser the sandbox
does not have, and a mandatory verify phase spends the task's revisions
and a replan on `connection refused` before giving up. `[sandbox] verify_mode` (#682) says how much the in-sandbox checks decide:

```toml
[sandbox]
verify_mode = "advisory"   # full (default) | advisory | ci-only
```

`full` is the gate as it was: a task's verify commands and the project
gate must pass. `advisory` runs them all and blocks on none — a failure is
a `phase.end` with `status = "advisory"` in the chronology (⚠ in the
channel), an evidence section in the review prompt, and a
**Verification** section in the pull request body, so the reviewer weighs
a `connection refused` against the diff instead of the loop spending
budget on it. `ci-only` submits no verify job and skips the gate stage
(both recorded `skipped`); landing's CI round, on a runner that has the
services, is the verification. A `[[github.repos]]` entry sets the mode per
repository. The mode never changes on its own: under `full`, a compose
file, `testcontainers` in a lockfile or a `services:` block in a workflow
is named once per run as a `verify.services_detected` event before the
plan is written — the moment a human can still turn the knob — and the
planner is told to scope verify commands to the subset that runs without
the services either way.

## The daemon: an always-on outer loop

`sbxloop daemon` is deliberately small. It polls the one configured
repository (`--repo` / `[github] repo` — the repo being worked on) for open
issues carrying the trigger label (`sbxloop:run`), claims each one, runs it
as **one** engine run — task graph, gate, draft PR, review, fix rounds, CI,
merge — and settles the outcome on the issue. One labeled issue is one run
is one pull request. There is no other work source, and the daemon **never
files work of its own**: only a human labelling an issue — directly, or by
asking the chat concierge, which files the issue *with* the label —
starts a run.

The labels are the state machine, and every transition is visible on the
issue:

- **Claim.** `sbxloop:run` → `sbxloop:in-progress`, plus a claim comment
  (`<!-- sbxloop-claim <token> host=… pid=… started=… -->`) that doubles as
  the lock between daemons. The token is persisted *before* the comment is
  posted and SIGTERM is held until the claim is complete, so a process that
  dies mid-claim leaves a row the next start settles against the issue —
  finishing the claim if the comment landed, forgetting it if not. A claim
  comment from a dead process (this host, dead pid; or older than
  `[daemon] claim_stale_after_s` with no run started) is released and
  reclaimed, and a claim that turns out not to be ours (another daemon won,
  the issue closed, the label went away) leaves no row at all — the next
  poll re-creates it if the trigger label is still there.
  The comment is the lock: GitHub has no compare-and-swap on labels, but a
  comment is created exactly once and ordered, so two daemons watching one
  repository cannot both take an issue. A `Run <id> started.` comment
  follows once the run is dispatched.
- **`merged`** — the PR landed. The daemon comments the PR link, swaps
  `in-progress` for **`sbxloop:completed`** and closes the issue
  (`state_reason: completed`). The PR body also carries `Closes #N`, so
  GitHub links the pair and closes the issue even when the daemon is down.
- **`failed`** — the run gave up: a task or a round budget ran out, or it
  errored. The daemon comments the reason and retries with backoff while
  the item has attempts left (`max_attempts_per_item`, default 2;
  `retry_backoff_s` × the attempt number), then abandons it:
  `in-progress` → **`sbxloop:failed`**, with re-trigger instructions
  (just re-add `run`; the claim clears `failed` itself). Any PR stays a
  draft.
- **`blocked`** — the run cleared its own bar and GitHub would not let it
  finish: a protection rule wanting an approval the loop's identity cannot
  give, CI that never reported, an update-branch budget spent. The PR is
  left **open and out of draft** — one click from done — the issue stays
  open with **`sbxloop:blocked`** and a comment saying why, and the item
  neither retries nor counts toward the breaker, because nothing another
  attempt would change. Merge or fix by hand and close the issue, or re-add
  `sbxloop:run` once the cause is dealt with — that restarts it on the same
  branch and PR (`!sbx retry <item>` restarts from scratch instead).
- **cancelled** — `!sbx cancel` settles the item as cancelled, attributed
  to whoever asked, with no automatic retry; the run stays resumable.

**Restarting an issue: re-add the trigger label.** Re-applying
`sbxloop:run` to an issue whose last attempt finished — done, failed,
blocked or cancelled — re-queues it on the next poll whether or not the
issue text changed; the label is never silently inert. The restarted run
continues from whatever the previous attempt pushed to the GitHub origin:
the same branch and, if one was opened, the same (draft) PR, so its commits
are kept rather than redone. When nothing usable is on origin — no branch,
or a branch unrelated to the current base — the run simply starts fresh and
logs why. `!sbx retry <item>` remains the way to ask for a clean restart
from scratch.

Everything else the daemon does is a guardrail or a recovery. It is
**fully autonomous** — a label alone starts a run *and merges the result* —
so treat the trigger label as "execute arbitrary instructions with
`GH_TOKEN`'s repo scope" and restrict who can apply it. The `[daemon]`
guardrails are the safety net, and they are **daemon-wide**: they bound what
this host does in total, not what one repository does, so with several
repositories registered they are shared across all of them —

- a **calendar-day run cap** (`max_runs_per_day`, default 12) counting the
  runs *started* since 00:00 in `run_cap_timezone` (any IANA zone, default
  `UTC`) and resetting at that boundary, so a run started just before
  midnight does not free a slot early;
- the **per-item attempt cap** with backoff (above), and a **per-item
  resume cap** (`max_resumes_per_item`, default 2): an interrupted run
  (SIGTERM, crash) is resumed on the next start — through the same
  guardrails as any dispatch — and past the cap the interruption counts as
  a failed attempt instead;
- a **circuit breaker** (`max_consecutive_failures`, default 3, then
  `breaker_cooldown_s`, default 1 h) that is persisted, so a restart cannot
  reset it — and counts *consecutive failures across repositories*, so a
  repo that keeps failing pauses the whole daemon;
- **pause and cancel**, from Discord or `sbxloop daemon ctl` (below);
- **reconciliation**: on start, and every tick while nothing is executing,
  runs the store still shows in flight with no process behind them are
  closed with a recorded reason (`run_stale_after_s`, default 6 h; `0`
  disables the staleness sweep), so `sbxloop status` and `!sbx status`
  agree about what is active;
- **retention**: run directories past `prune_runs_after_days` are swept on
  start and daily (see [Sandbox hygiene](#sandbox-hygiene)).

Polling and issue lifecycle run through a long-lived github-ops sandbox the
daemon owns, so the host still never holds the PAT. Runs are one at a time,
across every configured repository. Ship it as a systemd user service with
[`contrib/systemd/`](contrib/systemd/).

Individual items are steerable from another shell without stopping the
daemon: `sbxloop daemon items` lists them (state, attempts, pinned run, last
error); `sbxloop daemon abandon <item> [--reason …]` gives one up (a live
daemon cancels its in-flight run and tells the issue — the report is owed
on the row and paid by the next tick or the next daemon start, once);
`sbxloop daemon retry <item>` re-queues an abandoned, blocked or cancelled
item with attempts reset and a **fresh build session** — not a resume of
the approach that failed; and `sbxloop daemon requeue <item>` drops a
running item's pinned run so its next dispatch starts over (attempts and
backoff kept). The same controls are `!sbx items|abandon|retry|requeue` on
Discord.

`<item>` is a work item id. GitHub items are **typed** —
`gh:issue:<number>` for the issue a run was claimed from, `gh:pr:<number>`
for a pull request referenced as a work-item resource — and the untyped
legacy form `gh:<number>` is still accepted everywhere as an alias for
`gh:issue:<number>`, so old commands, checkpoints and watches keep working.
Everything sbxloop prints uses the typed form. See
[Work item ids](docs/architecture.md#work-item-ids) for the full grammar.

**Workspace posture for unattended runs.** Point `[sandbox] workspace` at a
**dedicated clone nobody edits** (`git clone <repo> ~/sbxloop-runner/src`),
not the checkout you work in. Before each fresh run the daemon
`git fetch`es that clone and fast-forwards its branch to its upstream (the
remote the branch tracks; `origin/<branch>` when none is configured) — never
a merge or rebase; a diverged branch or a colliding local edit is left alone
and logged — so runs start from the current remote branch rather than a
stale local HEAD (`[daemon] refresh_workspace`). Daemon runs use `clone`
isolation regardless of `[sandbox] workspace_isolation` (`[daemon] workspace_isolation`, default `clone`): a dirty tree proceeds from committed
HEAD with a warning, because `auto`'s refusal has no human present to answer
it. Per-run clones point their `origin` at the source's origin URL (metadata
only; any userinfo such as an embedded token is stripped from the URL, so
no credentials leave the host). And the daemon keeps its state
**outside the workspace** at an absolute path — `[daemon] state_dir`, else
an explicitly configured `state_dir`, else a pre-existing legacy
`./.sbxloop/state.db`, else `$XDG_STATE_HOME/sbxloop/<runner-dir-name>`
(`~/.local/state/…`) — so a checkout never accretes one full clone per run.
The daemon logs the resolved location at start (in its `daemon.starting`
summary); the `sbxloop daemon items|abandon|retry|requeue` controls follow the same rule, while
`sbxloop status`/`logs`/`gc` from the runner directory need
`SBXLOOP_STATE_DIR` pointed at it.

The daemon's log stream (stderr → journald under systemd) is structured:
`--log-level DEBUG|INFO|WARNING|ERROR` (`[daemon] log_level`,
`SBXLOOP_DAEMON__LOG_LEVEL`; default `INFO`) and `--log-format console|json`
(`[daemon] log_format`; `json` is one object per line for log shippers). At
`INFO` you get the startup config summary, every claim, `run.dispatch` /
`run.finished` with durations, the run's own lifecycle mirrored under the
`sbxloop.run` logger (task/phase transitions, sandbox provisioning, worker
jobs, steering), operator commands, and why the daemon is idle (paused,
breaker open, backing off, capped) whenever that changes; `DEBUG` adds every
tool call, `sbx` invocation and poll. See
[docs/architecture.md → Logging](docs/architecture.md#logging).

#### The `[daemon]` keys

This is the whole list — the landing knobs live under
[`[landing]`](#github-integration):

```toml
[daemon]
poll_interval_s = 60.0
trigger_label = "sbxloop:run"             # the label that queues work
in_progress_label = "sbxloop:in-progress"
completed_label = "sbxloop:completed"     # the PR merged; the issue closes
failed_label = "sbxloop:failed"           # the run gave up; re-trigger by hand
blocked_label = "sbxloop:blocked"         # GitHub would not let the loop land the PR
gated_label = "sbxloop:awaiting-merge"    # parked by [landing] merge_gate
max_runs_per_day = 12                     # calendar-day cap, persisted across restarts
run_cap_timezone = "UTC"                  # day boundary for the cap (resets at 00:00 there)
max_attempts_per_item = 2
max_resumes_per_item = 2                  # interrupted runs resumed at most this often per item
retry_backoff_s = 900.0                   # times the attempt number
max_consecutive_failures = 3              # circuit breaker ...
breaker_cooldown_s = 3600.0               # ... and how long it stays open
shutdown_grace_s = 60.0                   # keep below systemd TimeoutStopSec
prune_runs_after_days = 14                # run-directory retention; 0 disables
run_stale_after_s = 21600                 # staleness reconciliation; 0 disables
workspace_isolation = "clone"             # clone | auto | in-place, for daemon runs
refresh_workspace = true
# state_dir = "~/.local/state/sbxloop/my-project"
log_level = "INFO"
log_format = "console"
version_check = true                      # ask PyPI once at start; false = no request, no advice
# upgrade_command = "pipx upgrade sbxloop"  # what the drift notice tells the operator to run
```

#### Upgrading a pre-1.0 daemon

The 1.0 pipeline retired the daemon's other lanes — the agent backlog,
post-mortems, scheduled audit charters, the review lane, the inbox source,
the per-run tracking issue — and with them their `[daemon]` keys and
`[github] report` / `deliver`; the landing knobs (`deliver_draft`,
`merge_method`, `delete_branch_on_merge`, `merge_update_attempts`) moved to
`[landing]`. A config still carrying a retired key **fails to load** (every
config model forbids unknown keys), so delete them before upgrading a
0.7.x host straight to 1.0 — the 0.7.55–0.7.56 releases loaded them with a
warning and a `sbxloop doctor` row to make that edit unhurried
(`auto_merge = true` simply goes: landing is always on). A pre-1.0
`state.db` is moved aside to `state.db.pre-1.0` on first start rather than
migrated, and the old lanes' issues and labels are closed by hand;
[CHANGELOG → 1.0 cutover](CHANGELOG.md#10-cutover) has the steps.

### Chat: chronology out, steering in — Discord or Slack

The daemon's human channel is one chat service, chosen by `[chat] backend = "discord" | "slack"` — or inferred from whichever of `[discord]` / `[slack]`
carries a `channel_id`; configuring both without choosing is a config error,
and neither means the daemon runs headless (`sbxloop daemon ctl` only).
Everything in this section works the same on both: Discord is described
first, the Slack differences follow. With `pip install 'sbxloop[discord]'`,
`DISCORD_BOT_TOKEN` in the environment, and `[discord] channel_id` set, a
gateway bot posts a headline card per run in the control channel (source issue, run id, branch, PR,
task tally — colour follows the state) and streams that run's
chronology into a thread under it, in Discord's own formatting: agent
messages as Markdown with persona attribution, split at paragraph and
code-fence boundaries instead of clipped — their **narration only**, never the
JSON payload a structured phase returns; what that payload *decided* is posted
in its own words instead: the task roster (`🧩 3 task(s)`, re-announced with
persisted state on resume) — while the builder narrates its approach in
prose, and each attempt closes with a `🔨 build` report-excerpt line; each burst of tool calls
digested into **one line edited in place** (`⚙ 23 tool calls (bash x21, view x2) — last: pytest -q`, with a "may be stuck" nudge when the last
calls are near-identical) — failed calls still get their own detail
block, and `chronology_level = "verbose"` streams every call batched into
code blocks instead; one **status line edited in place** as tasks
progress (`⏳ task 2/5 · Add tests · verify`); issue, PR
and branch as links; verify failures, worker errors, denied permissions and
refused egress called out; and a finished report card (the headline turns
✅/❌/⚠) with the final state, the task tally and the PR. How each item
settled is a one-line notice in the control channel, pointing at the run's
thread — `🎉 gh:issue:9 merged (2/2 tasks done) · PR …`,
`❌ gh:issue:4 failed (…); 1 attempt(s) left`, `🚧 gh:issue:7 blocked: … — a human needs to look` when an issue lands in `sbxloop:blocked`, `🛑 circuit breaker opened …` — with every URL masked so nothing sprouts a preview.
With `[landing] merge_gate = "chat"` — the one opt-in human touchpoint — a
run that clears every bar parks instead of merging: `⏸ ready to merge — waiting for your approval` lands in the run's thread @mentioning whoever
asked for the work — with a persistent **Approve merge** button on
Discord — and `!sbx merge <item>` (here or in the control
channel; `sbxloop daemon ctl merge <item>` works headless) completes the
landing, while `!sbx abandon <item>` declines and leaves the PR open. No
deadline; the park, its prompt and its button survive restarts.
A base branch that *requires* an approving review is not a block: the run
parks `awaiting_review` — PR un-drafted, `[github] reviewers` requested,
`👀 awaiting review` in the run's thread @mentioning the requester and
`[landing] review_notify` — and the daemon polls the PR every
`review_poll_interval_s`. A human's approval (or a human merging it) lands
the run; a changes-requested review resumes it for a fix round; the PR
closed abandons it. After `review_wait_s` without a verdict the item goes
`paused_review` (one more mention, no more polling) until
`!sbx resume <item>` picks it up again. The park survives restarts.
A person converting the PR to draft is the same park with a different
end: `✋ held in draft` in the thread, no reviewers requested, and the poll
waits for the PR to be marked ready for review — approvals alone do not
end it — then completes the landing without ever un-drafting on its own
(`review.ready`). A landing that comes back waiting on something else
(the base grew a review rule; someone re-drafted it) re-parks the hold for
that (`review.reparked`) rather than failing it.
Mentions are otherwise always disabled, so model output can never ping the
channel, and Discord's automatic link previews (unfurls) are suppressed on
every send *and* every edit, so no message sprouts a grey preview card — the
bridge's own embed cards still render. `[discord] embeds` (set `false` to
render those cards as plain-markdown twins instead; unfurl suppression is
unaffected), `status_line`, `tool_batch_lines`,
`tool_output_lines` (tail output lines echoed for a *successful* call,
default `0` = none) and `tool_fail_output_lines` (head+tail lines echoed for a
*failed* call, default `20` — a watcher needs the stderr) tune it, along with
`chronology_level`. Excerpts are line-clipped, body-capped and clamped to
Discord's 2000-character message limit, with any elision marked
`… N lines elided …`. **@mention the bot in a run's thread to steer that run**
(or reply to one of its messages there) — the same rule the control channel
uses, so people can talk about a run in its own thread without derailing it.
Your message is
relayed to the agent exactly like the CLI's `--chat` (answered at the next
checkpoint, which can be minutes into a long step — a note under your
message says where the agent is, `⏳ steer queued — agent is mid-execute on t2 (12/40 tool calls so far)`, edited in place until the ⏳ reaction turns ✅
when the reply lands). `!sbx status|pause [--hold NAME]|resume [--hold NAME|--all]|cancel [--retry]|queue|items|abandon <item> [reason]|retry <item>|requeue <item>|grant-rounds <run> <n>|resume-repo <owner/name>` in the control channel drive the daemon
itself. Pause is a set of **named holds**: a bare `pause`/`resume` acts on the
operator's hold, the deploy pipeline holds `deploy-<run id>` while it waits for
the daemon to go idle, and the daemon idles while any hold stands — so an
operator pause survives a deploy and `resume --all` is the override for a hold
whose owner never released it. `!sbx cancel` stops the current run at its next boundary and settles
the item as **cancelled** — attributed to you on the source, no automatic
retry, no breaker count — while the run stays resumable (`sbxloop resume RUN`
on the daemon host); `!sbx cancel --retry` re-queues it for a fresh run
instead, and `!sbx retry <item>` reruns any cancelled or abandoned item with
its attempt budget reset. Every `<item>` argument takes either the typed
`gh:issue:<n>` / `gh:pr:<n>` form or the legacy bare `gh:<n>`
([Work item ids](docs/architecture.md#work-item-ids)); replies always quote
the typed form. Those verbs work in a run's thread too, answered
where you typed them. Anyone who can post in the channel
can steer — that is the boundary to set. The bot ignores messages from bots
(itself included), so scripts drive the daemon with `sbxloop daemon ctl <verb>`
instead — the same verbs through the same dispatcher, no Discord needed; a
request no daemon picks up within `--timeout` (30s) is withdrawn, so a stale
`cancel` never fires when the daemon starts later. Timing out is not "not
executed": once the daemon has taken a request it keeps running (item verbs
cross the ops sandbox), and `ctl` reports it as pending (exit 1) rather than
absent (exit 2). Scripts that need the state rather than the prose read
`sbxloop daemon ctl status --json` — one JSON object with `current`, `claiming`,
`holds`, `paused` and the rest — and post their own notices with
`sbxloop daemon notify "<text>"`, which goes through the configured chat backend
from the host even while the daemon is down ([docs/deploy.md](docs/deploy.md)).

**Chat with the daemon.** @mention the bot in the control channel (or reply
to one of its messages) and the **concierge** answers — the channel's own
agent, which knows how to operate sbxloop and what it is building. Ask
"what's running?", "why did `r7…` fail?", "show me the diff of that PR",
"pause after this one", or "also please add retries to the fetch client"
— it runs the same `!sbx` verbs through the same dispatcher, reads the
run store (runs, tasks, chronology, reports), fetches PR/issue/diff/file
details through the github-ops sandbox, and turns a described feature or
bug into work in **one hop**: `create_issue` files the issue in the
configured repo with a self-contained title and body *and* the
`sbxloop:run` label, and the daemon claims it on its next poll (backlog
capture, triage notes and canaries take the explicit opt-in path instead —
filed with no trigger label, left for `label_issue_for_run`). What the run
then reads is the whole issue, not just its title and body: the comments
under it (minus the loop's own claim and status comments, and its identity's
where it can resolve one) and the issues and pull requests they link to — on
a real tracker the body is a one-liner and the repro, the maintainer's
scoping and "do it the way #123 did" live in the thread. The discussion is
capped at `[budgets] outcome_max_chars` (16,000 characters; the body is
never cut, and the cut is marked), and a thread that could not be read is
said so in the outcome rather than silently missing. There is
no triage lane in between — which is why the concierge writes a body a
fresh clone can act on, and why the channel is the access boundary. That
body is **symptom-first**: what the person observes today in their own
words, the change they asked for as a hint, the concierge's restatement of
the goal, and acceptance criteria written against the symptom — because the
loop optimises hard for the words in the issue, and an issue that names a
mechanism gets exactly that mechanism (#519 asked for "the embeds" removed
and meant Discord's link unfurls). A fix-shaped ask with no observed
symptom is the one thing the concierge asks about before filing: "what are
you seeing that you want gone?" — one question, then the issue. The
decomposer plans against the symptom and the reviewer judges the PR against
it, so a change that implements the mechanism without curing the symptom is
sent back in round 1.

**Clarifying questions you answer by clicking.** When the concierge needs
one more thing from you *and* the plausible answers are enumerable — "is
this about the wording, the layout, or the timing?", "close #12 as a
duplicate, or as completed?" — it posts the question with a **button per
answer**, so unblocking the bot is one click rather than a typed reply.
Clicking is the whole answer: the daemon feeds the selected option back
into the conversation exactly as if you had typed it, so the outcome is
identical either way, and the message is edited to record which option was
chosen and by whom.

Typing still works, always. The buttons are an extra way in, never the only
one: the same numbered options stay in the message body, so "2", "the
layout", or an answer in your own words is understood just as it was before
— and an answer that names none of the options is passed through to the
concierge as ordinary prose, unchanged.

Not every question gets buttons. When the answers are **not** enumerable —
"paste the traceback you saw", "what should the new title be?" — the
concierge asks free text and the message carries no components, rather than
forcing you into an unsuitable set of choices.

The interactive message degrades safely. An outstanding question stays
clickable for 15 minutes; after that the buttons are greyed out with a note
that typing still works, and a click that arrives late (or on a question
already answered, or after a daemon restart, which forgets them — nothing
is persisted) gets a private nudge to answer in the channel instead. The
bot never waits on a click: a Discord that rejects the components, or a
host without them, simply gets the plain numbered question. Backends
without interactive components — Slack today — always get that prose
rendering, so nothing about them changes.
"What's open?" lists the repository's open issues and which are queued or
running; `queued: false` shows everything the daemon is not currently
queued or running — the backlog plus issues that failed or are blocked and
need a person — and a `state` argument narrows to one exact state.
Ask what a run cost and it reports that run's input/output tokens per
agent persona and totalled; "how much have we spent today?" totals the
current calendar day in `run_cap_timezone` — the same day the run cap
counts — next to that cap. Tokens are attributed to when they were spent,
so a run spanning midnight counts on both days. The
backend reports tokens but not cost, so it says that rather than
converting to money — and a run from before usage reporting answers "not
recorded", never zero.
Ask "are we up to date?" and it compares the installed `sbxloop` /
`sbxloop-worker` / `sbx` versions against the latest releases on PyPI —
sbxloop's releases ship frequently while upgrading a host is an operator's
step, so the daemon also says so once at startup when it is behind. (It
only reports: the advice names `[daemon] upgrade_command` when one is set
and otherwise says the command depends on how sbxloop was installed; a
restart follows either way. `[daemon] version_check = false` switches the
PyPI lookup off entirely — no request leaves the host, no notice is posted
— for an air-gapped or mirror-pinned host, or one a deploy pipeline keeps
current.)
Ask "what is the daemon doing?" or "why is nothing running?" and it quotes
the daemon's own recent log lines — `daemon.idle`, `breaker`,
`github.poll_failed` — through `daemon_log(tail, level, grep)`, the journal
without ssh. It reads a **bounded in-process ring buffer** the running
daemon fills (the last 2000 rendered lines, already redacted), not the full
systemd journal: anything older than the buffer, or from a previous daemon
process, still needs `journalctl --user -u sbxloop-daemon`. `tail` is how
many records (default 50, at most 500), `level` keeps only records at or
above `DEBUG`/`INFO`/`WARNING`/`ERROR`, and `grep` is a plain
case-insensitive substring — never a regular expression, so no pattern from
chat can wedge the daemon. The result is clipped to
`[concierge] max_tool_result_chars` like every other tool result.
Say "tell me when r7… is done" (a run id or a work item id) and `watch_run`
registers your interest: it confirms, and when that run lands the daemon
posts in the control channel @mentioning you with the outcome — final
state, task summary, PR, and the reason when it failed or was blocked.
Watching a run that has already finished answers with the outcome
immediately instead of registering. Watches are **persisted** in the daemon state: they are
reloaded at startup, so a watch registered before a daemon restart still
pings you when the run lands.

It finishes triage too: "reply on #12 that we're waiting on upstream"
posts a comment signed with your name, and "close #12 as a duplicate of
#7" comments and closes it as *not planned* (or *completed*) — but only
after it has asked and you have said yes naming the issue, and never while
a run is working that issue. `[concierge] create_issues` gates all of it.
Actions are otherwise direct — it acts
with the same authority as `!sbx`, so anyone who can mention it drives the
daemon; restrict the channel accordingly — and every tool it used is
listed in one edited `🛠 concierge: sbx_control(status) · run_detail(r7…)`
line under your question, so nothing happens invisibly. Steering a live
run still happens by @mentioning the bot in that run's thread; asked from
the control channel, the concierge points at the thread. It runs as a Copilot session in a
**long-lived agent sandbox** the daemon owns (`sbxloop-concierge-<digest>`,
reused across daemon restarts so the conversation keeps its memory; the
SDK session is rotated after `[concierge] session_turns` messages) and
reaches the daemon only through host tools — the same
`COPILOT_GITHUB_TOKEN` a run needs must be on the daemon host.
`[concierge] enabled | model | timeout_s | max_tool_calls | session_turns | github_tools | create_issues`
tune it (`sbxloop init` documents them; `sbxloop doctor` shows the row).
Plain messages in the control channel are left alone — people talk among
themselves without the bot answering.

Bot setup, once: create an application in the Discord Developer Portal, add
a bot, enable the **Message Content** privileged intent, copy the token, and
invite the bot to your server with View Channel, Send Messages, Create
Public Threads, Send Messages in Threads, Add Reactions, and Read Message
History. Chat is observability, never a dependency: if it is down, the
daemon logs and carries on.

**Slack instead.** `pip install 'sbxloop[slack]'`, set `[slack] channel_id = "C…"` (the channel's *id*, from its details pane — not its name) and put
`SLACK_BOT_TOKEN` (`xoxb-…`, the Web API) and `SLACK_APP_TOKEN` (`xapp-…`,
the Socket Mode connection) in the environment / `.env` — never in
`sbxloop.toml`; they are read from the environment only and never logged.
The app runs in **Socket Mode**, so it dials out and needs no public URL or
request signing — what a daemon on a home server needs. Create the app once
at api.slack.com/apps (from scratch): under *Socket Mode* enable it and
generate an app-level token with `connections:write`; under *OAuth &
Permissions* add the bot scopes `chat:write`, `channels:history`,
`channels:read`, `groups:history`, `groups:read`, `reactions:write`,
`users:read` and `app_mentions:read`; under *Event Subscriptions* subscribe
the bot to `message.channels`, `message.groups` and `app_mention`; install
the app to the workspace and `/invite @your-app` into the control channel.
On Slack's shapes: the run thread is the reply thread under the headline
message (its `ts` is the thread id; `thread_per_run = false` posts everything
top-level), cards are coloured attachments (`[slack] embeds`), link unfurls
are off on every post and edit, reactions use Slack's emoji names, agent
prose is entity-escaped so it can never `<!channel>` anyone, and Slack has
no "reply to a message" outside threads, so the concierge and steering are
@mention-only there (`<@app>` in the control channel or in a run's thread).
`sbxloop doctor` shows one `chat bridge (slack)` row: extra installed, both
tokens present. Switching backends is a config change plus a daemon
restart; runs recorded under the other backend keep their thread rows but
are not re-posted.

## Artifacts

Every job in a run executes in the run's **workspace** — a host directory
(`.sbxloop/runs/<run>/workspace`) that sbx mounts into the agent microVM.
Provisioning *discovers* the in-VM mount point (marker file + bounded search)
rather than assuming one. A run that has no checkout to work on (nothing
configured, not started from inside one) uses an empty per-run directory
instead; when *that* mount can't be found, jobs run in a fallback dir that is
**harvested** to `.sbxloop/runs/<run>/artifacts` with `sbx cp` at each task
end and at run finalize. A configured checkout that fails to mount stops the
run instead (`sbxloop doctor` has the workspace-mount probe). Either way the files an agent
produces survive the sandbox:

```bash
sbxloop run "write a fib.py with tests"   # summary ends with an artifact tree
sbxloop artifacts <run>                   # list a past run's files (--tree for a tree)
cat "$(sbxloop artifacts <run> --path)/fib.py"
```

Harvest, listings and delivery all skip the same set of path components,
matched at any depth: run/VCS state (`.git`, `.sbxloop`) plus the
regenerable dependency and build trees of the supported languages —
`node_modules`, `__pycache__`, `.venv`/`venv`, `*.egg-info`, the Python
tool caches, `target` (cargo/Maven), `.gradle`, `obj` (.NET), `.bundle`,
`CMakeFiles`. Entries may use glob patterns, matched against whole path
components (`*.egg-info` catches pip's project-named metadata directory).
They are large, reproducible from the manifests that *are* delivered, and
nobody wants them in a delivery PR diff. The ambiguous generic names —
`bin`, `build`, `dist`, `out`, `lib`, `vendor` — are **not** excluded, since
each is build output in one ecosystem and checked-in content in another; add
them to `[artifacts] exclude` if your project wants them dropped. Whatever is
excluded is always counted and reported (`12 file(s) excluded (node_modules)`)
in run summaries, `sbxloop artifacts`, and the delivery PR body — never
silently truncated.

## GitHub integration

sbxloop has **no** GitHub capability until you name at least one repository
it may work with — either per run on the command line:

```console
$ sbxloop run "build the thing" --repo you/your-repo
```

or persistently in `sbxloop.toml`:

```toml
[github]
repo = "you/your-repo"   # the ONE repo sbxloop may act on
deliver_base = ""        # base branch for the PR; unset uses the repo's default (or `--deliver-base`)
create_repo = false      # create the repo if missing (or `--create-repo`)
create_public = false    # created repos are private unless flipped (or `--create-public`)
```

Several repositories can be registered instead, as an array of tables — each
entry carries its own delivery settings, an `enabled` switch and an optional
per-repo token environment variable:

```toml
[[github.repos]]
repo = "you/one"
workspace = "~/src/one"   # this repo's host checkout; runs clone from it
deliver_base = "main"

[[github.repos]]
repo = "you/two"
workspace = "~/src/two"
enabled = false           # registered but not polled
token_env = "GH_TOKEN_TWO"  # unset uses the daemon-wide GH_TOKEN
trigger_label = "sbxloop:go" # unset uses [daemon] trigger_label
in_progress_label = "loop:wip"  # any lifecycle label can be renamed per repo
labels = ["team:core"]      # extra labels for this repository
```

Every lifecycle label — `trigger_label`, `in_progress_label`, `failed_label`,
`completed_label`, `blocked_label`, `gated_label` — can be renamed on an
entry; unset ones take the `[daemon]` value, and the six must stay distinct
(case-insensitively) per repository. Nothing creates the trigger label a
human is told to apply, and GitHub creates the lifecycle labels on first
attach with a random color and no description: **`sbxloop init-repo owner/name`** creates the six (plus `[landing] followup_label`) with colors
and descriptions up front, idempotently, through one github-ops sandbox —
run it again after renaming a label. `sbxloop doctor` reports missing labels and a
repository whose Issues are disabled as advisory rows; it does not fix
them. Claiming an issue needs a token that can write issue labels
(fine-grained token or GitHub App: Issues → read and write; classic PAT:
`repo`): a triage-only token can read and comment but not label, and the
claim fails with an error that says so instead of a bare 403.

A run's github-ops sandbox is provisioned **scoped to the repository its work
item came from**: it is told which repository it acts on, and it is given that
repository's `token_env` credential — falling back to the daemon-wide
`GH_TOKEN`/`GITHUB_TOKEN` when the entry names none. The credential split is
unchanged by any of this: the GitHub token only ever enters the github-ops
sandbox, never the agent sandbox (which holds the Copilot token alone) and
never the host.

#### A workspace per repository

A **workspace** is the host git checkout a run's tree is cloned from: every
fresh run clones it into `runs/<run_id>/workspace` on its own branch, so the
run never disturbs the checkout, and the daemon fast-forwards it from
`origin` before each run. With several repositories that checkout cannot be
a single daemon-wide path — one repo's runs would be built out of another
repo's tree — so each entry names its own with `workspace`:

```toml
[[github.repos]]
repo = "you/one"
workspace = "~/src/one"

[[github.repos]]
repo = "you/two"
workspace = "~/src/two"
```

**The origin check.** For every enabled repository, the checkout's
`origin` remote must name that repository (`.git` suffix, ssh vs https and
case are normalised away). A mismatch is a hard failure, named at three
points: `sbxloop doctor` reports it as a failing check, `sbxloop daemon`
refuses to start, and provisioning refuses to clone even if it were somehow
reached. The message names both repositories and the fix. Nothing falls
back to another repository's tree, ever — silently building `you/two` from
`you/one`'s checkout is the bug this check exists to prevent.

**No workspace.** An entry with no `workspace` (and no legacy one that
belongs to it) has no host tree, so its runs clone the repository from its
own remote into the run directory (from the server `[github] api_url`
names, single-branch, optionally blob-filtered — see
[Working against an existing checkout](#working-against-an-existing-checkout)).
The clone authenticates with the run's own GitHub credential — the
daemon-wide `GH_TOKEN`, the entry's `token_env`, or a GitHub App
installation token minted on the host — so **private repositories clone
like public ones**. The token reaches git through a one-shot credential
helper that exists only in that clone's environment: it is never on the
command line, never in the clone's `.git/config` or remote URL, and any
credential helper the host user has configured is switched off for that
process, so the host still holds no git credential of its own. With no
GitHub credential configured at all only a public repository can be
cloned; a failure names which case applied, rather than falling back to
anything. `sandbox.workspace_clone` records whether the clone was
authenticated.

**Migration.** A single-repo deployment's `[sandbox] workspace` keeps
working exactly as before. When you add a second repository, **move
`[sandbox] workspace` into the matching `[[github.repos]]` entry** as
`workspace = "..."` and give the other entries their own. Left at the top
level with several repositories configured, it applies only to the entry
whose `origin` it actually matches; every other repository is refused at
`doctor`/start rather than run from the wrong tree.

The two forms are mutually exclusive: migrate by moving `[github] repo` (and
its `deliver_base` / `create_repo` / `create_public`) into one
`[[github.repos]]` entry. A single `[github] repo` keeps working unchanged and
is normalised internally into a one-entry repo list. Everything under
`[[github.repos]]` is **per repository**; the daemon-wide guardrails — the
daily run cap, the per-item retry cap, the consecutive-failure circuit breaker
and one-run-at-a-time — stay global to the daemon. Work items are keyed by
issue number **and** repository, so issue #4 in two registered repositories is
two independent items; an existing daemon state database is migrated in place
on first start. Rows written before the migration carry no repository: when
exactly one repository is configured they are backfilled with it. When
several are, the daemon first names each row from its issue URL
(`store.repo_attributed_from_url`); of what is left, only rows still sitting
untouched in the queue are dropped (logging `store.repoless_items_dropped`)
because only those can be rediscovered — claiming an issue swaps the
`sbxloop:run` label for `sbxloop:in-progress`, so an already-claimed or
in-flight item can **never** be picked up again by discovery. Those rows are
therefore failed rather than deleted, with an operator notice naming each
item id and issue URL (`daemon.repoless_items_stranded`): their issues keep
the in-progress label until a human clears it and re-adds `sbxloop:run`.
Finished items stay as history either way.

`sbxloop config repos` lists the registered repositories with their enabled
state, base branch, token variable and trigger label; `sbxloop doctor` checks
each enabled repository on its own line (a failing repo never masks the
others' verdicts); `sbxloop status` and `sbxloop daemon items` carry a `repo`
column so every run and work item shows which repository it belongs to.
Commands that need one repository — `sbxloop run`, `config repos --repo` —
default to the sole configured repository and, when several are registered,
ask for `--repo owner/name` rather than guessing.

`repo` is the gate, and there is no separate switch behind it: unset, no
github sandbox is provisioned, `GH_TOKEN` is not needed, and a run ends
`completed` after its gate with the work in the workspace. Set, **every run
that passes its gate opens a pull request there and carries it through
review, CI and the merge** — delivery is not an optional step at the end of
a run, it is the second half of one. CLI flags win over the toml, so
`--repo` can also redirect a configured setup at a different repository for
one run.

The repository is probed right after provisioning, so a missing or typo'd
`--repo` fails the run up front instead of after the work is done. For a
fresh project, add `--create-repo` and sbxloop creates it (private by
default, `--create-public` to flip) with an initial commit, then delivers
the work as a normal reviewable PR — creation is opt-in precisely so a
typo'd repo name errors instead of silently landing in a brand-new
repository. Creating repos needs a token allowed to do so for that owner;
the per-repo minimal token suffices for everything else. An
existing-but-empty repository (no commits yet) is also handled: delivery
bootstraps the initial commit itself.

With `repo` set, runs provision the github-ops sandbox and require a second
PAT, `GH_TOKEN`, used *only* by that sandbox. It needs the repository
permissions in [docs/permissions.md](docs/permissions.md) — contents and
pull requests to deliver and merge, issues to claim and settle, checks and
actions to wait on CI and read failed-job logs. Without it, no github
sandbox exists and repo-facing features refuse to run. The PAT can be
replaced wholesale by a GitHub App installation — see
[GitHub App auth](#github-app-auth). `sbxloop doctor --probe` checks the
token against that table before a run can fail on it (#696): a required
permission the token lacks is a failing row naming the permission and the
feature that first needs it, a missing `workflows:write` is a warning
(only a delivery touching `.github/workflows/` needs it), and a
`github repo <r> ci` row says what Actions the repository has for the CI
stage to wait on — or that it has none.

**Delivery** is one atomic commit via the git data API on branch
`sbxloop/<run>`, opened as a draft pull request, with the harvested tree
filtered by `[artifacts] exclude`. A run clone delivers its `git diff`
against the base — deletions and renames included; a workspace without a
git history delivers a snapshot of the tree. Either way the tree records
what is on disk (#695): an executable script arrives `100755` and a symlink
arrives as a symlink (`120000`, its target as the content), never flattened
to a plain file. Every fix round re-delivers onto the same
branch — force-moved, the open PR reused — so one run is one PR, and
`sbxloop resume <run>` at `delivering` is the retry path when a delivery
failed. The PR's description is the repository's own pull request template
(`.github/PULL_REQUEST_TEMPLATE.md` and the other places GitHub reads it
from) verbatim, followed by the loop's summary — so a check that parses the
template sees its sections — and the planner is told the template exists
and that the last task should write it filled in to `.sbxloop/pr-body`
under the workspace, which then *is* the description (read, never
delivered); a fix round can rewrite the description the same way when a
check judges it. `Closes #N` is always the last line. The PR stays a draft until the review approves and CI is green, so a
watching human reads "draft" as "sbxloop is still working on this". A
repository plan without draft pull requests (GitHub answers the draft with
a 422 saying so) gets a ready PR on one retry, logged
`deliver.draft_unsupported`; the run is otherwise unchanged. The loop clears
only the draft it made: a PR *a person* converts to draft — before the
landing, or after the loop's own un-draft — is a hold, not a block (see
`awaiting_review` in the daemon section).

**Repository conventions.** What the repository says about itself reaches
every phase: `AGENTS.md`, `CLAUDE.md`, `.cursorrules`,
`.github/copilot-instructions.md`, `CONTRIBUTING.md` and `CODEOWNERS` (the
locations GitHub reads them from) are read from the workspace and handed to
the planner, the builder and the reviewer under one heading — "Repository
conventions (from the repository itself — follow them over the defaults
below)" — so "run `make lint` before committing", "never touch
`generated/`" or "PRs need a changelog entry" shape the plan and the review,
not just the build. A file symlinked or copied under two names is rendered
once. The block is capped at `[budgets] repo_context_max_chars` (12,000
characters; the cut is marked, and 0 hands the prompts none of it). Neither
agent backend is left to find these files by its own convention: the block
is the one route, the same on both.

**Naming.** The branch, the PR title and the commit message are rendered
from `[github]` templates — `branch_prefix` (default `sbxloop/`, the run id
appended), `pr_title_template` (default `sbxloop: {title}`) and
`commit_message_template` — with `{title}`, `{outcome}`, `{run_id}` and
`{repo}` as placeholders; each can be overridden per `[[github.repos]]`
entry, so a repository with a title lint, a commit lint or a branch ruleset
gets names it accepts. `{title}` is the plan's own `pr_title`, written in
the repository's commit style (the decomposer is shown the recent `git log`), falling back to the run's outcome. A fix round can retitle the PR
by writing `.sbxloop/pr-title` in the workspace — the file is read, never
delivered — so a red title-lint check is curable like any other; a
re-delivery whose title changed renames the PR. A repository that lints
titles as conventional commits — a `commitlint.config.*` or
`.commitlintrc*`, a `commitlint` key in `package.json`, a workflow running
`amannn/action-semantic-pull-request` or `wagoid/commitlint-github-action`
— is detected from the tree: the planner is told to write `pr_title` as
`type(scope): summary`, and when `pr_title_template` is the default the PR
gets the bare conventional title instead of `sbxloop: {title}` (a title
without a type becomes `chore: …`, lowercased; a template the operator
wrote is left alone). A branch creation GitHub refuses (422) fails the delivery quoting GitHub and naming the knob its
wording points at: a branch-name or creation rule → `branch_prefix`; a
signature rule → signed commits, satisfied by a GitHub App credential; a
locked or archived repository → nothing to configure; wording the loop
does not recognise names no knob, so a guess never sends anyone to the
wrong setting.

**The review is the run's own.** A fresh read-only session reads the diff
and returns a verdict; the run acts on that verdict whatever GitHub does
with it. It is also posted to the PR for the record. **Single-identity
mode** is the common case: one token opens the PR *and* reviews it, and
GitHub refuses `REQUEST_CHANGES` / `APPROVE` from a PR's own author — so
when the PR's author is the loop's login, the review is posted as PR
comments instead of through the review feature: each anchored finding as
its own review comment (a thread that can be replied to and resolved, which
is what reconciliation does in later rounds), and the verdict — in words,
`**Review verdict: changes requested** (round 2)` — with the summary and
any finding that got no thread (no line, over the inline cap, or an anchor
GitHub refused, degraded per finding rather than per review) in one
top-level comment. No review-feature call is attempted, so a round costs no
422s. When a *different* identity reviews (a second token), the verdict is
posted as `APPROVE` / `REQUEST_CHANGES`, falling back to a `COMMENT` review
if the repository refuses it. Neither gates anything on GitHub's side, which
is fine: the gate is in the run. A *human's*
standing `REQUEST_CHANGES` on the PR is honoured — it costs a fix round on
the CI budget — and a human merging the PR themselves is the acceptance,
while a human closing it unmerged fails the run.

**Bots that review.** A GitHub App reviewing the PR (CodeRabbit, Copilot,
Sourcery…) leaves a `CHANGES_REQUESTED` it never dismisses. That is a
signal, not a veto: it buys **one** dedicated fix round with the bot's
findings in the brief and a reply on each of its threads, and a bot review
still standing afterwards is merged over and named in a PR comment — it
never blocks the landing. A person's review keeps full authority, and a
person beside a bot still wins. Who is a bot is read from GitHub
(`user.type`, `author.__typename`), never guessed from a name;
`ignore_reviewers` adds User-type accounts to treat the same way (a
reviewer bot on a personal token). There is no reverse list.

**Whose red is it.** A red check on the delivered head is judged against
the commit the PR is built on (its merge base with the base branch, never
the base's current head) and against what the base's protection and
rulesets require. Red on the base too is *preexisting*: merged over and
named in a PR comment, never fixed — unless the base requires that check,
in which case it is fixed (GitHub would refuse the merge) and the fix brief
says the failure was inherited. Red only on the PR is a *regression*: a
required one gets the full `max_ci_rounds`; one the base does not require
gets one round and is then merged over and named, so a signal no human
demanded never blocks a landing. Absent from the base, or a baseline that
could not be read, counts as the PR's own. A base that declares no required
checks gates on all of them. `required_checks` names the gating set
explicitly; `ignore_checks` drops a check everywhere.

Classic branch protection is readable only by an admin, and an
organization's bot usually has write, not admin. When the base's rules
cannot be read, the required set is taken from the pull request itself:
GitHub marks each check on the PR's head as required or not, evaluated
against the very rules the token cannot read, and serves that with pull
access. It is re-read on every poll (only checks that have reported are
listed), the `ci.status` / `landing.checks` events say `source = "pr-rollup"`, and `sbxloop doctor` says on the repository row that the
checks will come from the PR. Only when that is unreadable too does the
loop fall back to gating on every check.

**A workflow awaiting approval** — a check at `action_required`, which is
how GitHub holds a first-time contributor's or a fork's workflow until a
maintainer approves the run — is neither a failure nor something to wait
out: the run ends `blocked` at once, naming the check and the approval it
needs, with no fix round spent. A real red beside it is fixed first.

**Branch protection.** "Require branches to be up to date" is handled: the
landing stage calls update-branch (bounded by `merge_update_attempts`, each
one API call) and re-judges the new head. A rule the loop cannot satisfy —
signed commits or approval of the last push, say — shows as a
`blocked` mergeability once the checks are green, or as a 405 on merge,
which no retry fixes; the run ends `blocked` with the PR open and out of
draft for a human, and the reason is read from the base's rulesets and
classic protection in full — one line per rule the loop cannot satisfy:
approval of the last push (never satisfiable: the loop is always the last
pusher), signed
commits (satisfied by a GitHub App credential, whose API commits GitHub
signs), a linear-history rule against `merge_method = "merge"`, a
required deployment. The `run.blocked` event carries the same list, and
`sbxloop doctor` reports it per repository before any run. When the *only*
unmet rules are review rules — N approving reviews, a CODEOWNERS review —
the run does not block at all: it parks `awaiting_review` and waits for
the human (see the daemon section). A **merge queue** is not a blocker
either: on a base that merges through one, the loop never sends the merge
itself — where the merge would happen, every other bar cleared, it
enqueues the PR (`land.enqueued`) and polls the queue every
`ci_poll_interval_s` until the queue merges it, removes it, or
`ci_timeout_s` runs out. A removal whose checks failed on the queue's own
merge commit is one CI fix round with those checks named (the usual
`max_ci_rounds` budget); a removal with nothing to fix — a human dequeued
it — ends the run `blocked`; a timeout leaves the PR in the queue and says
so.
Conversation resolution is not a blocker: the loop resolves the threads it
answers. A 409 is a race with a push that landed since; the next poll
re-judges.

**Merge method.** `merge_method = "auto"` (the default) takes the first of
squash, merge, rebase that the repository's settings allow, resolved once
per landing and logged. An explicit method the repository disallows is
never swapped for another: `sbxloop doctor` says so on the repository row,
and a run reaching the merge ends `blocked` naming it.

The post-build stages are configured under `[landing]`, effective only with
a repository:

```toml
[landing]
deliver_draft = true            # the PR opens as a draft; un-drafted once review and CI pass
max_review_rounds = 3           # how many times the review may request changes
max_ci_rounds = 2               # rounds for the mechanical failures: red gate, red CI, conflict, human objection
retry_rounds = 2                # daemon: an exhausted run resumes its own PR once with this many more; 0 hands it to a human
ci_poll_interval_s = 60         # how often the delivered head's check runs are polled
ci_settle_s = 90                # "no check runs yet" must persist this long to mean "this repo has no CI"; calibrated to Actions' registration latency — raise for CI that registers later
ci_timeout_s = 3600             # per wait, not charged to max_wall_clock_s; exceeding it ends the run blocked
merge_method = "auto"           # auto | squash | merge | rebase — auto: the first the repository allows
delete_branch_on_merge = true
merge_update_attempts = 3       # update-branch calls when protection wants "up to date"; 0 disables
required_checks = []            # the checks that gate the merge; empty = what the base's protection/rulesets declare, else all
ignore_checks = []              # fnmatch patterns never waited on, fixed or reported (e.g. "codecov/*")
ignore_reviewers = []           # User-type logins treated as automated reviewers: one fix round, never a block
followups = "issues"            # after the merge, the review's out-of-scope notes: issues | comment | off
followup_label = "sbxloop:follow-up"  # never the trigger label — a human promotes a follow-up to work
max_followups_per_run = 5
review_diff_max_chars = 150000  # the diff shown inline to the reviewer; past it, the reviewer reads the tree
```

Landing is not optional and has no off switch: a run with a repository
either merges, ends `failed` with its PR still a draft, or hands a `blocked`
PR to a human. On a repository whose merges publish — sbxloop's own releases
to PyPI and redeploys the daemon host on every merge to `main` — every
merged run is therefore an unattended release. That is the existing pipeline
working as designed, with nobody in front of it; the round budgets and the
daemon's guardrails are what you are trusting instead.

### GitHub App auth

The github-ops side can authenticate as a **GitHub App installation**
instead of a PAT (#568): create a GitHub App with the repository
permissions in [docs/permissions.md](docs/permissions.md) (Contents, Pull
requests and Issues read & write; Checks and Actions read; Workflows read
& write if runs may edit workflow files), install it on the repository, and
configure

```bash
GITHUB_APP_ID=12345                         # the App's numeric id
GITHUB_APP_INSTALLATION_ID=987654           # from the installation's settings URL
GITHUB_APP_PRIVATE_KEY_PATH=~/keys/app.pem  # or GITHUB_APP_PRIVATE_KEY (PEM inline)
```

in the environment / `.env`, leaving `GH_TOKEN`/`GITHUB_TOKEN` unset. The
host signs a short-lived App JWT with its own `openssl`, exchanges it for a
~1 hour **installation token**, and delivers only that token to the
github-ops sandbox; the private key never leaves the host, and the agent
sandbox still sees no GitHub credential. Every operation — issue claims,
comments, labels, PRs, reviews, merges — is attributed on GitHub to the
app (`<app-name>[bot]`) rather than to a personal account, with no
personal-token expiry to babysit.

That attribution is also how the loop tells its own review threads and
reviews from a person's. The identity comes from the credential — the
App's slug, or `GET /user` on a PAT — and carries whether it is an App,
so a person whose login happens to be `foo` is never mistaken for the
App `foo[bot]` (GitHub lets both exist; the two spell the same once the
`[bot]` suffix REST adds and GraphQL omits is folded). When neither
source can answer — a fine-grained token that cannot call `GET /user` —
set `[github] bot_login` (per repository in `[[github.repos]]`) to the
login GitHub attributes the loop's writes to; the delivered PR's author
is a last resort only because the same credential opened it. A
reconciliation reply counts only when the loop wrote it: a person
quoting the loop's marker back does not make a thread answered.

Tokens refresh themselves: before each github job the loop re-mints when
less than ten minutes of lifetime remain and rewrites the sandbox's env
file, so long runs and the daemon's long-lived polling sandbox never hit
an auth failure mid-flight. The mode is chosen by which credentials you
set: configuring **both** a PAT and App credentials — or an incomplete App
set — is a startup error that names the fix, raised before any microVM
boots. A `[[github.repos]] token_env` stays an explicit per-repo PAT
choice and wins over App credentials for that repository. (App JWTs are
signed with the host `openssl` binary; `sbxloop doctor` checks it is on
PATH.)

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
to compile has to be there. Toolchains are installed before the agent's first
turn, instead of the agent discovering a missing compiler on its first build
and spending revision budget on it. Which ones is resolved once per run:

1. `[sandbox] languages`, when set — the operator's choice, never
   second-guessed.
2. Otherwise, **what the workspace declares**: a `go.mod` selects `go`, a
   `package.json` selects `javascript`, a `Cargo.toml` selects `rust`, and so
   on through the manifests in the table below. The root and two levels of
   subdirectories are read (so a monorepo's `packages/<name>/` count;
   `node_modules`, `vendor`, and dot-directories do not), and every match is
   selected — a repo carrying both `pyproject.toml` and `package.json` gets
   both. A manifest that is not valid UTF-8 (a latin-1 author name)
   is decoded leniently — it still selects its language and its version
   pin is still read — rather than failing the provision.
3. Otherwise `python`, so a workspace with no recognizable manifest behaves
   as it always has.

The run log records the answer and its provenance as a `sandbox.languages`
event (`source` is `config`, `detected`, or `default`, and `signals` names the
manifests that matched), so "why did this run install Go?" has an answer.

**Which series** a toolchain provisions is read from the workspace too. Python
honours `.python-version` (an exact pin) and then `[project] requires-python`
in `pyproject.toml` (a PEP 440 specifier); Node honours `.nvmrc` /
`.node-version` (a major, a full version, or an `lts/<codename>` alias) and
then `engines.node` in `package.json` (a node-semver range). .NET honours
`global.json` — the `sdk.version` band together with its `rollForward`
policy, exactly as the SDK applies it, so a `8.0.400` pin under the default
`patch` policy provisions the pinned 8.0.4xx SDK and refuses a 9.x one.
Java honours `.java-version`, `.sdkmanrc`, `.tool-versions` and a Gradle
`toolchain { languageVersion }` / `sourceCompatibility` as an exact major,
and `maven.compiler.release` / `java.version` in `pom.xml` as a floor (a JDK
21 compiles `--release 17` sources; only a level above the default forces a
newer JDK). Ruby honours `.ruby-version`, `.tool-versions` and the Gemfile's
`ruby` requirement (an exact release, or a RubyGems range from which the
highest installable series is taken).

The rule is the same everywhere: the default series when it satisfies the
declaration, else the highest series this host can install that does. A
declaration no series satisfies **stops the run at resolution**, before any
microVM, with a `toolchains.version_unsatisfiable` error naming the file,
the constraint and the installable series — never the default with a
warning, because a project that pins its runtime refuses the wrong one at
the gate (`dotnet` and `bundler` both hard-fail), and a run that spends its
turns finding that out is worse than one that never starts. Widen the
declaration or pin an installable series. A declaration this host cannot
read at all (`jruby-…`, a `graalvm` alias, a non-JSON `global.json`) is
treated as undeclared, with a `toolchains.version_unreadable` warning.
Every choice is a `sandbox.toolchain` event naming the `series`, its
`source` (the file read, or `default`) and the `constraint` it was read
from — so a probe failure is read against the interpreter the project asked
for. Go needs none of this: its own `toolchain` directive in `go.mod` makes
`go` fetch what the module declares.

A pinned Ruby is compiled from source with `ruby-build` (there is no
official binary), which takes several minutes and is the one install with
its own budget (30 minutes) rather than the provisioning default. A project
that pins Ruby is the strongest case for `sbxloop bake`: bake it once and
every run probes the compiled interpreter instead of rebuilding it.

```toml
[sandbox]
languages = ["python"]   # optional; unset = detect from the workspace
```

| Value        | Also accepts               | Detected from                                                                                      | Installs                                                                                                                     |
| ------------ | -------------------------- | -------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `python`     | `py`, `python3`            | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt`, `Pipfile`, `uv.lock`, `poetry.lock` | `python3-venv`, `python3-pip` (apt), `uv` + Python 3.13 by default (3.8–3.14 by declaration)                                 |
| `cpp`        | `c`, `c++`, `cxx`, `c-cpp` | `CMakeLists.txt`, `meson.build`, `configure.ac`                                                    | `build-essential`, `cmake`, `ninja-build`, `pkg-config` (apt)                                                                |
| `ruby`       | `rb`                       | `Gemfile`, `Rakefile`, `*.gemspec`                                                                 | `ruby-full`, `ruby-dev`, `bundler`, `build-essential` (apt) by default; 3.1–4.0 by declaration, compiled with `ruby-build`   |
| `java`       | `jdk`, `jvm`               | `pom.xml`, `build.gradle[.kts]`, `settings.gradle[.kts]`                                           | `openjdk-21-jdk`, `maven` (apt) by default; JDK 8/11/17/25 by declaration (Temurin tarballs), plus `JAVA_HOME`               |
| `php`        | —                          | `composer.json`                                                                                    | `php-cli` + mbstring/xml/curl/zip (apt), Composer (pinned)                                                                   |
| `javascript` | `js`, `node`, `nodejs`     | `package.json`                                                                                     | Node 24 + npm/npx by default (18/20/22 by declaration; pinned tarballs from `nodejs.org`), plus `pnpm`/`yarn` corepack shims |
| `typescript` | `ts`                       | `tsconfig.json`                                                                                    | `tsc` from npm, on top of `javascript`                                                                                       |
| `bun`        | —                          | `bun.lock`, `bun.lockb`                                                                            | bun (pinned, from npm; the `packageManager` pin by declaration), on top of `javascript`                                      |
| `go`         | `golang`                   | `go.mod`                                                                                           | Go toolchain (pinned tarball from `go.dev`)                                                                                  |
| `rust`       | `rs`, `cargo`              | `Cargo.toml`                                                                                       | cargo, rustc, rustfmt, clippy (pinned rustup)                                                                                |
| `dotnet`     | `csharp`, `c#`, `net`      | `global.json`, `Directory.Build.props`, `*.sln`, `*.csproj`, `*.fsproj`                            | .NET SDK 10 by default (8/9 by `global.json`; pinned builds from Microsoft), plus `DOTNET_ROOT`                              |
| `make`       | `gnumake`                  | `Makefile`, `makefile`, `GNUmakefile`                                                              | `make` (apt)                                                                                                                 |
| `just`       | —                          | `justfile`, `.justfile`, `Justfile`                                                                | just (pinned release binary from GitHub)                                                                                     |
| `task`       | `go-task`, `taskfile`      | `Taskfile.yml`, `Taskfile.yaml`                                                                    | Task (pinned release binary from GitHub)                                                                                     |

Selecting an entry also selects what it is built on — `languages = ["typescript"]` provisions the Node runtime first, then `tsc`.

The three task runners are entries because the gate detector emits their
commands: `make check` on a Go repo fronted by a Makefile needs `make`, and
no sandbox has `just` or `task` unless something installed it. A manifest
selects the runner like any other entry — whether or not it declares a gate
target, since the agent runs `make build` too.

The `javascript` entry covers the package managers too. `corepack enable`
puts `pnpm` and `yarn` shims on PATH; each shim runs the version the
workspace's `package.json` `packageManager` field pins (a lockfile alone
selects the client with corepack's default version), fetched from the npm
registry on first use. `bun` is not a corepack client, so it is an entry of
its own, selected by its lockfile. The verify-command lint requires the
project's scripts to run through the client the lockfile names (`pnpm run …`, `yarn run …`, `bun run …`), and a bare JavaScript dev binary (`eslint`,
`jest`, `vitest`, `tsc`, `prettier`, `mocha`) is rejected in favour of
`npx --no-install <bin>` or the package.json script — a bare binary resolves
to whatever global the sandbox carries, not the version the project pins.

The `python` entry is uv-aware: when the workspace carries a `uv.lock`, the
prompts steer the agent to `uv sync` / `uv run …` instead of a hand-made
`.venv`, and the verify-command lint requires `uv run` heads there (a
`.venv/bin/pytest` beside a lockfile does not carry a uv workspace's own
members). Without a lockfile the `.venv/bin/…` convention is unchanged.
`sbxloop doctor --deep` reports the template's own `python3` against the
pinned series in the `python-version` row.

Three rules apply to every entry. Provisioning is **probe-first** — a template
that already ships the toolchain costs no install and no network. It is
**never fatal** — a failure warns with the toolchain named and the run
continues, since the agent has passwordless `sudo apt-get` as an escape
hatch. And it is **selected, not accumulated** — an explicit `languages`
replaces detection rather than adding to it, so nothing is installed for a
language you did not ask for. The one rider is `git-lfs` (#693): not a
language and not selectable, it is added to whatever set was resolved
whenever a `.gitattributes` in the workspace routes files through
`filter=lfs`, so the sandbox can read and write the assets the repository
keeps there. Heavier toolchains are better baked into a template
(`sbxloop bake`) than downloaded per run.

### Installer hosts are allowed for the selected toolchains

The apt-only entries (`cpp`, `make`) need only the apt mirrors, which are in
the sandbox's always-reachable baseline; `ruby` and `java` are apt-only at
their default series and download only for a declared one, but the allowlist
is computed before the workspace is read, so their hosts are always allowed.
The rest download from a vendor or registry, and **provisioning runs before any task**, so a task's
`egress` declaration is too late to help it. Each toolchain therefore carries
its installer hosts, and the agent sandbox is created with the hosts of the
*selected* toolchains allowed — under a default-deny sbx preset too. A
language that was not selected opens nothing, and `[policy] deny` still wins
over an installer host (the toolchain then fails to provision, loudly).

| Language     | Allowed at provisioning time                                                                       |
| ------------ | -------------------------------------------------------------------------------------------------- |
| `python`     | `github.com`, `release-assets.githubusercontent.com`                                               |
| `ruby`       | `github.com`, `codeload.github.com`, `release-assets.githubusercontent.com`, `cache.ruby-lang.org` |
| `java`       | `api.foojay.io`, `github.com`, `release-assets.githubusercontent.com`                              |
| `php`        | `getcomposer.org`                                                                                  |
| `javascript` | `nodejs.org`, `registry.npmjs.org`                                                                 |
| `typescript` | `nodejs.org`, `registry.npmjs.org`                                                                 |
| `bun`        | `nodejs.org`, `registry.npmjs.org`                                                                 |
| `go`         | `go.dev`, `dl.google.com`                                                                          |
| `rust`       | `static.rust-lang.org`                                                                             |
| `dotnet`     | `builds.dotnet.microsoft.com`                                                                      |
| `just`       | `github.com`, `release-assets.githubusercontent.com`                                               |
| `task`       | `github.com`, `release-assets.githubusercontent.com`                                               |
| `git-lfs`    | `lfs.github.com`, `github-cloud.githubusercontent.com`, `media.githubusercontent.com`              |

`sbxloop bake` allows the same hosts for the configured `languages` (there is
no workspace to detect from at bake time) and installs those toolchains into
the template. A run on a prebaked template probes its own resolved set in one
shot and provisions only what the template lacks, under the run's allowlist.

Without them the install warns and the run continues — the agent falls back to
bootstrapping the toolchain itself, which is the behavior these entries exist
to improve on, not a broken run. Baking the toolchain into a template
(`sbxloop bake`) sidesteps the per-run download entirely.

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
(merged/completed/failed/blocked/cancelled), unknown to this working copy's state DB, or
non-terminal but silent past `--min-age` (default 1 hour — the persisted event
stream, heartbeats included, is the liveness signal). Sandboxes deliberately
kept for debugging are excluded unless you pass `--include-kept`. `sbxloop doctor` reports the current orphan-candidate count.

Run directories accrete too: every run leaves `<state_dir>/runs/<run>/` — a
full clone of the target checkout under workspace isolation, plus harvested
artifacts — and an always-on daemon fills the disk with them. The daemon
sweeps them on start and once a day (`[daemon] prune_runs_after_days`,
default 14; `0` disables), and `sbxloop gc` runs the same policy by hand:

```bash
sbxloop gc --dry-run             # classify every run directory, remove nothing
sbxloop gc                       # remove those past the retention window
sbxloop gc --older-than 3        # a tighter window for this sweep only
```

Only terminal runs (merged/completed/failed/blocked/cancelled) past the window go, and never
one whose sandboxes were kept or whose delivery failed — that directory is the
only copy of the work until it is fetched or redelivered. The SQLite rows stay
(they are the audit trail); each removal is recorded as a `daemon.gc` event on
the run, and `resume` refuses a run whose workspace is gone. Fetch results
back within the retention window — the finish summary prints it.

## Setup

1. Install [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/), then
   `sbx login` and `sbx policy init balanced`.

2. Create a fine-grained GitHub PAT:

   - `COPILOT_GITHUB_TOKEN` — personal account, **Copilot Requests**
     permission. Used *only* by the agent sandbox.

   Export it, or put it in a `.env` file (loaded from `~/.config/sbxloop/`
   and from the working directory when that is not inside a git checkout;
   real environment variables always win):

   ```bash
   cp sbxloop.toml.example sbxloop.toml   # every key, commented, with its default
   cp .env.example .env                   # then fill in the token(s)
   ```

   `sbxloop.toml.example` is the file `sbxloop init` writes (`sbxloop init --stdout` prints it), and `.env.example` names every credential and
   `SBXLOOP_*` override the code reads.

3. **Optional** — configure the [GitHub integration](#github-integration)
   (adds the second credential: the `GH_TOKEN` PAT, or
   [GitHub App auth](#github-app-auth)).

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
`sbxloop doctor --fail-on-drift` turns that warning into an exit code (any
drifted, errored, or unprobed probe fails) — the CI e2e lane uses it, and the
scheduled `sbx-conformance` workflow runs it against the newest sbx release
ahead of adoption. Under the copilot backend doctor also checks the
installed Copilot SDK's permission-kind vocabulary against the
field-verified snapshot backing the read-only critic barrier.

### Secret registration hygiene

sbx keys custom secrets by env var name (one registration per var, whatever
the scope), so leftover registrations from old runs or old versions surface
as `already exists in scope …` collisions. Provisioning recovers
automatically, and `sbxloop secrets` manages the same state proactively:

```bash
sbxloop secrets list             # registrations + pre-collision warnings
sbxloop secrets clean            # dry-run removal of stale entries (--apply to execute)
sbxloop secrets rotate           # replace the agent credential's registration
                                 # (COPILOT_GITHUB_TOKEN, or ANTHROPIC_API_KEY under claude)
                                 # (token from env/.env or --prompt, never argv)
```

`rotate` also reports which secret strategy (proxy vs plain-env fallback) the
next run will use. None of these commands touch the built-in `github` service
secret or registrations owned by other tools.

## Configuration

Configuration resolves, in order, from `SBXLOOP_*` environment variables,
`sbxloop.toml`, `pyproject.toml [tool.sbxloop]`, and a user-level
`~/.config/sbxloop/sbxloop.toml` (`$XDG_CONFIG_HOME` honoured) for settings
that follow you rather than the checkout. The two files are looked for in
the current directory and, inside a git checkout, in each parent up to the
checkout's top level — the nearest one wins, so a command typed from
`packages/foo/` of a monorepo sees the root config. `sbxloop init` writes a
commented starter file — the same `sbxloop.toml.example` committed at the
repo root; `sbxloop config show` prints every resolved value and where it
came from.

**Whose file is it.** A config file the target repository *carries* —
tracked in git, so any merged pull request can change it, the loop's own
included — is project config: it may set how the tree is built and checked
(`[sandbox] languages`, `gate_command`), how its branches and PRs are named
(`[github] branch_prefix`, `pr_title_template`, `commit_message_template`)
and `[artifacts] exclude`, and nothing else. Egress policy, the merge gate,
budgets, the daemon, `state_dir`, which repository the token delivers to:
those are honoured only from files the operator owns — the user config, an
*untracked* `sbxloop.toml` (what `sbxloop init` writes, or a daemon's runner
directory outside any checkout) or the environment. Keys a tracked file may
not set are dropped with a `config.project_layer.ignored` warning naming
them. The same boundary applies to `.env`: it is read from the working
directory only outside a git checkout (a checkout's `.env` belongs to the
application in it) and always from `~/.config/sbxloop/.env`.

The notable knobs:

| Key                                                       | Default            | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| --------------------------------------------------------- | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `model`                                                   | `auto`             | Model id for the configured `[agent] backend` (`--model` overrides per run; `sbxloop list-models` lists them).                                                                                                                                                                                                                                                                                                                                                                                            |
| `state_dir`                                               | `~/.sbxloop`       | Runs, workspaces, artifacts, SQLite state, event logs.                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `keep_sandboxes` / `keep_on_failure`                      | `false`            | Sandbox retention for debugging (see above).                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `secret_strategy`                                         | `proxy`            | `proxy` keeps token values out of the VM; `plain-env` skips the sbx proxy — tokens are piped per job over worker stdin when this sbx supports it, else written to an in-VM env file. On current sbx the cached exec-visibility verdict makes the non-proxy / env-file fallback the common case even under `proxy`, not an edge case (#46; interim hardening #592).                                                                                                                                        |
| `[sandbox] template`                                      | unset              | Baked template ref from `sbxloop bake`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `[sandbox] workspace`                                     | unset              | Where runs execute; unset gives each run a fresh dir under `state_dir`.                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `[sandbox] workspace_isolation`                           | `auto`             | Per-run clone isolation when `workspace` is a git checkout (see below).                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `[sandbox] gate_command`                                  | detected           | The project's own gate, run over the whole tree before delivery.                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `[sandbox] clone_filter`                                  | unset              | Git partial-clone filter (`"blob:none"`) for the remote clone of a repository with no host checkout; opt-in, see the clone section for the lazy-fetch hazard.                                                                                                                                                                                                                                                                                                                                             |
| `[sandbox] clone_submodules`                              | `true`             | Whether a fresh run clone's submodules are populated — from the host checkout's copy when it has the recorded commit, else from the `.gitmodules` URL with the run's credential; a submodule neither can populate fails provisioning by name. `false` leaves them empty.                                                                                                                                                                                                                                  |
| `[sandbox] clone_lfs`                                     | `true`             | Whether a fresh run clone's Git LFS pointer files are populated — from the host checkout's LFS store when it holds the object, else from the repository's LFS endpoint with the run's credential; needs `git-lfs` on the host, and an object neither can supply fails provisioning by name. `false` leaves the pointer files.                                                                                                                                                                             |
| `[sandbox] fetch_tags`                                    | `"auto"`           | Whether a fresh run clone fetches the repository's tags — from the host checkout when it has them, else from origin with the run's credential. `auto` fetches when a manifest names a tag-derived versioning tool (`setuptools_scm`, `hatch-vcs`, `GitVersion`, `git describe`, …); `always` fetches regardless; `never` leaves the `--no-tags` clone as it is.                                                                                                                                           |
| `[sandbox] extra_allow_domains`                           | `[]`               | Static egress allows applied to every run.                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `[sandbox] env`                                           | `{}`               | Environment for the agent sandbox's worker and everything it runs: plain values only — the agent sandbox holds no operator secret (a registry token goes on `[[registries]] auth_env`, a service key on `[[credentials]]`; `secret_env` is refused by name, #766). Per-repo overridable; see "Environment for the agent sandbox".                                                                                                                                                                         |
| `[sandbox] apt_packages` / `setup_commands`               | `[]` / `[]`        | OS packages ensured beside the toolchains (fail closed), and commands run in the workspace before the first phase, each a `sandbox.setup` event. Per-repo overridable; see "OS packages and setup commands".                                                                                                                                                                                                                                                                                              |
| `[sandbox] verify_mode`                                   | `full`             | What the verify phase and the gate decide: `full` gates, `advisory` runs and reports without blocking, `ci-only` skips them and relies on the PR's checks. Per-repo overridable; see "Suites that need services".                                                                                                                                                                                                                                                                                         |
| `[[registries]]`                                          | none               | Private package registries: an open entry opens `host` for the agent sandbox and writes the ecosystem's client config there (`~/.npmrc`, `PIP_INDEX_URL`, `GOPRIVATE`, `~/.cargo/config.toml`, `settings.xml`, `NuGet.Config`, `BUNDLE_*`); an entry with `auth_env` is reached only from the service sandbox, which fetches into the shared `.sbxloop/deps` cache while the agent sandbox works offline with a `fetch_dependencies` tool (#766). Per-repo overridable; see "Private package registries". |
| `[[credentials]]`                                         | none               | The catalogue of credentials a run may be granted by name (#765): `name`, `env` (the daemon-environment variable holding the value), `host` (the one host it is good for), `header` / `scheme` (how it is attached; `Authorization: Bearer` by default). A granted run gets a third, service sandbox holding exactly those values and a `call_service` build tool; the agent sandbox never holds them. `sbxloop doctor` checks every `env` is set.                                                        |
| `[sandbox] languages`                                     | detected           | Toolchains pre-installed in the agent sandbox; unset = detect from the workspace's manifests, `python` if none (see below).                                                                                                                                                                                                                                                                                                                                                                               |
| `[policy] allow` / `deny`                                 | `[]`               | Bounds for task-declared egress.                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `[github] repo`                                           | unset              | The GitHub integration gate: with a repository every run delivers, reviews and merges. `deliver_base`, `create_repo`, `create_public`, `pr_title_template`, `commit_message_template`, `branch_prefix`, `bot_login` beside it.                                                                                                                                                                                                                                                                            |
| `[github] api_url`                                        | api.github.com     | The GitHub REST root — GitHub Enterprise Server: `https://ghe.example.com/api/v3`. One source of truth for the REST transport, App auth, `gh` (`GH_HOST`) and both sandboxes' network allows; a `GH_HOST` in the daemon's environment that names another host is refused at config load. FIELD-UNVERIFIED on GHES.                                                                                                                                                                                        |
| `[landing]`                                               | see above          | `deliver_draft`, `max_review_rounds`, `max_ci_rounds`, `retry_rounds`, `followups`, `followup_label`, `max_followups_per_run`, `ci_poll_interval_s`, `ci_settle_s`, `ci_timeout_s`, `merge_method`, `delete_branch_on_merge`, `merge_update_attempts`, `required_checks`, `ignore_checks`, `ignore_reviewers`.                                                                                                                                                                                            |
| `[artifacts] exclude`                                     | see above          | Path components dropped from listings, harvest and delivery (replaces the default, does not add to it).                                                                                                                                                                                                                                                                                                                                                                                                   |
| `[budgets]`                                               | see above          | `max_revisions_per_task`, `max_replans_per_task`, `max_tasks`, `max_wall_clock_s`, `per_job_timeout_s`, `max_tool_calls_per_phase`, `max_parallel_tasks`, `repo_context_max_chars`, `outcome_max_chars`.                                                                                                                                                                                                                                                                                                  |
| `[limits]`                                                | `85` / `95` / `90` | `disk_warn`, `disk_abort`, `mem_warn` percentages (0 disables).                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `[daemon] trigger_label` … `gated_label`                  | `sbxloop:run` …    | The issue labels: `trigger_label`, `in_progress_label`, `completed_label`, `failed_label`, `blocked_label`, `gated_label`; each can be renamed per `[[github.repos]]` entry. `sbxloop init-repo` creates them.                                                                                                                                                                                                                                                                                            |
| `[daemon] max_runs_per_day`                               | `12`               | Runs allowed per calendar day, counted by start time in `run_cap_timezone`; the count resets at 00:00 there.                                                                                                                                                                                                                                                                                                                                                                                              |
| `[daemon] run_cap_timezone`                               | `UTC`              | IANA timezone defining the run cap's day boundary.                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `[daemon] max_attempts_per_item` / `max_resumes_per_item` | `2` / `2`          | Per-item retry and resume caps; `retry_backoff_s`, `max_consecutive_failures`, `breaker_cooldown_s` beside them.                                                                                                                                                                                                                                                                                                                                                                                          |
| `[daemon] run_stale_after_s`                              | `21600`            | With no run executing, non-terminal runs idle this long are reconciled to a terminal state (`0` disables).                                                                                                                                                                                                                                                                                                                                                                                                |
| `[daemon] prune_runs_after_days`                          | `14`               | Run-directory retention, swept on start and daily (`0` disables).                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `[daemon] workspace_isolation`                            | `clone`            | Isolation for daemon runs against a git-checkout workspace (dirty tree proceeds with a warning).                                                                                                                                                                                                                                                                                                                                                                                                          |
| `[daemon] refresh_workspace`                              | `true`             | `git fetch` + fast-forward the workspace checkout before each fresh daemon run.                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `[daemon] state_dir`                                      | unset              | Absolute daemon state location; unset resolves to `$XDG_STATE_HOME/sbxloop/<runner-dir>` (see above).                                                                                                                                                                                                                                                                                                                                                                                                     |

sbxloop does not size the sandbox: `sbx create` is called without CPU or
memory flags, so the microVM is whatever size sbx gives every sandbox.
Memory pressure is instead made visible through `[limits]`: `mem_warn` emits
a warning; `mem_abort` (off by default, because a parallel test run spikes
memory transiently) fails the task with an explicit "sandbox memory
exhausted" error instead of letting an in-VM OOM surface as an inexplicable
test failure.

### Sizing budgets for a larger repository

The `[budgets]` defaults (2 h wall clock, 60 tool calls per phase) suit a
project whose gate finishes in seconds. The signal that they are too small
is measured gate duration, not repository size: when one run of the gate
command — the test suite plus linters, what every verify pass executes —
takes two minutes or more, 20 tasks × 3 attempts × verify presses on the wall
clock and a multi-package tree eats the tool cap before any edit. `sbxloop init --preset large-repo` writes a starter file with the packaged preset
appended (4 h wall clock, tool cap 80, `[limits]` with `mem_abort` on); the
preset ships inside the wheel as
[`sbxloop/data/presets/large-repo.toml`](packages/sbxloop/src/sbxloop/data/presets/large-repo.toml)
and its header says how to apply the same sections to an existing file.
Verify output handed back to the builder keeps the first 2 KB and the last
4 KB of each command, so a long test run's first traceback and its failure
summary both survive.

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

[`AGENTS.md`](AGENTS.md) (also reachable as `CLAUDE.md`) is the working
agreement for this repository — what sbxloop is for, the principles each
change is held to, where things live and the gate sequence — written for a
contributor's coding agent as much as for the contributor. Read it before
opening a pull request.

`make lint` also runs `scripts/check_self_references.py`, the gate against
sbxloop leaking into what users see: a bare `#N` from this tracker in an
error message, a doctor row, a prompt body or a file `sbxloop init` writes,
an sbxloop source path quoted into a prompt, or a maintainer/host identifier
outside `contrib/`, `docs/`, `.github/`, package metadata and tests. It fails
with `path:line: rule: text`; the deliberate exceptions live in one reviewed
file, `scripts/self-references.allow`, and an entry that no longer matches
anything fails the gate too. CI's push trigger is `main` alone — working
branches are built through their pull request, and a push filter that also
matched them would run every job twice.

Unit and contract tests run against a **fake sbx CLI** — no Docker Sandboxes
install is required for development. The suite runs parallel by default
(pytest-xdist; pass `-n0` for a serial run when debugging with `-s`/`--pdb`).
Every test on the fake sbx is process-bound and carries the `slow` marker
automatically: `make test-fast` (`-m "not slow"`) is the two-minute commit
gate, `make test` is everything, and CI runs the slow half spread over
runners with `--shard I/N`. The real-sbx end-to-end suite runs in CI via a
manually dispatched workflow.

## Documentation

- [Architecture](docs/architecture.md) — layers, the sandbox-pair security model, the loop, persistence/resume, landing, the daemon
- [Worker protocol](docs/worker-protocol.md) — the host↔worker contract: job kinds, events, transports
- [Running the daemon as a service and upgrading it](docs/deploy.md) — systemd, the two-command upgrade, and the optional workflow that keeps a host current from PyPI: drain, upgrade, restart, health check, roll back
- [How sbxloop deploys itself](docs/self-deploy.md) — this repository's own host and pipeline, and its cutover notes
- [Spike: agent-session backend](docs/spikes/46-agent-session-backend.md) — feasibility study for proxy-held secrets via sbx native sessions (issue #46)
- [Changelog](CHANGELOG.md)

## Requirements

- Python ≥ 3.13
- [Docker Sandboxes (`sbx`)](https://docs.docker.com/ai/sandboxes/) on the host (macOS Apple silicon, Windows 11, or Ubuntu 24.04+/KVM)
- A GitHub Copilot subscription (any plan) + a fine-grained PAT (plus a second PAT or a GitHub App installation only if the GitHub integration is configured — see above)

## Releasing

Releases are fully automated — just merge to `main`. Every merge runs the full check suite, auto-bumps the patch version via a new `vX.Y.Z` git tag ([hatch-vcs](https://github.com/ofek/hatch-vcs) derives both package versions from the tag), and publishes both distributions to PyPI via [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC, no token secrets). See [RELEASING.md](RELEASING.md) for details, including how to cut a minor/major release. The manually-dispatched `e2e.yml` workflow installs real sbx on a GitHub runner for end-to-end validation.

## License

MIT
