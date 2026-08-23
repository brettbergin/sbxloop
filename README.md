# sbxloop

[![CI](https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml/badge.svg)](https://github.com/brettbergin/sbxloop/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sbxloop)](https://pypi.org/project/sbxloop/)
[![Python](https://img.shields.io/pypi/pyversions/sbxloop)](https://pypi.org/project/sbxloop/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Agentic loop orchestration on [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) (`sbx`), with hard credential isolation.**

sbxloop turns a large outcome ("migrate this service to async", "add coverage
to every untested module") into a supervised agentic loop: it **decomposes**
the outcome into a task graph, then for each task
**builds → verifies**, with
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
              BUILD ─▶ VERIFY ─▶ done
                ▲        │fail (≤ revisions: same session resumes;
                └────────┘        exhausted: fresh session, one replan)
```

- **Decompose** — produces the task DAG, and with it every task's
  `verify_commands` (the whole mechanical exam — the builder cannot edit
  them) and any declared network egress needs (see
  [Network egress](#network-egress-least-privilege-by-plan)).
- **Build** — one Copilot agent session plans and does the work in the
  sandbox workspace, narrating its approach first. A revision resumes the
  same session; a replan (or a chat steer) starts a fresh one.
- **Verify** — mechanical: the task's `verify_commands` must exit 0, run from
  the workspace root. No LLM. The full command transcript is persisted with
  the attempt, so a resumed run judges with the real evidence.

There is no in-run critic: the per-task review stages audited task
completion and rubber-stamped it while diff-level defects leaked to the PR.
Adversarial review lives in the daemon's post-delivery review lane, which
sees the whole diff and drives bounded fix rounds on the delivered PR.

**Budgets, not vibes.** Revisions, replans, task count, and wall clock are all
bounded (`[budgets]` in config; defaults: 2 revisions and 1 replan per task,
20 tasks, 2 h wall clock, 15 min per job). Budget exhaustion fails the *task*;
its dependents are skipped and the run continues, finishing `failed` if any
task failed. One deliberate exception: when revisions are exhausted by
*verify-command* failures, the task spends a replan first when budget
remains — the builder cannot edit verify commands, so only a fresh session's
fresh approach can unstick work that disagrees with where a check looks.

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

| Command                               | What it does                                                                                                                                                                                                                                          |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sbxloop run "OUTCOME"`               | Start a run. Options: `--repo`, `--report`, `--deliver`, `--deliver-base`, `--deliver-draft`, `--model`, `--keep-sandboxes`, `--keep-on-failure`, `--no-tui`.                                                                                         |
| `sbxloop daemon`                      | The always-on outer loop: poll labeled issues + an inbox dir, run each item, report back, mirror to Discord. Options: `--repo`, `--inbox`, `--backlog`, `--discord-channel`, `--once`, `--dry-run`.                                                   |
| `sbxloop daemon ctl CMD`              | Drive the running daemon from a script or cron: `status`, `pause`, `resume`, `cancel`, `queue` — the same verbs as Discord's `!sbx`, over a file queue in `state_dir/daemon/ctl/`.                                                                    |
| `sbxloop resume RUN`                  | Re-provision sandboxes and continue a checkpointed run under its persisted config.                                                                                                                                                                    |
| `sbxloop deliver RUN`                 | Deliver (or re-deliver) a completed run's artifacts as a PR from a github-ops sandbox alone — the retry path when end-of-run delivery failed. Options: `--repo`, `--deliver-base`, `--deliver-draft`, `--create-repo`, `--create-public`, `--report`. |
| `sbxloop cancel RUN`                  | Cancel an in-flight run.                                                                                                                                                                                                                              |
| `sbxloop status [RUN]`                | List runs, or show one run's task/phase detail.                                                                                                                                                                                                       |
| `sbxloop logs RUN`                    | The persisted event stream. `--type` filters by prefix (e.g. `--type policy.`), `--task` by task id.                                                                                                                                                  |
| `sbxloop artifacts RUN`               | List a run's harvested files. `--tree` renders a tree; `--path` prints just the directory (for scripting).                                                                                                                                            |
| `sbxloop shell RUN`                   | Interactive shell in a run's sandbox. `--role agent\|github` picks the pair member; `-c CMD` runs one command.                                                                                                                                        |
| `sbxloop init`                        | Write a commented starter `sbxloop.toml` (`--force` overwrites).                                                                                                                                                                                      |
| `sbxloop bake`                        | Bake a sandbox template with the worker preinstalled (`--ref`, `--from`, `--keep`).                                                                                                                                                                   |
| `sbxloop doctor [--deep]`             | Verify the host setup; `--deep` boots a scratch sandbox for the full sbx conformance suite.                                                                                                                                                           |
| `sbxloop sandbox ls\|rm\|prune`       | Inspect, remove (`--run`, `--all`), or garbage-collect orphaned sbxloop sandboxes.                                                                                                                                                                    |
| `sbxloop gc`                          | Remove old run directories (workspace clones, harvested artifacts) past the retention window; `--older-than DAYS`, `--dry-run`.                                                                                                                       |
| `sbxloop secrets list\|clean\|rotate` | Manage the sbx custom-secret registrations sbxloop owns.                                                                                                                                                                                              |
| `sbxloop config show\|policy`         | Resolved configuration with per-key sources; the effective egress policy.                                                                                                                                                                             |

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

## The daemon: an always-on outer loop

`sbxloop daemon` wraps the run loop in a persistent process that discovers
work, runs each item as a full inner-loop run, reports back to wherever the
work came from, and keeps going. Two work sources, usable together:

- **GitHub issues** in the target repo (`--repo` / `[github] repo` — the
  repo being worked on) carrying the trigger label (`sbxloop:run` by
  default). The daemon claims the issue (label swap + comment), runs it with
  reporting and delivery forced on (PRs arrive as **drafts** by default),
  and on success comments the summary + PR link and adds
  `sbxloop:delivered`. The issue settles when the PR **merges**: the daemon
  watches the delivered PR, and on merge closes the issue and swaps in
  `sbxloop:completed` (the PR body also carries `Closes #N`, so GitHub
  links the pair and closes the issue even when the daemon is down). A PR
  closed without merging marks the item failed and leaves the issue open.
  Run failures retry with backoff, then land in `sbxloop:failed` with
  re-trigger instructions. `tracking_issue = false` in `[daemon]` skips the
  per-run tracking issue (the summary comment on the source issue carries
  the same info).
- **Inbox files**: drop a `.md` (first `# heading` = title) into
  `<inbox>/pending/`; it moves through `running/` to `done/` or `failed/`
  with a `<name>.result.md` beside it.

It is **fully autonomous** — a label or a file alone starts a run — so the
spend guardrails in `[daemon]` are the safety net: a calendar-day run cap
(`max_runs_per_day`, default 12 — the name and default are unchanged) that
counts the runs *started* since 00:00 in `[daemon] run_cap_timezone` (any
IANA zone, default `UTC`) and resets at that boundary, so a run started just
before midnight does not free a slot early (earlier releases aged each run
out individually a fixed period after it started rather than at the day
boundary); a per-item attempt cap, a per-item resume cap, and a consecutive-failure
circuit breaker (persisted, so a restart cannot reset it). Treat the
trigger label as "execute arbitrary instructions with GH_TOKEN's repo scope"
and restrict who can apply it. Inner agents can file follow-up work they
discover (`--backlog github|inbox`) — those land in **triage** (the
`sbxloop:backlog` label / `inbox/triage/`) and never run until a human
promotes them, unless `backlog_auto_trigger` is set.

**The discovery lane.** An issue carrying `sbxloop:audit` (`[daemon] audit_label`) is a *charter*, not a change: the run investigates — "review
`daemon/loop.py` for guardrail holes", "post-mortem run rXXXX" — and its
deliverable is findings, each written as one file under `.sbxloop/backlog/`
with **Evidence** (file:line), **Repro**, **Proposal**, **Size** and
**Kind**, which the daemon files as `sbxloop:backlog` issues (so
`--backlog github` is required for the lane to produce anything). Audits
never deliver a PR, close on completion with a `Filed: #…` comment, and an
audit that finds nothing real says so. Promoting a finding is a label swap
(`sbxloop:backlog` → `sbxloop:run`) — a human decision, so the loop's
precision is visible before anyone hands it the keys.
When a patch item is abandoned (or completes without delivering) the daemon
files a **post-mortem** as an audit charter — the plan, the last verify
transcript, the failure events, all in the issue body, because the auditor
works in a fresh clone and cannot read the daemon's state — so the loop's own
failures become findings too (`[daemon] postmortems`, capped per calendar day
in `run_cap_timezone`, never for audit items).
And with `[daemon] audits = true`, **charters versioned in the repository**
(`.github/sbxloop/audits/<name>.md`, front-matter `every: 7d`) are opened as
`audit: <name>` issues on schedule — reviewed like code, visible as issues,
deduplicated against GitHub itself so a fresh state dir cannot double-file.
sbxloop's own repo carries four (verify-lint vs. prompts, daemon guardrails,
e2e markers, test flakes); every finding they produce is one more
`sbxloop:backlog` issue for a human to promote or close.

Two more things close the loop. Every PR the daemon delivers gets a
**review audit** (`[daemon] review_deliveries`): a fresh run reads the source
issue and the diff as a skeptical maintainer and files defects, missing
tests and scope drift as backlog issues — sbxloop evaluating the code
sbxloop wrote. And findings *about the tool itself* — the decomposer wrote a
bad verify command, a prompt misled the agent, delivery mishandled a case —
are never dumped on the project's tracker: the audit contract routes them
to `.sbxloop/backlog/tool/`, and `[daemon] tool_repo = "brettbergin/sbxloop"`
files them upstream (unset: they are noted in the closing comment only).

Polling and issue lifecycle run through a long-lived github-ops sandbox the
daemon owns, so the host still never holds the PAT. Runs are one at a time;
an interrupted run (SIGTERM, crash) is resumed on the next start — through
the same guardrails as any dispatch, and at most `max_resumes_per_item` times
before it counts as a failed attempt. Ship it as a systemd user service with
[`contrib/systemd/`](contrib/systemd/).

Individual items are steerable from another shell without stopping the
daemon: `sbxloop daemon items` lists them (state, attempts, pinned run, last
error); `sbxloop daemon abandon <item> [--reason …]` gives one up (a live
daemon cancels its in-flight run and tells the issue/inbox file — the report
is owed on the row and paid by the next tick or the next daemon start, once);
`sbxloop daemon retry <item>` re-queues an abandoned or cancelled item with attempts
reset and a **fresh build session** — not a resume of the approach that
failed; and
`sbxloop daemon requeue <item>` drops a running item's pinned run so its
next dispatch starts over (attempts and backoff kept). The same controls are
`!sbx items|abandon|retry|requeue` on Discord.

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

#### Working a design tracker

A source issue always settles on **merge**, not on delivery: at acceptance
the daemon comments the run summary and PR link, removes
`sbxloop:in-progress`, and adds `sbxloop:delivered`; when the PR merges it
closes the issue (`state_reason: completed`) and swaps `sbxloop:delivered`
for `sbxloop:completed`. A PR closed without merging marks the item failed
(`sbxloop:failed`) and leaves the issue open for the human to re-trigger or
close. (`close_on_success`, which used to close the issue at acceptance, is
now a deprecated no-op.) For a tracker whose issues are design discussions
rather than a queue of chores:

- `[daemon] tracking_issue` (default `true`) — with `false` no per-run
  tracking issue is opened; the summary comment on the source issue is the
  record, so the design thread stays in one place.

```toml
[daemon]
deliver_draft = true          # PRs arrive as drafts for review
tracking_issue = false        # the summary comment is the record
workspace_isolation = "clone" # never touch the runner's checkout
refresh_workspace = true      # fast-forward it before each fresh run
```

### Discord: chronology out, steering in

With `pip install 'sbxloop[discord]'`, `DISCORD_BOT_TOKEN` in the
environment, and `[discord] channel_id` set, a gateway bot posts a headline
card per run in the control channel (source, run id, branch, tracking
issue, PR, task tally — colour follows the state) and streams that run's
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
✅/❌/⚠) that also names what the run filed — an audit's findings, linked
(`Filed`, `Upstream` for findings routed to `[daemon] tool_repo`, or
`no findings`). Audit-lane notices in the control channel follow the same
shape — `🔎 audit #701 filed for charter flakes · audit: flakes`,
`🔎 review #801 filed for PR #9 · gh:4`, `🔎 post-mortem #901 filed for gh:4 · abandoned: …`, `✅ gh:9 done (2/2 tasks done) · filed #50` — with every
issue number a link. Mentions are always disabled, so model output can never ping the
channel. `[discord] embeds`, `status_line`, `tool_batch_lines` and
`chronology_level` tune it. **@mention the bot in a run's thread to steer that run**
(or reply to one of its messages there) — the same rule the control channel
uses, so people can talk about a run in its own thread without derailing it.
Your message is
relayed to the agent exactly like the CLI's `--chat` (answered at the next
checkpoint, which can be minutes into a long step — a note under your
message says where the agent is, `⏳ steer queued — agent is mid-execute on t2 (12/40 tool calls so far)`, edited in place until the ⏳ reaction turns ✅
when the reply lands). `!sbx status|pause|resume|cancel [--retry]|queue|items|abandon <item> [reason]|retry <item>|requeue <item>` in the control channel drive the daemon
itself. `!sbx cancel` stops the current run at its next boundary and settles
the item as **cancelled** — attributed to you on the source, no automatic
retry, no breaker count — while the run stays resumable (`sbxloop resume RUN`
on the daemon host); `!sbx cancel --retry` re-queues it for a fresh run
instead, and `!sbx retry <item>` reruns any cancelled or abandoned item with
its attempt budget reset. Those verbs work in a run's thread too, answered
where you typed them. Anyone who can post in the channel
can steer — that is the boundary to set. The bot ignores messages from bots
(itself included), so scripts drive the daemon with `sbxloop daemon ctl <verb>`
instead — the same verbs through the same dispatcher, no Discord needed; a
request no daemon picks up within `--timeout` (30s) is withdrawn, so a stale
`cancel` never fires when the daemon starts later. Timing out is not "not
executed": once the daemon has taken a request it keeps running (item verbs
cross the ops sandbox), and `ctl` reports it as pending (exit 1) rather than
absent (exit 2).

