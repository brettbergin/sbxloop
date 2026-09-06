# The operator console: `sbxloop tui`

`sbxloop tui` is a terminal console run **on the daemon host**. It gives an
operator everything the CLI and journald give, in one place, live — and the
same experience a Discord or Slack channel gets: the run headline and thread,
the status line and tool digest edited in place, steering, the concierge,
clarifying-choice buttons and the merge-gate approve button. Operators on the
host are trusted completely; the console has no authority model of its own.

## What it reads and how it drives the daemon

- **State.** The daemon's `state.db`, through
  `sbxloop.daemon.mailbox.MailboxClient`: a read-only SQLite handle (no
  schema statement ever runs from the console, so it never migrates a store
  under a running daemon). Runs, tasks, phase attempts, the event tail by
  `seq`, work items, merge gates, review holds, the breaker and the local
  bridge's mailbox all come from there.
- **Control.** The `ctl` file queue under the home's `state/daemon/ctl/` —
  `sbxloop.daemon.control.ControlClient`, the same dispatcher `sbxloop daemon ctl` and chat's `!sbx` use, so the console cannot drift from them.
  `status` is asked every few seconds; a `None` answer is "daemon down",
  a `stale` one is "daemon starting".
- **Chat.** The daemon's local chat bridge (`sbxloop.daemon.local`): a
  third `ChatBridge` whose transport is the `daemon_local_messages` table.
  Every message the bridge would post becomes a row; what the operator types
  is a row the bridge claims. See *Chat* below.

The console reads the same home as the daemon (`~/.sbxloop`, or
`SBXLOOP_HOME`), so it needs no flag to find the state. `--run RUN`
opens that run's screen at once; `--read-only` removes every action.

## Layout and navigation

```
┌─────────────┬──────────────────────────────────────────────────────────────┐
│             │ ● running  r7ab3kq2m · Add retries   queue 2   runs 4/12 UTC  │
│ 1 Overview  │ sbxloop 1.5.2   bridge ✓   up since 2d ago   ctl 12 ms        │
│ 2 Runs      ├──────────────────────────────────────────────────────────────┤
│ 3 Queue   2 │                                                              │
│ 4 Chat      │                     <active screen>                          │
│ 5 Sandboxes │                                                              │
│ 6 Daemon  1 │                                                              │
│ 7 Config    │                                                              │
│ 8 Doctor    ├──────────────────────────────────────────────────────────────┤
│ ? Help      │ / Filter  r Refresh  q Quit                     ^p palette    │
└─────────────┴──────────────────────────────────────────────────────────────┘
```

The **navigation rail** runs down the left of every screen: every screen
with the key that reaches it, the one you are on marked, and a badge where
a screen you are *not* on wants attention — the queue's depth, unread
control-channel rows, gates and holds waiting for a human. Clicking a row
is the same verb as pressing its key. It is the console's map; the footer
keeps its row for the verbs of whichever screen is up. Below 90 columns the
rail hides itself and the keys still reach everything.

`sbxloop.tui.widgets.navrail.NAV` is the single source of the console's
shape — the rail renders it and the app builds its bindings from it, so a
screen cannot be reachable by key and missing from the map.

The two-line **status bar** is on every screen: the daemon (`●` running,
`◌` idle, `⏸` paused with its holds, `🛑` breaker open, `…` starting, `✖`
down), the run in flight, the queue depth, the calendar-day cap, consecutive
failures, the bridge's liveness (its heartbeat in `daemon_state`), uptime,
the ctl round trip, and the clock. `[tui] emoji = false` swaps the glyphs
for ASCII.

Screens are **modes**: each keeps its cursor and scroll when you jump away
and back. A run opens as a pushed screen over the one you were on; `Esc`
returns.

| key               | where                       | what                                                               |
| ----------------- | --------------------------- | ------------------------------------------------------------------ |
| `1` … `8`         | anywhere                    | Overview, Runs, Queue, Chat, Sandboxes, Daemon, Config, Doctor     |
| click             | the rail                    | the same as that row's key                                         |
| `?`               | anywhere                    | Help                                                               |
| `ctrl+p`          | anywhere                    | the command palette: screens and argument-less verbs by name       |
| `r`               | anywhere                    | refresh now (store and `ctl status`)                               |
| `q`               | anywhere                    | quit                                                               |
| `j`/`k`, arrows   | any list                    | move                                                               |
| `g`/`G`           | any list                    | first / last row                                                   |
| `ctrl+d`/`ctrl+u` | any list                    | page                                                               |
| `/`               | Runs, Events tab            | filter (Runs: any column; Events: a type prefix such as `policy.`) |
| `Esc`             | anywhere                    | clear a filter, close a run                                        |
| `Enter`           | Runs, Queue, Overview lists | open the run                                                       |
| `Enter`           | Config, Resolved tab        | edit that setting (`e` too); `a` adds one by dotted path           |
| `f`               | a run                       | toggle following the event tail                                    |
| `v`               | a run                       | Thread tab as the `sbxloop run` transcript or as dense lines       |

