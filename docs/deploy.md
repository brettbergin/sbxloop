# Running the daemon as a service and upgrading it

This is the generic guide: install `sbxloop daemon` under systemd, upgrade it by hand, and
— optionally — let a GitHub Actions workflow on the host keep it current from PyPI. Nothing
here is specific to any one host or repository. How this repository deploys its *own*
daemon is a separate reference: [docs/self-deploy.md](self-deploy.md).

## The one rule: never restart under a live run

A restart kills the in-flight run's sandboxes. The run stays resumable and is picked up on
the next start, but the resume spends one of the item's resume budget and everything the
run had done since its last task boundary. Every upgrade path below therefore takes a named
pause hold, waits for the daemon to be idle, and only then installs and restarts.

`sbxloop daemon ctl status --json` is what scripts read for that. It prints one JSON object:

```json
{"current": null, "claiming": null, "holds": ["deploy-12"], "paused": true, "queued": 2, ...}
```

- `current` is the running item (`{item_id, run_id, title}`) or `null`.
- `claiming` is an item whose claim is in progress — busy for this purpose, so a restart is
  never timed into the window between the claim comment landing and the claim being
  persisted.
- `holds` is the set of named pause holds; `paused` is whether any stand. Holds are
  in-memory only: **every restart comes back unpaused**, so an operator's hold has to be
  snapshotted before the restart and re-taken after it.

The prose `ctl status` is for people and may change; the JSON is for scripts. Exit codes:
`0` answered, `1` answered but pending or without a structured status (a daemon older than
this flag), `2` no daemon answered — the last one means there is nothing to drain. A
`status` call mutates the circuit breaker, so poll no faster than every 15 s.

## Install

[contrib/systemd/README.md](../contrib/systemd/README.md) walks through it. The layout it
sets up, all relative to the service user's home, is what every upgrade path assumes:

|                   |                                                                                       |
| ----------------- | ------------------------------------------------------------------------------------- |
| Interpreter       | a venv at `~/.sbxloop-venv`, with `sbxloop[discord,slack]` and `sbxloop-worker` in it |
| Command           | `~/.local/bin/sbxloop` — a symlink or wrapper for `~/.sbxloop-venv/bin/sbxloop`       |
| Working directory | `~/sbxloop-runner` — `sbxloop.toml`, `.env` (tokens, mode 0600), the workspace clone  |
| Service           | user unit `~/.config/systemd/user/sbxloop-daemon.service`                             |
| Sandbox backend   | user unit `sbx-sandboxd.service`, which the daemon unit `Requires=`                   |

`ctl` resolves the config and `state_dir` from the current directory, so every `ctl` and
`notify` call runs from the working directory. Installing both chat extras makes
`[chat] backend` a config change, not a reinstall.

## Upgrading by hand

Two commands as the service user, once the daemon is idle:

```bash
cd ~/sbxloop-runner
sbxloop daemon ctl pause --hold upgrade
until [ "$({ sbxloop daemon ctl status --json 2>/dev/null || echo '{}'; } | jq -r '.current // .claiming // "idle"')" = idle ]; do sleep 15; done

~/.sbxloop-venv/bin/pip install --upgrade 'sbxloop[discord,slack]==X.Y.Z' 'sbxloop-worker==X.Y.Z'
systemctl --user reset-failed sbxloop-daemon && systemctl --user restart sbxloop-daemon
```

`reset-failed` matters: `StartLimitBurst=5` per 600 s leaves a unit that crash-looped in
`failed`, where a plain `restart` will not revive it. The daemon comes back unpaused (holds
are in-memory), so re-take any hold you want to keep. Pin the version exactly — a
downgrade is the same two commands with an older `X.Y.Z`. Then check it:

```bash
systemctl --user is-active sbxloop-daemon
sbxloop --version
sbxloop doctor                       # never --deep or --probe here; those boot microVMs
sbxloop daemon ctl status --json     # exit 2 = no daemon came up
```

## Upgrading automatically

[contrib/workflows/deploy-daemon.yml.example](../contrib/workflows/deploy-daemon.yml.example)
is the by-hand procedure as a workflow, plus rollback. Copy it to
`.github/workflows/deploy-daemon.yml` in the repository that owns the host. It needs a
self-hosted Actions runner on the host, running as the service user, and takes **no**
checkout — it installs from PyPI and needs nothing from the tree.