**Chat with the daemon.** @mention the bot in the control channel (or reply
to one of its messages) and the **concierge** answers — the channel's own
agent, which knows how to operate sbxloop and what it is building. Ask
"what's running?", "why did `r7…` fail?", "show me the diff of that PR",
"pause after this one", or "also please add retries to the fetch client"
— it runs the same `!sbx` verbs through the same dispatcher, reads the
run store (runs, tasks, chronology, reports), fetches PR/issue/diff/file
details through the github-ops sandbox, queues new work as an inbox
item with a self-contained title and body, and files issues in the
configured repo from a described feature or bug — created with the
`sbxloop:backlog` label (triage), after which it **asks you** whether to
add `sbxloop:run` and labels only on your yes; "what's in the backlog?"
lists the open `sbxloop:backlog` issues and asks which, if any, to work.
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
every merge to `main` publishes a patch while upgrading this host is
manual, so the daemon also says so once at startup when it is behind.
(It only reports: upgrading is `pip install --upgrade sbxloop` plus a
restart, by a human on the host.)
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
Ask "how is PR #41 doing?" and `pr_status(number)` answers with the CI check
runs ("3 checks passed, 1 failed (test (3.13) — url)"), the review decision
and reviewers, whether GitHub calls it mergeable, and whether the branch is
behind its base — a PR that does not exist is answered plainly, and the
result is clipped like every other tool result. It is read-only: the
concierge never merges a PR from chat.
Say "tell me when r7… is done" (a run id or a work item id) and `watch_run`
registers your interest: it confirms, and when that run lands the daemon
posts in the control channel @mentioning you with the outcome — final
state, task summary, tracking issue, PR, delivery error, anything filed.
Watching a run that has already finished answers with the outcome
immediately instead of registering. Watches live in the bot's **memory
only**: a daemon restart forgets every one of them, so re-ask after a
restart.

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
History. Discord is observability, never a dependency: if it is down, the
daemon logs and carries on.

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