### Overview

The run in flight (state, stage, title, PR, rounds, last-event age), the
queue in dispatch order, who is **waiting on a human** (open merge gates,
review holds), the most recent runs, and the daemon's own answer to
`status` when it is up. With no daemon answering, history stays browsable.

### Runs and a run

Runs lists every run newest first: id, state (the reason dimmed after it,
as `sbxloop status` prints it), stage, item, repository, title, PR, review
and CI rounds, last update. A run has six tabs:

- **Thread** — the run's transcript: the same renderers `sbxloop run --tui` uses (agent messages as Markdown panels, tool calls as lines,
  failed calls with their excerpt), tailed from the persisted events.
- **Tasks** — the roster with state, revisions/replans, the verify-suspect
  flag and the last feedback.
- **Phases** — every phase attempt with status, start, duration, input and
  output tokens, cache reads/writes and turns, with totals. Spend is never
  rendered as a currency: the agent backend reports tokens, not money.
- **Landing** — the PR, branch and head, the stage, the round counters, the
  last verdict, the item's attempts and last error, an open merge gate or
  review hold, and the newest of each landing event (`review.verdict`,
  `ci.status`, `land.*`, `run.gated`, `run.blocked`, …).
- **Artifacts** — the run's artifact directory as a tree.
- **Events** — the dense one-line form `sbxloop logs` prints, following the
  tail; `/` narrows to a type prefix.

### Queue

