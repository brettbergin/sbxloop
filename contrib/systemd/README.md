# Running `sbxloop daemon` under systemd (user service)

Everything lives under the **sbxloop home**, `~/.sbxloop` (`SBXLOOP_HOME`
moves it): the interpreter, the launchers, the config and secrets, the
state, the runs, the workspaces, the logs, the unit files. One command
builds it.

1. Install and initialise the home:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/brettbergin/sbxloop/main/scripts/install.sh | sh
   ```

   That puts `uv`, a CPython and the `sbxloop[discord,slack]` venv under
   the home, then runs `sbxloop init --systemd`, which writes the
   launchers (`~/.sbxloop/bin/sbxloop`, `~/.sbxloop/bin/sbx`), installs
   Docker's `sbx` under `~/.sbxloop/sbx`, writes `config/sbxloop.toml` and
   a 0600 `config/secrets.env`, renders the units into `~/.sbxloop/systemd`
   and enables them (never starts them), and turns lingering on so user
   units outlive the login. Put `~/.sbxloop/bin` on your `PATH`. Already
   have sbxloop installed some other way? `sbxloop init --systemd` from
   that install builds the same home; `sbxloop init --migrate --purge`
   moves a pre-home installation into it first.

2. Fill in the config and the secrets, then check:

   ```bash
   $EDITOR ~/.sbxloop/config/sbxloop.toml    # [github], [daemon], [discord] / [slack]
   $EDITOR ~/.sbxloop/config/secrets.env     # the tokens; sbxloop reads this file itself
   sbx login && sbx policy init balanced     # the home's sbx, through its wrapper
   sbxloop doctor                            # tokens, sbx, the home, the units
   ```

   One daemon can tend several repositories: declare them as
   `[[github.repos]]` entries. Each entry carries its own base branch,
   labels, `enabled` switch and optional `token_env` — export any per-repo
   token in `secrets.env`. The `[daemon]` guardrails (daily run cap,
   attempt/resume caps, circuit breaker, one run at a time) are daemon-wide
   and shared across all of them, so one service is enough.

   The daemon keeps a **dedicated clone** of each repository under
   `~/.sbxloop/workspaces/<owner>/<name>`, cloned on first use and
   fast-forwarded before every run; nothing to set up. Point
   `[sandbox] workspace` (or a repo entry's `workspace`) at a checkout of
   your own only if you want that one used instead — never the checkout
   you work in. Run state (per-run clones, artifacts, SQLite) lands under
   `~/.sbxloop/runs` and `~/.sbxloop/state`; the daemon logs its home in
   its `daemon.starting` line.

3. Start it and watch it:

   ```bash
   systemctl --user start sbxloop-daemon     # sbx-sandboxd starts first (Requires=)
   systemctl --user status sbxloop-daemon
   journalctl --user -u sbxloop-daemon -f    # or: sbxloop daemon logs -f
   # only the runs' own lifecycle (tasks, phases, sandboxes, worker jobs):
   journalctl --user -u sbxloop-daemon -f | grep sbxloop.run
   ```

   The log is structured (`event key=value …`) and also written to
   `~/.sbxloop/logs/daemon.log` (rotated by size). `[daemon] log_level = "DEBUG"` in `config/sbxloop.toml` turns on the per-call firehose, and
   `log_format = "json"` renders one JSON object per line for a log
   shipper.

`systemctl --user stop` sends SIGTERM: the daemon stops claiming work, asks
the in-flight run to cancel at its next task boundary, waits up to
`[daemon].shutdown_grace_s`, and exits. The interrupted run stays resumable
and is picked up on the next start.

## Upgrading

By hand, as the daemon's user, once nothing is running. Take a named hold
so the daemon stops claiming, wait for idle, snapshot, install the exact
version into the home's venv, re-run init (idempotent: it refreshes the
launchers and units for the new version and keeps your config), restart:

```bash
sbxloop daemon ctl pause --hold upgrade
until [ "$({ sbxloop daemon ctl status --json 2>/dev/null || echo '{}'; } | jq -r '.current // .claiming // "idle"')" = idle ]; do sleep 15; done

sbxloop backup --label pre-X.Y.Z
~/.sbxloop/bin/uv pip install --python ~/.sbxloop/venv/bin/python --upgrade 'sbxloop[discord,slack]==X.Y.Z' 'sbxloop-worker==X.Y.Z'
sbxloop init --systemd
systemctl --user reset-failed sbxloop-daemon && systemctl --user restart sbxloop-daemon
```

Every command runs from any directory: the home is the home. `reset-failed`
matters: `StartLimitBurst=5` per 600s leaves a unit that crash-looped in
`failed`, where a plain `restart` will not revive it. The daemon comes back
**unpaused** regardless — holds are in-memory only — so re-take any you want
to keep. A downgrade is the same commands with an older version, or
`sbxloop backup restore <name>` for the config and state of a snapshot.

To automate exactly this (plus a health check and rollback) from a GitHub
Actions runner on the host, copy
[contrib/workflows/deploy-daemon.yml.example](../workflows/deploy-daemon.yml.example)
into the repository that owns the host; [docs/deploy.md](../../docs/deploy.md)
walks through it.

## The units

The files in this directory are the templates `sbxloop init --systemd`
renders with the home's absolute paths into `~/.sbxloop/systemd/` and
enables from there (`systemctl --user enable <path>` links them into
`~/.config/systemd/user`). Do not copy them by hand: a re-run of init
rewrites the rendered copies.

`sbx-sandboxd.service` supervises the sandbox backend through the home's
`sbx` wrapper. Without it `sbx daemon start` is a bare process: if it dies
nothing restarts it, and every run fails with no systemd trace. The daemon
unit `Requires=` it, so a manual `systemctl --user start sbxloop-daemon`
brings the backend up first.

`github-runner.service` is **only needed for the automated upgrade above**.
It runs a GitHub Actions runner as the *same user*, which is what lets a
workflow do `systemctl --user restart sbxloop-daemon`;
`sbxloop init --systemd --runner ~/actions-runner` renders it. Skip it if
you upgrade by hand. All three are user units, so `loginctl enable-linger`
(which init does) covers them.