Harvest, listings and delivery all skip the same set of path components,
matched at any depth: run/VCS state (`.git`, `.sbxloop`) plus the
regenerable dependency and build trees of the supported languages —
`node_modules`, `__pycache__`, `.venv`/`venv`, `*.egg-info`, the Python
tool caches, `target` (cargo/Maven), `.gradle`, `obj` (.NET), `.bundle`,
`CMakeFiles`. Entries may use glob patterns, matched against whole path
components (`*.egg-info` catches pip's project-named metadata directory).
They are large, reproducible from the manifests that *are* delivered, and
nobody wants them in a `--deliver` PR diff. The ambiguous generic names —
`bin`, `build`, `dist`, `out`, `lib`, `vendor` — are **not** excluded, since
each is build output in one ecosystem and checked-in content in another; add
them to `[artifacts] exclude` if your project wants them dropped. Whatever is
excluded is always counted and reported (`12 file(s) excluded (node_modules)`)
in run summaries, `sbxloop artifacts`, and the delivery PR body — never
silently truncated.

## GitHub integration

sbxloop has **no** GitHub capability until you name the one repository it
may work with — either per run on the command line:

```console
$ sbxloop run "build the thing" --repo you/your-repo --deliver
```

or persistently in `sbxloop.toml`:

```toml
[github]
repo = "you/your-repo"   # the ONE repo sbxloop may act on
report = false           # post run progress as a tracking issue (or `--report`)
deliver = false          # PR the run's artifacts to the repo (or `--deliver`)
deliver_base = ""        # base branch for delivery PRs (or `--deliver-base`)
deliver_draft = false    # open delivery PRs as drafts (or `--deliver-draft`)
create_repo = false      # create the repo if missing (or `--create-repo`)
create_public = false    # created repos are private unless flipped (or `--create-public`)
```

CLI flags win over the toml, so `--repo` can also redirect a configured setup
at a different repository for one run.

The repository is probed right after provisioning, so a missing or typo'd
`--repo` fails the run up front instead of after the work is done. For a
fresh project, add `--create-repo` and sbxloop creates it (private by
default, `--create-public` to flip) with an initial commit, then delivers
the artifacts as a normal reviewable PR — creation is opt-in precisely so a
typo'd repo name errors instead of silently landing in a brand-new
repository. Creating repos needs a token allowed to do so for that owner;
the per-repo minimal token suffices for everything else. An
existing-but-empty repository (no commits yet) is also handled: delivery
bootstraps the initial commit itself.

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
  never fail a completed run. When one does fail, `sbxloop deliver <run>`
  retries it later without re-running the work: it provisions only the
  github-ops sandbox, reuses the run's persisted config (`--repo` and the
  other options override its `[github]` section, so a run that never named a
  repo can still be delivered), force-moves the same `sbxloop/<run>` branch
  and reuses an already-open PR, and `--report` refreshes the tracking issue
  with the PR link.

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