What the daemon will dispatch next (with the backoff's `not before`) and
every work item with its repository, state, attempts, pinned run, title,
last error and last update. `Enter` opens the item's run.

## Chat

The Chat screen (`4`) is the control channel and a run's **Thread** tab is
that run's thread — both are the daemon's local chat bridge's rows, the
same rows Discord or Slack would show: the headline card, the status line
and tool digest edited in place, agent messages as Markdown, notices, the
concierge's replies with its `🛠 concierge: …` tool line, clarifying
questions, the merge-gate prompt.

```
┌ control channel ───────────────────────────────────────────────────────────┐
│ 13:40:02  brett   what's running?                                          │
│ 13:40:05  sbx     r7ab3kq2m — Add retries… is on task 2 of 5     (edited)  │
│           🛠 concierge: sbx_control(status) · run_detail(r7ab3kq2m)        │
│ 13:45:11  sbx     Is this about the wording, the layout, or the timing?    │
│           [ 1 Layout ]  [ 2 Timing ]                                       │
│ 13:55:40  sbx     ⏸ ready to merge — waiting for your approval             │
│           [ Approve merge ]                                                │
├────────────────────────────────────────────────────────────────────────────┤
│ @sbx ▸ ask the concierge…  (ctrl+t: addressed ✓ · !sbx for commands)       │
└────────────────────────────────────────────────────────────────────────────┘
```

The routing rules are the bridge's, not the console's:

| you type                                                                        | the daemon reads it as                                                     |
| ------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `!sbx status` (any `!sbx` verb)                                                 | a command — the same dispatcher `sbxloop daemon ctl` uses                  |
| `@sbx …`, or anything with the address gesture on (`ctrl+t`, sticky per screen) | in the control channel a **concierge** turn; in a run's thread a **steer** |
| `r`, then text                                                                  | a reply to the bot's latest row — addressed by definition                  |
| plain text                                                                      | left alone, as on Discord: people talking among themselves                 |

`Esc` leaves the form (a reply target is cleared first) and `i` returns to
it; while the form is focused every key types, so `q` and the mode numbers
act after `Esc`. A question with enumerable answers shows a button per
answer; on the Chat screen with the form left, `1`–`5` pick without the
mouse (with no question open the numbers are the mode keys again) and `r`
replies to the bot's latest row. In a run's thread, click the button or
type the number. Typing the answer works too. A long channel opens on its newest rows, with a note counting the
older ones the daemon still keeps. A merge gate shows
**Approve merge** while the gate stands; `!sbx merge <item>` is its typed
twin. Your own rows show dimmed until the daemon claims them; a row typed
while no daemon was reading is refused with a note, never executed. Edits,
reactions (`⏳` → `✅` under a steer) and resolved gates repaint in place.
The console speaks as `[tui] operator_id` (the login name by default), and
the bar counts unread control-channel rows while you are elsewhere.
`--read-only` disables the form and the buttons.

## Administration

Every admin verb goes through one path: refused under `--read-only`
(sandbox shells included), refused without a live daemon when the daemon
must execute it, confirmed by its tier, run off the UI thread, reported as
a toast (or a screen when the output is long), then the screen re-polls.
Two confirmation tiers, and a few verbs that just run (resuming the
daemon or a hold, re-checking a review, resuming a repository, asking the
concierge):

- **`y`/`n`** for a bounded verb — pause, cancel, retry, requeue, approve
  a merge, grant rounds, start the unit, stop a sandbox.
- **Typed** for a destructive one — the target's name or the verb, exactly:
  `stop` (graceful daemon stop), the unit name (stop / restart the unit),
  the item id (abandon), the sandbox name (remove), `prune`, `gc`,
  `upgrade`.

What the daemon executes travels over the `ctl` queue, attributed as
`<operator> via sbxloop tui` (the issue reads "cancelled by brett via
sbxloop tui"). The item verbs have the CLI's row-only twin when no daemon
is running: the store row changes, and the next daemon start reports it to
the source and closes the dead run — the same note `sbxloop daemon retry`
prints. While the daemon is *starting* they wait.

### A run's verbs

| situation                       | offered                                                  | mechanism                                                                                      |
| ------------------------------- | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| the daemon's current run        | `c` cancel · `C` cancel and retry                        | `ctl cancel [--retry]`: settled on the item, the issue told                                    |
| any other in-flight run         | `c` cancel                                               | the `sbxloop cancel` store write: cancelled at its next phase boundary                         |
| a run pinned to an item         | `R` retry · `u` requeue · `A` abandon · `w` check review | `ctl retry / requeue / abandon / resume <item>` when live; the row-only twin when down         |
| a gated run                     | `m` approve merge                                        | `ctl merge <item>`; the Thread tab's **Approve merge** button is the chat twin                 |
| a held workload result          | `m` release                                              | `ctl release <item>`; the Thread tab's **Release result** button is the chat twin              |
| a run that exhausted its rounds | `+` grant rounds                                         | `ctl grant-rounds <run> <n>`: more fix rounds, resumed now                                     |
| an unfinished run with no item  | `R` resume here                                          | a detached `sbxloop resume RUN --no-tui --no-chat` in its own session, log under the state dir |
| any run                         | `s` / `S` shell                                          | `sbx exec` into the agent / github sandbox with the terminal handed over                       |

A run is never resumed *inside* the console: that would tie it to the
console's lifetime.

### A new run

On the Queue screen `n` asks for an outcome and posts it to the control
channel addressed to the concierge, which files the issue with the trigger
label; the daemon claims it like any labeled issue. That is the daemon's
way to a run — a human asks, the daemon never files work for itself. `N`
instead starts a detached `sbxloop run "…" --no-tui --no-chat` on this
host, outside the daemon.

### Sandboxes (`5`)

`sbx ls`, every `sbxloop-*` sandbox classified against this store the way
`sbxloop sandbox prune` classifies it (role, run, run state, age, verdict;
the daemon's own github-ops and concierge boxes as role `daemon`, never
pruned), and the run directories the daemon's daily sweep would remove, as
a dry run with sizes. `s` opens a shell in the selected sandbox (the
console suspends, the shell gets the terminal, the console returns when it
exits), `x` removes one (typed name; a run sandbox takes its secret
registrations with it), `X` stops one, `P` prunes every orphan (typed
`prune`; the orphans are classified again as the removal runs, so a run
resumed since the last poll keeps its boxes), `G` removes the prunable
run directories (typed `gc`; run rows stay — the audit trail is never
removed; `[daemon] prune_runs_after_days = 0` disables this as it does
the daemon's sweep), `k` includes kept-for-debugging sandboxes in the
orphan verdicts (the prompt says so, and their kept marker is cleared).

### Daemon (`6`)

```
┌ process ─────────────────────────────┐┌ versions ─────────────────────────┐
│ unit     active (running) · pid 4242 ││ sbxloop   1.4.2 installed · 1.4.5 │
│ daemon   pid 4242 · up 2d · 1.4.2    ││ on PyPI · BEHIND by 3 releases    │
│ current  r7ab3kq2m — Add retries     ││ sbxloop-worker 1.4.2 …            │
│ holds    none · breaker closed       ││ sbx CLI   0.38.1                  │
│ cap      4/12 runs today (UTC)       ││ checked 12m ago                   │
└──────────────────────────────────────┘└───────────────────────────────────┘
┌ repositories ────────────────────────┐┌ waiting on a human ───────────────┐
│ o/r         ok                       ││ ⏸ gh:issue:39 ready to merge · 2h │
│ o/other     suspended  token expired ││ 👀 gh:issue:37 awaiting_review    │
└──────────────────────────────────────┘└───────────────────────────────────┘
┌ journalctl --user -u sbxloop-daemon · level ≥ info · grep '' · follow on ─┐
│ 2026-09-05T14:01:58+0000 … [info     ] daemon.tick queued=2 …              │
└───────────────────────────────────────────────────────────────────────────┘
```

- **The unit.** `systemctl --user show` on `[tui] daemon_unit` (default
  `sbxloop-daemon`, the name `contrib/systemd/` ships; `--unit` overrides)
  every 15 s. `S` starts it, `T` stops it and `B` restarts it (typed unit
  name: the run in flight is interrupted, resumable by design). A host
  without systemd, or without a user bus in this session (a bare ssh
  login — see [deploy.md](deploy.md)), reads as "no systemd here"; a host
  without the unit reads as "no unit".
- **The process.** What `ctl status` says: pid, uptime, version, the
  current run, holds, the breaker, the day cap, and whether a graceful
  stop is under way. `p` pauses, `u` resumes, `a` releases every hold,
  `c`/`C` cancel the current run (and retry), `g` asks for a graceful stop
  (typed `stop`: claim nothing new, finish the run, exit — under systemd
  the unit restarts it; `T` stops it for good).
- **No unit? Spawn one.** `D` starts `sbxloop daemon` from the console in
  its own session, reading this directory's config, its output in
  `<state dir>/console/daemon.log` (which the journal pane then tails).
  It outlives the console, on the console's state dir. `e` stops it
  (SIGTERM: nothing new is claimed, the run in flight is interrupted at
  its next boundary and stays resumable, the process exits), and quitting
  asks whether to.
- **Versions.** The same report the concierge's `versions` tool gives
  (installed, latest on PyPI unless `[daemon] version_check = false`, the
  sbx CLI), refreshed hourly. `U` runs `[daemon] upgrade_command` in a
  login shell, verbatim — the text the drift notice tells an operator to
  paste — (typed `upgrade`) and shows its output; the daemon keeps running
  the code it started with until restarted.
- **Repositories.** Per-repository polling health from `status`; `R`
  resumes a suspended one.
- **The journal.** `journalctl --user -u <unit> -n 200 -f -o short-iso`,
  streamed, every line through the credential redactor. `/` greps, `l`
  cycles the level floor (lines without a level — a traceback, or every
  line under `[daemon] log_format = "json"` — always pass), `f` toggles
  follow. The stream and the polls stop while another screen is shown. The `!sbx log` verb in chat is the on-demand
  twin from the daemon's own ring buffer.

### Config (`7`)

Three tabs, and every change made from the first of them one key at a
time. **There is no text editor here.** Handing the operator a file and
leaving them to find the line is what this screen replaced; for a change
no key describes, edit `~/.sbxloop/config/sbxloop.toml` on the host.

**Which file an edit lands in:** the home's `config/sbxloop.toml` — what
`sbxloop init` writes, what a deploy preserves and what `sbxloop backup`
snapshots. Its path is the first line of the Resolved tab. The loader
reads it out of the home whatever directory anything was started in, so
the console writes the same file the daemon reads no matter where either
was launched. A `sbxloop.toml` in a working directory is *project* config
a repository carries; the console shows it as a layer but never writes to
it. If one is sitting in the home itself the loader applies it **over** the
operator config — the screen names it so the split is visible, and per-key
edits say when that file still wins.

- **Resolved** — every setting as one addressable key with its value and
  the layer that set it (home config, `pyproject.toml`, `sbxloop.toml`,
  env, default), as `sbxloop config show` prints; `/` filters keys, values
  and sources. It is resolved **from the home**, not from the directory the
  console was started in: that is where the daemon runs, so this is the
  configuration the loop actually gets, and it is the same root an edit is
  validated against — a save shows up here at once. Arrays of tables are
  walked, so the second repository is `github.repos[1].deliver_base` rather
  than one blob you have to find in a file, and a leaf inherits the layer
  that supplied the array it lives in. Lists of scalars (`policy.allow`)
  stay one key: the useful edit there is the whole list.

- **Editing one key.** `Enter` on a row — or `e` — opens that setting on
  its own: what it accepts (a type, the set a `Literal` allows, the bounds
  the model carries), what it holds now and which layer is answering, and
  the file the answer is written to. The widget follows the type — a
  picker for a bool or a fixed set, one item per line for a list, a line of
  text otherwise — so a string needs no quotes and a bad value is named
  before the loader sees it. `^U` unsets the key instead, so the file stops
  saying anything about it and the layer beneath answers.

  `Enter` (a picker: `^S`) applies. The edit starts from the file as it is
  on disk — there is no buffered draft to go stale — and the value is
  written at that path and nowhere else, **every comment in the file
  kept**. The whole result then goes through the real loader, and only a
  file it accepts is saved: the write is atomic, the previous file is kept
  beside it as `sbxloop.toml.bak-<stamp>`, and a restart of the unit is
  offered, since the daemon reads its configuration only at start. A file
  the loader refuses is never written. The dialog is the confirmation, so
  nothing else is asked. A key the environment or a `sbxloop.toml` in the
  home also sets is still written and the verdict says so, naming the layer
  that wins and the value the loop actually sees. `--read-only` refuses
  every edit.

- **Adding a key.** `a` takes a dotted path the resolved view has no row
  for — `sandbox.env.RAILS_ENV`, `github.repos[2].repo` — and opens the
  same dialog. An index one past the end appends an entry; an index beyond
  that is refused by name.

- **Policy** — the effective per-phase egress policy `sbxloop config policy`
  prints, from the same fold (`sbxloop.cli.policyview.policy_view`).

- **Repos** — the configured repositories as `sbxloop config repos` lists
  them. `Enter` on a repository narrows the Resolved view to that entry's
  keys.

### Doctor (`8`) and secrets

`d` runs what `sbxloop doctor` runs — the host checks and the cheap sbx
conformance probes — in the background with its progress line, and shows
the two tables with the verdict (ready / not ready, warnings, drift) and
the age of the check. `D` runs the live probes (boots a scratch sandbox)
and `p` asks GitHub about each configured repository from a github-ops
sandbox; both ask first. The data is `sbxloop.cli.doctor.doctor_report`,
the same the CLI renders.

`S` opens the **secret registrations**: the tracked custom secrets as
`sbxloop secrets list` judges them (expected, actual, status, note). `x`
cleans the stale ones — a dry run first, then typed `clean` — and `X`
every sbxloop-owned one; `K` rotates the agent credential's registration
from a hidden prompt (typed `rotate`): the sbx registration is replaced
with a global one on the canonical host, live sandboxes that may still
hold the old token are named, and the sandbox-booting visibility check
stays with `sbxloop secrets rotate --verify`. The token is never an
argument and never logged.

### The command palette

`ctrl+p` lists every screen (Secrets included) and every argument-less
verb (pause, resume, cancel the current run, stop the daemon, start / stop
/ restart the unit, spawn a daemon, upgrade) by name. Verbs with a target
live on their rows.

## Configuration

`[tui]` in `sbxloop.toml` — always on, nothing to enable:

| key              | default          | what                                                                                                         |
| ---------------- | ---------------- | ------------------------------------------------------------------------------------------------------------ |
| `operator_id`    | `""`             | who the console speaks as; empty means the login name                                                        |
| `emoji`          | `true`           | glyph markers (false: ASCII)                                                                                 |
| `daemon_unit`    | `sbxloop-daemon` | the systemd `--user` unit the console tails and restarts                                                     |
| `refresh_s`      | `0.5`            | how often a live screen re-reads the store                                                                   |
| `retention_days` | `14`             | how long the daemon keeps the console's mailbox rows (`0` keeps them; an open gate's prompt is never pruned) |

The rendering knobs (`command_prefix`, `thread_per_run`, `chronology_level`,
`max_message_chars`, `embeds`, `status_line`, `tool_batch_lines`,
`tool_output_lines`, `tool_fail_output_lines`) are the ones `[discord]` and
`[slack]` share.
