# Changelog

All notable changes to sbxloop are documented here. The project adheres to
[Semantic Versioning](https://semver.org/) and both distributions (`sbxloop`,
`sbxloop-worker`) release in lockstep.

## [Unreleased]

### Added

- **`sbxloop tui`: the operator console** (#770). A person on the daemon
  host had the CLI's one-shot commands and journald. `sbxloop tui` is a
  terminal console that reads the daemon's `state.db` read-only and asks
  the daemon `status` through the `ctl` queue every few seconds: an
  Overview (the run in flight, the queue, who waits on a human, recent
  runs), Runs with a per-run screen (the `sbxloop run` transcript tailed
  from the store, tasks, every phase attempt with its tokens and turns —
  never a currency, landing state and the newest landing events,
  artifacts, the dense event lines with a type filter), the Queue, and
  Help. `--run` opens a run at once, `--read-only` removes every action,
  `--state-dir` overrides the daemon's rule. Textual is a core dependency.
  `docs/tui.md` documents the layout and keys; the docs' mention of a
  `sbxloop watch` TUI that never shipped now names this. The chat screens
  and the admin screens follow.

### Added

- **The daemon always runs a local chat bridge for the operator console**
  (#769). A person on the daemon host had the CLI and journald; everything
  a run *shows* — the headline, the thread, the status line and tool digest
  edited in place, steering, the concierge, clarifying-choice buttons, the
  merge-gate approve button — existed only on Discord or Slack, and a
  headless daemon had no concierge at all. The daemon now runs a
  `LocalBridge` beside whatever `[chat] backend` names, through one
  fan-out frontend: a third `ChatBridge` whose transport is a mailbox in
  the daemon's own `state.db` (`daemon_local_messages`) — every message
  the bridge would post becomes a row, edits rewrite it, reactions
  decorate it, and what an operator types in `sbxloop tui` arrives as a
  row the bridge claims, the same file-drop shape as the ctl queue. A row
  typed before the daemon started is refused with a note, never executed.
  The concierge is built whenever `[concierge] enabled`, headless
  included — a headless host now boots the concierge sandbox at start and
  needs the agent credential. `[tui]` carries the console's knobs
  (`operator_id`, `emoji`, `daemon_unit`, `refresh_s`, `retention_days`)
  beside the shared rendering ones; `[chat] backend` still names only the
  external service. `sbxloop.daemon.mailbox.MailboxClient` is the
  console's handle: read-only for state, one kind of write, no schema
  statement — so a console never migrates a store under a running daemon.
  Both stores open read-only for it (`readonly=True`: a `mode=ro` URI,
  no schema statement). With two bridges up, each renders only the
  requester and watcher ids it owns (a snowflake is Discord's, a member
  id Slack's, a login name the console's), and the concierge words each
  reply for the surface the message came in on. `ctl status` reports
  `pid`, `started_at` and `version`; `sbxloop doctor` shows an `operator console` row and its concierge row no longer needs a chat backend. The
  console itself (`sbxloop tui`) follows.

### Changed

- **Chat state is keyed by backend** (#768). The daemon is about to run
  the operator console's local chat bridge beside Discord or Slack, and
  the store assumed one bridge: `daemon_chat_threads` was keyed by run
  alone, a run's watchers were one list that the first bridge to finish
  drained, the merge gate's prompt location lived on the gate row, and
  every bridge's clarification sweeper fired every backend's due asks.
  Threads are now `(run, backend)` (the bare lookup prefers the external
  backend's thread, what a link in prose points at), watches carry their
  backend, gate prompts live in `daemon_gate_prompts` per backend, and a
  sweep takes only its own backend's asks, and the clarification cap
  counts one backend's asks. An existing store is rebuilt on open, once,
  with the indexes recreated inside the rebuild's own transaction; a
  pre-upgrade watch or gate prompt is filed under the backend that opened
  the run's thread — a Slack daemon's under `slack` — and the gate row's
  old prompt columns are cleared once carried, so the step is idempotent
  through a rollback. Nothing changes for a one-bridge daemon.
  `tests/fakes/legacy_db.py` freezes the shape before this as
  `pre_local_bridge`.

### Fixed

- **No bare sbxloop issue numbers reach users** (#635). A provisioning
  error ended "(see #46)", doctor's conformance drift rows carried
  "(#57)", "(#250)", "(issue #122)", "(#592)", and the files
  `sbxloop init` writes into the user's project said "(#533)" and
  "(#568)" — references into sbxloop's tracker that read as noise, or as
  the user's own repository's #N, to anyone not developing sbxloop. All
  stripped; code comments and docstrings keep theirs.

- **CI runs once per pull request** (#643). `ci.yml`'s push trigger was
  `[main, "sbx/**"]` — a personal branch convention, and one that made
  every job run twice on such a branch's PR (push and `pull_request`
  both fire). It is `[main]` alone now; working branches are built
  through their pull request. The loop's own `sbxloop/<run>` delivery
  branches were never in the filter and stay that way, for the same
  reason.

- **Rollback keeps both chat extras** (#619). The deploy pipeline's rollback
  reinstalled `sbxloop[discord]` while the upgrade installs
  `sbxloop[discord,slack]`, so a rolled-back Slack host would have lost its
  bridge. Both lines now install `[discord,slack]`, and
  `test_deploy_workflow.py` asserts the two stay in step — in the
  repository's own workflow and in the contrib example alike.

- **Host commands follow `[agent] backend`** (#617). One descriptor
  (`sbxloop.backends`) says what each backend needs — credential env var
  and sbx binding host, the network hosts that credential path reaches,
  the missing-credential wording, where its model ids come from — and
  `sbxloop doctor`, `sbxloop secrets list|clean|rotate`, `sbxloop list-models`, `--model` help, provisioning and sandbox pruning read it.
  Under `backend = "claude"`: doctor's credential, network-policy and
  concierge rows name `ANTHROPIC_API_KEY` / `api.anthropic.com` and the
  Copilot-SDK permission-kinds row is not emitted; `secrets` manage the
  Anthropic registration (`rotate` reads/prompts for `ANTHROPIC_API_KEY`);
  `list-models` lists the Anthropic Models API (`GET /v1/models`, all
  pages, stdlib only; FIELD-UNVERIFIED against a live key); prune removes
  either backend's agent registration since the backend may have changed
  since the sandbox was provisioned. The copilot path is byte-identical.

- **Run clones are single-branch and tagless** (#632). Every per-run
  clone — of a host checkout or of the remote — is cut
  `--single-branch --no-tags`, so a repository's whole branch and tag
  history no longer travels into each run. Safe because
  `merge_from_base` now fetches the delivery base by explicit refspec
  before merging and diffing, so a base that is not the clone's branch
  still resolves; continuing an existing branch fetches that branch the
  same way. Shallow clones stay off the table: a `--depth 1` clone has no
  history to compute a merge base from.

- **The "created repository" link is host-correct**: the Discord embed
  uses the repository's `html_url` from the probe instead of assuming
  `github.com`.

- **Follow-ups on a repository with Issues disabled are not lost** (#631).
  `POST /issues` answers 410 Gone there, and the filing's best-effort
  guard logged it and moved on — the review's out-of-scope notes were
  silently dropped. The delivery probe now reads `has_issues` off the
  repository payload it already fetched and downgrades
  `[landing] followups = "issues"` to the PR checklist comment, recorded
  on the `run.followups` event (`downgraded_from = "issues"`,
  `reason = "issues_disabled"`); a payload that did not say downgrades on
  the 410 itself. The crash-window dedup skipped nothing the issues
  endpoint listed — including pull requests — so a labelled PR quoting a
  marker suppressed the issue; PRs are now filtered out. The issue body
  names the trigger label only when a daemon dispatched the run; under
  `sbxloop run` nothing polls the repository and the old sentence pointed
  at a label that did nothing.

- **A red non-Actions check's own log reaches the fix brief** (#629). The
  worker now follows the check's `details_url` / `target_url` best-effort
  — unauthenticated, https only, `text/*` or JSON bodies, the Actions log
  size clamp — when the check reported no output of its own. Any failure
  (a host the sandbox policy does not allow, an HTML page, an auth wall)
  leaves the brief at the check's name, link and reproduce-locally
  instruction. No new knob: `[sandbox] extra_allow_domains` is where a CI
  host worth reading goes.

### Changed

- **The version story no longer assumes the user's repository publishes
  sbxloop** (#638). The concierge prompt, the `version_status` tool
  description and `daemon/versions.py` said "the main branch publishes a
  release on every merge" and told the operator to
  `pip install --upgrade` in a venv — sbxloop's own release cadence and its
  own install layout, presented to every host. They now say that
  sbxloop's releases ship frequently while upgrading a host is an
  operator's step, and the upgrade instruction renders from
  `[daemon] upgrade_command` when set — otherwise "the exact command
  depends on how sbxloop was installed (pip in a venv, pipx, `uv tool`, a
  container image, a deploy pipeline)". The prompt's "the configured
  repository" is now "a configured repository" (there may be several).

- **The large-repo preset is package data, framed by gate duration**
  (#636). `contrib/presets/large-repo.toml` moved to
  `sbxloop/data/presets/large-repo.toml` inside the wheel (the contrib
  path is a symlink to it), and its header no longer cites sbxloop's own
  numbers as the reference case: the trigger is a repository whose gate
  command takes two minutes or more, whatever its size. The template's
  `[budgets]` comment and the README point at
  `sbxloop init --preset large-repo` instead of a checkout path, so
  nothing `sbxloop init` writes references a file outside the user's
  project. (Recording observed gate duration so a second run self-sizes
  its budgets stays a separate follow-up.)

- **The deploy pipeline reads structured control, not files** (#639).
  `deploy.yml` drives the daemon with `ctl status --json` + `jq` and posts
  with `daemon notify`; no step sources `secrets.env`, parses
  `sbxloop.toml` or calls the Discord API — a Slack-backend host deploys
  unchanged. **Cutover:** the drain step fails closed when the running
  daemon answers without a structured status, which a daemon older than
  this release does, so the first deploy after it lands stops at "Wait for
  the daemon to go idle" *before installing anything*. Upgrade once by hand
  (contrib/systemd/README.md, "Upgrading"); every deploy after that is
  unattended again.

- **The deploy host is one variable** (#640). `deploy.yml` targets
  `runs-on: [self-hosted, "${{ vars.SBXLOOP_DEPLOY_HOST || 'db' }}"]` and
  derives every path from `$HOME` (job-level `env:` values are literals, so
  a first step writes them to `$GITHUB_ENV`); moving the daemon is setting
  the repository variable and registering a runner with that label — no
  edit to the workflow, nothing `make check` runs. The
  drain/hold/upgrade/health-check/rollback pattern ships as
  `contrib/workflows/deploy-daemon.yml.example` (`schedule` +
  `workflow_dispatch`, installs from PyPI, names nothing), and
  `test_deploy_workflow.py` checks both files for the security invariant,
  the extras parity and the absence of names.

- **Deploy docs split** (#642). `docs/deploy.md` is now the generic "run the
  daemon as a service and upgrade it" guide — no hostnames, usernames or
  repository slugs, enforced by a test — and `docs/self-deploy.md` the
  clearly labelled reference for how sbxloop deploys its own host, with the
  cutover notes. The systemd README's upgrade section leads with the
  two-command manual path (hold, wait for idle via `ctl status --json`,
  pin, `reset-failed` + restart) and mentions the workflow as optional
  automation; `github-runner.service` is marked as needed only for it. The
  1.0 cutover steps moved from `docs/deploy.md` to this file (below).

### Added

- **A gate against self-references in user-facing surfaces** (#645).
  `scripts/check_self_references.py` (stdlib only; run by `make lint` and
  the CI lint job, and by the unit suite) fails with `path:line: rule: text` on a bare `#N` in a prompt body below its contract header, in any
  `raise`'s message in either package, in the CLI package's and the
  conformance table's string literals, or in a file `sbxloop init`
  writes; on an sbxloop source path inside a prompt body; and on a
  maintainer or deploy-host identifier in any tracked file outside
  `contrib/`, `docs/`, `.github/`, package metadata and tests. Comments
  and docstrings are not surfaces. Deliberate exceptions live in one
  reviewed file, `scripts/self-references.allow` (today: the concierge
  prompt's worked-example numbers); an entry that matches nothing fails
  the gate too, so the list cannot rot.

- **`[daemon] version_check` and `[daemon] upgrade_command`** (#641, #638).
  `version_check = false` switches the PyPI release lookup off for the
  whole daemon — no startup drift check, no drift notice, and the
  concierge's `version_status` reports the installed versions without
  looking "latest" up (zero outbound HTTP, alongside the existing `.dev`
  skip) — for hosts a pipeline upgrades, which retires the contradiction
  between `docs/deploy.md` and the drift notice. `upgrade_command`
  (e.g. `"pipx upgrade sbxloop"`) is what the drift notice and the
  concierge's report tell the operator to run; unset, they say the command
  depends on how sbxloop was installed. Both are `SBXLOOP_DAEMON__*`
  overridable; a blank `upgrade_command` is a config error. Operators of
  the self-deploy pipeline in `docs/self-deploy.md`: set
  `version_check = false` on that host.

- **`sbxloop init --preset NAME`** (#636) appends a packaged preset's live
  sections to the starter file (`--stdout` streams the same), so
  `sbxloop init --preset large-repo` yields one self-contained
  `sbxloop.toml` from a wheel with no checkout around. Every table in the
  template is commented out, so the appended `[budgets]`/`[limits]` are
  the only live ones. An unknown name exits 2 naming the presets that
  exist.

- **`sbxloop daemon ctl status --json`** (#639) — the daemon's status as one
  JSON object (`current`, `claiming`, `holds`, `paused`, `queued`, …) for
  scripts, instead of grepping the prose, which is now free to change. The
  reply carries the structured dict alongside the text; a daemon that
  predates the flag answers prose only and `ctl` exits 1 ("answered without
  a structured status") — distinct from exit 2, no daemon.

- **`sbxloop daemon notify "<text>"`** (#639) — post one message to the
  control channel through the configured `[chat] backend`, from the host
  and without the daemon, so a deploy script can say "rollback also failed"
  while the daemon is down. Reads the channel from `sbxloop.toml` and the
  bot token from the environment (`DISCORD_BOT_TOKEN` / `SLACK_BOT_TOKEN`,
  the working directory's `.env` included); Slack text is re-dialected the
  way the bridge does it; link previews and pings are suppressed; a
  headless daemon cannot notify and says so.

- **`[github] api_url`** (#623) — the GitHub REST root
  (`https://api.github.com`; `https://ghe.example.com/api/v3` for GitHub
  Enterprise Server) is the one source of truth for the REST transport,
  App-auth minting, the remote clone URL, PR links and both sandboxes'
  network allows. The github sandbox receives `GH_HOST` (for `gh`) and the
  worker `SBXLOOP_GITHUB_API_URL` only when the host is not github.com. A
  `GH_HOST` in the daemon's environment that disagrees with `api_url`
  fails config load with a message naming both. Deliberately not derived:
  the Copilot token exchange host stays `api.github.com` (Copilot is served
  from github.com even for GHES), and sbx's built-in `github` service
  secret stays github.com-keyed. FIELD-UNVERIFIED — no GHES to test
  against.

- **`[sandbox] clone_filter`** (#632) — opt-in git partial-clone filter
  (`"blob:none"`) for the credential-free remote clone of a repository
  with no host checkout. Off by default because lazy blob fetches happen
  wherever git next needs one, the VM included; a git without `--filter`
  logs `workspace.clone_filter_unsupported` and clones in full.

- **`sbxloop init-repo owner/name`** creates the labels the loop relies
  on (#630): the six lifecycle labels and `[landing] followup_label`, each
  with a color and a description, idempotently, through one github-ops
  sandbox. Nothing created the trigger label a human was told to apply,
  and the lifecycle labels auto-created on first attach with a random
  color and no description. `sbxloop doctor` gained advisory rows for missing labels (pointing
  at `init-repo`) and for a repository with Issues disabled; it stays
  advisory. Every lifecycle label (`trigger_label`, `in_progress_label`,
  `failed_label`, `completed_label`, `blocked_label`, `gated_label`) can
  now be renamed per `[[github.repos]]` entry — `Config.labels_for(repo)`
  is the one merge, and a repository's six must stay distinct. A claim
  that GitHub refuses with 403 on the label write now fails with an error
  naming the permission (Issues → read and write; classic PAT `repo`)
  instead of a bare status.

- **The agent sandbox's allowlist never names a host twice** (#616).
  `sbx policy allow` refuses a rule it already holds — including one
  created moments earlier from the same argv — and the refusal fails the
  whole call, so a repeated host did not waste a rule but failed
  provisioning outright. The tiers overlap by construction: the claude
  backend pulls in the javascript toolchain for the Claude Code CLI, and
  its installer host is the `registry.npmjs.org` the advertised baseline
  already promises, so *every* claude-backend sandbox hit it — field
  failure on `db`, where the concierge box could not provision at all
  (`concierge.warm_up_failed`) and Discord mentions were dead. The union
  is now deduped in `agent_policy_allows`, and again where any spec's
  list is applied, so an operator naming a host in `extra_allow_domains`
  that a toolchain or the baseline also opens is no longer fatal.

- **A head with no checks is settled at landing too** (#633). `land()`
  trusted "nothing has reported" on sight; only the CI stage after a
  delivery waited out `ci_settle_s`. A resume at the landing stage, a
  merge-gate approve and the head an update-branch makes now hand the
  read to `poll_checks`, settling from when the landing first saw that
  head — a slow CI's first run is no longer merged ahead of. The engine
  passes its delivery time along (`settle_from`) so the window is not
  paid twice in one drive. `ci_settle_s` is documented as calibrated to
  GitHub Actions' registration latency; raise it for CI that registers
  later.

- **A reconciliation marker counts only in the loop's own reply**
  (#618). `has_reply_marked(marker, login)` now requires the stamped
  comment to be loop-authored; a person quoting the marker back no
  longer makes a thread answered or acknowledged.

- **The loop's identity carries its kind** (#622). `Identity` /
  `identities_match` compare logins under the `[bot]` fold **and** the
  account kind when both sides know it (App from the slug or
  `user.type`/`__typename`, user from `GET /user`), so a person named
  `foo` is never the App `foo[bot]`. `resolve_identity` consults, in
  order, the App slug, `GET /user`, the new `[github] bot_login`
  (overridable per `[[github.repos]]` entry) and — only when the
  delivering credential is the reviewing one — the PR's author.
  `ThreadComment.is_bot`, `Pipeline.is_bot` and an `is_bot` argument on
  the landing, reconciliation and acknowledgement paths carry it through.

- **The merge method follows the repository, and a merge GitHub refuses
  is named** (#620). `[landing] merge_method` defaulted to `squash`,
  which a repository with squash merging disabled answers with a 405 the
  loop reported as "branch protection"; and a PR whose checks were green
  but whose `mergeable_state` was `blocked` was polled to the timeout.
  The default is now `auto`: the first of squash, merge, rebase the
  repository's settings allow, resolved once per landing and logged in
  `land.merge_method`. An explicit method the repository disallows is
  never swapped for another: `sbxloop doctor` reports it on the repo
  row (`merge method squash not allowed`) and a run ends `blocked`
  naming it. A `blocked` mergeability after green checks is re-read
  once and then explained from the base's protection — the required
  approvals its identity cannot give (with the `merge_gate = "chat"`
  pointer), CODEOWNERS, the merge queue, unresolved conversations —
  and a bare 405 carries the same reading. `unstable` (a non-required
  check red) stays mergeable.

- **The PR title, commit message and branch name are the operator's,
  and the plan can title its own PR** (#621). Every PR was
  `sbxloop: <outcome>` on `sbxloop/<run>` with a fixed commit message,
  which a repository's title lint, commit lint or branch ruleset
  refuses — and the refusal read as a mystery 422. `[github]` gains
  `pr_title_template` (default `sbxloop: {title}`),
  `commit_message_template` and `branch_prefix` (default `sbxloop/`),
  each overridable per `[[github.repos]]` entry, with `{title}`,
  `{outcome}`, `{run_id}` and `{repo}` placeholders; the defaults
  render byte-for-byte what shipped before. The decomposer may return
  a `pr_title` in the repository's own commit style (it is shown the
  recent `git log`); `{title}` falls back to the outcome when it does
  not. A fix round can retitle the PR by writing `.sbxloop/pr-title`
  in the workspace — a red title-lint check is thereby curable — and a
  re-delivery whose title changed PATCHes the PR (`deliver.title_changed`).
  A branch the repository's rulesets refuse fails the delivery naming
  `[github] branch_prefix` rather than the raw 422.

- **A workflow waiting on a maintainer's approval ends the run blocked
  and named, not timed out** (#612). A first-time contributor's — or a
  fork's — workflow run sits at `action_required` until a maintainer
  approves it; the loop read that as a failure and spent its fix rounds
  on it. It is now its own bucket (`ChecksVerdict.needs_approval`): the
  poll returns at once, no fix round is spent, and the run ends
  `blocked` with "check X needs a maintainer to approve the workflow
  run". A real red beside it is fixed first, and the approval is
  re-judged on the re-delivery.

- **A backend switch no longer leaves a stale concierge sandbox behind**
  (#533). The daemon's concierge box is deliberately reused across
  restarts, and the reuse gate asked only whether the installed worker
  matched this host. But the worker is installed with `[agent] backend`'s
  extra, so a box built under copilot carries the Copilot SDK and no
  Claude Code CLI while reporting the very same version: switching the
  backend kept it, and every mention then failed with
  `BackendUnavailableError` until an operator removed the sandbox by
  hand. The gate now also asks whether the box is equipped for the
  configured backend (`WorkerClient.backend_ready`), and re-provisions
  when it is not. The question is answered by the worker's own
  precondition, hoisted out of `run_session` into
  `backends.ensure_available`, so there is no second host-side copy of
  what each backend needs to drift.

- **`sbxloop doctor`'s concierge row follows `[agent] backend`** (#533).
  It named `COPILOT_GITHUB_TOKEN` unconditionally, so a claude-backend
  host was told "mentions will fail" while nothing was wrong — and was
  told nothing about `ANTHROPIC_API_KEY`, the credential that actually
  gates it. The row now names the configured backend's credential, like
  the agent credential row above it.

- **`status`, `logs`, `artifacts` and `gc` look where the daemon actually
  writes** (#255). The daemon anchors its state at
  `$XDG_STATE_HOME/sbxloop/<project>`, away from the top-level
  `state_dir`; the run commands read `state_dir` verbatim, so on a daemon
  host they reported an unrelated — usually stale, often empty — world,
  with no flag to correct them. `sbxloop daemon` and its `ctl`
  subcommands already resolved this way; the run commands now do too, via
  `paths.resolve_cli_state_dir`. The redirect fires only when a daemon
  store is actually present, so a single-user `sbxloop run` host is
  unaffected, and nothing moves on disk.

- **`sbxloop bake` installs the configured languages, and a prebaked run
  tops up what the template lacks** (#615). The bake ignored `[sandbox] languages` and installed Python alone; a run on that template then
  verified the baked worker, skipped the install ladder — and with it
  the toolchain provisioning of #624 — and handed the agent a sandbox
  without the language it had just resolved for. The bake now
  provisions the configured languages (through the same allowlist
  builder the runs use, so their installer hosts are reachable at bake
  time too) and records what actually landed in `bake.json` and the
  host's bake record. A prebaked run keeps the fast path but probes its
  full resolved set in one batched `sh -c`; whatever is absent is
  provisioned on top, named in `worker.prebake_topup` and in the
  `sandbox.prebaked` event (`topped_up`). A probe that cannot answer
  falls back to the per-tool probes rather than assuming presence.
  `sbxloop doctor` gains a *languages in template* row that compares the
  baked set with `[sandbox] languages` and says when a re-bake would
  stop the per-provision top-up; a bake that cannot probe its own
  result fails instead of recording a guess.

- **The gate is detected for more than a Python-and-npm repo** (#625,
  #626). Detection knew a `check`/`ci` target in a makefile, justfile or
  Taskfile, an npm script, tox and nox — and always ran the npm script
  with `npm run`, which on a pnpm or yarn workspace fails to resolve
  the very tools the script names. The package.json script now runs
  under the client the project uses: the `packageManager` field, else
  the lockfile (`pnpm-lock.yaml`, `yarn.lock`, `bun.lock[b]`), else
  npm. `verify` joins `check`/`ci` as a gate target everywhere (`all`
  does not: it is the default build). New detectors: a Rakefile
  `ci`/`check`/`default` task (`bundle exec rake <task>`), composer
  `check`/`ci` scripts, a Gradle build with its wrapper (`./gradlew check`; the sandbox has no Gradle of its own, so a build without a
  wrapper is no gate), `pom.xml` (`./mvnw -q verify` or `mvn -q verify`), and a `[alias] ci` in `.cargo/config.toml` (`cargo ci` — a
  `check` alias is not honored because cargo silently shadows it with
  the built-in). Go, Rust and .NET have no task-runner convention, so
  when such a repo declares nothing the tool itself is the gate: `go vet ./... && go test ./...`, `cargo test`, `dotnet test` (a .NET
  tree with one solution, or no solution and one project). Every
  detector is tied to the language whose toolchain runs it and is
  consulted only when that language was resolved for the sandbox
  (#624), so the loop never asks the sandbox for a command it cannot
  run; the task-runner detectors are consulted under any set.

- **Red checks are judged against the base the PR is built on** (#611).
  A red check on the head used to mean "this PR broke it": every red
  spent a CI fix round, and a base whose own CI was already red — a
  flaky job, a broken nightly — could never be landed on. The landing
  stage now folds the same checks on the PR's merge base (the compare
  API; never the base's *current* head, whose red is someone else's)
  and reads the base's protection and rulesets (`gh/protection.py`,
  also what `doctor` reads now). A red already red on the base is
  **preexisting**: merged over and named in a PR comment. A red the PR
  caused is a **regression**: fixed for the full `max_ci_rounds` if the
  base requires that check, for **one** round if it does not — after
  which it is merged over and named, so a signal no human demanded
  never blocks a landing. Absent from the base (a check that only runs
  on pull requests) or an unreadable baseline counts as the PR's own —
  "could not tell" fails closed. A base that declares no required
  checks gates on all of them, as before; a required check red on the
  base is still fixed (GitHub will refuse the merge otherwise), and its
  fix brief says the failure was inherited. Only gating checks are
  waited on. New `[landing] required_checks` (an explicit gating set,
  overriding what the base declares) and `ignore_checks` (fnmatch
  patterns dropped everywhere); new `landing.checks` event with the
  gating set and its source, the pending, fix, regression, preexisting,
  advisory and ignored names, and the baseline sha.

- **An automated reviewer's changes-requested review is a signal, not
  a veto** (#613). A GitHub App that reviews pull requests (CodeRabbit,
  Copilot, Sourcery…) leaves a `CHANGES_REQUESTED` it never dismisses,
  and the landing stage read it as a person's: one fix round, then
  `blocked` for ever with "only they can dismiss it", so no PR on a
  repository with such a bot ever landed. Reviewers now carry whether
  they are a bot — REST `user.type == "Bot"` on reviews and comments,
  GraphQL `author.__typename` on threads (`ThreadComment.is_bot`,
  `ReviewThread.opened_by_bot`, `HumanObjection.is_bot`; the field
  #622 reads). A bot's standing review buys **one** dedicated fix round
  (`fix.round` kind `bot`, its findings in the brief, its threads
  answered by the reconciliation that follows the fix — spending a CI
  round when one is left, skipped when none is); a bot review still
  standing after that is merged over, named in a PR comment ("bots do
  not dismiss their reviews"), and reported as `land.bot_standing`. It
  never produces the terminal block. A person's review is untouched:
  full authority, and a person standing beside a bot still wins. A
  bot's inline threads are neither acknowledged nor a reconciliation
  block — the gate is for people. New `[landing] ignore_reviewers`
  names User-type accounts to treat as bots (a reviewer on a personal
  token); there is no reverse list, an App is never a person. Human
  thread acknowledgments are capped at 25 per landing pass
  (`land.human_ack_capped`), the remainder blocking truthfully with a
  note rather than posting hundreds of replies in one go.

- **Review, comment, check, and thread reads are paginated** (#614).
  Every GitHub list read took the first page (30 entries) as the whole
  list, so on a busy pull request a standing `CHANGES_REQUESTED` past
  the first page of reviews was invisible, inline comments past the
  first page were never answered, and `reviewThreads` stopped at 100
  threads / 50 comments per thread with no cursor — the exact silent
  merge the reconciliation gate exists to prevent. REST list reads now
  go through one `raw_pages` walk (`per_page=100`, following `page=`
  to a short page: reviews, review comments, issue comments, the
  follow-up dedupe's label issue list, check runs, commit statuses, the daemon's claim comments and label events;
  the worker's `checks.failed_logs` walks the same way); the thread
  listing follows `pageInfo.endCursor` and reads each thread's comments
  at the connection maximum. Consistent with the gate's "we could not
  tell is not there is nothing to answer": a list longer than ten full
  pages, or a thread whose comments have a further page, raises
  `PaginationError` — the landing gate blocks on it at once, naming
  the thread, instead of retrying or judging a prefix.

- **Commit statuses count as CI** (#610). The CI gate read only the
  Checks API, so a repository whose CI reports through the older Status
  API — Jenkins, Buildkite, Travis, CircleCI's default integration,
  Codecov, most org bots — looked like a repository with no CI at all,
  and its red delivered head was settled as done. `pr_checks` now reads
  `/commits/{sha}/status` alongside `/commits/{sha}/check-runs` and folds
  both into one verdict (red beats pending beats green; `failure` and
  `error` are both red; names are the check name or the status context,
  untagged, so they match branch protection's required contexts). The
  fold keys on the `statuses` list, never the payload's top-level
  `state`, because GitHub reports `pending` for a commit with no statuses
  — reading that would deadlock every Checks-only repository. "No CI"
  now means both lists empty. The failed-logs op reports red statuses
  too, carrying the status `description` and `target_url`.

- **A check without a readable log is briefed as such** (#629, minimum).
  The fix brief used to show `(no log output was available)` under a
  failing check, which reads like an empty log. It now shows the check's
  link next to its name, says the log is not readable from the sandbox,
  and tells the fixer to reproduce the failure with the project's own
  gate before changing anything. Every failing check's `details_url` /
  `target_url` is now in the brief.

### Added

- **The toolchain series a run provisions comes from the workspace** (#627).
  Every Python project got Python 3.13 and every Node project Node 24,
  whatever they declared; a `requires-python = ">=3.11,<3.12"` project's
  own `uv sync` then refused the interpreter it was handed. `Toolchain`
  entries with a series now read the declaration — Python from
  `.python-version` then `[project] requires-python` (PEP 440, via
  `packaging`, a new runtime dependency), Node from `.nvmrc` /
  `.node-version` (a major, a full version or an `lts/<codename>` alias)
  then `engines.node` (node-semver ranges) — and provision the default
  series when it satisfies the declaration, else the highest series this
  host can install that does (Python 3.8–3.14; Node 18, 20, 22, 24, each a
  pinned, checksum-verified tarball), else the default with a
  `toolchains.version_unsatisfiable` warning. Each choice is a
  `sandbox.toolchain` run event carrying the series, its source (the file
  read, or `default`) and the constraint, so a probe failure reads against
  the interpreter the project asked for. `[sandbox] languages` still
  decides *which* toolchains; the workspace decides the series either way.
  A prebaked template is topped up to the declared series rather than
  trusted at the default. An undeclared project provisions exactly what it
  did before. Go needs none of this: `go.mod`'s `toolchain` directive
  already makes `go` fetch what the module declares.

- **The config-override lint reads TypeScript and Ruby projects** (#628).
  `verifylint.CONFIG_SCOPED_TOOLS` entries now say how their tool treats
  an explicit path: the Python entries keep their include-set rule (a path
  outside `[tool.mypy] files`, ruff `src`/`include` or pytest `testpaths`
  overrides it; one inside only narrows the run), `rubocop` gains an
  *exclude* rule (a file named on the command line is inspected even when
  `AllCops/Exclude` in `.rubocop.yml` lists it — what `--force-exclusion`
  exists to switch off — while a directory argument is still filtered) and
  `tsc` a *whole* rule (any input file makes it ignore `tsconfig.json`
  entirely; `-b`/`--build` takes projects and disarms it). Config sources
  may be YAML (`pyyaml` is a new runtime dependency), a source with no key
  is satisfied by the file's presence, `npx`/`npm exec`/`pnpm exec`/
  `pnpm`/`yarn`/`bundle exec`/`dotnet` prefixes are seen through like
  `uv run`, Go/Rust/TypeScript/Ruby/Java/C# file suffixes and directory
  names (`cmd`, `pkg`, `internal`, `spec`, `crates`) read as paths, and
  the suggested bare form keeps the command's flags (`uv run mypy --strict packages` → `uv run mypy --strict`). The worked example each entry
  renders into the prompts (#634) is now asserted to be exactly what the
  lint flags, and the ecosystem fixtures gain a `lint` column. Deliberately
  *not* added: eslint and golangci-lint. The issue asked for `npx eslint src` against `eslint.config.js` and `golangci-lint run ./pkg/...` against
  `.golangci.yml` to be flagged, but both tools keep applying their
  configured ignores to command-line paths (eslint flat config's
  `ignores`; golangci-lint v2's `exclusion_paths`, the v1 explicit-directory
  carve-out is gone), so those commands are narrowings, and flagging them
  would reject a correct verify command.

- **Toolchains are detected from the workspace** (#624, #616, #644). A run
  with `[sandbox] languages` unset now provisions what the repository
  declares — `go.mod` selects Go, `package.json` JavaScript, `Cargo.toml`
  Rust, `pom.xml`/Gradle files Java, `Gemfile` Ruby, `composer.json` PHP,
  `*.csproj`/`*.sln` .NET, `tsconfig.json` TypeScript — reading the root
  and two levels of subdirectories (dependency trees and dot-directories
  excluded) and selecting every match. Python remains the answer only when
  nothing is recognized, so runs on repos with no manifest are unchanged;
  an explicit `languages` still replaces detection outright. The resolved
  set is decided once per run, before the agent sandbox exists, and
  reported as a `sandbox.languages` event (`source`: `config` / `detected`
  / `default`, plus the manifests that fired); the egress allowlist, the
  toolchain install, and the verify-command lint all read that one answer.
  Each toolchain now also carries its installer hosts, and the agent
  sandbox is created with the *selected* toolchains' hosts allowed
  (`nodejs.org`, `go.dev` + `dl.google.com`, `static.rust-lang.org`,
  `builds.dotnet.microsoft.com`, `getcomposer.org`), so a Node or Go
  project provisions under a default-deny sbx preset without
  `extra_allow_domains`; a language that was not selected opens nothing,
  and `[policy] deny` still wins. `sbxloop bake` allows the same hosts for
  the configured languages. An ecosystem fixture matrix under
  `tests/fixtures/ecosystems/` now pins these expectations per project
  shape.

- **Chat names the agent backend next to its model** (#601). Discord and
  Slack messages that surfaced only a model reference now read
  `backend · model` (`copilot · gpt-5`, `claude · claude-sonnet-4-5`), so a
  reader of a control channel or a run thread can tell a GPT model offered
  through Copilot apart from a Claude model offered from Claude without
  leaving chat. The pair appears everywhere the model was surfaced before —
  run headline cards (text and embed), agent message attribution in a run
  thread, and the concierge's `run_usage` / `usage_today` reports — and is
  identical on both chat backends. The backend shown is the one the run
  itself recorded, so re-rendering an old run under a switched backend does
  not relabel it. Runs and usage events recorded before this change carry no
  backend and render as `unknown` rather than a blank, a placeholder or an
  error.

- **Re-adding the trigger label restarts an issue** (#600). Applying
  `sbxloop:run` again to an issue whose last attempt finished (done,
  failed, blocked or cancelled) now re-queues it on the next poll whether
  or not the issue text changed — the label is never silently inert, and
  an operator `!sbx retry` is no longer the only way back in. The
  re-queued item keeps what the previous attempt pushed to origin (its run
  id, branch and PR, in the new `prior_run_id` / `prior_branch` /
  `prior_pr_number` columns, added in place on open) so the restart
  continues that branch instead of redoing it; with nothing usable on
  origin the run simply starts fresh. Live items (queued, claimed,
  running, resume-pending) still dedup, so a poll never double-dispatches.

- **An optional Claude agent backend** (#533). `[agent] backend = "claude"`
  runs every agent persona through the Claude Agent SDK (the Claude Code
  harness) instead of the Copilot SDK, which stays the default with
  unchanged behaviour. The agent sandbox then holds `ANTHROPIC_API_KEY`
  alone — bound to `api.anthropic.com`, delivered by the same secret tiers
  as every credential, redacted everywhere the Copilot token is — and
  provisioning installs the runtime the SDK spawns (Node plus
  `@anthropic-ai/claude-code`, probe-first) and keeps the CLI hermetic
  (no telemetry/auto-update egress). Contract parity throughout: the same
  `agent.*` event stream, the read-only critic barrier (allowlist with
  default-deny on Claude's tool vocabulary), the tool-call governor,
  session resume as an optimisation with fresh-session fallback, host
  tools as an in-process MCP server, and token usage reported through the
  existing `run_usage`/`usage_today` accounting. Invalid or missing
  configuration fails fast: an unknown backend fails config loading; a
  missing key fails before any microVM boots; `sbxloop doctor` shows the
  backend-appropriate credential and egress rows.

- **Per-job stdin secret delivery** (#592). When sbx's proxy cannot feed
  exec'd workers, credentials are no longer written at rest into the
  sandbox as `~/.sbxloop/env.sh`: the host pipes each job's exports into
  the worker launch's stdin, the login shell evals them *after* its
  profile ran (so a stamped stale sentinel loses), and the value transits
  worker process memory only — never the sandbox filesystem, never any
  argv. Whether this sbx passes exec stdin through is a new field probe
  (`exec-stdin-env`, cached per version, also a `doctor --deep` row); a
  version that doesn't falls back to the 0600 env file exactly as before,
  and `sandbox.secret_env_fallback` events now carry `delivery: "stdin" | "env-file"`. In App mode the per-job provider re-mints the
  installation token inside its refresh margin by itself, so the hourly
  in-VM env-file rewrite disappears on the stdin tier. The strategic fix
  remains #46 (proxy-held secrets); this is the interim hardening under
  today's `sbx exec` worker model.

- **A persistent Approve-merge button on Discord** for the merge gate. The
  prompt carries a `discord.ui` button whose view is persistent
  (`timeout=None` + a stable `custom_id` from the gate row) and re-armed
  via `Client.add_view` on every (re)connect — a gate's button survives
  restarts and never expires, unlike the concierge's clarifying-question
  buttons (#570). A click runs `approve_merge` off the gateway loop and
  answers ephemerally (approval, lost CAS, or refusal); a failed landing
  re-opens the gate and the same button works again; resolution clears the
  view. Every failure mode — no component support, a rejected send, a dead
  view — falls back to the typed `!sbx merge`, which stays in the prompt
  body on every backend.

- **The opt-in merge gate — the one human touchpoint** (`[landing] merge_gate = "chat"`, default `"off"`). A run that clears every bar —
  review, CI, reconciliation — parks `gated` instead of merging: sandboxes
  freed, breaker reset, the daemon moves on, and an approval prompt lands
  in the run's chat thread @mentioning whoever asked for the work. One
  approval — `!sbx merge <item>` in chat (any backend), `sbxloop daemon ctl merge <item>` on the host — completes the landing with gh ops
  alone (update if behind, re-checked CI, the same reconciliation gate,
  merge, then the ordinary merged settle); `!sbx abandon <item>` declines
  and dismisses the gate. No deadline; the park survives restarts (a new
  `daemon_merge_gates` table is the durable state, interrupted approvals
  re-open at boot), a double-approve loses a CAS instead of double-merging,
  and the issue carries `[daemon] gated_label` (`sbxloop:awaiting-merge`)
  plus a how-to comment while parked. New `run.gated` chronology and
  `gate.approved` / `gate.merge_failed` / `gate.dismissed` notices tell the
  story in the thread.

- **GitHub App installation auth as an alternative to a PAT** (#568). With
  `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` and
  `GITHUB_APP_PRIVATE_KEY[_PATH]` configured (env / `.env`, like the PATs),
  the host signs an RS256 App JWT with its own `openssl` (no new
  dependency), exchanges it for a short-lived installation token, and
  delivers only that token to the github-ops sandbox via the in-VM env
  file — the private key never leaves the host, the agent sandbox still
  sees no GitHub credential, and every daemon/run operation is attributed
  on GitHub to the app (`<app>[bot]`) rather than a personal account.
  Tokens auto-refresh: `WorkerClient` invokes `Provisioner.gh_refresher`'s
  hook before each github job, re-minting and rewriting the env file
  inside a 10-minute expiry margin, so runs and the daemon's long-lived
  polling sandbox outlive the ~1 hour token. PAT-only deployments are
  untouched (`GH_TOKEN`/`GITHUB_TOKEN` and per-repo `token_env` behave
  exactly as before); supplying both credential sets, or a partial App
  set, is a named startup error before any microVM boots. `sbxloop doctor`
  reports the selected mode (plus an openssl check in App mode), and the
  README / `.env.example` / architecture docs describe both modes.

- **Slack as an alternative chat backend** (#532). The daemon's human
  channel — headline cards, a thread per run streaming its chronology,
  `!sbx` operator commands in the control channel and in run threads,
  @mention steering of a live run, watch/outcome pings and the concierge
  — now runs on Discord *or* Slack, chosen by `[chat] backend = "discord" | "slack"` in `sbxloop.toml` (inferred from whichever of `[discord]` /
  `[slack]` carries a `channel_id`; both without a choice, or a named
  backend without its section, fail at load with a clear error; neither
  means headless as before). The Discord bridge is refactored behind
  `sbxloop.daemon.chat.ChatBridge` — the service-agnostic pump, rendering,
  steering, watches and commands — with `DiscordBridge` and the new
  `SlackBridge` (Socket Mode via the `sbxloop[slack]` extra;
  `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` from the environment only, never
  logged) as its two transports; Discord behaves exactly as before.
  Threads persist in `daemon_chat_threads` (text ids — Slack's message
  `ts` would not survive INTEGER affinity); an existing
  `daemon_discord_threads` table is folded in on first open. `sbxloop daemon --slack-channel`, a `chat bridge (slack)` doctor row, the
  `sbxloop.toml.example` / `.env.example` entries and the README's Slack
  app setup (scopes, events, Socket Mode) document it.

### Changed

- **Prompt examples no longer tell the loop's own story** (#634). The
  decomposer's and reviewer's config-override worked example is rendered
  per run from the resolved toolchains — a Python repository reads the
  `[tool.mypy] files` story, a TypeScript one reads `tsc` ignoring
  `tsconfig.json` when handed input files, a Ruby one reads rubocop
  inspecting an `Exclude`d file named on the command line, a Go one reads
  a build tag pulling an integration suite into `go test` — so the anchor
  the model pattern-matches against is correct for the repository in front
  of it, and it costs the same tokens as the one story every run used to
  read. The ecosystem-agnostic examples (the persisted-state enumeration,
  the symptom-versus-mechanism review, the repro and follow-up JSON, the
  concierge's fix-shaped-ask walk-through) now come from a generic web
  service, and no prompt body names an issue or PR number, a path, state
  name or product vocabulary from this repository; the fix brief's test-id
  example is no longer pytest syntax. `test_prompt_bodies_stay_domain_neutral`
  holds the line, and the prompt tests anchor on rule phrases so any
  example can be swapped while the rule text stands.

- **Landing never waits on a human it never asked.** A human inline
  thread outside a standing changes-requested review — an aside on a
  COMMENT or approving review — used to block the merge forever: nothing
  in the pipeline replied to it and the #520 gate refused to merge over
  it. Landing now answers it itself with one marker-stamped "noted — does
  not hold up the merge" reply (`land.human_ack` event; the thread is
  never resolved, it stays the human's), a failed thread listing is
  retried before "could not be read" blocks, and a review round whose
  record never reached GitHub (#503) reposts it as a marker-stamped PR
  comment instead of stranding the run behind the review-record gate. A
  standing changes-requested review still blocks after its objections are
  answered — a human's voluntary override, not a gate the loop erected.
  `sbxloop doctor --probe` now flags a delivery base that requires
  approving reviews: the loop cannot approve its own PR, so every merge
  there answers 405.

- **Intake asks, but never blocks.** The concierge still asks its one
  clarifying question before filing a fix-shaped request with no symptom
  (#535) — but an unanswered question no longer parks the goal forever.
  Every filing-blocking ask now @mentions the requester and carries the
  concierge's own best guess (a fenced `sbx-pending` block, persisted in a
  new `daemon_pending_clarifications` table so a restart only delays the
  fallback), and after `[concierge] clarify_ttl_s` (default 15 minutes,
  now also the clickable-choice TTL) the bridge announces the assumption
  and drives one nudge turn that files the issue with a *Symptom
  (assumed)* section — loudly, in the channel, never in silence. Any reply
  from the asker settles the wait; a `close_issue` confirmation never
  proceeds on silence. The bridge also records the asker as the filed
  issue's requester again (`submit_turn` was never handed the author id),
  so finish pings reach whoever asked.

- **Provisioning skips the doomed proxy-secret dance on a known sbx
  version** (#568). The register→probe→auto-downgrade sequence (and its
  per-run `sandbox.secret_env_fallback` *warning*) ran on every provision,
  even though the probe's verdict — sbx proxy secrets never reach `sbx exec` workers — has been field-stable since sbx 0.35 and was already
  recorded in the version-keyed conformance cache. Under the default
  `proxy` strategy provisioning now consults that cache first: a cached
  invisible/sentinel-under-exec verdict goes straight to the in-VM env
  file (one calm `cached=true` event, info-level log), while an unknown or
  new sbx version still registers + probes exactly as before, so the cache
  re-learns per version and a future sbx that fixes exec injection is
  picked up automatically. GitHub App installation tokens never use the
  proxy path at all — they rotate ~hourly and every refresh rewrites the
  env file, so registering each one with sbx would be pure ceremony.

### Fixed

- **A click on a clarifying question that lands before the posted message
  id is resolved is now answered instead of being told the question
  expired** (#573). `_post_choice_question` used to send the message with
  its buttons attached and only afterwards resolve the message id and
  register the question, so an interaction arriving in that window found
  nothing outstanding and got the "expired — type your answer" ephemeral
  note even though the question was brand new. The question is now
  registered under a provisional key *before* the send, handed to the
  view so a click can resolve through it, and rekeyed to the real message
  id once the transport reports it (keeping the original deadline). A
  transport that cannot report an id leaves the question answerable under
  the provisional key rather than dropping it; a failed post drops it.

- **App-auth runs no longer block on their own review threads — the
  REST/GraphQL identity split** (field runs r9t8hnv33, ry2t99za6,
  ra2k5bv6z). REST attributes an App as `sbxloop[bot]`; GraphQL reports
  the same actor as bare `sbxloop`. The resolved login carried the suffix
  while `pr_review_threads` (GraphQL) did not, so every loop thread
  classified as a human's: the loop ack-replied to its own findings and
  fully reconciled PRs still ended blocked on "human review threads have
  no reply". Identity comparisons now go through `logins_match` (strip a
  trailing `[bot]`, casefold) at every thread/review/author site, so the
  two spellings are one identity; an empty login still matches nobody.

- **App-auth runs no longer strand behind "N human review threads have
  no reply"** (#569 x #536). Under a GitHub App installation token
  `GET /user` 403s, and the loop's login could degrade to `""` — which
  made `unreconciled_threads` classify every loop-authored thread as a
  human's and every reconciled PR end `blocked`. The loop's identity now
  comes from the credential itself (`<app-slug>[bot]`, one cached
  `GET /app` per process; App mode skips the doomed `GET /user`
  entirely), with the delivered PR's author as the fallback, and landing
  refuses to classify with an empty login — blocking with the real reason
  in the vanishing case where every identity source is dead.

- **Runs no longer fail after delivering their PR when github-ops runs as
  a GitHub App installation** (#581; field runs `r5ctmq7e8`, `rb20denz3`).
  The engine read the loop's own login with `gh api GET /user` — a
  user-token endpoint an installation token cannot call (403 "Resource
  not accessible by integration"), so runs that had already opened a
  working PR died on the identity lookup before review/CI/merge. The
  login now falls back to the delivered PR's author (the same token
  opened it, so the author *is* the loop's identity under both credential
  shapes), and when even that is unreadable it degrades to unknown with a
  plain warning instead of raising. `ensure_repository`'s create path
  survives the same 403 by taking the organization route.

- **`doctor` no longer fails every repository row under GitHub App auth**
  (#568 follow-up). `GET /repos/{repo}` reports user-centric permission
  booleans that are all `false` for an installation token — including
  `pull`, which the successful request itself disproves — while the real
  capabilities live on the installation. The permission check now treats
  a payload that denies even `pull` as not authoritative instead of
  reporting `token missing issues:write, contents:write, pull_requests:write` for a credential that holds all three (doctor-only;
  nothing gated dispatch on it).

- **Shape-mimicking sbx proxy placeholders are now recognized as
  sentinels everywhere** (#576 follow-up; field failure db 2026-08-31).
  sbx's *service*-secret placeholders mimic real token shapes
  (`gho_sbxproxymanaged…`, docker/sbx-releases #231) — and sbx 0.38's
  shell-docker template attaches a `github` secret slot to every sandbox,
  stamping that mimic into exec environments regardless of registrations.
  The worker's sentinel detector only knew `sbx-cs-…`, so the mimic beat
  the env file's real token (daemon github ops 401'd once the stale
  registration behind the proxy was purged; before that they silently ran
  as the wrong identity), and the secret-visibility probe classified the
  mimic as a usable credential — caching a wrong `visible-under-exec`
  verdict. `is_sbx_sentinel` now also matches the `sbxproxymanaged`
  marker, `looks_like_github_token` excludes sentinels, and both
  provisioning probes (secret visibility, the #576 shadow probe) test the
  sentinel shapes first — so the env file wins in the worker, GitHub App
  installation tokens work on template-stamped boxes, and the shadow
  probe no longer refuses a box for a placeholder the worker overrides.

- **A stale sbx secret registration can no longer shadow env-file
  credentials — GitHub App boxes were still acting as the retired PAT**
  (#576). The daemon/doctor github sandboxes have stable names, `sbx rm`
  leaves sandbox-scoped registrations behind, sbx stamps registered
  secrets into the VM at create, and sbx 0.38 stamps exec environments
  too — so after the App cutover the leftover `github` service
  registration's shape-mimicking `gho_…` sentinel outranked the
  installation token in `~/.sbxloop/env.sh` (the worker keeps
  credential-shaped values; the egress proxy rewrote them with the old
  PAT). Every write kept succeeding, silently, as the wrong identity.
  Env-file provisioning now **purges registrations parked at the sandbox
  name before `create`** (`sandbox.stale_registration_purged`), and
  github boxes get a post-write shadow probe that fails provisioning
  loudly (`sandbox.credential_shadowed`) when a credential-shaped
  GH_TOKEN/GITHUB_TOKEN is still stamped (e.g. a global-scope
  registration the purge must not touch), instead of running as the
  wrong identity. Proxy-mode PAT provisioning is unchanged
  (`set_secret_replacing` already replaces in place).

- **Filing follow-up issues no longer errors when the follow-up label
  already exists** (#556). The run blind-POSTed `/repos/<repo>/labels`
  before filing, so every repository that already carried
  `[landing] followup_label` took a guaranteed 422 "already_exists" — an
  error in the run's chronology for a routine condition. `_ensure_label`
  now asks first through the new `label.get` worker op and its host-side
  `GithubOps.label_lookup`, which — like `repo.get`/`ref.get` under
  `allow_missing` (#222, #518) — answers an absent label as
  `{"missing": true}` on an ok result, so the repository that *lacks* the
  label creates it with one clean call and no `worker.error` panel either.
  Only a 404 is a miss: a 403 from a token without repo scope, or a 5xx,
  is one warning and no doomed POST behind it. The 422 catch on the create
  is kept for the race where the label appears between the two calls, and
  a label the run cannot create still does not stop the filing.

- **A fix-round re-delivery no longer fails the branch create before
  force-moving it** (#518). The delivery branch is a pure function of the
  run id, so on every round after the first `deliver` blind-POSTed
  `/git/refs`, took the guaranteed 422 "Reference already exists", and
  only then force-moved the branch — one doomed API call (~3 s through the
  github sandbox), a `worker.error` panel in the run's Discord chronology
  and a `worker.job_done error=` per *healthy* re-delivery (field run
  `rfxja288b`, rounds 2 and 3), with a hint that misdescribed it as "a
  prior attempt". `_point_branch` now asks first (`ref_lookup`, the miss
  travels as data): a missing ref is created with one call as before, an
  existing one goes straight to the force-move, and
  `deliver.branch_force_moved` says what it superseded (`from=<old sha> to=<new sha> round=N`; the manual `sbxloop deliver <run>` path has no
  round to report). The 422 catch is kept only for the race where the ref
  appears between the lookup and the create.

- **A repository that keeps failing to poll is backed off and suspended on
  its own, not warned about every tick forever** (#516).
  `MultiRepoIssueSource` swallowed a per-repository poll failure so the
  healthy repositories still fed the queue — right for an outage, wrong
  for a renamed, private or misspelled repository, which logged a warning
  and wasted an API call every poll indefinitely while the daemon looked
  healthy from Discord and `status`. Each repository now has its own
  health: a failure backs it off (poll interval doubling per consecutive
  failure, capped at an hour) while its neighbours poll on; after
  `[daemon] repo_suspend_after` (default 10) consecutive failures — or at
  once when GitHub says the repository is gone for this token (404/410, a
  permission 403; rate limits and 5xx back off instead) — it is
  **suspended**: excluded from polling, announced once on Discord
  (`source.repo_suspended`), shown in `ctl status` (`repos:` line, only
  when something is wrong), the concierge's `list_repos` and `sbxloop doctor` (from the health the daemon persists), and resumed with the new
  `ctl resume-repo <owner/name>` / `!sbx resume-repo`, or by a daemon
  restart. Recovery is one info line and one notice. The all-repositories-
  failed re-raise that drives the loop-level source backoff is unchanged,
  and a suspended repository no longer counts toward it.

- **`sbxloop doctor` boots nothing by default, and at most one github
  sandbox per credential when it probes** (#515). Multi-repo support (#511)
  wired a reachability probe that provisioned one github-only microVM per
  configured repository on every `doctor` invocation, so the deploy health
  step's wall clock scaled with the repository count. Probing is now behind
  `--probe` (implied by `--deep`); the default rows say "reachability
  unverified from the host … `sbxloop doctor --probe` boots one to ask".
  When it probes, repositories are grouped by credential (`token_env`, or
  the daemon-wide token) and share one sandbox per group; a credential
  whose sandbox will not boot answers "unverified" for every repository on
  it without re-provisioning.

- **Discord's automatic link previews are suppressed in bridge output**
  (#519). The grey unfurl cards Discord generates under any message
  containing a bare URL were what made the control channel hard to read —
  not the bridge's own embed cards, which stay. Every send now sets the
  `SUPPRESS_EMBEDS` message flag unless the message carries one of our
  embeds, in which case the body is angle-bracketed through the new
  `discord_format.no_unfurl` (idempotent; leaves code spans, already-
  bracketed URLs and markdown link targets alone). Edits go through a new
  `DiscordBridge._edit` that re-asserts the flag, because discord.py
  clears it otherwise and the first edit of a live status, tool digest or
  concierge note would bring the preview back. `[discord] embeds = false`
  still falls back to the plain-markdown twins of the cards; unfurl
  suppression is unaffected by that toggle.

- **The concierge files symptom-first issues and asks before filing a fix
  with no symptom** (#535). #519 was filed as the mechanism the person
  named ("remove the Discord embeds"); the loop implemented exactly that
  (PR #525), and it was wrong — they were seeing link-preview unfurls — so
  it was reverted: one run, two releases, two deploys. `create_issue` now
  takes `symptom` (the person's own words), `requested_change` (a hint),
  `goal` and `acceptance_criteria`, and composes the body in that order
  with criteria written against the symptom; a call with a requested
  change and no symptom is refused with the one question to ask ("What are
  you seeing that you want gone or changed?"). The concierge prompt says a
  fix-shaped ask with no observed symptom is genuinely ambiguous — one
  question, then file — with the #519 conversation as the worked example;
  the decomposer treats a Symptom section as the spec and the requested
  change as a hint it may overrule; the reviewer judges the PR against the
  symptom in round 1, and a PR that implements the mechanism without
  removing the symptom is `request_changes` on the plan. Plain `body`
  filing still works.

### Added

- **Follow-up issues from a landed run** (#517). The reviewer's out-of-scope
  notes used to be prose in a review body nobody reads after the merge
  (run rfxja288b left two, both worth issues, both filed by hand).
  `ReviewVerdict` gains `followups` (`title`, `body`, optional
  `path`/`line`), the review prompt asks for them separately from
  `findings` and forbids promoting one to a finding, and they render in
  the review body under their own heading. After the pull request merges —
  never on a failed or blocked run — the engine files them as issues on the
  run's repository, along with the findings the fix rounds `deferred:`
  (#522), each cross-linked to the PR, originating issue, run and round.
  Deduplicated by normalised title within the run and by a body marker
  against the repository (a resume between filing and recording does not
  double-file), capped by `[landing] max_followups_per_run` (5), labelled
  `[landing] followup_label` (`sbxloop:follow-up`) and **never** the trigger
  label — the loop still files no work of its own; a human promotes one. A
  PR comment lists what was filed; `followups = "comment"` lists them on the
  PR instead of filing, `"off"` drops them. Narrated as `run.followups`.

### Fixed

- **The fixer can no longer drop a non-blocking finding on the floor**
  (#522). Only blocking findings reached the fix brief, so a `minor`
  finding got neither an `addressed:` nor a `refuted:` line, was re-raised
  once, and was then carried as prose until the run failed (PR #512's
  unread `RepoConfig.labels`). Every finding of a `request_changes` round is
  now in the brief — blocking ones to address or refute, the rest to
  address, refute or **`deferred: <path:line> — why`** (parsed alongside
  the other two; the thread is resolved and the finding is closed for this
  PR as a follow-up). A finding with no line is *unanswered*: the engine
  logs `fix.unanswered_findings` and narrates `fix.unanswered` in Discord,
  the next fix brief lists those findings first marked as previously
  unanswered, the review history marks them `UNANSWERED`, and the reviewer
  is told silence is not closure — the finding stays at its original
  severity and is carried as `still_open`. Refuted and deferred findings
  are what the reviewer must not re-raise without a rebuttal. A late answer
  — a finding round *k* left unanswered that round *k+n*'s report finally
  addresses, refutes or defers — is replied onto round *k*'s own thread
  under the later round's marker. Also fixed on the way: a finding the
  reviewer *re-filed* on an earlier anchor (rather than confirming it) was
  carried by `split_carried` but never reached the fix brief, because the
  engine read `carried_forward` off the pre-split verdict.

- **A daemon killed mid-claim no longer orphans the issue** (#530). A
  restart between "claim comment posted" and "claim persisted" left the
  new process losing the claim race to its own dead predecessor and
  terminal-failing the row — permanently, since `failed` is what discovery
  dedups against (#527 was fixed by hand). Four changes, one per hole: the
  claim comment carries `host=… pid=… started=…` and a claim from a dead
  pid on this host, or older than `[daemon] claim_stale_after_s` (default
  300 s) with no "Run … started" comment after it, is released and
  reclaimed (`github.claim_reclaimed`); a claim that is not ours — lost
  race, closed issue, trigger gone, GitHub down — leaves no row at all
  (`DaemonStore.discard`), so the next poll re-creates it if the trigger
  label is still there; the claim token is persisted
  (`daemon_work_items.claim_token`) before the comment goes up, and
  recovery settles a half-claim against the issue (`settle_claim`:
  comment present → finish the label swap and dispatch; absent → claim
  again), narrated as `recovery.claim_settled`; and SIGINT/SIGTERM are
  held for the seconds a claim takes (`defer_signals`) and delivered after
  it is persisted. The pre-#530 store shape is in the legacy-db fixture.

- **Single-identity review posts PR comments, not a doomed review** (#513).
  One token opens the PR and reviews it, so every round POSTed
  `REQUEST_CHANGES`/`APPROVE`, took a 422 ("can not request changes on your
  own pull request") and a `gh.review_event_refused` warning, and re-posted
  as a `COMMENT` review — two doomed calls per round on every run. When the
  PR's author is the loop's login (decided once per drive from the PR), the
  review is now posted as PR comments: each anchored finding as its own
  review comment via `POST /pulls/{n}/comments` (a resolvable thread, which
  later rounds reply in and resolve exactly as before), and the verdict —
  in words, `**Review verdict: changes requested** (round 2)` — with the
  summary and every finding that got no thread in one top-level comment. An
  anchor GitHub refuses fails only its own comment and lands in the body
  (per-finding degradation, the #514 shape), instead of 422ing the whole
  review. A distinct reviewer identity still uses the review feature with
  the `COMMENT` fallback; the review body now opens with the verdict line in
  every mode.

- **A change to persisted state gets its own upgrade-path task** (#524).
  Issue #511's plan buried the store migration inside two tasks with no
  acceptance criteria of its own, and all four review rounds on PR #512 —
  and the run's failure — were about that migration. The decomposer prompt
  now carries a risk pass: when the outcome alters a SQLite schema or row
  meaning, an id or key format, a stored config key or a state-directory
  layout, it must add a dedicated *upgrade path for existing state* task
  whose acceptance criteria enumerate the row states and id forms a
  deployed instance can hold and whose verify commands run tests that
  start from a raw pre-change database. The reviewer asks the same
  question of the plan in round 1 (a missing task is a blocking finding on
  the plan), and the concierge adds a "Migration of existing state"
  section to issues whose ask touches persisted state.
  `tests/fakes/legacy_db.py` freezes every released schema shape (daemon:
  pre-#508, pre-#511, pre-#523; engine: pre-workspace through
  pre-granted-rounds) with helpers that write raw rows, and
  `tests/unit/test_legacy_db.py` sweeps one work item per state × id form
  through each shape; the existing migration tests build on it.

- **Fix rounds no longer converge one adjacent case at a time** (#521). Run
  `rfxja288b` spent its whole review budget on one migration, one real
  finding per round, each in the previous round's new lines, because the
  reviewer's reproduction reached the fixer only as prose and the fixer
  tested the shape the finding named. A `ReviewFinding` now carries the
  reviewer's `repro` (required on blocking/major findings — `ReviewGuard`,
  formerly `RefutedGuard`, sends back once a verdict missing one; the
  prompt asks the reviewer to reproduce before filing and to name the
  neighbours). The fix brief renders each repro as a regression test that
  must fail on the current tree first — built the way the repro describes,
  not through the code path under test — asks the fixer to list the other
  inputs the same path sees, and shows the earlier rounds with each
  finding's fate in the previous fixer's words (`render_fix_history`). The
  fixer's `addressed:` line names the test it added (`; test: <id>`);
  `reconcile()` records it (`Reconciliation.test`) and the thread reply and
  the next fixer's history carry it. Repros also appear in the posted
  inline comments and review body.

- **A run that exhausts its fix-round budget resumes its own PR instead of
  starting over** (#523). Exhausting `max_review_rounds` / `max_ci_rounds`
  used to be an ordinary failed attempt: the item's retry was a fresh
  decompose/build on a new branch with a second PR, while the failed run's
  branch sat green one round from mergeable. The engine now records which
  budget ran out (`runs.exhausted`); under the daemon the first exhaustion
  grants `[landing] retry_rounds` (default 2) more rounds and schedules a
  resume of the same run after the retry backoff — no attempt spent, no
  breaker count, no resume-budget slot — and a second exhaustion hands the
  item over with the run still pinned. `sbxloop daemon ctl grant-rounds <run> <n>` (also `!sbx grant-rounds`, and the concierge understands "give
  rXXXX two more rounds") grants more and resumes at once, skipping the
  backoff; `sbxloop resume --grant-rounds N` is the CLI equivalent, and a
  bare resume of an exhausted run is refused with that hint rather than
  re-exhausting after one wasted review. The `run.exhausted` notice says
  which budget ran out and what happens next. State: `runs` gains
  `exhausted` and `granted_rounds`, `daemon_work_items` gains `not_before`
  (a scheduled retry's earliest dispatch); both migrate in place and are
  tested from raw pre-upgrade databases.

- **A deploy never restarts the daemon under a live run** (#534). The deploy
  pipeline's drain was capped at 20 minutes and then restarted anyway; with
  the loop merging its own PRs every merge deploys, and the next queued item
  is usually already running when the deploy lands, so runs were being
  interrupted mid-task and charged a resume-budget slot for it. The drain now
  waits for `current: idle` without a cap (the job's 8 h `timeout-minutes` is
  the only bound, and a timeout installs nothing), and a claim in progress
  counts as busy — `ctl status` reports `current: claiming <item>` — so a
  restart is never timed into the window that orphaned #527 (#530).

- **Pause is a set of named holds.** `ctl pause --hold NAME` / `ctl resume --hold NAME` (and `!sbx` likewise) take and release a named hold; a bare
  `pause`/`resume` acts on the operator's hold; `resume --all` clears every
  hold. The daemon idles while any hold stands, `status` lists them, and the
  transitions are narrated in Discord (`daemon.paused` / `daemon.resumed`,
  naming the hold and who took it). The deploy holds `deploy-<run id>`,
  snapshots the *other* holds immediately before the restart and re-takes
  them afterwards, and releases its own on `always()` — so an operator pause
  survives a deploy, including one issued while the deploy was already
  waiting (the two pause/restore races seen on 2026-08-29). Rollback now runs
  only once the upgrade step has, and the Discord deploy notices say whether
  the restart was deferred behind a run and how long it waited.

### Added

- **Review findings are reconciled on the pull request** (#520): between a
  fix round's re-delivery and the next review, the engine now speaks the
  fixer's per-finding answer back onto the review's own threads. Each
  prior-round finding with an inline thread gets exactly one reply —
  `addressed in <sha>: <what changed>` (and the thread resolved), `refuted: <why>`, or a note that the round did not answer it (both left open) —
  while findings posted body-only are gathered into a single
  `Reconciliation — round n` pull request comment. Every reply carries a
  machine-readable `run`/`round` marker, and the reply/resolve is recorded
  in the state database as it happens, so a resume between posting and
  recording does not double-reply. A new `review.reconciled` event carries
  the addressed/refuted/unanswered counts into the log sink and the Discord
  chronology.

- **The next review round confirms carried-over findings in their own
  threads** (#520): a round-*n+1* reviewer now returns an anchor-keyed
  `confirmations` list — `confirmed_fixed` or `still_open` per finding an
  earlier round raised — and the engine posts each verdict as a reply in
  that finding's existing thread, resolving the ones confirmed fixed. The
  new review body carries only the overall summary and genuinely new
  findings; a carried finding is never restated there. A `still_open`
  verdict leaves the thread unresolved and carries the original finding
  (its severity and words, plus the reviewer's note) into the next fix
  round. First-round reviews are unchanged, and the confirmation replies
  are marker-stamped and store-recorded so a resume does not double-post.

- **A human's changes-requested review is answered on its own threads** (#520): a `NeedsFix("human")` round now carries the objections it was
  seeded with — the reviewer's review body and each of their inline
  comments — and after the fix re-delivers, each inline objection receives
  one reply stating the change (`addressed in <sha>: …`), the fixer's
  reasoned explanation (`not changed: …`), or, when the round said nothing
  about it, that it is being left open. A human's thread is **never**
  resolved by the loop; objections raised in the review body are answered in
  a single pull request comment instead. Each answered objection is recorded
  in the state database, which fixes a repeat-work bug: only its author can
  dismiss a `CHANGES_REQUESTED`, so the same review still stands on the next
  landing pass — that pass now hands over as `Blocked` naming the replied
  objections rather than spending another full `max_ci_rounds` fix pass on
  words already answered.

- **A pull request does not merge until its review record is complete**
  (#520): `land()` gained two preconditions immediately before the merge
  call. The approving round's review must actually have posted — a run whose
  review post failed used to merge with no review on the pull request at all
  — and every inline review thread must be reconciled: a loop thread counts
  when it is resolved or carries a later loop reply (the refuted case), a
  human thread when the loop replied in it at all. Anything left over ends
  the run as `Blocked`, naming the offending anchors
  (`N review threads unreconciled: …`), and a thread read that *fails*
  blocks too, since "we could not tell" is not "there is nothing to answer".
  `docs/architecture.md` gained a *Reconciling review findings on the pull
  request* section covering the contract, the fixer's per-finding report
  format and this gate.

- **`sbxloop.toml.example` at the repository root** (#527), covering every
  section and key the config model knows — including both `[github]` forms
  (the legacy single `repo` and the `[[github.repos]]` array with
  `workspace`/`deliver_base`/`enabled`/`token_env`/`trigger_label`/`labels`)
  — with the default and a one-line comment per key. The top-level keys are
  live and every section ships commented out, so a fresh copy is exactly the
  built-in defaults. It is now the single
  source `sbxloop init` writes from (shipped as package data), and the new
  `sbxloop init --stdout` prints it. `.env.example` was refreshed: the
  `DISCORD_BOT_TOKEN` and `GITHUB_TOKEN` alias credentials, the per-repo
  `token_env` pattern, the daemon-host `~/.config/sbxloop/secrets.env`
  layout, and the single-repo `SBXLOOP_GITHUB__REPO` override marked legacy.
  Tests pin the example against `sbxloop init` and the config model and
  reject anything that looks like a real token, snowflake, host path or
  non-placeholder repository.

### Changed

- **Reverted #525** ("Remove Discord embeds from daemon bridge output in favour of plain markdown", #519): the plain-markdown bridge output read worse in the field than the embeds it replaced. Embeds, the `[discord]` keys #525 removed, and the previous rendering tests are back exactly as they were.

### Added

- **One daemon can tend several GitHub repositories** (#511). `sbxloop.toml`
  accepts an array of `[[github.repos]]` entries, each carrying its own
  `deliver_base`, `create_repo`/`create_public`, `trigger_label`, extra
  `labels`, an `enabled` switch and an optional `token_env`. The daemon polls
  every enabled repository for the trigger label, work items carry the
  `owner/name` they came from (ids are repo-qualified — `gh:o/r:issue:12` —
  with the legacy `gh:12` form still resolving), and a run's clone, branch,
  draft PR, review, CI polling, merge and issue comments/labels all target
  that repository. Its github-ops sandbox is provisioned scoped to that repo
  and given that repo's credential; the agent/github credential split is
  unchanged. The single `[github] repo = "owner/name"` form still loads and
  behaves exactly as before, normalised internally into a one-entry list;
  the two forms are mutually exclusive and duplicate or malformed entries
  fail config loading with an explicit error. `sbxloop doctor` checks each
  configured repository on its own line, `sbxloop status` and
  `sbxloop daemon items` carry a `repo` column, `sbxloop config repos` lists
  the registrations, and the concierge gained a `list_repos` tool (plus an
  optional `repo` selector on its GitHub-reading tools) so "what projects are
  you configured to work on?" is answerable from chat. The `[daemon]`
  guardrails — daily run cap, per-item attempt and resume caps,
  consecutive-failure circuit breaker, one run at a time — remain
  **daemon-wide** and are shared across every repository; README, the
  architecture doc, the deploy doc and the `sbxloop init` template say so
  explicitly, and tests assert the cap and the breaker apply across items
  from different repositories. The daemon's work-item store keys an item by
  `(issue number, repository)` rather than the issue number alone, so issue
  #4 in two repositories is two items; a store written before multi-repo
  support is migrated in place on open and its rows keep working.

### Fixed

- **A run for repository B is no longer built from repository A's checkout**
  (#526). Multi-repo support resolved a run's *workspace* — the host git
  checkout every run clones its tree from — from the single daemon-wide
  `[sandbox] workspace`, so a daemon upgraded from a working single-repo
  deployment routed everything else per repository (claim, labels, PR) while
  building each run out of whichever repository that one checkout happened
  to be. `[[github.repos]]` entries now take their own `workspace`, and both
  the pre-run fast-forward and the provisioner clone resolve it per
  repository. A checkout whose `origin` names a different repository is a
  hard failure at three points — `sbxloop doctor` fails a check per
  offending repo, `sbxloop daemon` refuses to start, and the provisioner
  refuses the clone — each naming both repositories and the fix. A repository
  with no workspace clones from its own remote (public repositories only:
  the host holds no git credential, see #46) or fails the run with that
  reason; there is no fallback to another repository's tree anywhere.
  `[sandbox] workspace` still works unchanged for a single repository; with
  several, migrate by moving it into the matching `[[github.repos]]` entry.

### Changed

- **GitHub work-item ids are typed: `gh:issue:1234`, `gh:pr:1234`** (#508).
  The old `gh:1234` said nothing about what it pointed at, and a run carries
  an issue number *and* a PR number side by side in chat, issue comments,
  Discord threads and logs — readers had to guess. A new `sbxloop.ghids`
  module owns the whole grammar (`format_gh_id`/`issue_item_id`/`pr_item_id`
  to render, `parse_gh_id`/`try_parse_gh_id`/`normalize_item_id` to read) and
  nothing else slices `gh:` strings by hand. Rendering is strict — every id
  produced now carries its kind — and parsing is lenient: a bare `gh:<n>`
  read from an old checkpoint, an old watch or typed by an operator is
  accepted as the issue it always meant and normalised on the way in.
  Adopted at every construction site (GitHub source discovery, the work item
  model, the concierge's issue lookups), every lookup in the daemon store
  (which also resolves legacy rows under either spelling, so no state
  migration is required), the operator verbs (`items`, `queue`, `abandon`,
  `retry`, `requeue` — both spellings in, typed out), the concierge tools
  and their descriptions, Discord headline cards, thread names and control
  replies, the GitHub comments and PR text a run writes back, and the
  daemon's log/event fields. README, the architecture doc and the concierge
  prompt lost their `gh:12`-style examples.

### Fixed

- **The verify-suspect signal no longer collides distinct failures, over-reaches
  on narrowing commands, or orders an impossible re-author** (#387 review of
  PR #509). Three corrections: the duration normaliser no longer treats
  `line:column` coordinates as a timestamp (clock-style durations are matched
  only after `in`/`took`/`elapsed`/`time`), so the same error at two different
  positions is two fingerprints rather than one false "suspect"; the
  config-override lint now fires only when the given path lies *outside* the
  configured file set, keeping `uv run mypy packages` flagged while allowing
  `uv run pytest -q tests/unit` and `uv run mypy packages/sbxloop/src/…`; and
  the suspect feedback is written for the builder — the only agent it reaches,
  and one that cannot edit the decomposer-authored commands — asking it to
  satisfy the command as written or report it unpassable, with the suspect
  state now surfaced in the run's failure reason.

- **A fix round re-delivers onto the pull request it knows, never a blind
  create.** Field run `r8tzse1qa` (#387 → PR #505): round two force-moved
  the branch, POSTed a new PR, got the `gh` transport's bare "Validation
  Failed (HTTP 422)" — the transport dropped the API's error body, so the
  "pull request already exists" match never fired — and the run failed at
  `delivering` with its PR number in hand. Three layers: delivery takes the
  run's recorded `pr_number` and skips the create entirely; a 422 on a
  create is confirmed by looking up the branch's open PR rather than by
  matching prose; and the `gh` transport keeps the API error body in the
  message. (The old loop had filed exactly this as #488/#490/#495/#497
  before its findings were closed at the cutover — a reminder that the
  lane, not the findings, was the problem.)

- **Every fix round starts from the current base**, not only a `conflict`
  one. CI judges GitHub's test merge of the branch with its base, so a red
  check can exist only in that merge — PR #505 failed a test that landed
  on `main` after its run branched — and a fixer on a stale clone cannot
  even reproduce it. `hostgit.merge_from_base` now runs before any fix
  round; a base that no longer merges cleanly leaves conflict markers the
  brief lists.

### Fixed

- **A review refused for its anchors is re-posted with its findings in the
  body.** Field run `rx8amxxvm` (#130 → PR #503) approved with two nits
  anchored to lines outside the diff; GitHub 422'd the APPROVE *and* the
  COMMENT fallback, and nothing reached the PR (the verdict, which is the
  run's own, still decided correctly). The engine now retries once
  without inline comments, listing every finding in the review body. The
  stale `last_event_ts` docstring that review caught is fixed here too.

### Changed

- **Streaming deltas are no longer persisted.** `agent.message_delta` is
  per-chunk UI telemetry: live surfaces (TUI, Discord) still receive every
  delta over the event bus, but `StateStore.append_event` drops them
  instead of writing one committed row per chunk. The full `agent.message`
  is persisted as before, so `sbxloop logs <run>` and the daemon log sink
  are unchanged apart from the absent delta lines, and resume — which
  never read deltas — behaves identically. The state DB also runs
  `PRAGMA synchronous=NORMAL`, the safe setting under WAL, so a commit no
  longer fsyncs per event.

- **Retired config keys are errors.** The `[daemon]`/`[github]` keys the 1.0
  pipeline retired (see 0.7.55 below) were loaded with a warning for two
  releases so the unattended daemon deploy could not roll back on them;
  that tolerance — `Config.retired_keys`, the `config.retired_keys`
  warning, the `retired config keys` doctor row — is gone, and an unknown
  key fails config loading like any other. Edit `sbxloop.toml` before
  upgrading a 0.7.x host straight to 1.0 ("1.0 cutover", below).

- **The run thread follows the pipeline.** The per-run status line now says
  which stage the run is in once its tasks are built — `🚦 gate`,
  `🔀 delivering`, `🔍 review round 2`, `🛠 fix round 1 (review, budget 1/3) · build`, `⏳ CI · 2 pending` / `❌ CI red` / `✅ CI green`,
  `🚀 landing · out of draft` — instead of "1 task(s) planned" for the
  whole second half of the run, and ends `🎉` / `🚧` for `merged` /
  `blocked`. The note under a queued steer says the same ("the run is
  waiting on CI; answered now" — a message wakes a GitHub wait at once).

- **A conflict fix round starts from the merged base.** Before a
  `conflict` round the engine merges `origin/<base>` into the run's clone
  (`hostgit.merge_from_base`): uncommitted work is checkpointed, a clean
  merge just lands, and a conflicting one is left in progress with the
  conflicted paths quoted in the fix brief for the fixer to resolve and
  commit. Before this, delivery overlaid the run's files onto the current
  base tree, so the conflicting hunks were silently overwritten with the
  run's version. The review diff is likewise taken against the *current*
  base commit, so a round after such a merge reviews the run's changes
  and not the base branch's movement.

- **One run, from issue to merged pull request.** The engine now carries a
  run past its task graph: GATE (the project's own gate over the whole
  tree) → DELIVER (a draft PR) → REVIEW (the run's own adversarial pass
  over the PR's diff, a fresh read-only session; its verdict is
  authoritative and is also posted to the PR) → FIX rounds (one seeded
  `fix-N` task each, built and verified like any other, re-delivered onto
  the same branch, back through the gate) → CI (red fetches the failing
  jobs' logs into the next fix brief) → LAND (un-draft, update-branch,
  merge with the judged head). Run states grew `gating`, `delivering`,
  `reviewing`, `fixing`, `awaiting_ci`, `landing`, `merged` and `blocked`
  (`running` is now `building`; `finalizing` is gone); `runs.stage` keeps
  the last stage entered so `resume` re-enters there — a crash during a CI
  wait costs a re-poll, not a rebuild. The knobs live in a new `[landing]`
  section (`max_review_rounds`, `max_ci_rounds`, `ci_poll_interval_s`,
  `ci_settle_s`, `ci_timeout_s`, `merge_method`, `delete_branch_on_merge`,
  `merge_update_attempts`, `deliver_draft`). Merging is not optional any
  more: a run that cannot land its PR ends `blocked` with the PR open for a
  human; one that runs out of rounds ends `failed` with the PR still a
  draft. Waiting on GitHub is not charged to `max_wall_clock_s`, and a
  Discord message or a cancel wakes a wait at once.

- **Findings carry forward inside the run.** Every review round sees the
  earlier rounds' findings and the fixer's per-finding `addressed` /
  `refuted: <why>` list; a verdict that only re-raises refuted findings is
  sent back once with the history quoted. This, plus the round budgets, is
  what stops a run arguing with itself.

- **The daemon files nothing.** Gone: the agent backlog lane
  (`.sbxloop/backlog/*.md` → `sbxloop:backlog` issues), post-mortem
  issues, scheduled audit charters (`.github/sbxloop/audits/`), tool
  findings routed to `tool_repo`, the per-run tracking issue, and the
  review lane's charter issues that re-entered the queue as `audit` work
  items. In the field the loop had filed the same finding on consecutive
  days under different issue numbers while only 17 of 225 issues ever
  reached `sbxloop:completed`. A work item is now exactly one labeled
  GitHub issue → one run; the inbox source and its `enqueue_work`
  concierge tool are gone with it. The daemon settles each run's outcome
  on the issue: `merged` → closed with `sbxloop:completed`, `failed` →
  `sbxloop:failed`, `blocked` → the new `sbxloop:blocked`.

- **Config cutover, tolerated.** `[daemon]` lost `inbox_dir`, `backlog`,
  `backlog_max_per_run`, `backlog_auto_trigger`, `backlog_label`,
  `audits`, `audit_dir`, `audit_label`, `delivered_label`, `postmortems`,
  `postmortems_per_day`, `review_deliveries`, `await_review`,
  `review_rounds`, `tool_repo`, `tracking_issue`, `close_on_success`,
  `auto_merge` (landing is always on) and gained `blocked_label`;
  `[github]` lost `report` and `deliver` (a repository means deliver). The
  moved knobs (`deliver_draft`, `merge_method`, `delete_branch_on_merge`,
  `merge_update_attempts`) are carried into `[landing]` when found in
  their old place. A config still carrying any of these loads with a
  `config.retired_keys` warning and a `sbxloop doctor` row rather than
  failing — the daemon host deploys unattended and a hard failure there
  would roll the release back before anyone could edit the file. They
  become errors in 1.0.0.

- **State cutover.** The daemon's tables changed shape (no PR-state,
  review, audit, post-mortem or backlog tables; one item kind). A pre-1.0
  `state.db` is moved aside to `state.db.pre-1.0` on first start rather
  than migrated ("1.0 cutover", below).

- **Removed with the above:** `sbxloop deliver` (resume at `delivering` is
  the retry path), `sbxloop run --report/--deliver/--deliver-draft`, the
  `GithubReporterHook` tracking issue, `sbxloop daemon --inbox/--backlog`,
  the `run.report` event, `LoopEngine.deliver()`. New worker op
  `checks.failed_logs` (Actions job logs for failed check runs; the REST
  transport does not forward the bearer token on the redirect to blob
  storage) and `GithubOps.checks_failed_logs` / `pr_review_feedback`.

### 1.0 cutover

The 1.0 pipeline (one run from issue to merged PR; no self-filed audits,
post-mortems or backlog issues; landing under `[landing]`) changes what the
daemon keeps on disk and which config keys exist. Because a daemon host may
deploy unattended, none of that may fail the restart:

- **State.** A pre-1.0 `state.db` carries the old lanes' tables and item
  kinds. On its first start the new daemon moves the whole file aside to
  `state.db.pre-1.0` (plus `-wal`/`-shm`; a timestamp is appended if that
  name is taken), logs `store.archived_legacy`, tells the control channel, and starts
  with empty tables. Engine run history goes with it — both stores share the
  file. Nothing is migrated; renaming the file back restores the old world
  for a 0.7.x rollback.
- **Config.** The retired keys — `[daemon] inbox_dir, backlog*, audits, audit_dir, audit_label, backlog_label, delivered_label, postmortems*, review_deliveries, await_review, review_rounds, tool_repo, tracking_issue, close_on_success, auto_merge` and `[github] report, deliver` — are unknown keys since 1.0.0 and fail config loading like any
  other (`Extra inputs are not permitted`). The `deliver_draft`,
  `merge_method`, `delete_branch_on_merge` and `merge_update_attempts`
  knobs live under `[landing]`. The two releases before 1.0.0 (0.7.55,
  0.7.56) tolerated them with a `config.retired_keys` warning and a
  `sbxloop doctor` row precisely so an unattended deploy could not fail on
  them; a host that skipped those releases must edit `sbxloop.toml` before
  installing 1.0, or the daemon will not start (an automated deploy's
  health check then rolls back).
- **Issues and labels.** The old loop's `sbxloop:backlog` / `sbxloop:audit`
  issues are closed by hand at cutover (`gh issue close --reason "not planned"`), those two labels and `sbxloop:delivered` deleted, and
  `sbxloop:blocked` created. Any of the old loop's PRs still open
  (`gh pr list --search "head:sbxloop/ is:open"`) are merged or closed by
  hand — their items went with the archived state.

### Added

- **The loop can land its own pull request (`[daemon] auto_merge`, default
  off).** *(Superseded above before release: landing is now unconditional
  and configured under `[landing]`.)* A delivered PR that clears the full acceptance bar — every check
  green *and* the review satisfied — is taken out of draft, brought up to
  date with its base if protection requires that, and merged; the source
  issue then settles through the merge path that already existed (closed,
  `sbxloop:completed`). Before this, a green approved PR was still a draft
  waiting on a person, so every run ended with a human doing the last step.
  New knobs: `merge_method` (`squash` | `merge` | `rebase`, default
  `squash`), `delete_branch_on_merge` (default `true`), and
  `merge_update_attempts` (default 3, `0` disables branch updating).
  Details:
  - Off by default on purpose. Merging is the only irreversible thing
    sbxloop does to a repository, and on a repo whose merges publish — this
    one releases to PyPI and redeploys the daemon host on every merge to
    `main` — turning it on means unattended releases.
  - The bar is the *full* one. A PR the gate accepted for a weaker reason
    (green CI with no reviewer available) still settles the old way and is
    never merged.
  - `GithubOps` grew `pr_ready_for_review`, `pr_merge`, `pr_update_branch`
    and `branch_delete`, all through the existing `raw.api` transport — no
    new worker op. Un-drafting is the one GraphQL call in the codebase
    (`markPullRequestReadyForReview`; REST cannot un-draft a PR), and it
    reads the response *body* rather than its status, because GraphQL
    answers a failed mutation with a 200 and an `errors` array.
  - Two GitHub refusals arrive as data, not exceptions: `405` ("not
    mergeable right now" — commonly a protection rule wanting an approval
    sbxloop's identity cannot give) hands the item over with the PR left
    open and out of draft, one click from done, rather than spending budget
    on a refusal no round can fix; `409` is a race, and the next poll
    re-judges the new head.
  - `daemon_pr_state` gained `updates` and `update_head` (additive
    migration, applied at open). `update_head` records the head an
    update-branch was requested at, so a poll can tell an update still in
    flight from one that has landed instead of spending the budget asking
    twice. The request carries `expected_head_sha`, so an update racing
    another push fails rather than merging over it. A database created by an
    earlier unreleased build of this work keeps its now-unused
    `delivered_head` and `landing` columns — both are vestigial and harmless
    (`landing` is `NOT NULL DEFAULT 0`, so inserts still work), and only a
    freshly created database omits them.

### Removed

- **`Usage` no longer carries a spend field, and no usage block renders a
  money-shaped number.** #439. After #386 stopped both producers writing it
  (`usage_from_sdk_sample` and the concierge's `_USAGE_FIELDS`), the field on
  `Usage`, its last-wins branch in `Usage.merged`, the `phase_attempts.cost`
  column and the concierge's `f"...{...:.4f}"` spend render were reachable only from
  a hand-built object in a test — dead wire plumbing whose surviving render
  was precisely the currency shape #386 forbade. `run_usage` and `usage_today`
  now end with a fixed "spend: not reported by the agent backend (tokens above
  are the whole record)" line, and the Discord run summary drops its `$x.xx`
  bit. Existing state databases are unaffected: the dropped column is simply
  no longer written or read.

### Changed

- **PR takeover detection is gone (#483).** The daemon no longer compares a
  PR's head against the sha its delivering run recorded, no longer emits
  `review.taken_over`, and no longer hands a PR to a human because commits
  it did not deliver appeared on the branch. In practice the "stranger" was
  sbxloop itself — a review run pushing fix-round commits onto the
  delivering run's branch — and the guard stopped the loop short of the
  merged PR it exists to produce. Review fix rounds now continue from the
  PR's current head regardless of who pushed it, and the
  "re-trigger the source issue to opt back in" escape hatch is removed with
  it. Know the trade this makes: a fix-round delivery force-updates the PR
  branch, so a human commit pushed onto a live sbxloop PR is now silently
  overwritten by the next fix round — that is exactly what the guard was
  catching, and nothing replaces it.

- **`sbxloop watch` shows the same bounded excerpt of a failed tool call
  that Discord threads do.** The line-selection half of the excerpt policy
  — the head+tail split, the per-line clip, and the `… N lines elided …`
  marker counted from the event's `output_lines` — moved into a shared
  `sbxloop.excerpt` module. The TUI previously kept its own ad-hoc
  last-N-lines tail, so a failure whose cause was near the top of its
  output showed only the trailing noise and gave no sign that anything had
  been dropped. There is now one copy of the truncation rules, documented
  in that module; only Discord's character caps remain layer-specific.

- **Run watches are persistent.** `watch_run` registrations are stored in
  `daemon_run_watches` (`run_id -> [requester ids]`) and reloaded when the
  daemon starts, so a watch registered before a restart or upgrade still
  pings the person who asked when the run finishes. The tool description,
  confirmation text and docs no longer warn that a restart forgets watches.

- **The per-task pipeline is three phases, not six: DECOMPOSE → BUILD →
  VERIFY.** PLAN and EXECUTE merged into one BUILD session that plans and
  does the work (narrating its approach first — the chronology's plan-card
  replacement, carried as a report excerpt on the build `phase.end`), and
  the SCRUTINIZE and VALIDATE critics are gone along with the
  `verify_suspect` protocol and the degraded-critic guard (#123's guard
  protected clean critic verdicts; with no JSON critics left there is
  nothing to guard — mechanical VERIFY fails loudly on its own, and the
  review lane's missing-verdict handling already leaves a broken review
  visibly un-reviewed). Rationale, measured on the `rews3ssdn` baseline:
  the critics rubber-stamped task completion (scrutinize 6/6 pass, validate
  5/5 accept) while the defect classes that actually leak to delivered PRs
  are diff-level and cross-cutting — invisible to a per-task completion
  audit by construction; meanwhile the non-executor stages cost ~35% of
  tokens and ~43% of wall clock, much of it fresh sessions re-deriving what
  the previous stage had already read. Adversarial review now lives solely
  in the daemon's post-delivery review lane, which sees the whole diff and
  drives bounded fix rounds. Consequences through the system:

  - `verify_commands` are **decomposer-authored only** (the plan-level set
    is gone): the agent that does the work never writes its own exam (#94),
    and the builder is shown the commands verbatim. The wrong-exam failure
    class formerly caught by `verify_suspect` is mitigated at authoring
    time — plan.md's workspace-root/`sh -c`/portability rules moved into
    decompose.md — and by the verify-exhaustion escape hatch, which now
    restarts BUILD in a fresh session (spending a replan) instead of
    re-planning.
  - **Egress is task-declared**: the decompose JSON gains per-task
    `egress`, bounds-checked at graph acceptance (one retry with the
    operator-bounds message) and granted at BUILD entry, same grant-late
    rationale as before.
  - Task states are `pending/executing/verifying/done/failed/skipped`;
    rows persisted by the six-phase pipeline are remapped at read time
    (`planning`→`executing`, `scrutinizing`/`validating`→`verifying`), so
    mid-flight runs resume across the upgrade with no data migration — and
    the new vocabulary is a strict subset of the old, so a rollback can
    still resume runs the new code wrote.
  - `steer_task` restarts the build session with the guidance (budget-free)
    rather than re-planning; `phase.plan`/`phase.verdict` events are gone
    (`phase.start` was dead code and went too), the Discord plan and
    verdict cards with them — the roster card, status line, builder prose,
    build report excerpts, and end-of-run summary (rework now counted from
    verify failures) remain the chronology.
  - A fix round's seeded task now carries the operator's
    `sandbox.gate_command` as its verify command when one is set
    (host-authored; with none set the PR's own CI stays the mechanical
    arbiter, which the acceptance gate already polls).
  - `budgets.max_tool_calls_per_phase` default 40 → 60 for the merged
    session; retune from per-phase usage data once the merged pipeline has
    field numbers.

### Removed

- **The per-phase system-message trimming flag under `[budgets]`, and the
  whole trimming code path behind it.** #386. The flag was added earlier in
  this same unreleased cycle on the premise that the ~22k tokens of fixed
  context riding every turn were ~62% of a run's input spend. The usage fields
  shipped alongside it now answer that: run `rrhb28j7n` shows
  `cache_read_tokens` is a *subset* of `input_tokens` — turn 0 writes ~20k and
  turn 1 reads exactly that back — and 86.5% of the run's input tokens
  (3,615,785 / 4,180,827) are cache reads (executor 91.7%, planner 83.3%,
  validator 82.4%, scrutinizer 79.1%, decomposer 68.5%). The static
  system-prompt prefix the flag trimmed is precisely the most-cached region,
  and the "62% of spend" premise was 62% of *tokens*, billed at cache-read
  rates. Left in place the flag was a trap for a future operator expecting a
  62% reduction, so the code path is gone with it: the per-phase drop-section
  table and the `PhaseRunner` method that consulted it, the job-request field
  that carried the section list to the worker, the SDK `customize` branch of
  `system_message_config` (now append-only) along with the SDK section
  vocabulary snapshot and its lookup helper, and the `sbxloop doctor`
  prompt-sections drift check. Prompt assembly is byte-identical to the flag's
  default-off behaviour, so no run changes. **Breaking config change:** the
  loader forbids unknown keys, so a config still setting that key under
  `[budgets]` is now rejected — delete the key.

### Changed

- **The PR review lane hunts defects adversarially, and its briefs stop
  pointing at tools that cannot work.** #437. `REVIEW_INSTRUCTIONS` now
  names the defect lenses that field-verified as leaking past the pipeline
  to outside reviewers — concurrency/locking (TOCTOU), failure ordering and
  partial writes, input validation at trust boundaries, and cross-module
  interaction ("walk every caller") — with an explicit "a green gate is
  necessary, not sufficient". The review charter sends the reviewer to
  `git diff origin/<base>...HEAD` instead of claiming `gh pr diff` works
  (the agent sandbox holds no GitHub credential), and a review-driven fix
  round now *quotes the standing objections into its brief*: the daemon
  fetches the change-requesting review bodies and inline comments through
  the github-ops sandbox (`pr_review_feedback`, latest verdict per reviewer,
  anchors preserved) rather than telling the fix agent to run
  `gh pr view --comments` in a sandbox where it cannot. Fix-round dispatch
  also stops nesting one brief's boilerplate inside another's: the persisted
  brief rides on the seeded task verbatim.

### Fixed

- **A downgraded review no longer parks its item forever.** GitHub refuses
  either verdict from an identity that is not an accepted reviewer and
  posts it as a non-gating `COMMENT` (422). The daemon recorded *that* as
  the item's verdict, but the verdict column exists precisely for the case
  where the review does not gate — so both of the gate's fallbacks,
  `verdict == "REQUEST_CHANGES"` (spend a fix round) and `== "APPROVE"`
  (land it), were permanently false on any repo that downgrades, which is
  every repo the loop reviews its own PRs on. The item sat in `reviewing`
  behind a debug-level "waiting on approval": no fix round, no merge, no
  notification. Field: `gh:424`, whose review requested changes with five
  inline comments on PR #480 and was never acted on. The requested verdict
  is stored now; `gates` already carried the downgrade. The same accepted-
  vs-requested mix-up also announced a downgraded *approval* as "review
  requested changes" — the opposite of what it said.

- **An inert `sh -c '...'` no longer costs a work item.** The verify lint
  rejected every nested shell, including single-quoted payloads with no `$`
  in them, which the outer shell passes through verbatim — they run exactly
  as they would unwrapped. Item `gh:478` was abandoned over two of them
  (`sh -c 'exit 0'`, `sh -c 'git diff --quiet && git diff --cached --quiet'`), and PR #476 went unreviewed as a result. That form is now
  *unwrapped and its payload linted*, so the wrapper still cannot hide a
  bare `pytest` or an `apt-get` from the rules that matter, while every
  shape that can actually misbehave — `bash -c` (may not be installed),
  `sh -lc` (rewrites the environment), and any double-quoted or unquoted
  payload (`$` eaten by the outer shell, field failure `r7ef26eht`) — is
  rejected exactly as before. The decompose prompt's ban is reworded to
  match: it read as forbidding only the double-quoted form.

- **"No reviewer" no longer stands in for "the review broke".** A PR whose
  review charter was filed and then died settles on green CI the same way a
  deployment with no reviewer does, but the acceptance line called both "no
  reviewer" — telling an operator their deployment has no reviewer when in
  fact its review had just failed (field: `gh:478`'s charter, then PR #476
  accepted). The gate now reports which of the two it was.

- **A run that dies inside a phase is closed when it settles.** The `runs`
  row is written only by the run loop, so a run killed mid-phase stayed
  `decomposing` until the stale sweep timed it out — six hours in the field
  (`rv2y1a8ke`, `rq826h546`), during which `list_runs` and every active-run
  count disagreed with reality. Both settles close it now; neither the
  retry (whose item drops its run pin) nor the abandon (whose item is
  terminal) could ever have resumed it.

- **The end-of-run summary card is back as the thread's last post.** The
  fix delivered for #420 (tool-call rendering) also deleted the summary
  card machinery shipped hours earlier — `RunStats`, `summary_text`,
  `summary_embed`, their bridge wiring and tests — without its outcome
  calling for it; the removal was unintentional and is restored here,
  adapted to the three-phase pipeline: the "needed work" ledger now counts
  verify failures (there are no critic verdicts to count), and "went well"
  reports tasks verified without revision.

- **A fix round can no longer force-update a PR branch a human has taken
  over.** #412. The daemon now baselines the branch head its own delivery
  produced (observed on the first poll after delivering; a re-delivery
  re-baselines) and, when a later poll sees a head it did not deliver,
  hands the item over instead of queueing a fix round: the item is
  abandoned out loud with the PR left open and theirs, and re-triggering
  the source issue opts the loop back in. Previously whoever pushed last
  won.

- **Verify commands may no longer reach for the network.** #440. A
  decomposer-authored `gh pr view … | grep -q .` failed a review task whose
  deliverable — a local file — was present and valid, because the sandbox's
  anonymous GitHub quota was exhausted. The verify-command lint now rejects
  `gh` outright and `curl`/`wget` against anything but an unambiguously
  local address (probing a server the command itself started stays legal),
  with the rule quoted in the retry feedback and stated in the decompose
  prompt: a verify command judges the workspace, not the network.

- **Review charters are filed once, stay abandoned, and never run against a
  PR that already merged.** #442. Filing now consults the existing
  `daemon_reviews` record: a charter that already exists — or existed, in
  any terminal state — for a delivery is not re-derived under a new issue
  number, so an operator's abandon is durable and green checks become the
  whole bar (the "no reviewer" precedent). And a charter whose PR merged or
  closed while it waited in the queue is settled at dispatch without
  spending an engine run — the field case burned three items and four runs
  auditing a PR that had already landed.

### Added

- **Per-phase usage columns on `phase_attempts`.** Every phase attempt row
  now bills the tokens and model turns its agent sessions actually spent:
  `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`,
  `cost`, and `turns`, populated from the worker's per-turn usage samples
  (`JobResult` gains a `turns` count) and accumulated across JSON retries and
  critic re-runs so a failed first attempt still bills to the phase it served.
  Mechanical phases (verify) record NULLs. Existing state databases gain the
  columns in place on open — the additive-migration mechanism previously
  covering only `runs` now covers any table. This is the instrument for
  before/after cost comparison of upcoming pipeline changes; until now
  per-phase spend attribution required folding `agent.usage` events by
  persona.

- **`pr_status(number)`, a concierge host tool for how a delivered PR is
  doing.** #333. "How is PR #41 doing?" now gets an answer without a
  browser: the CI check runs summarised as counts with each failing check
  named and linked, the review decision and who reviewed, whether GitHub
  calls the PR mergeable, and whether the branch is behind its base. It is
  served through the github-ops box with `GithubOps.raw`
  (`GET /repos/{repo}/pulls/{n}`, `/commits/{sha}/check-runs`,
  `/pulls/{n}/reviews`), the output is clipped like every other tool
  result, and a PR that does not exist is answered plainly. **Read-only:
  there is no merge or write path — the concierge cannot merge from chat.**

### Changed

- **Run threads now show what a tool call ran and what it produced.** A bash
  call used to render as two near-identical lines — `agent.tool_start` and
  `agent.tool_end` — each showing an ellipsised command whose only surviving
  text was the long, identical run path, and neither carrying the outcome, so
  a watcher could see that `ruff` and `mypy` ran for 95 seconds without
  learning whether they passed. Now:

  - **Commands truncate informatively.** The new `sbxloop.cli.cmdfmt` collapses
    the boilerplate `cd <absolute run path> &&` prefix to `cd $RUN &&` and, if
    the line still exceeds `COMMAND_DISPLAY_CLIP` (160 chars, a named default),
    elides the *longest argument tokens* rather than the middle of the whole
    string — the leading verb always survives and every shortened token carries
    a literal `…`, so nothing is ever cut mid-token without a marker. Applied
    to the Discord chronology, the TUI and `sbxloop logs` alike.
  - **One thread entry per call.** `ToolBatcher` records a start as pending
    (emitting nothing) and writes a single line on completion — `$ bash  cd $RUN && uv run mypy  ✓ 1.5s` / `✗ exit 1 · 1.5s`. Pairing is strictly by
    `tool_call_id`, never by comparing command text, so concurrent calls
    finishing out of order still carry their own command; an unmatched end
    renders from its own args. A call still in flight survives routine
    flushes untouched (its one line lands on completion) and only the
    run-end flush renders leftovers as `… running`, so no call is ever
    printed twice.
  - **Results reach the thread, bounded.** A completed call gets a ✓/✗ header
    with its exit status and a fenced head+tail excerpt of its output, with
    elision marked `… N lines elided …` counted from the new `output_lines`.
    Failures get the larger budget and prefer stderr; successes are quiet by
    default. Caps are named constants (`TOOL_OUTPUT_LINES_DEFAULT` 0,
    `TOOL_FAIL_OUTPUT_LINES_DEFAULT` 20, `TOOL_EXCERPT_LINE_CLIP` 300,
    `TOOL_EXCERPT_MAX_CHARS` 1200), the two line budgets are configurable as
    `[discord] tool_output_lines` / `tool_fail_output_lines`, and the rendered
    message is clamped to `DISCORD_MAX_MESSAGE` so nothing can overflow
    Discord. The worker-side excerpt likewise keeps the first and last 20 lines
    instead of a blind 1000-char tail, so the *start* of a failure survives.
  - **Redaction is guaranteed at both ends.** Because this publishes more of
    what a command printed, the worker redacts output and error text before
    emission (`sbxloop_worker.secrets.redact_secrets`) and the renderer scrubs
    every command and excerpt again with the new, idempotent
    `sbxloop.log.redact_text` before it can reach a thread.
  - **The protocol change is additive only.** `agent.tool_end` gained optional
    `output_lines` and `duration_ms` (measured from the matching start);
    nothing was removed or renamed, older workers simply omit them, and
    consumers tolerate their absence — so `run_events`, `sbxloop logs`, the log
    sink and checkpoint/resume all keep working and **existing chronologies
    need no migration**. Truncation and excerpting are display-only: the stored
    event keeps the full command and output. See `docs/worker-protocol.md` and
    `docs/architecture.md`.

- **Cost is no longer summed across turns.** #386. The Copilot SDK's
  `AssistantUsageData.cost` is a per-turn *constant* (15.0 on every turn of
  every session), not a delta, so folding it through `Usage.merged` turned a
  147-turn run into `cost: 2205.0000` — a fabricated figure the concierge
  would repeat in Discord as fact. The host no longer lifts `cost` out of
  `agent.usage` events, `Usage.merged` carries it last-wins rather than
  additively, and `_cost_line` renders a figure only when it is strictly
  positive; `run_usage` and `usage_today` say "cost: not reported by the agent
  backend" instead. The field's *unit is unknown*: a value identical on every
  turn of every session is far more likely a premium-request multiplier or a
  quota unit than a currency amount, so it must never be rendered as a
  currency figure until the unit is established.

- **The acceptance loop now runs its gates cheapest-first, and a fix round is
  one task.** #399 shipped the gate; this makes it efficient enough to run.

  - **CI decides before a review is spent.** A review was filed the moment a
    PR was delivered, before the build had reported — so every red PR burned a
    whole review run on work that had to change anyway. The order is now:
    checks pending → wait (free); checks red → fix round, no review;
    green and unreviewed → file the review; green and satisfied → accept.
  - **A fix round is a single seeded task, not a decomposition.**
    `LoopEngine.start(tasks=...)` pre-seeds the graph and skips DECOMPOSE. A
    normal run is ~270 turns across ~5 phases per task; a round addressing
    "`mdformat` failed" is already decomposed, because the failures *are* the
    acceptance criteria. Its brief names them and tells the executor not to
    redo the work already on the branch.
  - **A round can be re-reviewed.** GitHub keeps a `CHANGES_REQUESTED` review
    standing until the reviewer says otherwise — new commits do not clear it —
    so the old once-per-run guard meant the loop could only ever run one
    direction. It is now one review *in flight* at a time.
  - **`[daemon] reviews_per_day` is removed.** Once reviews and fixes are
    runs, they already count against `max_runs_per_day`, the real ceiling. A
    second global counter for one lane starved other work for the wrong
    reason: a stubborn PR eating today's reviews says nothing about whether
    the next delivery deserves one. `review_rounds` (now 3, was 20) bounds any
    single PR so one item cannot eat the day — it counts rounds, which are
    runs, not polls.

  The acceptance record moved from the run to the **item**, which is what it
  always belonged to: the PR belongs to the work item, and every fix round is
  a new run against the same PR.

  Expected cost per accepted PR: 2 runs on the happy path (deliver, review),
  4 with one round, ~8 at the cap — against the previous shape, where a red PR
  burned a review before anyone looked at the build.

  **A fix round runs on its pull request's own branch.**
  `hostgit.clone_existing_branch` checks the run's workspace out at
  `origin/<branch>` and the delivery targets that same branch, so the PR is
  updated rather than a second one opened. A branch that is not on the remote
  **fails provisioning** rather than falling back: a round starting from the
  default branch would deliver a tree that never contained the PR's work, and
  force-updating the branch with it destroys that work. A failed provision is
  recoverable; that is not.

  Cloning a clone copies the source's *local* branches into `origin/*`, not
  its remote-tracking refs — and a PR's branch is only ever remote-tracking in
  the daemon's checkout, which fetched it but never checked it out. The clone
  therefore asks the source for that exact ref rather than mutating it.

### Added

- **A delivered item is not done until its PR is accepted.** `_settle` used to
  call `mark_done` the moment a run succeeded and a PR existed. Nothing looked
  at CI: PR #389 was settled as done with `mdformat` and `security` failing,
  and the review that followed cut five style issues without noticing the
  build was red.

  A patch item that delivers a PR *and* has a review queued for it now enters
  a new non-terminal state, `reviewing`. Its source issue stays open. Each
  tick polls the PR and settles it only when the checks are green **and** the
  review is satisfied — `pending` is deliberately not `green`, since reading
  "no failures yet" as success is the original bug.

  The polling runs before the pause / breaker / daily-cap gates, on purpose:
  it spends GitHub reads rather than engine wall clock, and a PR that went
  green while the daemon was paused should settle when it resumes rather than
  burn its budget waiting.

  **Nothing waits forever.** `[daemon] review_rounds` (default 20) bounds how
  many polls may find the PR unsatisfied; past it the item is abandoned with
  the PR named in the notice and in `last_error`, and a human takes over. The
  wall-clock budget is roughly `review_rounds × poll_interval_s`. `[daemon] await_review` turns the whole gate off.

  Two cases that would otherwise strand an item are handled explicitly: a
  review that could only be posted as a `COMMENT` (the identity is not an
  accepted reviewer) never yields an approval, so the gate waits on checks
  and a *non-blocking* review rather than on an approval nobody can give; and
  an item whose review record is missing is accepted rather than left waiting
  on nothing.

  The gate is tied to a review actually being queued. No reviewer means no
  verdict to converge on, and holding an item open for one that will never
  arrive is how a queue silently stops.

### Changed

- **A review of a delivered PR now lands on that PR, not in the issue
  tracker.** The loop reviewed its own work by filing issues: a charter issue
  per delivered PR, and then one backlog issue per finding. In the field that
  produced `PR #389 → #391 → #392, #393, #394, #395, #396` and
  `PR #375 → #378 → #379 to #383` (two of those duplicates of each other) —
  every one of them feedback about a diff, filed where the diff is not, with
  nothing to converge on and a human left to triage the pile.

  The review run now writes its verdict to `.sbxloop/review.json` — the same
  shape the backlog lane already uses, and for the same reason: the agent
  sandbox holds no `GH_TOKEN`, so the daemon posts it afterwards through the
  github-ops sandbox. The verdict is the GitHub one: `REQUEST_CHANGES` or
  `APPROVE`, with findings as inline comments anchored to the lines they are
  about. A review item files no backlog issues at all — a reviewer with an
  issue-shaped outlet will use it, which is the behaviour being replaced.
  Anything genuinely out of scope goes in the review body as prose for a
  human to decide on.

  The parse is defensive, because the JSON is agent-authored, and its most
  important property is what it does *not* do: an unparseable or verdict-less
  review posts **nothing** rather than defaulting to an approval. Leaving a PR
  visibly un-reviewed is honest; waving work through is not. One malformed
  inline comment costs that comment, not the review, and comment overflow past
  the cap is counted in the body rather than dropped silently.

  The ordinary backlog lane is untouched: an item with no recorded review
  target files exactly as before.

### Added

- **The concierge can read the daemon's recent log lines in chat.** "Why is
  nothing running?" used to be answerable only over ssh — the state store
  knows what a run is doing, but says nothing about `sbx` invocations, GitHub
  polls, breaker state changes or worker errors, all of which live in the
  journal. `configure_logging` now installs a bounded in-process ring buffer
  handler (a `deque` of the last 2000 already-rendered, already-redacted
  lines: no locks, no I/O, no unbounded growth in a long-lived daemon), and
  the new `daemon_log(tail, level, grep)` host tool reads it. `level` filters
  at or above a threshold, `grep` is a plain case-insensitive substring rather
  than a regex, and the output is clipped to `[concierge] max_tool_result_chars` like every other tool result. It is the journal
  without ssh, not a replacement for it: lines older than the buffer, or from
  a previous daemon process, still need `journalctl`.

- **A run must prove the project's own gate before it delivers.** PR #389 was
  delivered green-looking and sat red: `mdformat` and `security` failing, both
  plain `make check` targets. The plan's verify commands were a *subset* of
  what the repository enforces, so nothing in the run ran what CI runs, and
  `_settle` marked the item done anyway. The review that followed cut five
  style issues and never mentioned the build was broken.

  When a project declares a gate — a `check` target in the makefile GNU make
  would actually read — the task graph must run it. Enforced in
  `verifylint.lint_verify_commands` at JSON acceptance, so a decomposition
  that skips it costs one retry with the rule quoted rather than a PR, a
  review round and a human noticing.

  Required **of the graph, not of each task**: demanding it per task would run
  a multi-minute check once per task for no extra signal, and decompositions
  already tend to end with an "everything green" task, which is where it
  belongs. A project that declares no gate has none invented for it — a
  requirement the executor cannot satisfy is worse than no requirement, since
  it cannot edit verify commands.

- **A pull-request review capability** (`GithubOps.pr_review_create`,
  `pr_review_state`, `pr_checks`, `pr_get`). These go through the existing
  `raw.api` escape hatch rather than new worker ops — the reviews and
  check-runs endpoints need no parameter shaping the generic transport does
  not already do — with typed methods keeping the untyped hatch confined to
  one layer.

  The folds carry the judgement and are pure over payloads: a check run with
  no conclusion yet is **pending, not green** (reading "no failures so far" as
  success is how a red PR gets settled as done); a conclusion nobody
  recognises **fails closed**; `neutral` and `skipped` are not failures; a
  repository with no CI reads green rather than deadlocking the loop. For
  reviews, only each reviewer's *latest* verdict counts, `COMMENT` reviews
  never change it, and a dismissed one stops standing.

  `pr_review_create` returns the event GitHub **actually accepted**, not the
  one requested: an identity the repository will not accept as a reviewer has
  `REQUEST_CHANGES` refused, and the feedback is re-posted as a `COMMENT`
  rather than lost. A `COMMENT` gates nothing, so callers must read the
  returned event — an acceptance loop that assumed otherwise would wait
  forever for an approval nobody was asked to give.

  Nothing calls the review methods yet; the loop change that uses them lands
  separately.

### Fixed

- **A revision no longer re-derives what its own previous attempt already
  established.** Field failure (run `rrhb28j7n`, task t5): five executor
  sessions on a single task, each with its own session id, each running
  `uv sync --all-packages` and the whole lint gate from scratch, each
  concluding "no changes needed" — in nearly the same words. A revision was
  told what the critic objected to and nothing about what the last attempt had
  found, so it went and found it again.

  Two things were unwired, and they compounded. `task.session_id` was captured
  and persisted but never passed back as `resume_session_id`, so every attempt
  opened a cold session. And the previous attempt's own report — already
  committed to `phase_attempts` — was never handed to the next one; the
  execute prompt carried the plan, the critic's feedback and standing guidance,
  but nothing about the work already done.

  EXECUTE now resumes the previous attempt's session where the SDK still has
  it, and receives that attempt's report either way. The report is the
  load-bearing half: it survives a resumed run, a re-provisioned sandbox and an
  expired session, all of which kill the session id. A replan clears the
  session deliberately (`_discard_plan`) — a revision continues the same plan,
  but a replan threw the approach away and a resumed session would carry the
  discarded one forward as though it were still the intent. Only session ids
  created by this process are resumable, since sandboxes are cattle and a
  persisted id from a previous incarnation names a VM that no longer exists.

  **The critics do not resume, by design.** A reviewer that inherited the
  executor's session would inherit its conclusions, and that independence is
  the loop's integrity check. A resume the SDK cannot honour falls back to a
  fresh session rather than failing the job; the host notices, because the id
  that comes back differs from the one it asked for (`phase.resume_missed`).

### Fixed

- **`ctl` no longer reports a starting daemon as an absent one** — which cost
  the 0.7.23 deploy a rollback of a perfectly healthy release. The control
  server starts only *after* `loop.recover()` (deliberately: an `abandon` or
  `requeue` served while recovery is still settling the item it snapshotted
  would be overwritten by recovery's own verdict), so every request submitted
  in between is swept as "submitted before the daemon started". That window is
  as long as recovery takes — 67s on the failed deploy, which had four orphaned
  runs to reconcile and a concierge sandbox to re-provision, and orphaned runs
  are exactly what a restart creates. The deploy's health check submitted 56s
  before recovery finished, read the refusal as "the daemon never came up", and
  rolled back; the daemon reached `daemon.started` one millisecond after
  refusing it.

  A longer timeout could not have helped: the request is *answered*, not left
  pending, so any budget failed the same way. `ControlClient` now resends —
  with a fresh stamp — for as long as the caller's budget lasts, which is what
  the refusal's own text ("resend it") always asked for and nothing did. The
  stale verdict rides on the reply structurally (`CommandReply.stale`) rather
  than being matched out of its prose, and a reply from a daemon predating the
  field reads as a plain refusal rather than sending the client into a loop.
  The guarantee is unchanged: the guard exists to stop a command of *unknown
  age* firing at boot, and a client still sitting in `submit()` is by
  definition current. A daemon that never starts still fails, at the deadline.
  The deploy's own `ctl status` budget goes 60s → 300s to cover a slow
  recovery.

### Added

- **Run cost is governed by turns, not jobs** — and four changes act on that.
  Field measurement of run `rews3ssdn` (7 tasks, 272 assistant turns across 25
  jobs) put **~22,000 tokens of fixed context on every turn**, about 62% of the
  run's input spend, against a phase prompt template of ~1,900 tokens; wall
  clock tracked the same turn count at ~10s/turn (55 minutes). The run itself
  was not thrashing — 1 revision and 0 replans, with scrutinize passing 6/6 and
  validate accepting 5/5 — so the spend was baseline session cost, not rework.
  Batching phases into fewer, longer sessions would have moved turns between
  columns without removing any, so the per-task phase structure is unchanged.

  - **Usage reporting reads the fields the SDK was already sending.** The
    Copilot backend mapped only `model`/`input_tokens`/`output_tokens` off
    `AssistantUsageData` and discarded `cost`, `cache_read_tokens`,
    `cache_write_tokens` and the tool-schema count. Everything downstream
    already carried them, so `run_usage` was reporting cost as "not reported by
    the agent backend" while the backend reported it on every turn — and how
    much of that 22k/turn is served from cache was unknowable. The mapping is
    now a pure `usage_from_sdk_sample` (unit-tested despite the module being
    coverage-omitted, like `read_only_denial` before it), and `run_usage` breaks
    spend down by turns and jobs per persona.
  - **`[budgets] max_parallel_tasks`** (default 1) runs independent tasks
    concurrently. The task DAG already knew which tasks were independent; the
    loop walked them one at a time regardless. Readiness is now evaluated
    explicitly — a task starts once every dependency has *finished* — rather
    than being implied by position, and a lane that dies stops new launches but
    lets its siblings checkpoint before the error propagates, which is what
    keeps the run resumable. **Raising it above 1 is an informed choice:**
    concurrent tasks share one agent sandbox and one workspace, and
    `depends_on` is agent-authored, so nothing guarantees two "independent"
    tasks touch disjoint files. Safe today only for outcomes whose tasks are
    genuinely file-disjoint; per-task workspace isolation is what would make it
    unconditional.
  - **SCRUTINIZE is handed the diff instead of sent to find it.** Evidence was
    `git diff --stat` clipped to 1,500 characters while the prompt told the
    critic to verify claims itself — an instruction to spend tool calls at
    21.9s/turn, the most expensive turns in the run. It now receives
    `git diff HEAD` (staged work included) clipped head-and-tail at 20,000
    characters, and the prompt directs it to judge from the patch.

- Concierge: **`run_usage`** and **`usage_today`** put Copilot spend in chat
  (#334). The daily run cap was visible in `!sbx status` but token usage was
  not visible anywhere — the worker has always emitted `agent.usage` events and
  nothing read them. `run_usage` folds one run's samples into a per-persona
  breakdown and a total; `usage_today` totals the rolling 24 hours next to
  `runs_today/max_runs_per_day`. Tokens are attributed to when they were spent,
  not to the day the run started, so a run spanning midnight counts on both
  days. Nothing is invented: a run with no samples answers "not recorded"
  rather than zero — `Usage.merged` keeps None as None precisely so those stay
  distinguishable — and cost is shown only when the backend actually reports it
  (see the usage-field fix below, which is what made it report).

- **Automated deploys to the daemon host** (`.github/workflows/deploy.yml`).
  Every merge to `main` already auto-released to PyPI, but getting that release
  onto the running daemon was manual, so a host silently drifted behind its own
  releases (#331). The new workflow chains off `Release` on a self-hosted runner
  and does the last mile: pause the daemon and wait for the in-flight run to
  finish (20 min cap, 15s poll floor — `status()` mutates the breaker, #309),
  `pip install` the exact released version, `systemctl --user restart`, then
  health-check it (unit active, `--version` matches, `sbxloop doctor`, `ctl status`, and a 45s settle against crash loops). A failed check rolls back to
  the version that was running. The operator's pause state is recorded up front
  and re-applied afterwards — required, because pause is in-memory only (#308)
  and a restart otherwise resumes autonomous dispatch silently. Deploy start and
  outcome are posted to the Discord control channel. See `docs/deploy.md`.

- `contrib/systemd/github-runner.service` and `contrib/systemd/sbx-sandboxd.service`.
  The runner is a **user** unit, not GitHub's `svc.sh` system unit: a system
  service has no `XDG_RUNTIME_DIR`/`DBUS_SESSION_BUS_ADDRESS`, so
  `systemctl --user restart sbxloop-daemon` fails from one. `sbx-sandboxd`
  supervises the sandbox backend, which was previously a bare process nothing
  would restart; `sbxloop-daemon.service` now `Requires=` and is ordered after it.

- Concierge: **`version_status`** answers "are we up to date?" — the
  installed `sbxloop`, `sbxloop-worker` and `sbx` versions against the
  latest releases on PyPI, with a `pip install --upgrade` hint when the host
  is behind. Every merge to `main` auto-releases a patch while deploying to
  a daemon host was manual, so the daemon now also posts one drift line to
  the control channel at startup when it is behind — a tool only helps
  whoever thinks to ask. Read-only and always available (no GitHub, no new
  config knob); it reports drift rather than acting on it. The PyPI lookup is
  the host's only outbound HTTP besides Discord: unauthenticated, bounded by
  a 4s timeout and a response cap, memoised for five minutes, and degrading
  to "could not reach PyPI" rather than failing a turn. A `.devN` build or a
  `0.0.0` never-built tree is reported as such instead of being compared —
  hatch-vcs names a dev build for the version it is heading *toward*, so
  `0.7.12.dev0` is not the released `0.7.12`.

- Concierge: **`comment_on_issue`** and **`close_issue`** finish triage from
  chat. The first posts a comment on an issue, attributed to the Discord
  user in a trailer like `create_issue`; the second closes one as
  `completed` or `not_planned`, optionally posting the reason as a comment
  first, and removes the `trigger_label` so a reopen does not silently
  re-queue a run. A close is the one concierge action that is **not**
  direct: the prompt requires an explicit yes naming the issue and the tool
  requires a `confirmation` argument quoting what the person said, logged
  with the close. The issue is read first, so a pull-request number, an
  already-closed issue and one a run is working right now are refused
  without writing anything, and the result names the title, the reason and
  the url. Closing does not dequeue the daemon's own `gh:<n>` item, so the
  result says whether that item was already claimed (a run can still start
  — abandon it) or not (the loop re-checks and drops it). Gated by the same
  `[concierge] create_issues` knob as the rest of the issue surface.

- Concierge: **`list_issues`** lists the repo's open issues — by default
  the `backlog_label` ones (the triage backlog), `all=true` for every open
  issue, `label=` to narrow — with labels, age, author, comments and url,
  flagging those already queued (`trigger_label`) or running; the prompt
  then has it ask which, if any, should be worked, labelling only the ones
  named.

- Concierge: **`create_issue`** files a feature/bug the person described as
  an issue in the configured repo, created with the `[daemon] backlog_label`
  (triage) and attributed to the Discord user; the prompt then has it ask
  whether to add the `trigger_label`, and **`label_issue_for_run`** does so
  only after an explicit yes. `[concierge] create_issues` (default on,
  needs `github_tools` and `[github] repo`) gates both.

- Worker protocol: **host tools** for `agent.session` jobs. `JobRequest`
  gains `host_tools` (name, description, JSON-schema `parameters`),
  `host_tools_dir`, `host_tool_timeout_s` and `available_tools`; the worker
  registers each host tool as a custom SDK tool and relays every call to
  the host as an `agent.tool_request` event, then waits for the host's
  `HostToolResponse` file at `<host_tools_dir>/<call_id>.json`
  (`sbxloop_worker.hosttools`, `agent.tool_response` on completion). This
  is the transport the daemon's Discord concierge uses to let an in-sandbox
  session drive the daemon. The echo backend scripts `host_tool_calls`, so
  the round trip is testable without the SDK. See
  `docs/worker-protocol.md`, "Host tools".

- Host side of the same round trip: `WorkerClient.submit(job, tool_handler=…)` answers a job's host-tool requests through a
  `HostToolBroker` (`sbxloop.worker.hosttools`). The handler runs on a
  small host thread pool, never on the thread draining the event stream,
  and its `HostToolResponse` is `sbx cp`'d into
  `~/.sbxloop/tools/<job_id>/<call_id>.json`; handler exceptions become
  `ok=false` answers the model can read. `WorkerClient.verify_installed()`
  is the cheap "is a matching worker already in this sandbox?" probe for
  reusing a long-lived sandbox.

- **Discord concierge** core — the control channel's agent
  (`sbxloop.daemon.concierge.Concierge`, prompt
  `engine/prompts/concierge.md`). It runs as a Copilot session in the
  daemon's long-lived agent sandbox and reaches the daemon only through
  host tools; this PR ships the service (one turn at a time; the SDK
  session resumed message after message with its id in `daemon_state`,
  rotated after `session_turns`; a dead sandbox or lost session costs one
  retry; timeouts and a missing `COPILOT_GITHUB_TOKEN` become actionable
  replies) with its first two tools: `sbx_control` (every `!sbx` verb via
  the same `control.dispatch`, attributed `… (via concierge)`) and
  `enqueue_work` (a pending inbox item), plus the inspection tools
  `list_runs` / `run_detail` / `run_events` / `item_detail` (state-store
  reads: outcome, tasks, tracking issue / PR / delivery error, guidance,
  the run's Discord thread) and `github_get` (PR / files / diff / issue /
  comments / file reads through the github-ops sandbox, configured repo
  only, on when `[concierge] github_tools`).

- Discord: **@mention the bot (or reply to it) in the control channel to
  talk to the concierge.** Routing is a pure function
  (`sbxloop.daemon.discord_routing.route_message`: command / concierge /
  steer / ignore); the reply is threaded under the question, split at
  paragraph and fence boundaries, with ⏳ → ✅/⚠ reactions and one edited
  `🛠 concierge: sbx_control(status) · run_detail(r7…)` audit line naming
  every tool used. `sbxloop daemon` wires it when `[discord]` and
  `[concierge]` are enabled (not with `--once`), warms the sandbox up in
  the background, and `sbxloop doctor` grows a "discord concierge" row.

- Foundations for the Discord concierge (the control channel's agent,
  landing in follow-up PRs): `[concierge]` config (`ConciergeConfig`, in
  the `sbxloop init` template), `DaemonStore.get_value` / `set_value` on
  the generic `daemon_state` table and `item_for_run`, `InboxSource.enqueue`
  (queue a work item on a human's behalf), `DaemonLoop.report_for`.

- `Provisioner.ensure_agent_only` / `agent_only_spec`: one agent-role
  sandbox (Copilot token, no `GH_TOKEN`, prompt-advertised baseline allows)
  outside a run's pair, sharing `ensure_github_only`'s fail-fast/rollback
  path. `sbxloop.daemon.agentbox.DaemonAgent` wraps it as the daemon's
  long-lived **concierge sandbox** (`sbxloop-concierge-<state-dir digest>`):
  provisioned lazily, dropped and re-provisioned on failure at most once
  per five minutes, and — unlike the github-ops box — **reused across
  daemon restarts** when `verify_installed()` still matches this host (the
  SDK's session store, i.e. the concierge's memory, lives inside the VM).
  `sbxloop sandbox prune` reports both daemon-owned families and never
  touches them; `sbxloop sandbox rm` removes them explicitly.

- The daemon's log is structured ([structlog](https://www.structlog.org/)
  routed through the standard library, `sbxloop.log`) and configurable:
  `--log-level` / `[daemon] log_level` / `SBXLOOP_DAEMON__LOG_LEVEL`
  (default `INFO`) and `--log-format console|json` (`[daemon] log_format`).
  Third-party loggers (discord.py, httpx) are held at `WARNING` unless
  `DEBUG` is asked for. Every run's event stream is mirrored into the log
  under the `sbxloop.run` logger (`sbxloop.daemon.logsink`): lifecycle at
  `INFO`, worker/tooling failures at `WARNING`, tool calls at `DEBUG` —
  a daemon without Discord no longer goes silent between claim and settle.
  New messages cover what the journal could not answer before: a
  `daemon.starting` config summary (state dir and why, sources, guardrails,
  Discord on/off), `run.dispatch` / `run.finished` with duration,
  `run.interrupted` at shutdown, why the daemon is idle whenever that
  changes (paused, breaker, backoff with queue depth, daily cap), claims and
  lost claim races, GitHub polls, every operator command (`operator.command`
  with `by=`/`via=`), Discord steers, the github-ops sandbox's lazy
  provisioning with duration, `sbx` subprocess invocations (redacted argv,
  rc, duration; slow calls at `INFO`), worker install/job submit/timeouts/
  kills, Copilot phase calls with duration and token usage, delivery steps
  and PR URL, and store transitions at `DEBUG`. Fields carry correlation ids
  (`run=`, `item=`, `job=`, `sandbox=`); a `redact_secrets` processor masks
  credential-named fields.

### Changed

- Daemon: the **run cap is now a wall-clock calendar-day gate**, not a
  trailing 24-hour rolling window. Dispatch counts the runs whose start
  time falls within the current day in the new `[daemon] run_cap_timezone`
  (any IANA zone, default `UTC`, validated at config load) and the count
  resets at 00:00 in that zone, i.e. the cap is counted per calendar day —
  so a run started just before midnight no longer frees a slot 24 hours
  later, it frees it at the boundary.
  `max_runs_per_day` keeps its name and its default of 12, so existing
  configs need no migration; the per-day review and post-mortem caps share
  the same day window. Status lines, logs and Discord/GitHub messaging now
  state the semantics unambiguously
  (`runs today (UTC): 7/10, resets at 00:00 UTC`). The concierge's
  `usage_today` totals that same calendar day rather than a trailing 24
  hours, so the spend and the cap on its head line still describe one
  period (`today (UTC) · 3 run(s) with usage · 7/10 runs today`).

- **What the structured phases decided is now an event**, so it can be shown
  without showing the agent's JSON: `run.tasks` (the roster, re-announced on
  resume with each task's persisted state), `phase.plan` (steps, expected
  artifacts, verify commands, egress grants) and `phase.verdict` (a critic's
  call, its issues by severity, and the feedback the executor is about to be
  told). All three carry only what the agent's reply already said — the reply
  was simply the sole carrier, so dropping it dropped the plan and the critic's
  reasoning with it. Discord renders each as a card; the daemon log mirrors
  them at `INFO`.

- Discord: an agent's **JSON payload no longer reaches the channel**. Every
  structured phase — decompose, plan, scrutinize, validate, steer — asks its
  agent for one fenced JSON block and gets narration around it, and the bridge
  posted both: the block, and the rendering of what the engine parsed out of it
  (the status line, the phase lines, the steering reply, the report card). The
  same facts twice, one of them in the shape a human reads least, split across
  several messages when the plan was large. Agent messages now arrive as their
  narration only; a reply that was payload only posts nothing at all. Detection
  mirrors `sbxloop_worker._json.extract_json` — fenced blocks tagged `json` or
  simply parsing as one, then a bare document running to the end of the reply —
  so an unfenced payload and a `bash` block the agent is talking about are told
  apart. Nothing is lost: what the payload decided is posted as its own card
  (above), and the block itself is still in the run's event store
  (`sbxloop logs`) and the phase ledger.

- Concierge: the `sbx_control` tool no longer appends the raw status dict as
  JSON to its own reply text. A JSON blob in a tool result is a JSON blob the
  model may paste into Discord, next to the same numbers it just wrote in
  words; the two fields the text did not spell out (current work item,
  consecutive failures) follow it as prose instead. The prompt's style rules
  now say it outright: answer in prose, never in raw JSON.

- Discord: steering a run now takes an **@mention of the bot in that run's
  thread** (or a reply to one of its messages there), the same rule the
  control channel already used. Previously *every* message in a run thread
  was relayed to the agent as steering, so watching a run and talking about
  it in its own thread repeatedly paused and re-planned the run. The bot's
  mention token is stripped before the text is relayed; plain messages in a
  thread are ignored in silence, and a bare mention does nothing. `!sbx <verb>` now also works inside a run thread, answered where it was typed.
  The bot listens on exactly two surfaces — the control channel and threads
  it opened itself; a DM or an unrelated channel is ignored outright, mention
  or not.

- Discord: a plain (non-command, non-mention) message in the control
  channel is now **ignored** — the canned "type in the run's thread to
  steer" reply is gone; people can talk among themselves, and the concierge
  explains where steering happens when asked. `!sbx <unknown verb>` now
  also suggests @mentioning the bot.

- Daemon-path operator narration moved from Rich `console.print` on stdout
  to the log (validation errors, the state-dir line, "daemon interrupted"),
  so journald has one stream in one format; `--dry-run`'s candidate listing
  and `--once`'s `tick:` line stay on stdout (they are the command's output)
  and are logged as well. Several Discord failures that were logged at `DEBUG`
  (dropped work, digest/status/headline edits, embed fallback) are now
  `WARNING`; the gateway connect failure and an unreachable channel are
  `ERROR`; `run.delivery_failed`, `run.abandoned` and `breaker.opened` are
  `ERROR`. Log messages are event names with fields (`github.claim_failed item=gh:12 …`) rather than prose.

- The audit lane's Discord messages follow the bridge's formatting pattern
  instead of ad hoc strings: filed refs render as masked links
  (`gh:12` → `[#12](https://github.com/<repo>/issues/12)`, upstream
  `owner/name#5` likewise) via shared builders in
  `sbxloop.daemon.discord_format` (`filed_notice`, `findings_summary`,
  `filed_lines`, `charter_skipped_notice`, `ref_link`); the scheduled-audit,
  delivery-review and post-mortem notices name the item so the bridge threads
  them, and use `·`-separated fields rather than `:`/`→`; the finish card (and its
  text fallback) gains `Filed` / `Upstream` / `Noted` fields, so an audit's
  deliverable is on the card next to the PR (`no findings` when clean); the
  `✅ … done` notice lists what the run filed and the separate
  `filed N backlog item(s)` notice is gone; `!sbx queue` marks audits like
  `!sbx items` does; a broken charter's notice says where to fix it.

### Fixed

- **Orphaned runs no longer sit in `running` forever** (#374). The run row was
  only ever written by the in-process run loop, so a cancelled work item — or a
  daemon that died mid-run — left the run stuck in `running`/`decomposing`
  while `!sbx status` reported `current: null`; anything counting active runs
  (guardrails, resume logic, cost reporting) was misled by phantoms. Two sweeps
  now close them, both *appending* a `run.reconciled` chronology event rather
  than rewriting history: daemon startup reconciles every non-terminal run that
  is neither executing in this process nor pinned for resume, and every tick —
  including while paused — reconciles runs idle longer than
  `[daemon] run_stale_after_s` (default 6h; `0` disables) while nothing is in
  flight. A run whose item was cancelled becomes `cancelled` with reason
  `work item cancelled` and the operator attribution preserved; anything else
  becomes `failed` with `orphaned: daemon restarted while run was in flight`.
  Cancellation itself now transitions the run record in the same path as the
  work item, so the two cannot diverge, and the recorded reason is rendered
  next to the state in `sbxloop status` / `list_runs` output, in the run detail
  view and in the TUI header.

- Sandbox secrets: a proxy **sentinel** is no longer mistaken for a delivered
  credential. sbx's secret proxy exports `sbx-cs-…` in place of the value and
  swaps the real one in on the way out — which works for anything that just
  puts it in a header, and not at all for a client that inspects it. The
  provisioner's visibility probe was `test -n`, so when sbx began exporting the
  sentinel into `sbx exec` login shells the probe read "visible", skipped the
  in-VM env-file fallback, and handed the agent a token-shaped hole: every
  Copilot session died with `401 Requires authentication`, the SDK validating
  the format client-side. The probe now asks what the consumer asks — does this
  look like a credential — and treats a sentinel exactly like an absent one,
  recording a distinct `sentinel-under-exec` verdict. Two follow-on holes are
  closed with it: the worker's `apply_env_file` used `os.environ.setdefault`,
  so an injected sentinel beat the real token the fallback had just written and
  the fallback silently did nothing; and the conformance probe now sets a
  token-shaped value so it can tell the three answers apart, with `expected`
  relaxed to None because provisioning handles all of them. Field failure on
  the daemon host, 2026-08-21.

- Deploy: install the wheels attached to the GitHub Release instead of pulling
  from PyPI at all. The simple index is Fastly-cached with `max-age=600`, so for
  up to ten minutes after a release pip can still be served an index page that
  predates it. Two consecutive deploys died on this — v0.7.17 on `sbxloop`, then
  v0.7.18 on `sbxloop-worker`, a separate project whose index page is cached
  independently, which is why the first attempt at a fix (waiting on the host
  package's index entry) did not hold. `Release` attaches the same `dist/` it
  publishes, so the wheels exist the moment it finishes; installing both
  together also satisfies the host wheel's exact `sbxloop-worker==X` pin without
  the index being consulted for it. Rollback still uses PyPI, where the older
  version is never racing. Both failed runs failed safe — restart and health
  check skipped, rollback restored the running version, pause state survived.

- Discord: a reply to the bot is still recognised when discord.py leaves
  `reference.resolved` unset (the referenced message came only from the
  cache), and a reply to a *deleted* message no longer counts as one. This
  gate decides steers now, not just concierge turns.

- Discord: `daemon_discord_threads` gains an index on `thread_id`, the
  column `run_for_thread()` filters on — it is consulted per inbound message
  in a non-control channel and was scanning a row per run the daemon had
  ever done. The bridge also drops its engine handle when a run finishes
  instead of leaving a finished run's engine reachable.

- Logging: fields whose value is `None` are dropped before rendering
  (`sbxloop.log.drop_none_fields`) — `worker.job_done … error=None exit_code=None`, `job_submit … cwd=None`, `provision_start … template=None` and every host event's `job=None` no longer clutter the
  daemon's log; absence is the record.

- Audit runs no longer sink on a verify command they never needed: the
  audit contract now tells the planner an audit changes no code and needs
  no `verify_commands` (never the project's suite/build/lint — field failure
  rakvqn6fr, an audit chartered to find a failing test was asked to prove
  the suite green), and an audit that still fails on the harness has its
  findings collected and filed instead of lost.

### Fixed

- Scheduled-audit charters carry their metadata as an HTML comment
  (`<!-- sbxloop: every=7d -->`) instead of `---` front-matter: mdformat
  rewrote the front-matter into a thematic break plus an H2 the first time
  the charters shipped, and the daemon then saw no metadata at all (`---`
  YAML still parses; the mangled form is a clear error naming the fix).
  The scheduler also fast-forwards the daemon's checkout (throttled, 10 min)
  before reading charters — it used to be refreshed only when a run started,
  so charters merged after the last run were invisible.

### Fixed

- verify-lint rejects a check wrapped in its own `sh -c "..."` / `bash -c`
  (field failure r7ef26eht, the first sbxloop-on-sbxloop run): each verify
  command already runs under `sh -c`, so a dollar expansion inside the
  wrapper's double quotes is consumed by the outer shell — the plan's
  `git status | awk '{print $2}'` guard printed whole lines and failed every
  revision and the replan of a correct change. The plan prompt says so too.

### Added

- **Discovery lane for the daemon**: issues carrying `[daemon] audit_label`
  (`sbxloop:audit`) are charters — the run investigates and files findings
  as `sbxloop:backlog` issues (evidence / repro / proposal / size / kind
  per finding, at most 5, "nothing real" is a valid outcome) instead of
  delivering a PR. `WorkItem.kind` (`patch` | `audit`, persisted) drives a
  two-label poll, a kind-aware claim, `deliver=False` for audits, the audit
  contract in the outcome text, and an audit-success comment that names what
  it filed (`RunReport.filed`) and always closes the audit issue. Discord
  cards and `daemon items` show the kind. Promotion stays a human label swap.
- **Post-mortems the daemon files itself** (`[daemon] postmortems`, default
  on): when a patch item is abandoned or completes without delivering, the
  daemon opens a `sbxloop:audit` issue carrying a dossier — plan and verify
  commands, the last verify transcript, failure events, recent event tail,
  and the `SBXLOOP_STATE_DIR=… sbxloop logs <run>` line — so the discovery
  lane turns its own failures into evidenced findings. Once per run, never
  for audit items (no recursion), `postmortems_per_day` (3) cap.
- **Scheduled area audits, charters versioned in the repo** (`[daemon] audits = true`, `audit_dir = ".github/sbxloop/audits"`): each
  `<name>.md` with front-matter `every: 7d` (and optional `enabled`) is a
  charter the daemon opens as an `audit: <name>` issue when due. GitHub is
  the schedule's source of truth (a still-open audit is never re-filed; one
  created within the interval counts), the store is a cache; broken charters
  are reported once and skipped. sbxloop's own repo ships four:
  verify-lint-vs-prompts, daemon-guardrails, e2e-markers, test-flakes.
- **Delivery reviews** (`[daemon] review_deliveries`, default on): after a
  patch item delivers a PR, the daemon opens `review: PR #N` as an audit
  charter — the loop evaluating the code it just wrote (defects, missing
  edge cases, scope drift, unjustified claims) and filing findings for a
  human to promote. Once per run, `reviews_per_day` (5) cap.
- **Findings about the tool are routed, never dumped on the project.** The
  audit contract asks the agent to put findings about sbxloop itself
  (planner, prompts, lint, delivery, daemon) under
  `.sbxloop/backlog/tool/`; with `[daemon] tool_repo = "owner/sbxloop"`
  they are filed upstream (`RunReport.tool_filed`, named in the closing
  comment), otherwise only noted in the comment (`tool_noted`) — the
  project's tracker never receives issues about the tool that ran on it.

## [0.7.0] — 2026-08-16

The "run sbxloop on sbxloop" release. Everything a readiness audit
(2026-08-15) found standing between the daemon and its own repository,
landed as three reviewed PR stacks (#257–#277) plus the daemon field-test
fixes: diff-based delivery for git workspaces, `.gitignore`-aware artifact
scans, a uv-aware Python toolchain with a pinned 3.13 and `git` as baseline
tooling, per-phase tool-call ceilings, larger-repo budgets, daemon
guardrails (cancel semantics, gated recovery, persisted breaker,
comment-lock claims, per-instance sandbox names), operator item controls,
GC of run directories, a dedicated fetch-refreshed daemon workspace,
configurable close-on-success, `sbxloop deliver`, structured GitHub error
status, probes-as-data, an anchored `state_dir`, a tightened fake-sbx drift
loop, documented prompt contracts, and a Discord-native chronology
(embeds, batched tools, live status line, tool-burst digests, steer-wait
surfacing, `sbxloop daemon ctl`).

### Fixed

- Daemon: an operator `!sbx cancel` is no longer settled as a failed
  attempt (field: cancelled from Discord → "failed; 1 attempt(s) left" →
  re-run fresh after the 15-minute backoff, and counted toward the circuit
  breaker — #246). The item now settles as **cancelled**: a new terminal
  work-item state with no automatic retry and no breaker count, reported on
  the source with attribution ("cancelled by Discord user `x`"; GitHub:
  comment + in-progress label removed, trigger left for a human), while the
  run itself stays resumable — the finish card and source comment say
  `sbxloop resume RUN`. New `!sbx cancel --retry` re-queues the item for a
  fresh run instead, and `!sbx retry <item>` reruns any cancelled or
  abandoned item (a human retry resets the attempt budget and skips the
  failure backoff; the daily cap still applies).
- Discord bridge with `--once` and other short-lived runs (#236): a run
  that started and finished before the gateway connected lost its headline
  and everything after it (the pump only knew the *active* run's item).
  Events are now buffered per run until the bridge is ready, and `close()`
  waits (bounded, `DRAIN_WAIT_S`) for the pump to post what is already
  queued, so a short run's chronology is complete instead of truncated at
  process exit.

### Changed

- Discord `chronology_level = "normal"` no longer streams every tool call
  (#235): a burst is digested into one line edited in place — count,
  per-tool breakdown, last command, failure count — closed by the next
  agent message, phase or task boundary. A trailing run of near-identical
  commands is collapsed to `⚙ bash x17 similar commands` with a "may be
  stuck; `!sbx cancel` stops the run" nudge; the `agent.tool_cap` ceiling
  (#228) is surfaced too. Failed calls keep their own detail block. The
  previous stream-everything behaviour is `"verbose"`; the full stream stays
  in `sbxloop logs`.

### Changed

- `state_dir` now defaults to the per-user `~/.sbxloop` instead of the
  relative `.sbxloop` (#224). The old default meant "wherever the shell was
  standing": `sbxloop status`/`logs` showed an empty world from any other
  directory, and a run started from inside a checkout dropped a state dir into
  it (field run `r5a1d9m9c`; #218 patched the dirty-probe symptom). A relative
  `state_dir` remains the explicit opt-in for project-scoped state and is now
  anchored at the config's directory; `~` expands. **Migration:** an existing
  `./.sbxloop` keeps working when `state_dir = ".sbxloop"` is set in
  `sbxloop.toml` (or moved to `~/.sbxloop`); `sbxloop doctor` warns when an
  unconfigured run finds a legacy `./.sbxloop` it would otherwise ignore.

### Added

- `sbxloop deliver <run>` (#223): deliver — or re-deliver — a completed
  run's artifacts as a PR without re-running the work. End-of-run delivery
  was a one-shot side effect with no retry path (field failure `rgwp5z40x`:
  every task passed, delivery failed on the empty-repo 409, and `resume`
  refuses completed runs). The command provisions a github-ops sandbox only,
  reuses the run's persisted config with `--repo`/`--deliver-base`/
  `--deliver-draft`/`--create-repo` overrides on top, runs the normal
  `ensure_repository` + `deliver_workspace` path, and emits the usual
  `run.deliver` events so `logs` and the finish summary see it; `--report`
  refreshes the run's tracking issue with the PR link. Re-delivering a run
  whose `sbxloop/<run>` branch already exists (a prior partial attempt)
  force-updates the branch and reuses an already-open PR for that head
  instead of failing on the refs POST 422.

- Operator controls for individual daemon work items (#229).
  `sbxloop daemon items` lists every item with state, attempts, pinned run
  and last error; `sbxloop daemon abandon <item> [--reason]` gives one up,
  `sbxloop daemon retry <item>` re-queues an abandoned or cancelled item
  with attempts reset and a fresh plan (not a resume), `sbxloop daemon requeue <item>` unpins a running item from its run so the next dispatch
  starts over (attempts kept). Same controls on Discord as
  `!sbx items|abandon|retry|requeue`. A live daemon
  honors a CLI abandon/requeue of the item in flight within a second — the
  run is cancelled, the operator's decision wins over the run's own outcome
  (no retry, no breaker count), and the source hears `report_abandoned`
  exactly once. The CLI runs in another process and can only flip the row,
  so an abandon or retry leaves a durable `pending_report` debt on it: the
  daemon pays it on its next tick (paused or not) or on the next start —
  the abandon reaches the issue / inbox file (an unclaimed item loses its
  trigger label / leaves `pending/`), a retry re-claims (drops the failed
  label, moves the file out of `failed/`) — exactly once. Recovery also
  closes the dead run's ledger and removes its sandboxes and secrets, as
  does an in-process abandon/requeue of an item queued for resume. Item
  transitions are single conditional statements, so a daemon settling the
  item concurrently cannot have its verdict overwritten by a stale command. Field origin: a spiraling item (#228) could only be
  abandoned by poking `DaemonStore` from Python, and recovery would have
  resumed the doomed plan.

- `sbxloop daemon ctl <status|pause|resume|cancel [--retry]|queue|items|abandon|retry|requeue>`
  — a local control surface for the running daemon (#232). Requests go
  through a file queue in the daemon's `state_dir/daemon/ctl/`, served by a
  daemon thread so `cancel` lands mid-run; a request stamped before the
  daemon's start (or before recovery finished — the control thread only
  starts after `recover()`) is refused, one no daemon picks up within `--timeout` is
  withdrawn, and one the daemon has already taken but not answered is
  reported as pending — the command still executes. Discord's
  `!sbx` and `ctl` share one command dispatcher (`sbxloop.daemon.control`),
  so the two surfaces cannot drift; a ctl cancel/retry is attributed on the
  source as "`<user>` via sbxloop daemon ctl". The bridge still ignores
  bot-authored messages by design.

- The scrutinizer now judges the verify commands as well as the work (#231):
  its prompt carries the exact command list VERIFY will run and the feedback
  the executor was addressing, and its verdict gains `verify_suspect` /
  `verify_suspect_reason` (a flag without a reason is rejected and retried).
  When a revision was triggered by a verify failure (read from the persisted
  verify attempt, not from feedback text) and the scrutinizer passes the
  work while ruling the check itself wrong,
  the engine spends a replan immediately — the planner is told why the old
  check was wrong — instead of burning the remaining revisions against a
  check the executor cannot edit (field failure r567rsm4e: a portable,
  runnable `od | grep` check asserting a column layout `od` never prints).
  A speculative flag on a check that has not failed yet, or one raised with
  no replan budget left, is surfaced but not acted on. Every ruling is a
  `phase.end` event (`status=verify_suspect`, `honored`), shown in the
  Discord bridge and the transcript.

- `~/.config/sbxloop/sbxloop.toml` (`$XDG_CONFIG_HOME` honoured) is read as
  the lowest-precedence config layer, below `pyproject.toml [tool.sbxloop]`,
  for settings that follow the operator rather than the checkout (`model`,
  `app_name`, `[discord]`). `sbxloop config show` reports it as
  `user config`.

- Every engine prompt template (`engine/prompts/*.md`) now opens with an HTML
  comment stating its contract — `string.Template` syntax, the `$`-escaping
  rule, the variables it takes, and which test guards which section — and
  `docs/architecture.md` gains a "Prompt templates" paragraph. `render()`
  strips the header before the prompt reaches the model, so rendered prompts
  are byte-identical to before (#225).

- Discord bridge output is now Discord-native: headline, finished report and
  `!sbx status` as embed cards; agent messages split at paragraph/code-fence
  boundaries (fences re-opened with their language) instead of clipped;
  consecutive tool calls batched into one code block with `✗ exit N` markers;
  a per-run status line edited in place as tasks progress (persisted, so a
  restarted daemon keeps editing the same message); issue/PR/branch as masked
  links (raw URLs in notices wrapped so they don't unfurl); verify failures,
  worker errors, denied permissions, refused egress and tooling warnings
  surfaced; every send disables mentions. New `[discord]` knobs `embeds`,
  `status_line`, `tool_batch_lines`. Pure formatting layer
  `sbxloop.daemon.discord_format` (no discord.py needed to test it).

- Tighter drift loop around the fake sbx and the GitHub stubs (#226):
  `sbxloop doctor --fail-on-drift` exits 1 when any conformance probe
  drifted, errored, or is unprobed for the installed sbx build; the e2e lane
  uses it instead of a workflow warning, and a new scheduled
  `sbx-conformance` workflow runs the deep suite against the newest sbx
  release ahead of adoption with a rolling verdict cache (cross-version flips
  reported). Unit stubs now replay worker-shaped GitHub error strings from
  `tests/fixtures/github_field_errors.json` (field-recorded entries name
  their run; synthetic ones are marked as such) and a guard test rejects
  inline `HTTP 404`/`409` literals in unit tests, so the 404-for-empty-repo
  stub of #219 cannot recur; the e2e workflow uploads every GitHub error
  string a real run produced for promotion into the fixture. Every
  `TODO(e2e ...)` marker must now cite an issue or an e2e step name
  (`tests/unit/test_e2e_markers.py`).

- `git` is now baseline agent tooling, provisioned on every agent sandbox
  independent of `[sandbox] languages` (#252): the dev-tools ensure probes
  `command -v git` and installs it in the same pooled apt call as the selected
  toolchains; a prebaked template that lacks it is topped up from the single
  prebake probe. `sbxloop bake` records whether git landed and `sbxloop doctor`
  shows a soft "git in template" row.

- **uv-aware Python toolchain** (#250). The `python` toolchain now installs
  `uv` from its pinned, checksum-verified GitHub release (per-arch digests,
  like Node/Go) and a uv-managed Python 3.13 (`python3.13` linked onto PATH),
  and its probe checks the interpreter series rather than mere presence —
  uv-workspace repos declaring `requires-python >= 3.13` (sbxloop's own
  included) previously failed at `uv sync` because the sandbox had neither.
  The plan/execute/decompose prompts carry a "uv projects" note per Python
  block (`uv.lock` present → `uv sync --all-packages` in steps, `uv run …`
  to verify), and the verify-command lint requires `uv run` heads when the
  workspace root has a `uv.lock` (bare `pytest` and `.venv/bin/…` are both
  rejected with the uv remedy; without a lockfile `.venv/bin/…` is
  unchanged). `astral.sh` (uv's installer host) joins the declarable
  registries, and `release-assets.githubusercontent.com` (where GitHub
  release-asset downloads redirect) joins the agent sandbox's provisioning
  allowlist so both downloads actually complete. `sbxloop doctor --deep`
  gains a `python-version` conformance row reporting the template's own
  `python3` against the pinned series.

- Budgets and resources for larger repositories (#253): verify output handed
  to the critic keeps the first 2 KB and the last 4 KB of each command
  (previously the last 1.5 KB only, so a long pytest run's failure summary
  arrived without any assertion text); new `[limits] mem_abort` threshold
  (off by default) fails the task with an explicit "sandbox memory exhausted"
  error the way `disk_abort` does for disk, instead of an in-VM OOM surfacing
  as a confusing test failure; `contrib/presets/large-repo.toml` documents a
  4 h wall clock / 80 tool-call preset and the fact that sbxloop passes no
  CPU/memory sizing to `sbx create`.

- Daemon guardrails now cover recovery, restarts and multi-daemon setups
  (#254, #234). `recover()` no longer dispatches resumes itself: an interrupted
  run is re-queued with its run pinned and the tick resumes it behind the same
  breaker / daily-cap / pause gate as any dispatch. Resumes are recorded in a
  ledger of their own: the daily cap counts them (each is a fresh engine wall
  clock), `!sbx status` shows them, and a new `[daemon] max_resumes_per_item`
  (default 2) bounds them — past it the interrupted run is settled as a failed
  attempt so a plan that keeps getting interrupted cannot burn engine time
  forever. Circuit-breaker state persists in the daemon store, so a
  crash-restart loop no longer resets it. The GitHub claim is now a
  compare-and-swap: the claim comment is posted first and the label swap only
  proceeds if it is the first claim comment of the current trigger cycle, so
  two daemons on one repo cannot both take an issue. The daemon's github
  sandbox is named per state dir (`sbxloop-daemon-github-<hash>`) so a second
  daemon on the host no longer removes the first's at startup. Source polling
  raises on failure and backs off exponentially (up to 30 min); the github
  sandbox is re-provisioned at most once per 5 minutes.

- `[daemon] close_on_success` and `[daemon] tracking_issue` (#251), both
  default true (today's behaviour). `close_on_success = false` leaves a
  delivered source issue open with the new `delivered_label`
  (`sbxloop:delivered`) instead of closing it the moment a draft PR appears
  — the human closes it when the PR merges; a re-trigger or an operator
  re-queue clears the label.
  `tracking_issue = false` skips the per-run tracking issue for GitHub items,
  whose source issue already carries the run's summary comment.

- **Run-directory retention** (#233). Every run leaves
  `<state_dir>/runs/<run>/` behind (the workspace clone under isolation plus
  harvested artifacts) and nothing removed them; an always-on daemon accreted
  clones without bound. New `[daemon] prune_runs_after_days` (default 14, `0`
  disables): the daemon sweeps on its first tick and once a day thereafter,
  and `sbxloop gc [--older-than DAYS] [--dry-run]` runs the same policy by
  hand (`sbxloop.gc`). Only terminal runs past the window are removed — never
  an in-flight/resumable run, a run with kept sandboxes, or one whose last
  delivery failed (its workspace is the only copy of the work). SQLite rows
  are kept; each removal is a `daemon.gc` event on the run (path, bytes,
  age), the daemon reports counts and bytes freed to its frontend, `resume`
  refuses a run whose workspace gc removed, and the workspace-clone finish
  summary prints the retention window. A sweep and a `resume` of the same
  failed run can race across processes: gc writes its marker in one write
  transaction with a re-check that the run is still terminal, then renames
  the directory out of `runs/` (into `gc-pending/`) before deleting it, and
  `resume` leaves the terminal state before touching the workspace and
  re-checks the marker after — so the workspace is never half-removed and
  unmarked, and never pulled out from under a resume.

- **Daemon workspace posture for unattended runs** (#255). Daemon runs
  against a git-checkout workspace use `clone` isolation by default
  (`[daemon] workspace_isolation`): a dirty tree proceeds from committed
  HEAD with a warning instead of `auto`'s refusal, which no human is present
  to answer. Before each fresh run the daemon `git fetch`es the checkout and
  fast-forwards its branch to its upstream (the tracked remote, or
  `origin/<branch>` when none is configured) — never a merge/rebase; diverged or
  colliding trees are left alone and logged, fetch failures warn and run
  from local HEAD (`[daemon] refresh_workspace`, default on). Daemon state
  is anchored to an absolute path outside the workspace: `[daemon] state_dir`, else an explicit `state_dir`, else a pre-existing legacy
  `./.sbxloop/state.db`, else `$XDG_STATE_HOME/sbxloop/<runner-dir-name>`;
  the resolved location is printed at start and the `sbxloop daemon items|abandon|retry|requeue` controls follow the same rule. Per-run clones now point
  `origin` at the source checkout's origin URL instead of the host path
  (metadata only; URL userinfo such as an embedded token is stripped). Docs and the systemd contrib prescribe a dedicated clone
  nobody edits as the daemon's workspace.

- Discord bridge, steering latency made visible (#236): a queued steer gets
  a note under it — `⏳ steer queued — agent is mid-execute on t2 (12/40 tool calls so far); answered at the next checkpoint` — edited in place as
  the phase, task and tool-call count (against the #228 ceiling) move, then
  resolved to picked-up / answered / failed / not-answered-run-ended.

- GitHub op failures carry the HTTP status as a structured field (#221):
  worker `GithubOpError.http_status` (parsed from `gh api` stderr or taken
  from `urllib` `HTTPError.code`), `ErrorInfo.http_status` on the JobResult
  envelope, and host `GithubOpsError.http_status`. The empty-repo bootstrap
  (`409`), missing-repo probe (`404`) and already-absent trigger label
  (`404`) now compare status codes instead of grepping gh's prose — the
  wording-mismatch that broke delivery on run rgwp5z40x (fixed in #219) can
  no longer recur. Message matching remains only as a fallback for a worker
  that predates the field.

### Fixed

- Artifact listings, harvest reports and `--deliver` PRs now honour the
  workspace's own `.gitignore` rules (#249): the name-based
  `[artifacts] exclude` list cannot know a project's `dist/`, vendored
  wheels or generated `_version.py` are build byproducts, so any tree after
  a build/sync delivered them into the PR. Files git would ignore
  (untracked *and* ignored — force-added tracked files still travel) are
  dropped and tallied as `gitignored` in the surfaced exclusion note; the
  probe works on the per-run clone and on harvested copies (which carry
  `.gitignore` but no `.git`), applies only in-tree `.gitignore` files
  (never the operator's global excludes or an enclosing checkout's rules),
  and degrades to the name-based scan when git is unavailable.
  `[artifacts] exclude` remains the operator override on top.
- **Delivery of git-checkout workspaces commits the run's diff, not a
  snapshot** (#248). When the workspace is a git checkout (the per-run
  clone, or an in-place checkout), the PR tree now carries only what the
  run changed relative to its base commit — deletions as `sha: null` tree
  entries, renames as delete + add, executable scripts keeping `100755`,
  symlinks as `120000` — instead of layering every file as `100644` onto
  the base tree, which could never delete a file and flipped every exec
  bit. The diff base is the merge base with the PR's target commit when
  the clone knows it, else the commit the clone was cut from (pinned as
  `refs/sbxloop/base` at clone time). Non-git workspaces still deliver as
  a snapshot; a checkout with no base to diff against falls back to one
  and says so in the PR body. The artifact exclude denylist applies to
  both paths.
- Expected probe answers no longer render as red error panels (#222). The
  `--create-repo` existence probe and delivery's base-ref lookup asked GitHub a
  question whose expected answer was "no", but the worker raised on the 404
  (or the 409 an empty repository returns) and emitted its error event before
  the host could classify the miss as fine — three field runs showed the
  alarm on runs that delivered cleanly. `repo.get` and the new `ref.get` op
  accept `allow_missing: true` and return `{"missing": true}` as an **ok**
  result; `ensure_repository` and the base-commit lookup branch on that data
  instead of sniffing exception messages. `GithubOpError` in the worker now
  carries `http_status` (parsed from gh's trailing `(HTTP NNN)` or urllib's
  `HTTPError.code`) so the miss/error split is a status compare, not a
  substring match.

## [0.6.0] — 2026-08-15

Rollup release: everything below shipped incrementally as the auto-released
v0.2.1–v0.5.86 patch series; v0.6.0 marks the point where the GitHub
integration and existing-checkout workflows were completed and field-verified
end to end.

### Added

- **Per-run workspace isolation for existing git checkouts** (#216, #218).
  When `[sandbox] workspace` points at a git checkout, each run works in a
  self-contained per-run clone on branch `sbxloop/<run_id>` — the checkout's
  working tree, branches, and HEAD are never touched. New
  `[sandbox] workspace_isolation = auto|clone|in-place` (default `auto`):
  `auto` refuses a dirty source tree up front (uncommitted changes would
  silently not travel; sbxloop's own `.sbxloop` state dir is exempt),
  `clone` proceeds from committed HEAD with a warning, `in-place` is the
  old behavior. The finish summary prints where the results live and the
  `git fetch <clone> sbxloop/<run>` command to pull them back. Host-side
  git goes through GitPython (new dependency, host package only).

- **Verify commands are mechanically validated against toolchain
  conventions** (#217, #218) at both decompose and plan acceptance: bare
  `python`/`pip`/`pytest` (use `.venv/bin/...`), bare `rspec`/`rake` (use
  `bundle exec`), bare `phpunit` (use `./vendor/bin/`), and any
  `sudo`/`apt` are rejected with the remedy quoted, costing one JSON retry
  instead of a revision cycle plus an in-VM workaround. Go, Rust, .NET,
  Node, Java, and C/C++ commands are correctly bare and deliberately
  unrestricted. Prompts additionally require verify commands to be
  self-contained — start what they probe, tear it down, never depend on a
  process the executor left running (#212).

- **The GitHub integration is fully drivable from the run command**:
  `--repo owner/name` (overriding `[github].repo`, validated up front),
  `--deliver-base`, `--deliver-draft` (#213), and `--create-repo` /
  `--create-public` (#214) — the delivery repository is probed right after
  provisioning (fail fast on typos, before any work), created on demand
  when explicitly allowed, and an existing-but-empty repository is
  bootstrapped with an initial commit so delivery always lands as a normal
  reviewable PR (#214, #219).

- **Run outcomes are surfaced, not buried in scrollback** (#215): the
  finish summary gains a `github:` section (repository, created-this-run
  marker, tracking issue, delivery PR or failure) and completed runs close
  their tracking issue (`state_reason: completed`); failed runs leave it
  open as the thing still needing a human.

- **Read-only critic sessions may run shell** (#211): `shell` joins the
  critic allowlist so scrutinize/validate sessions can run inspection
  commands directly; everything else, including unknown SDK permission
  kinds, still fails closed.

- **Artifact excludes accept glob patterns** and `*.egg-info` joins the
  defaults (#215) — pip's project-named metadata directory was shipping in
  delivery PRs and no exact component name could catch it.

### Fixed

- **Permission denials and CLI validator refusals no longer masquerade as
  degraded critic tooling** (#211, #212): a rejected call's `success=False`
  completion echo is shielded by its `tool_call_id`, and "Command not
  executed" validator refusals land in a separate non-degrading
  `tool_refusals` tally — previously every denial or refusal downgraded a
  clean critic verdict until the revision budget died.

- **Empty-repository delivery** failed against real GitHub (#219): the ref
  lookup answers HTTP 409 "Git Repository is empty.", not the 404 the
  bootstrap listened for. Both now trigger the bootstrap.

- **sbxloop's own state directory no longer trips the isolation dirty
  refusal** (#218): any command run from inside a checkout drops a relative
  `.sbxloop` there, which is run state, not user content.

### Added

- **`[sandbox] languages = ["dotnet"]` provisions the .NET SDK** (issue
  #164), completing the ten-language set for layer 1 of #140. Availability
  and currency of `dotnet-sdk-*` in the base Debian/Ubuntu archives varies
  by release and is unreliable to depend on, so the SDK comes from
  Microsoft's own builds, pinned to an LTS patch and verified against the
  **sha512** its release metadata publishes (the .NET feed publishes sha512
  rather than sha256). The major is pinned because a project's `global.json`
  can demand an exact SDK and fails hard when it is absent. `DOTNET_ROOT` is
  recorded in the persistent env — a manual, non-package SDK install is only
  half-done without it — along with a telemetry opt-out, which under
  default-deny egress would otherwise only ever be a blocked outbound
  request. At roughly 220 MB the SDK is the strongest candidate in the set
  for `sbxloop bake` rather than a per-run download. Accepted spellings:
  `dotnet`, `csharp`, `c#`, `net`, `dotnet-sdk`. Adds a
  `builds.dotnet.microsoft.com` egress dependency (#141).

- **`[sandbox] languages = ["rust"]` provisions cargo and rustc** (issue
  #143). Debian/Ubuntu do ship `cargo`/`rustc`, but distro Rust routinely
  lags stable by several releases, which breaks edition- and MSRV-sensitive
  projects outright — and rustup is the norm nearly every Rust instruction
  assumes. `rustup-init` is downloaded and checksum-verified per
  architecture rather than piped from `sh.rustup.rs` into a shell, then run
  with `--profile minimal --component rustfmt --component clippy`: the two
  components a plan's verify commands actually reach for, without the large
  docs component nothing here needs. rustup's usual PATH wiring edits shell
  profiles that a bare `sbx exec sh -c` never sources, so the shims are
  linked into `/usr/local/bin` and the toolchain stays in the agent's home
  where `cargo install` can write to it without sudo. Adds
  `static.rust-lang.org` as an egress dependency (#141).

- **`[sandbox] languages = ["go"]` provisions the Go toolchain** (issue
  #153). The official `go.dev` tarball rather than `golang-go`: modules
  frequently declare a `go` directive newer than the distro build, which
  fails the build outright. Pinned and verified against upstream's published
  per-architecture sha256, resolved in-sandbox like the Node entry, and
  installed by replacing `/usr/local/go` rather than extracting over it —
  overlaying two versions leaves a broken tree. `GOTOOLCHAIN` is
  deliberately **not** pinned to `local`: doing so would make a project
  whose `go.mod` demands a newer Go fail outright, which is the very
  distro-lag failure this entry avoids. Left at the default, Go fetches what
  the module asks for during EXECUTE, where a plan can declare the Go proxy
  as egress. Adds a `go.dev` egress dependency at provisioning time (#141).

- **`[sandbox] languages = ["typescript"]` provisions `tsc` on top of Node**
  (issue #150). Toolchain entries can now declare what they are built on,
  and selecting one selects its requirements — TypeScript pulls in the
  JavaScript entry and the registry order guarantees the Node runtime is
  installed before `npm i -g typescript` runs. The compiler is pinned; a
  project with its own `typescript` devDependency will still use that, since
  the global install is for bootstrapping a project from nothing rather than
  for driving any particular build pipeline. Adds a `registry.npmjs.org`
  egress dependency on top of Node's `nodejs.org` (#141) — and because
  provisioning runs before the PLAN phase, a plan declaration cannot satisfy
  it; the README now documents `[sandbox] extra_allow_domains` as the way to
  make installer-based toolchains reachable today.

- **`[sandbox] languages = ["javascript"]` provisions Node** (issue #147).
  The official `nodejs.org` tarball rather than apt: Debian/Ubuntu stable
  ship a Node several majors behind current LTS, which breaks packages
  declaring modern `engines` constraints — a functional failure, not
  cosmetic lag. Pinned to an exact LTS release and verified against the
  upstream `SHASUMS256.txt` digest for the sandbox's architecture, resolved
  in-sandbox so the same config works on arm64 microVMs and amd64 CI
  runners; an unrecognized architecture fails loudly instead of downloading
  a binary that cannot run. The probe checks the pinned **major**, not just
  that `node` exists, so a template carrying an older Node is upgraded
  rather than accepted. Node is extracted into `/usr/local`, which also
  makes it npm's global prefix so `npm i -g` lands on PATH. Accepted
  spellings: `javascript`, `js`, `node`, `nodejs`, `javascript-node`. Adds
  a `nodejs.org` egress dependency (#141).

- **`[sandbox] languages = ["php"]` provisions PHP and Composer** (issue
  #167). apt for the interpreter and — more importantly — the extensions:
  `php-cli` alone passes a `command -v php` check and then fails the moment
  a project or Composer itself needs mbstring or zip, so `php-mbstring`,
  `php-xml`, `php-curl`, and `php-zip` come with it and the probe checks
  the extensions are actually loaded rather than just that `php` exists.
  Composer is not reliably packaged at a useful version, so it comes from
  upstream — but as a **pinned release verified against its published
  sha256**, not piped into the interpreter. Bumping the version means
  bumping the digest alongside it. Adds a `getcomposer.org` egress
  dependency (#141).

- **`[sandbox] languages = ["java"]` provisions a JDK and Maven** (issue
  #161). apt for both, pinned to `openjdk-21-jdk` rather than floating on
  `default-jdk`, which moves between distro releases. `JAVA_HOME` is part of
  the contract — many build tools read it directly rather than looking for
  `java` on PATH — so the entry records it in `/etc/sandbox-persistent.sh`,
  which the worker already loads into the environment the agent session and
  its shell commands inherit. The value is derived from the installed
  `javac` rather than hardcoded, since the JVM directory name embeds the
  distro architecture. Gradle is deliberately not installed: the `gradlew`
  wrapper is the norm and fetches its own distribution, which makes it an
  egress question (#141) rather than a package. Accepted spellings: `java`,
  `jdk`, `jvm`.

- **`[sandbox] languages = ["ruby"]` provisions Ruby** (issue #158).
  `ruby-full`, `ruby-dev`, `bundler`, and `build-essential` — apt only, and
  no egress beyond the always-reachable baseline. The dev headers and
  compiler are deliberately not optional: gems with native extensions
  (nokogiri, pg, …) fail to build when only `ruby` is present, which is the
  usual way a half-installed Ruby shows up. `build-essential` is shared with
  the C/C++ entry and installs once, not twice. Projects pinning an exact
  Ruby the distro does not carry still need `rbenv`, which is out of scope
  here. Accepted spellings: `ruby`, `rb`.

- **`[sandbox] languages = ["cpp"]` provisions a C/C++ toolchain** (issue
  #170). `build-essential`, `cmake`, `ninja-build`, and `pkg-config` — pure
  apt, no installer, and no egress beyond the apt mirrors already in the
  always-reachable baseline, which makes this the cleanest entry in the
  Layer 1 set. Accepted spellings: `cpp`, `c`, `c++`, `cxx`, `c-cpp`. The
  probe checks `gcc`, `g++`, `make`, `cmake`, and `pkg-config`; `ninja` and
  `clang` are optional extras a build can do without, so a template that
  lacks them is not reinstalled over.

- **`[sandbox] languages` selects which language toolchains the agent
  sandbox is provisioned with** (issue #144, layer 1 of the language-bias
  investigation #140). `_ensure_dev_tools` apt-installed `python3-venv` and
  `python3-pip` for the *agent's* project before its first turn — a real
  head start, but one only Python got: a Node, Rust, or Ruby task
  discovered its missing compiler on first failure and spent revision
  budget bootstrapping it. That call is now one entry in a registry
  (`sbxloop.toolchains`) selected by config rather than a hardcoded special
  case. Semantics are the ones the 0.4.0 field failure taught us and they
  apply to every entry: probe first (a template that already ships the
  toolchain costs no apt call and no network), never fatal (a failure warns
  with the toolchain named and the run continues on the agent's own
  `sudo apt-get` escape hatch), and opt-in only. Selected apt packages are
  pooled into a single `update && install`, so N languages is one round
  trip. **Behavior is unchanged for existing runs**: unset means
  `["python"]`. Setting the key replaces that default rather than adding to
  it — this is the point, since no language should be privileged by
  accident of implementation. Python is the only registered entry so far;
  the other nine follow.

### Changed

- **The plan and execute prompts no longer teach Python as the default
  ecosystem** (issue #142, layer 3 of the language-bias investigation).
  Their environment sections opened on PEP 668 and virtualenvs, and the one
  worked verify example was `.venv/bin/pytest`, so a planner had exactly one
  ecosystem to pattern-match against. The environment facts are now
  language-neutral (apt + sudo, egress) with the universal workspace-root
  verify contract kept prominent, and per-ecosystem specifics moved into a
  short reference block the model reads one entry of. Python is the first
  entry — its PEP 668 guidance is unchanged in substance, just no longer the
  framing for everyone else. Ecosystems covered so far: Python,
  JavaScript/Node, TypeScript, Go, Rust, Ruby, Java/JVM, C#/.NET, PHP,
  C/C++ — the full ten-language set Layer 3 tracks.

- **Package-registry egress levels up rather than down** (issues #141,
  #145). The network baseline privileged Python: `pypi.org` and
  `files.pythonhosted.org` were unconditionally reachable while every other
  language's registry cost a plan declaration the planner had to remember —
  and a forgotten declaration is a failed run, not a degraded one. #145
  settled the direction for the whole layer: promote the other registries to
  PyPI's tier rather than demote PyPI to theirs. Demotion could not have
  produced real parity anyway — the worker's own `pip install` runs at
  provision time, before a plan exists to declare egress in — and it would
  have broken every existing plan that never declared PyPI.

  Structurally, `policy.PROMPT_ADVERTISED_DOMAINS` is now the union of
  `BASELINE_REGISTRY_DOMAINS` (the language registry tier, starting with
  Python's two hosts) and `APT_MIRROR_DOMAINS` (language-neutral distro
  infrastructure, baseline regardless). `sbxloop config policy` prints the
  two tiers separately, so what is unconditionally reachable is legible
  without reading the source. Promoted into the registry tier so far:

  - Python — `pypi.org`, `files.pythonhosted.org` (#145)

  - JavaScript/Node — `registry.npmjs.org`, `registry.yarnpkg.com`, and
    `codeload.github.com` for `github:user/repo` dependencies, whose
    tarballs come from a different host than the clone (#148)

  - TypeScript — no new domains: the compiler and `@types/*` packages are
    plain npm packages, so the Node promotion covers the whole toolchain.
    A type-check-only task over vendored dependencies needs no egress at
    all, and an empty `egress` is a complete plan rather than a forgetful
    one (#151)

  - Go — `proxy.golang.org` and `sum.golang.org` together: a reachable
    proxy whose checksum database is blocked fails `go mod download` at
    verification, which reads as a broken toolchain rather than a policy
    decision (#154)

  - Java — `repo.maven.apache.org` and `repo1.maven.org` (Maven Central),
    plus `plugins.gradle.org` and `services.gradle.org`. Java was in
    *neither* tier: a plan could not even declare Central without operator
    configuration. Gradle needs the plugin portal and the wrapper
    distribution host as well — Central alone still fails the build (#162)

  - C#/.NET — `api.nuget.org` and `nuget.org`, also previously in neither
    tier. `dotnet restore` runs implicitly inside `dotnet build` and
    `dotnet test`, so an unreachable feed surfaced as a build failure
    rather than an install failure (#165)

  - PHP — `repo.packagist.org` and `packagist.org`, the third registry that
    was in neither tier. Composer also fetches many dist zips from
    `codeload.github.com`, already baseline since the Node promotion —
    without it, `composer install` fails only for the packages that happen
    to be served from GitHub (#168)

  - Ruby — `rubygems.org` and `index.rubygems.org` (bundler's compact index
    is a separate host). This was the case `policy.py` cited as motivating
    the declarable tier in the first place — "write a Rails app"
    bundle-installing out of the box — and it no longer depends on the plan
    remembering (#159)

  - Rust — `crates.io`, `static.crates.io`, and `index.crates.io`. Cargo
    resolves from the sparse index, downloads from static, and talks to the
    API separately; two of the three fails mid-resolution (#156). The
    `rustup` installer domains are toolchain rather than registry and stay
    with the Layer 1 work (#143)

  - C/C++ — no new baseline domains. Its default dependency source is apt,
    already baseline, and the apt-only path is now covered by tests: useful
    evidence that the baseline works when a language's dependencies come
    from it. Conan (`center.conan.io`) is declarable rather than seeded —
    a real registry, but not how C/C++ dependencies normally arrive — and
    vcpkg stays operator-configured, since it fetches source tarballs from
    whatever upstream each port names and no fixed host set can cover that
    (#171)

  The plan and execute prompts no longer hardcode the tiers: both lists are
  injected from `policy.py` at render time, so a promotion cannot leave the
  prompts telling planners to declare a domain that needs no declaration.

  `WELL_KNOWN_REGISTRY_DOMAINS` — the declarable-without-configuration tier —
  survives as the second-line case: a legitimate registry that is not how a
  language's dependencies normally arrive, so a plan names it and the grant
  is event-logged. Conan is its one member today. An empty tier is also a
  supported state: `sbxloop config policy` and the prompts both render it
  explicitly rather than printing a blank.

  The promotion trades some audit granularity — a baseline registry emits no
  `policy.allow` event, because there is no grant to log — so it is bounded
  to read-only public registries for supported languages.
  **`[policy] deny` now wins over the always-reachable tier too**: denied
  domains are filtered out of the set provisioning seeds
  (`policy.baseline_allows`) instead of being seeded and then refused a
  redundant re-grant. Previously a `deny` on `pypi.org` left it reachable,
  because provisioning seeded it before any grant could be refused.

- **Leftovers from the 0.2.0 `sdxloop` → `sbxloop` rename are gone.** The
  exception base class is now `SbxloopError` (was `SdxloopError`) across all
  51 references — a breaking rename for anything importing
  `sbxloop.errors.SbxloopError` directly, consistent with the no-shim
  cutover 0.2.0 already made; it was never exported from `sbxloop.__all__`,
  so the top-level public API is unchanged. CI's push trigger is `sbx/**`
  (was `sdx/**`, which never matched the branch convention README
  documents, so branch pushes silently skipped CI), and the stale
  `.sdxloop/` ignore entry and empty state dir were removed. Pre-0.2.0
  changelog entries keep the old name: they record what actually shipped.

- **Provisioning is no longer fully serial** (issue #127). The agent and
  github sandboxes — which share nothing but the host workspace dir — now
  create, policy, secret, and probe on parallel threads, and the engine
  installs both workers concurrently as well, cutting the fixed pre-run
  startup tax roughly in half on github-enabled runs. The prebaked-template
  verification collapsed from three `sbx exec` round trips (manifest read,
  import/version check, entrypoint smoke) into one in-sandbox script, so the
  templated happy path costs a single probe per sandbox. Supporting changes:
  `EventBus.publish` is now thread-safe (subscriber invocations stay
  serialized), the workspace wheel build is lock-guarded so concurrent
  installs share one `uv build`, and a provisioning failure on either thread
  still drains the other before rolling back everything the attempt created.

### Fixed

- **An inner command saying "not found" no longer aborts the run.**
  `sbx exec` classified any stderr containing that substring as an
  sbx-level failure and raised, instead of returning the nonzero result
  its callers are written against. But `exec` is the one CLI call whose
  stderr belongs to somebody else's program, and "not found" is the single
  most common thing a shell says — `sh: dpkg: command not found`, curl's
  `404 Not Found`, npm's `404 Not Found - GET`. Every best-effort probe
  built on `result.ok` could therefore be turned into a hard failure by a
  missing binary: the dev-tools ensure, the search-fallback ensure, and the
  bake that runs them. A missing *sandbox* now has to say so (real sbx and
  the fake both name the sandbox in that message); the infra markers for a
  stopped VM or an unreachable daemon are unchanged.

- **Agent glob/grep no longer die on 16 KiB-page sandboxes** (issue #122).
  The Copilot CLI's bundled ripgrep is a musl-static jemalloc build compiled
  for 4 KiB pages; on guests with a larger page size (16 KiB is common for
  Apple-silicon microVMs) every `glob`/`grep` tool call aborted with
  `<jemalloc>: Unsupported system page size`, silently stripping the agent —
  and especially the shell-less read-only critic — of its search tools. Three
  layers now cover it: the worker detects the guest page size before each
  Copilot session and reroutes glob/grep to the system ripgrep via the CLI's
  documented `USE_BUILTIN_RIPGREP=false` escape hatch (an operator-set value
  is never overridden; a `sandbox.tooling_warning` event records the reroute,
  or the degradation when no `rg` exists); provisioning's dev-tools ensure
  apt-installs `ripgrep` on non-4-KiB guests that lack one (probe-first — a
  4 KiB guest or a template that ships `rg` costs no apt and no network); and
  a new `page-size` conformance probe reports the guest page size and
  fallback readiness under `sbxloop doctor --deep`.

- **A read-only critic that lost its tooling can no longer emit a confident
  clean verdict (#123).** Field failure (the same incident as #122): a
  SCRUTINIZE session whose `glob`/`grep` calls crashed (and whose `shell`
  was denied by the read-only allowlist, as designed) was left with only
  `view` — and still returned `{"verdict": "pass"}` indistinguishable from
  a thorough review. Agent sessions now tally permission denials and
  tool-call failures (`SessionHealth` on the job result; each denial also
  emits an `agent.permission_denied` event), and the critic phases apply a
  degraded-tooling guard: a `pass`/`accept` from a session with failed tool
  calls is not trusted — the phase re-runs once in a fresh session that is
  confronted with the failures and must account for the reduced coverage
  (a transient crash gets its second chance), and a still-degraded clean
  verdict is downgraded to `revise`/`reject` with the tally in the feedback.
  Denials alone never trigger the guard (a critic probing `shell` is the
  barrier working, not a broken session). Every critic phase row now
  persists the session's `tooling_health` and a `downgraded` marker, and a
  downgrade emits a `phase.end` event with `status="degraded"`, so crippled
  critic runs are auditable live and after the fact. The scrutinize/validate
  prompts also tell the critic up front to report lost coverage instead of
  claiming verification it could not perform.

### Added

- **Well-known package registries are declarable out of the box.** Runs
  building Ruby/Node/Rust/Go projects used to die in `bundle install` /
  `npm install`: the default `[policy] allow` is empty, so a plan declaring
  `rubygems.org` failed validation and the sandbox blocked the host. A
  curated set of read-only registries — RubyGems (`rubygems.org`,
  `index.rubygems.org`), npm/yarn (`registry.npmjs.org`,
  `registry.yarnpkg.com`), crates.io (`crates.io`, `static.crates.io`,
  `index.crates.io`), and the Go proxy (`proxy.golang.org`,
  `sum.golang.org`) — is now always in-bounds for plan-declared egress
  (`policy.WELL_KNOWN_REGISTRY_DOMAINS`). They stay grant-late: unreachable
  unless a plan declares them with a justification, every grant
  event-logged, and `[policy] deny` still blocks them. The plan/execute
  prompts now name the set so planners declare what the toolchain needs.

- **Interactive chat with a running loop.** `sbxloop run` is no longer
  watch-only: the TUI grows a chat form (keystrokes captured in cbreak mode,
  the in-progress line rendered inside the pinned status panel; `--no-tui`
  reads plain stdin lines). A submitted message queues on the engine and is
  absorbed at the next phase boundary — the same checkpoint cancellation
  uses — where the agent pauses and answers it in a fresh read-only STEER
  session that may inspect the workspace. The verdict decides the course
  change: `continue` (answer only), `steer_task` (the current task re-plans
  immediately with the user's guidance as feedback, spending no
  revision/replan budget — user direction is not a failure), or `steer_run`
  (standing guidance injected into every later plan/execute prompt,
  persisted in a new `runs.user_guidance` column so resumed runs keep their
  direction; the schema migrates in place). Every chat turn is event-logged
  (`chat.message` / `chat.reply` / `chat.action`, query with
  `sbxloop logs RUN --type chat.`) and recorded as a `steer` phase attempt;
  a failed steer never fails the run. The status panel shows
  queued/answering messages, and `--chat/--no-chat` (on `run` and `resume`,
  default on with a TTY) controls the whole feature.

- **Transcript panels name the responding agent.** Agent feedback bubbles
  used to be titled a generic `agent <time>`, so you couldn't tell which
  Copilot session was speaking. Each phase now stamps its persona
  (`decomposer`, `planner`, `executor`, `scrutinizer`, `validator`) onto its
  job's `agent.*` events host-side (the in-sandbox worker doesn't know which
  phase it serves), the TUI header shows it, and `sbxloop logs` lines carry
  it as `[<name>]`. Events without a name (older runs) keep the `agent`
  title.

- **`sbxloop list-models`** — lists the models the GitHub Copilot SDK gives
  the authenticated subscription access to, straight from the SDK's
  `list_models()` API on the host (no sandbox): model id, display name,
  billing multiplier, context window, vision support, reasoning-effort
  levels (default marked), and policy state, with the configured `model`
  highlighted and a warning when it is not in the list. `--json` emits the
  SDK's raw model dicts for scripting. The SDK is optional host-side — the
  new `sbxloop[copilot]` extra installs it, and the command explains that
  when it is missing. Auth uses the SDK's normal env chain
  (`COPILOT_GITHUB_TOKEN` → `GH_TOKEN` → `GITHUB_TOKEN`, `./.env`
  included), and failures carry an auth diagnostic naming which token env
  var was visible.

- **Prebaked sandbox templates + `sbxloop bake` (#48).** `sbxloop bake` runs
  the worker install ladder once in a scratch sandbox (plus a best-effort
  Copilot runtime pre-cache) and persists the result with `sbx template save`. With `[sandbox] template` pointing at the baked ref, provisioning
  verifies the baked worker with fast probes (bake manifest → version match
  → entrypoint smoke) and skips the per-run install entirely; any probe
  failure falls back to the existing install ladder, so a stale template
  degrades to today's behavior instead of failing the run. Runs emit a
  `sandbox.prebaked` event either way, and `sbxloop doctor` warns when the
  configured template was baked with an older worker (re-run `sbxloop bake`) or is missing from `sbx template ls`.

- **`sbxloop doctor` now runs an sbx conformance suite** (#52): every
  field-learned assumption about sbx semantics is a named probe with a
  machine-checkable verdict — secret-env visibility under `exec`, the
  exec error channel, `cp <dir>/.` semantics, workspace-mount
  discoverability, template python3-venv presence, custom-secret keying and
  the parseable exists-error scope, plus cheap CLI-surface probes. Verdicts
  are cached per `sbx version` in the state dir; `doctor` runs the cheap
  probes and serves live-sandbox verdicts from the cache, `doctor --deep`
  boots one scratch sandbox for the full suite. When a verdict flips —
  against the verdict this sbxloop build depends on, or against a prior sbx
  version's cache — doctor warns loudly, naming the dependent behavior.
  Provisioning's existing checks (secret visibility, mount discovery) now
  feed the same cache, so ordinary runs keep the verdicts fresh for free.

- **`sbxloop secrets` command group** — proactive lifecycle management for
  the sbx custom-secret registrations sbxloop owns
  ([#55](https://github.com/brettbergin/sbxloop/issues/55)):

  - `secrets list` enumerates the tracked registrations (the Copilot token)
    across scopes and flags pre-collision state: stale registrations owned
    by dead run sandboxes, wrong host bindings from older versions, and
    foreign-scope conflicts. Enumeration tries `sbx secret ls` and falls
    back to a transient set-custom collision probe (the exists-error names
    the owning scope) when the build doesn't support listing.
  - `secrets clean` removes stale sbxloop-owned registrations — dry-run by
    default (`--apply` to execute, `--all` to include healthy ones). Never
    touches foreign scopes or the built-in `github` service secret.
  - `secrets rotate` replaces the registration with a new token value in
    one step (read from the environment/.env or a hidden prompt, never
    argv), warns when live sandboxes may still hold the old token, and
    verifies which secret strategy (proxy vs plain-env fallback) the next
    run will use via a throwaway sandbox (`--no-verify` skips).

- The 0.1.3 secret-collision recovery logic now lives in a shared module
  (`sbxloop.sbx.secretstate`); provisioning and the `secrets` commands use
  the same field-hardened implementation.

- **Plan-declared, least-privilege network egress** (#49). The PLAN phase
  may now declare external domains a task needs during EXECUTE (each with a
  justification) via a new `egress` field in the plan schema. Declarations
  are validated against operator-set bounds — a new `[policy]` section in
  sbxloop.toml with `allow`/`deny` domain patterns (exact, `*.wildcard`, or
  `*`) — and out-of-bounds requests fail plan validation with a remediation
  hint. In-bounds grants are applied grant-late (`sbx policy allow network <domain> --sandbox <agent>` at EXECUTE entry, so resumed runs re-grant on
  their fresh sandboxes) and every grant/refusal is emitted as a
  `policy.allow`/`policy.deny` run event, making the persisted event log an
  egress audit trail (`sbxloop logs RUN --type policy.`). `sbxloop config policy` renders the effective per-phase policy. sbx 0.35 has no
  revocation primitive, so grants persist for the sandbox's lifetime but
  never outlive a run (sandboxes are removed at run end).

- **`keep_on_failure`** (config + `--keep-on-failure`) — successful runs clean
  up as always; failed runs (task failures and infra crashes alike) leave the
  sandbox pair alive, mark the run `kept_reason="debug"` in the state DB, emit
  a `run.keep` event, and print a hint naming the sandboxes and the shell
  command to inspect them. `--keep-sandboxes` runs are now marked
  `kept_reason="manual"` so `sandbox prune` respects them too.

- **`sbxloop shell <run> [--role agent|github] [-c CMD]`** — opens an
  interactive shell (or runs a one-off command) inside a run's sandbox after
  verifying liveness via `sbx ls`. Works for kept, in-flight, and leaked
  sandboxes; the inner exit code is passed through.

- **`sbxloop sandbox prune`** — garbage-collect orphaned `sbxloop-*` sandboxes
  left behind by crashed hosts or killed runs, by cross-referencing `sbx ls`
  against the state DB. Dry-run by default; `--force` removes, `--min-age`
  (hours, default 1) guards against racing live runs, `--include-kept` also
  prunes kept-for-debugging sandboxes. `sbxloop doctor` now reports the
  orphan-candidate count. The runs table gained a `kept_reason` column (the
  kept-sandbox taxonomy prune respects; applied as an in-place migration).

### Changed

- **Provisioning batches network policy grants into one `sbx` call.**
  `sbx policy allow network` takes RESOURCES as a comma-separated list
  (documented since the first sbx CLI reference), so provisioning and
  template baking now grant all of a sandbox's allow-domains in a single
  invocation instead of one CLI round-trip per domain (13 for the agent
  sandbox). Plan-declared egress grants are unchanged: still one call per
  domain, each individually event-logged.
- Coverage moved out of the default pytest options and into CI /
  `make test-cov`: tracing cost ~15% of full-suite wall time, and the
  `--cov-fail-under` gate made every partial run (single file, `-k`,
  `--pdb`) fail spuriously. `make check` still enforces the 85% gate.
- The test suite runs parallel by default (pytest-xdist, `-n auto` with
  work-stealing): ~8min → ~2.5min locally. Pass `-n0` for a serial run when
  debugging with `-s`/`--pdb`. Two wheel-resolution tests were made hermetic:
  they no longer plant or depend on wheels in the real installed package's
  `_vendor/` directory, which raced with the build hook and real pip installs
  under parallel execution.
- **Releases are now fully automated** (aligned with the entrygraph release
  strategy): every merge to `main` runs the check suite, auto-bumps the patch
  version via a new `vX.Y.Z` git tag, and publishes both distributions to
  PyPI — no more release PRs or manual version edits. Package versions are
  derived from git tags by hatch-vcs (`dynamic = ["version"]`); the exact
  `sbxloop-worker==X.Y.Z` lockstep pin is injected into the host wheel
  metadata at build time. See [RELEASING.md](RELEASING.md).

### Fixed

- Artifact listings and delivery no longer silently drop dot-path artifacts
  (#67). `artifact_files` excluded every file with any dot-prefixed path
  component, so agent-produced `.github/workflows/*.yml`, `.gitignore`,
  `.env.example` and friends vanished from the run summary, `sbxloop artifacts`, and delivered PRs — with no indication anywhere. The exclusion
  is now a targeted denylist (`.git`, `.sbxloop` by default, matched as path
  components at any depth), tunable via `[artifacts] exclude` in config, and
  exclusions are always surfaced: the `run.artifacts` event carries per-entry
  excluded counts, the run summary and `sbxloop artifacts` print an
  "N file(s) excluded (…)" note, and delivery PRs list what was left out.
- The pinned status panel now shows the whole decomposed task list up
  front (#63). Previously a task's row only appeared when it started, so
  t2…tn were invisible until each prior task finished. The engine now
  announces the full roster (ids, titles, states) right after
  decomposition — and on resume, restoring each task's persisted state —
  and the TUI renders not-yet-started rows as `waiting` until their turn.
- Ctrl+C now finishes cleanly instead of surfacing tracebacks/`Aborted!`.
  Building on the #64 signal handlers and the #68 engine quiesce, the CLI
  handles the interrupt in both display modes: after the sandboxes are
  torn down it prints an `interrupted` notice and a `sbxloop resume RUN_ID` hint (interrupted runs stay resumable) and exits 130; a second
  Ctrl+C during teardown force-quits, deferring leftover sandboxes to
  `sbxloop sandbox prune`. The registry's `cleanup_all` additionally
  respects `--keep-sandboxes` pairs on abnormal exit instead of deleting
  sandboxes the run DB just marked as kept.
- **P4 papercut batch (#68):**
  - `--keep-sandboxes` is now tri-state
    (`--keep-sandboxes/--no-keep-sandboxes`, default "no override") like
    `--report`/`--deliver` already were, so a config-file
    `keep_sandboxes = true` can be forced off from the CLI.
  - `cancel` refuses runs already in a terminal state
    (`completed`/`failed`/`cancelled`) with a clear message instead of
    silently rewriting their recorded state to `cancelled`.
  - `logs --follow` no longer spins forever on a run whose driving process
    died hard (state stuck non-terminal): after `--stale-after` minutes
    (default 10; 0 follows forever) with no activity — no new events and no
    state change — it prints a note and exits.
  - Provisioning rollback now best-effort unregisters the secrets the
    failed attempt registered, symmetric with sandbox removal, so the next
    run starts clean instead of depending on collision-recovery
    scope-parsing heuristics against a registration owned by a
    now-deleted sandbox scope.
  - Ctrl-C in the TUI now signals the engine thread (via a new
    `LoopEngine.request_cancel()`, checked at the same phase boundaries as
    store-level cancellation) and joins it briefly before sandbox cleanup,
    instead of tearing sandboxes down under an engine still mid-`sbx exec`.
    The interrupted run's persisted state is untouched, so it stays
    resumable. Composed with the #64 signal handlers: the cleanup registry
    runs a driver-set quiesce callback before signal-triggered teardown,
    so SIGINT/SIGTERM stop the engine first, then remove the sandboxes.
  - The `Hook` protocol docstring no longer claims hooks "must not raise":
    the bus has always contained and logged subscriber exceptions, so hook
    authors need no defensive boilerplate (hooks should still be fast).
  - `status <run>` now prints the run's sandbox pair names with their
    current liveness per `sbx ls`, plus a `sbxloop shell` hint when one is
    running — no more reconstructing `sbxloop-<run>-agent` by hand.
- SIGTERM during a TUI-mode run no longer leaks the sandbox pair (#64). The
  TUI runs the engine on a background thread, and the cleanup registry's
  handler installer latched itself as "installed" *before* discovering it
  was off the main thread — so signal handlers were never installed and
  could never be installed later, and SIGTERM's default disposition kills
  the process without running the atexit hook. The latch now only sets
  after handlers actually install (later main-thread registrations retry),
  and the CLI explicitly installs the handlers from the main thread before
  handing the engine to the TUI's background thread. A TUI run receiving
  SIGTERM now stops and removes both sandboxes and exits 143; the lazy
  registration path remains as a fallback for library embedding.
- **Delivery now batches blob creation into O(1) worker jobs (#66).**
  `deliver_workspace` used to submit one `github.op` job per file — a full
  job cycle (`sbx cp` job JSON in, fresh worker process, `sbx cp` result
  out) per blob POST, so a 200-file workspace meant 200+ sequential job
  round trips and tens of minutes of delivery. A new `blobs.create_many`
  worker op receives the whole file manifest (base64-embedded in the job
  JSON) and performs the per-file blob POSTs inside the github sandbox,
  chunked only by a payload-size cap (4 MiB of base64 per job), with the
  job timeout scaled to the manifest size. The worker streams
  `gh.op_progress` events every 10 blobs so long deliveries stay visibly
  alive in the TUI, and a per-file failure names the failing file (and its
  position in the manifest) in the `run.deliver` error event. The e2e
  workflow gains a gated 50-file delivery smoke asserting the PR opens
  under a 120 s budget (`E2E_DELIVER_REPO` repository variable).
- The poll transport's event tailing is now binary-safe and its completion
  check parses events instead of substring-matching
  ([#65](https://github.com/brettbergin/sbxloop/issues/65)). Chunks are
  fetched base64-encoded and the byte offset advances by decoded byte count,
  so `\r\n` in worker output can no longer drift the offset (duplicating or
  dropping event lines), and a `tail -c` boundary that splits a multibyte
  UTF-8 character is held by an incremental decoder instead of crashing the
  host-side decode. Polling now ends only on a *parsed* `worker.end` event —
  an agent message whose payload merely contains the literal string
  `"worker.end"` no longer terminates the poll early.
- `resume` now runs under the config the run was started with (#60). The
  full config has always been persisted in the runs table, but resume drove
  with whatever `load_config()` produced at resume time — so editing
  config (or resuming from a different directory) silently changed budgets,
  model, or GitHub toggles mid-run, and could relocate the run into a
  fresh, empty workspace. Resume now rehydrates the persisted config
  (tokens still come from the current environment; `state_dir` stays where
  the run was found; the `keep_sandboxes`/`keep_on_failure` debug toggles
  stay resume-time choices so a crashing run can be resumed with keep
  flipped on), pins the workspace from the runs table instead of
  recomputing it, refuses a workspace mismatch, and reports any difference
  from the current on-disk config as a `run.config_drift` event.
- The fake sbx used in tests now scopes `pkill` to the sandbox's own
  processes, emulating the microVM boundary. A timeout kill's host-wide
  `pkill -f sbxloop_worker.*<job_id>` could TERM other tests' live worker
  processes under pytest-xdist (job ids repeat across tests), surfacing as
  spurious `exec rc=-15` / no-result failures in unrelated tests.
- Worker wheel builds no longer mutate the live source tree, removing the
  remaining parallel-test flakes and a version-skew hazard. Both
  `resolve_worker_wheel`'s workspace build and the packaging test built in
  place, which rewrote the packages' hatch-vcs `_version.py` files (raced
  by every worker subprocess import under pytest-xdist) and deleted/rebuilt
  the wheels in `src/sbxloop/_vendor/` mid-suite. Builds now run against a
  private temp copy with the version pinned to the host's
  (`SETUPTOOLS_SCM_PRETEND_VERSION`), which also guarantees the built wheel
  passes the install ladder's lockstep version check even when git HEAD has
  moved since the environment was last synced.
- A run resumed while a task was checkpointed `validating` no longer asks
  the VALIDATE judge to rule without evidence (#61). The verify-command
  transcript lived only in memory on the `PhaseRunner` (a class-level
  default of `"(verification not run)"`), so a fresh process entering
  VALIDATE rendered the prompt with the placeholder instead of the real
  results. VERIFY now persists its full command transcript on the
  `phase_attempts` row and VALIDATE reads it from there — the single source
  of truth for both fresh and resumed runs — and the class-level mutable
  state is gone. A checkpoint whose verify row predates this change (no
  stored transcript) rewinds to `verifying` on resume; VERIFY is mechanical
  and idempotent, so the evidence is cheaply repopulated.
- A delivery infrastructure failure can no longer mark a completed run as
  failed (#59). `--deliver` runs after the run has succeeded; worker- and
  sbx-level errors raised by the delivery op jobs (`WorkerError`,
  `WorkerTimeoutError`, `SbxError`) were escaping the delivery guard and
  leaving the run stuck in `finalizing`, reported as failed. Delivery now
  contains every sbxloop error, keeps the loud `run.deliver` event, and the
  run finishes `completed` as documented.
- GitHub progress reporting (`--report`) now actually reports (#58). The
  tracking issue was never created: the hook subscribed to run lifecycle
  events that are emitted before the github sandbox exists and after it is
  torn down. Run start/end are now explicit `open_run`/`close_run` calls
  made while the sandbox is alive; task-end comments flow via the bus as
  before, and the final summary posts before teardown. A resumed run
  re-finds its existing tracking issue instead of opening a duplicate.

### Security

- sbx infra failures can no longer silently downgrade the secret strategy
  (#63). The secret visibility probe only accepts a clean `test -n` answer
  (exit 0/1): an sbx-level failure or any other exit code is retried once
  and then **fails provisioning loudly** (with a distinct
  `sandbox.secret_probe_error` event) instead of being misread as "proxy
  secret invisible" and auto-writing the token into the VM. Supporting
  changes: `SbxCLI.exec` now classifies recognizable sbx-level stderr
  shapes (sandbox not running, daemon unreachable, transport failures) and
  raises `SbxError` for them instead of returning them as command results;
  mount discovery still degrades to harvest mode on a broken probe but its
  `sandbox.workspace_mount` event now distinguishes `probe="error"` from
  `probe="answered"` (and only clean answers refresh the conformance
  cache); `policy_check` raises on invocation failure rather than
  reporting infra trouble as "blocked", and `doctor` labels that case as a
  check error, not a policy verdict.
- **The read-only critic barrier is now allowlist + default-deny (#62).**
  SCRUTINIZE/VALIDATE sessions previously denied only `{"shell", "write"}`
  permission kinds and approved everything else — an unverified denylist,
  so an SDK rename or a new mutating kind would have silently handed the
  critic approve-all over the workspace it reviews. The barrier now allows
  only known-read kinds (`read`, `url`) and rejects everything else with
  feedback naming the denied kind, so unknown kinds fail closed (worst
  case: the critic loses a read capability and says so). The full `kind`
  vocabulary was field-verified against github-copilot-sdk 1.0.8 (`shell`,
  `write`, `read`, `mcp`, `url`, `memory`, `custom-tool`, `hook`,
  `extension-management`, `extension-permission-access`), and
  `sbxloop doctor` now compares the installed SDK's vocabulary against
  that snapshot, warning loudly on drift after an SDK bump.
- Secret values no longer leak through error text or the process-observable
  argv carried on `ExecResult`/`SbxError`: the value passed to
  `sbx secret set-custom --value` is masked (`***`) in every observable copy
  of the invocation, so provisioning failures cannot print the Copilot PAT
  into terminals, logs, or events (#57). The remaining `ps`-visibility of
  the live subprocess argv needs stdin support in sbx itself and stays
  tracked in #57.
- The `ps`-visibility half of #57 is now a doctor conformance probe
  (`secret-value-stdin`, cheap tier): desk-verified against the sbx docs and
  release history through v0.37, `sbx secret set-custom` accepts the secret
  only via `--token`/`--value` on argv (both documented as "less secure:
  visible in shell history") — no stdin path exists, so the exposure window
  cannot be closed from sbxloop's side yet. The probe checks
  `set-custom --help` on every `sbxloop doctor` run and alarms the moment an
  sbx upgrade grows a stdin path, naming the switch-over as the fix.
  `exec_interactive`'s missing-binary error now carries the redacted argv
  copy like every other observable path.

## [0.2.0] — 2026-07-23

### Changed

- **Project renamed: sdxloop → sbxloop — hard cutover, no compatibility
  layer.** The underlying Docker product is the `sbx` CLI; the name now
  matches. Everything renames with it: the distributions (`sbxloop`,
  `sbxloop-worker`), import names (`sbxloop`, `sbxloop_worker`), the CLI
  command (`sbxloop`), the env prefix (`SBXLOOP_*`), the config file
  (`sbxloop.toml` / `[tool.sbxloop]`), the state dir (`.sbxloop/`), and
  sandbox name prefixes (`sbxloop-<run>-*`). The old `sdxloop` /
  `sdxloop-worker` PyPI packages are frozen at 0.1.10 and will receive no
  further releases. Migration: `pip uninstall sdxloop sdxloop-worker && pip install sbxloop`, rename `SDXLOOP_*` env vars / `.env` entries and
  `sdxloop.toml`, and optionally rename `.sdxloop/` state dirs to
  `.sbxloop/` to keep old run history visible.

## [0.1.10] — 2026-07-23

### Changed

- The live run view is now a chat-style transcript: agent messages render
  as markdown panels (fenced \`\`\`json blocks syntax-highlighted and
  word-wrapped instead of truncated), errors as red panels, tool calls as
  compact colored lines, lifecycle events as dim one-liners. Streaming
  deltas, heartbeats, and stdout noise no longer flood the feed (they
  remain available via `sdxloop logs`). `--no-tui` prints the same
  chat-style entries sequentially.
- The secret-env fallback is one concise line and a single
  `sandbox.secret_env_fallback` event; explicit `plain-env` strategy no
  longer runs (and spuriously fails) the shell visibility check.

## [0.1.9] — 2026-07-23

### Fixed

- Runs work out of the box on real sbx: field testing confirmed the sbx
  secret proxy never exposes env vars to `sbx exec` processes (only to
  the interactive agent sessions sbx launches). Under the default proxy
  strategy, provisioning now auto-falls-back to the in-VM env file for
  any sandbox whose secret env is invisible, emitting a
  `sandbox.secret_env_fallback` warning about the tradeoff. If sbx later
  injects secrets into exec sessions, the verification passes and tokens
  stay out of the VM automatically.

## [0.1.8] — 2026-07-23

### Fixed

- Copilot session auth: the worker now runs under a login shell
  (`sh -lc`) so sbx-injected secret env vars reach it, and it also loads
  `/etc/sandbox-persistent.sh`. Provisioning verifies the secret env is
  visible in each sandbox and emits a `sandbox.secret_env_missing`
  warning (with a plain-env remediation hint) when it is not. Copilot
  session failures now append an auth diagnostic stating whether
  COPILOT_GITHUB_TOKEN is missing, well-formed, or looks like the sbx
  proxy sentinel (which the SDK's client-side token validation cannot
  use - switch to `secret_strategy = "plain-env"` in that case).

## [0.1.7] — 2026-07-23

### Fixed

- Job files staged into sandboxes are now world-readable: they were
  created 0600 by the host tempfile machinery and `sbx cp` preserves the
  mode, so the in-sandbox `agent` user could not read its own job files
  (Errno 13) and every job died before producing a result.

## [0.1.6] — 2026-07-23

### Fixed

- "produced no result file" failures are now diagnosable: the worker's
  stderr is drained (also fixing a potential pipe-deadlock for chatty
  workers) and the error carries the exec exit code, stderr tail, and
  the last lines of the in-sandbox events file.
- Worker installation now ends with an entrypoint smoke check:
  `python -m sdxloop_worker run` against a missing job must exit 64.
  Importing the package proves nothing about the entrypoint executing —
  broken entrypoints now fail at install time with a full traceback
  instead of as silent no-result jobs.

## [0.1.5] — 2026-07-23

### Fixed

- Worker wheels are staged into the sandbox under their canonical
  filename: pip validates the name-version-python-abi-platform structure
  of the wheel FILENAME and refused the previously renamed
  `sdxloop_worker.whl` ("Invalid wheel filename"). A new regression test
  runs real pip against the real wheel through the fake sbx.

## [0.1.4] — 2026-07-23

### Fixed

- Worker installation no longer dies when the sandbox template lacks
  python3-venv: it self-heals via `sudo apt-get install python3-venv python3-pip` and, failing that, falls back to a user-site pip install
  under the system python3 (handling PEP 668 externally-managed
  environments). Install/exec errors now include stdout as well as
  stderr — sbx exec surfaces some errors on stdout, which previously
  produced blank "rc=1" messages.

## [0.1.3] — 2026-07-23

### Fixed

- Secret provisioning now survives sbx's real conflict semantics: custom
  secrets are keyed by env name (one host per env), so the Copilot token
  binds to `api.github.com` only (the token-exchange host; the exchanged
  Copilot token lives in SDK memory). Exists-conflicts parse the owning
  scope out of sbx's error, try removal candidates from most to least
  specific, and NEVER fail provisioning — worst case the existing value
  is kept with a warning.

## [0.1.2] — 2026-07-23

### Fixed

- Provisioning no longer fails with "secret exists" on re-runs and resumes:
  sbx refuses to overwrite existing secrets, so the provisioner now removes
  and re-sets them (rotated tokens take effect). When removal is rejected,
  the existing value is kept with a warning instead of failing the run.

## [0.1.1] — 2026-07-23

### Changed

- `app_name` now defaults to empty: sdxloop shares the user's normal sbx
  application state, so `sbx login` and `sbx policy init balanced` apply
  directly. Isolation via `--app-name` is opt-in (and documented to need
  its own login/policy init). Previously the default isolated state
  silently triggered Docker's browser login on first `sdxloop doctor`.
- `sdxloop doctor` prints progress lines for slow checks (including a
  heads-up that Docker may open a browser for auth) and sanitizes
  multi-line sbx error output so the results table renders cleanly.

### Added

- `.env` support: the CLI and `LoopEngine` automatically load `./.env`
  (via python-dotenv) for the two PATs and `SDXLOOP_*` settings. Real
  environment variables always take precedence, and explicit `env=`
  mappings passed to `load_config` stay hermetic. A documented
  `.env.example` ships in the repo.

## [0.1.0] — 2026-07-22

Initial release.

- **Sandbox-pair primitive**: every run provisions an agent sandbox
  (`COPILOT_GITHUB_TOKEN` only, proxied to the Copilot API hosts) and a
  github-ops sandbox (`GH_TOKEN` only, built-in secret service), both under
  the balanced network policy with per-role allow rules, guaranteed cleanup
  (context manager + atexit/signal registry), and `--app-name` isolation.
- **Loop engine**: DECOMPOSE → PLAN → EXECUTE → SCRUTINIZE → VERIFY →
  VALIDATE with revision/replan budgets, read-only critic sessions,
  mechanical verification, SQLite checkpointing after every transition,
  crash-safe `resume`, wall-clock budgets, and cancellation.
- **Worker runtime** (`sdxloop-worker`): file-based job protocol with JSONL
  event streaming, GitHub Copilot SDK backend (lazy `[copilot]` extra),
  deterministic echo backend for testing, GitHub ops via gh CLI or
  pure-stdlib REST.
- **Worker delivery**: the host wheel embeds the worker wheel, so sandbox
  provisioning works before/without PyPI.
- **CLI**: `run` (rich live TUI), `resume`, `cancel`, `status`, `logs`,
  `sandbox ls/rm`, `config show`, `init`, `doctor`.
- **CI/CD**: ruff format+lint, mypy strict, pytest with 85% coverage gate
  (3.11–3.13), build with vendored-wheel assertion, PyPI Trusted Publishing
  on tags, manually-dispatched real-sbx e2e workflow.