| Value        | Also accepts               | Installs                                                         |
| ------------ | -------------------------- | ---------------------------------------------------------------- |
| `python`     | `py`, `python3`            | `python3-venv`, `python3-pip` (apt), `uv` + Python 3.13 (pinned) |
| `cpp`        | `c`, `c++`, `cxx`, `c-cpp` | `build-essential`, `cmake`, `ninja-build`, `pkg-config` (apt)    |
| `ruby`       | `rb`                       | `ruby-full`, `ruby-dev`, `bundler`, `build-essential` (apt)      |
| `java`       | `jdk`, `jvm`               | `openjdk-21-jdk`, `maven` (apt), plus `JAVA_HOME`                |
| `php`        | —                          | `php-cli` + mbstring/xml/curl/zip (apt), Composer (pinned)       |
| `javascript` | `js`, `node`, `nodejs`     | Node LTS + npm/npx (pinned tarball from `nodejs.org`)            |
| `typescript` | `ts`                       | `tsc` from npm, on top of `javascript`                           |
| `go`         | `golang`                   | Go toolchain (pinned tarball from `go.dev`)                      |
| `rust`       | `rs`, `cargo`              | cargo, rustc, rustfmt, clippy (pinned rustup)                    |
| `dotnet`     | `csharp`, `c#`, `net`      | .NET SDK (pinned build from Microsoft), plus `DOTNET_ROOT`       |

