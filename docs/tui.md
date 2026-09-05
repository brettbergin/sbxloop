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
- **Control.** The `ctl` file queue under `state_dir/daemon/ctl/` —
  `sbxloop.daemon.control.ControlClient`, the same dispatcher `sbxloop daemon ctl` and chat's `!sbx` use, so the console cannot drift from them.
  `status` is asked every few seconds; a `None` answer is "daemon down",
  a `stale` one is "daemon starting".
- **Chat.** The daemon's local chat bridge (`sbxloop.daemon.local`): a
  third `ChatBridge` whose transport is the `daemon_local_messages` table.
  Every message the bridge would post becomes a row; what the operator types
  is a row the bridge claims. See *Chat* below.

The state directory follows the daemon's own rule (`[daemon] state_dir`,
else the anchored XDG default); `--state-dir` overrides it. `--run RUN`
opens that run's screen at once; `--read-only` removes every action.

## Layout and navigation

```
┌────────────────────────────────────────────────────────────────────────────┐
│ ● running  r7ab3kq2m · Add retries   queue 2   runs 4/12 UTC   failures 0  │
│ sbxloop 1.4.2   bridge ✓   up since 2d ago   ctl 12 ms   14:02:11          │
├────────────────────────────────────────────────────────────────────────────┤
│                            <active screen>                                 │
├────────────────────────────────────────────────────────────────────────────┤
│ 1 Overview  2 Runs  3 Queue  ? Help  r Refresh  q Quit                     │
└────────────────────────────────────────────────────────────────────────────┘
```

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
| `1` `2` `3`       | anywhere                    | Overview, Runs, Queue                                              |
| `?`               | anywhere                    | Help                                                               |
| `r`               | anywhere                    | refresh now (store and `ctl status`)                               |
| `q`               | anywhere                    | quit                                                               |
| `j`/`k`, arrows   | any list                    | move                                                               |
| `g`/`G`           | any list                    | first / last row                                                   |
| `ctrl+d`/`ctrl+u` | any list                    | page                                                               |
| `/`               | Runs, Events tab            | filter (Runs: any column; Events: a type prefix such as `policy.`) |
| `Esc`             | anywhere                    | clear a filter, close a run                                        |
| `Enter`           | Runs, Queue, Overview lists | open the run                                                       |
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

*Lands with the console's chat screens.* The control channel (the
concierge, `!sbx` verbs, daemon notices) and each run's thread are the local
bridge's rows. Addressing the bot is the literal `@sbx` token (the console
inserts it with `ctrl+t`) or a reply to one of its rows — the two gestures
the routing rules already understand; plain text is left alone, as on
Discord. Clicking a choice or the approve button writes a `choice` /
`approve` row under the question or prompt.

## Administration

*Lands with the console's admin screens.* Sandboxes, daemon process
control (the systemd user unit named by `[tui] daemon_unit`), the journal,
run operations, config editing with validation, secrets and doctor.

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