```
schedule / workflow_dispatch → self-hosted runner on the daemon host
                                ├─ compare PyPI's latest (or the named version) with what is installed
                                ├─ take a named pause hold (deploy-<run id>)
                                ├─ wait — no cap — for the in-flight run to finish
                                ├─ pip install the exact version
                                ├─ snapshot the operator's holds; restart the unit
                                ├─ health check, or roll back to the previous version
                                └─ restore the other holds; release its own; tell the control channel
```

Step by step:

1. **Resolves the version** — the latest on PyPI, or the `workflow_dispatch` input — and
   **short-circuits** if the host already runs it, so the schedule costs nothing when there
   is nothing to do.
2. **Takes a hold and waits for idle**, polling `ctl status --json` every 15 s with no cap
   short of the job's `timeout-minutes`. A timeout installs nothing: the hold is released and
   the daemon runs on as it was. A daemon that answers nothing for five minutes straight has
   nothing to drain and the job proceeds. To make a deploy go now, `ctl cancel` the run (it
   stays resumable; `cancel --retry` re-queues it fresh).
3. **Upgrades** with both distributions pinned to the same version.
4. **Restarts** after `systemctl --user reset-failed`, having first snapshotted the standing
   holds — immediately before the restart, not at the start of the job, so an operator who
   paused *during* the wait is still paused afterwards.
5. **Health-checks**: unit active, `--version` matches, `sbxloop doctor` exits 0, the daemon
   answers `ctl status --json`, then a 45 s settle to prove it is not crash-looping.
6. **Rolls back** to the previously installed version on any failed check, restarts, and
   fails the job. Rollback only runs once the upgrade step has — a failure before that
   changed nothing on the host, and a rollback restart would be the needless restart this
   whole procedure exists to avoid.
7. **Restores the other holds** (after a rollback too — whatever version is live, operator
   intent survives), **releases its own** on `always()`, and **reports** with
   `sbxloop daemon notify`, including how long it waited and whether a failure happened
   before anything was installed.

Two settings, and no names in the file:

- The repository variable **`SBXLOOP_DEPLOY_HOST`** is the runner label the job targets
  (`runs-on: [self-hosted, "${{ vars.SBXLOOP_DEPLOY_HOST }}"]`) and the name the notices
  call the host. Moving the daemon to another host is registering a runner there with that
  label — or changing the variable; the workflow file does not change.
- The **`schedule`** is how often the host checks PyPI.

Every path is derived from `$HOME` in the job's first step (job-level `env:` values are
literals — GitHub does not expand `${HOME}` there), so a host that follows the layout above
needs no edits.

### `sbxloop daemon notify`