Selecting an entry also selects what it is built on — `languages = ["typescript"]` provisions the Node runtime first, then `tsc`.

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
hatch. And it is **opt-in** — setting `languages` replaces the default rather
than adding to it, so nothing is installed for a language you did not ask
for. Heavier toolchains are better baked into a template (`sbxloop bake`)
than downloaded per run.

### Toolchains that download from upstream need egress

The apt-only entries (`cpp`, `ruby`, `java`) work out of the box: apt mirrors
are in the sandbox's always-reachable baseline. So does `python`: its `uv`
release and the uv-managed Python 3.13 are both GitHub release assets
(`github.com`, redirecting to `release-assets.githubusercontent.com`), and
both hosts are in the agent sandbox's provisioning-time allowlist. The rest
fetch from a vendor or registry, and
**provisioning runs before any task**, so a task's `egress` declaration
is too late to help it. Until those domains are part of the provisioning
baseline, allow them explicitly:

```toml
[sandbox]
languages = ["typescript"]
extra_allow_domains = ["nodejs.org", "registry.npmjs.org"]
```

| Language     | Needs reachable at provisioning time |
| ------------ | ------------------------------------ |
| `php`        | `getcomposer.org`                    |
| `javascript` | `nodejs.org`                         |
| `typescript` | `nodejs.org`, `registry.npmjs.org`   |
| `go`         | `go.dev`, `dl.google.com`            |
| `rust`       | `static.rust-lang.org`               |
| `dotnet`     | `builds.dotnet.microsoft.com`        |

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
(completed/failed/cancelled), unknown to this working copy's state DB, or
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

