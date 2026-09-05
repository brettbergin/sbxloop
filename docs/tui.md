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