Posts one message to the control channel through the configured `[chat] backend`, from the
host and without the daemon — so a script can say "rollback also failed" while the daemon
is down. It reads the channel from `sbxloop.toml` and the bot token from the environment
(`DISCORD_BOT_TOKEN` or `SLACK_BOT_TOKEN`, from the working directory's `.env` if present),
so the workflow never sources a secrets file or parses the config itself. The text is the
chat's Markdown; on Slack it is re-dialected the way the bridge does it. Link previews and
pings are suppressed. A headless daemon (no chat backend) cannot notify, and says so.

### The runner

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -fsSLO https://github.com/actions/runner/releases/download/v<X>/actions-runner-linux-x64-<X>.tar.gz
echo "<sha256>  actions-runner-linux-x64-<X>.tar.gz" | sha256sum -c -
tar xzf actions-runner-linux-x64-<X>.tar.gz

# registration tokens expire in an hour; mint one at use time
./config.sh --url https://github.com/<owner>/<repo> \
  --token "$(gh api -X POST repos/<owner>/<repo>/actions/runners/registration-token --jq .token)" \
  --name <host> --labels <host> --work _work --unattended --replace

cp contrib/systemd/github-runner.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now github-runner
loginctl enable-linger "$USER"
```

`self-hosted`, `Linux` and `X64` are added automatically; `--labels <host>` is the one
`SBXLOOP_DEPLOY_HOST` must match. The runner is a *user* unit
([contrib/systemd/github-runner.service](../contrib/systemd/github-runner.service)) rather
than GitHub's `svc.sh` system unit: a system service has no `XDG_RUNTIME_DIR` or
`DBUS_SESSION_BUS_ADDRESS`, so `systemctl --user restart sbxloop-daemon` fails there with
"Failed to connect to bus". `KillMode=process` so stopping the runner never cuts off a
deploy mid-restart. Confirm with `gh api repos/<owner>/<repo>/actions/runners --jq '.runners[].status'`.

### Security

A self-hosted runner executes whatever a workflow says, as its user — who can restart
services on the host and, on many hosts, has `sudo`. So:

> **No job carrying the `self-hosted` label may be reachable from a fork-triggerable event** —
> `pull_request`, `pull_request_target`, or `issue_comment`.

A fork-triggered job on that runner would be a full host compromise. The example uses only
`schedule` (which runs the default branch in base-repository context) and
`workflow_dispatch` (which needs write access). Keep the invariant as a comment at the top
of the workflow so it survives future edits.

Supporting controls: protect the default branch with required status checks and
`enforce_admins`; leave default workflow permissions at `read` (the job narrows to
`contents: read`); and set *Fork pull request workflows from outside collaborators* to
**require approval for all external contributors**. Runner groups are org-only, so
per-workflow runner scoping is not available on a personal repository — the label invariant
is the control.

## Relationship to `version_status`

The concierge's `version_status` tool and the startup drift line report when a host is
behind PyPI; the upgrade paths above are what stops it happening. The concierge deliberately
cannot upgrade anything. On a host upgraded by hand, set `[daemon] upgrade_command` to the
two-command path (or whatever wraps it) so the notice tells the operator exactly what to run.
On a host *with* the workflow, set `[daemon] version_check = false`: the workflow is what
keeps the host current, a hand upgrade in between would be rolled to wherever the next
scheduled deploy lands, and the notice would only ever advise exactly that — so the host
makes no PyPI request and gives no advice, and a stale host shows up in the workflow's run
history instead.

## Multiple repositories on one host

One daemon tends every repository declared in `sbxloop.toml`, so a second
project does **not** need a second unit, state directory or control channel.
Declare them as `[[github.repos]]` entries — each with its own
`deliver_base`, `trigger_label`, extra `labels`, `enabled` switch and
optional `token_env` — and export any per-repo token from the host's
`.env` alongside `GH_TOKEN`. The legacy `[github] repo = "owner/name"`
still loads unchanged and is normalised into a one-entry list; migrating is
moving that key (and its `deliver_base` / `create_repo` / `create_public`)
into one `[[github.repos]]` entry. The two forms may not be mixed, and a
duplicated repository or a malformed slug fails config loading. Work items
queued by the pre-migration single-repo daemon carry no repository. At startup
the daemon attributes what it can from each row's issue URL; of the rest, only
items still sitting untouched in the queue are discarded and rediscovered,
repo-qualified, on the next poll — an issue still carrying `sbxloop:run` is
simply picked up again. An item that was already **claimed** (or running) is
not: claiming replaces `sbxloop:run` with `sbxloop:in-progress`, so discovery
will never see that issue again. Those items are failed with an explicit
reason instead of being dropped, and the daemon logs
`daemon.repoless_items_stranded` (and posts a control-channel notice) naming
each item id and issue URL, so you can clear the in-progress label and re-add
`sbxloop:run` by hand for anything that was in flight across the upgrade.

Everything under `[[github.repos]]` is per repository; the `[daemon]`
guardrails — the daily run cap, the per-item attempt and resume caps, the
consecutive-failure circuit breaker, and one run at a time — stay
**daemon-wide** and are shared across all of them. Polling health is the
one per-repository guardrail (#516): a repository that fails to poll is
backed off on its own (doubling, capped at an hour) and, after
`[daemon] repo_suspend_after` consecutive failures — or at once when GitHub
says it is gone for this token (404/410, a permission 403) — **suspended**
from polling, announced once in the control channel, shown in `ctl status`, the
concierge's repository listing and `sbxloop doctor`, and resumed with
`sbxloop daemon ctl resume-repo <owner/name>` (or a daemon restart, which
starts every repository fresh). The healthy repositories poll on as usual. That is what makes one
unit the right shape: the host's budget is bounded in total, and a
repository that keeps failing trips the breaker for the whole daemon. Deploy
health checks are unaffected; `sbxloop doctor` reports one row per
configured repository, so a broken repo is visible without masking the rest.

## When it all goes wrong

If a deploy fails *and* its rollback fails, the job says `ROLLBACK ALSO FAILED — <host> needs a human`. Fix by hand: the two commands under [Upgrading by hand](#upgrading-by-hand)
with the last good version, then `journalctl --user -u sbxloop-daemon -n 200` for why the
new one would not start.