Only terminal runs (completed/failed/cancelled) past the window go, and never
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
`sbxloop doctor --fail-on-drift` turns that warning into an exit code (any
drifted, errored, or unprobed probe fails) — the CI e2e lane uses it, and the
scheduled `sbx-conformance` workflow runs it against the newest sbx release
ahead of adoption. Doctor also checks the installed Copilot SDK's
permission-kind vocabulary against the field-verified snapshot backing the
read-only critic barrier.

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
`./sbxloop.toml`, `pyproject.toml [tool.sbxloop]`, and a user-level
`~/.config/sbxloop/sbxloop.toml` (`$XDG_CONFIG_HOME` honoured) for settings
that follow you rather than the checkout. `sbxloop init` writes a commented
starter file; `sbxloop config show` prints every resolved value and where it
came from. The notable knobs:

| Key                                    | Default            | Meaning                                                                                                 |
| -------------------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------- |
| `model`                                | `auto`             | Copilot model id (`--model` overrides per run).                                                         |
| `state_dir`                            | `.sbxloop`         | Runs, workspaces, artifacts, SQLite state, event logs.                                                  |
| `keep_sandboxes` / `keep_on_failure`   | `false`            | Sandbox retention for debugging (see above).                                                            |
| `secret_strategy`                      | `proxy`            | `proxy` keeps token values out of the VM; `plain-env` writes an in-VM env file.                         |
| `[sandbox] template`                   | unset              | Baked template ref from `sbxloop bake`.                                                                 |
| `[sandbox] workspace`                  | unset              | Where runs execute; unset gives each run a fresh dir under `state_dir`.                                 |
| `[sandbox] workspace_isolation`        | `auto`             | Per-run clone isolation when `workspace` is a git checkout (see below).                                 |
| `[sandbox] extra_allow_domains`        | `[]`               | Static egress allows applied to every run.                                                              |
| `[sandbox] languages`                  | `["python"]`       | Toolchains pre-installed in the agent sandbox (see below).                                              |
| `[policy] allow` / `deny`              | `[]`               | Bounds for task-declared egress.                                                                        |
| `[github] repo` / `report` / `deliver` | unset / `false`    | The GitHub integration gate and toggles.                                                                |
| `[artifacts] exclude`                  | see below          | Path components dropped from listings, harvest and delivery (replaces the default, does not add to it). |
| `[budgets]`                            | see above          | `max_revisions_per_task`, `max_replans_per_task`, `max_tasks`, `max_wall_clock_s`, `per_job_timeout_s`. |
| `[limits]`                             | `85` / `95` / `90` | `disk_warn`, `disk_abort`, `mem_warn` percentages (0 disables).                                         |

test failure.
exhausted" error instead of letting an in-VM OOM surface as an inexplicable
memory transiently) fails the task with an explicit "sandbox memory
a warning; `mem_abort` (off by default, because a parallel test run spikes
Memory pressure is instead made visible through `[limits]`: `mem_warn` emits
memory flags, so the microVM is whatever size sbx gives every sandbox.
sbxloop does not size the sandbox: `sbx create` is called without CPU or

pytest run's first traceback and its failure summary both survive.
critic keeps the first 2 KB and the last 4 KB of each command, so a long
a starting point (4 h wall clock, tool cap 80). Verify output handed to the
clock. [`contrib/presets/large-repo.toml`](contrib/presets/large-repo.toml) is
minutes of test time, and 20 tasks × 3 attempts × verify presses on the wall
packages to orient in — wants more headroom: one verify pass alone can be
small greenfield project. A large existing repo — thousands of tests, several
The `[budgets]` defaults (2 h wall clock, 40 tool calls per phase) suit a

### Sizing budgets for a larger repository

| `[daemon] workspace_isolation` | `clone` | Isolation for daemon runs against a git-checkout workspace (dirty tree proceeds with a warning). |
| `[daemon] refresh_workspace` | `true` | `git fetch` + fast-forward the workspace checkout before each fresh daemon run. |
| `[daemon] state_dir` | unset | Absolute daemon state location; unset resolves to `$XDG_STATE_HOME/sbxloop/<runner-dir>` (see above). |
| `[daemon] max_runs_per_day` | `12` | Runs allowed per calendar day, counted by start time in `run_cap_timezone`; the count resets at 00:00 there. |
| `[daemon] run_cap_timezone` | `UTC` | IANA timezone defining the run cap's day boundary (also used by the per-day review and post-mortem caps). |
| `[daemon] run_stale_after_s` | `21600` | With no run executing, non-terminal runs idle this long are reconciled to a terminal state (`0` disables). |

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
- [Deploying the daemon](docs/deploy.md) — merge to `main` releases, then deploys itself to the daemon host: drain, upgrade, restart, health check, roll back
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
