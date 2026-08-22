# Running `sbxloop daemon` under systemd (user service)

1. Make a project directory for the daemon to live in and put its config
   and tokens there:

   ```bash
   mkdir -p ~/sbxloop-runner && cd ~/sbxloop-runner
   sbxloop init                     # writes sbxloop.toml; edit [github], [daemon], [discord]
   cat > .env <<'EOF'
   COPILOT_GITHUB_TOKEN=github_pat_...
   GH_TOKEN=github_pat_...
   DISCORD_BOT_TOKEN=...            # only if [discord] channel_id is set
   EOF
   chmod 600 .env
   sbxloop doctor                   # confirms tokens, sbx, and the discord bridge row
   ```

   Give the daemon a **dedicated clone nobody edits** as its workspace and
   point `[sandbox] workspace` at it — never the checkout you work in
   (the daemon fetches and fast-forwards it before every run, and runs
   proceed from committed HEAD even when the tree is dirty):

   ```bash
   git clone https://github.com/you/your-repo ~/sbxloop-runner/src
   # sbxloop.toml: [sandbox] workspace = "src"
   ```

   Run state (per-run clones, artifacts, SQLite) lands under
   `~/.local/state/sbxloop/sbxloop-runner/` by default, outside the
   workspace; set `[daemon] state_dir` to choose. The daemon prints the
   resolved location on start.

2. Install the unit, pointing `WorkingDirectory` at that directory if you
   used a different path:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp contrib/systemd/sbxloop-daemon.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now sbxloop-daemon
   loginctl enable-linger "$USER"   # keep user services running when logged out
   ```

3. Watch it:

   ```bash
   journalctl --user -u sbxloop-daemon -f
   systemctl --user status sbxloop-daemon
   # only the runs' own lifecycle (tasks, phases, sandboxes, worker jobs):
   journalctl --user -u sbxloop-daemon -f | grep sbxloop.run
   ```

   The log is structured (`event key=value …`); `Environment=SBXLOOP_DAEMON__LOG_LEVEL=DEBUG`
   in the unit turns on the per-call firehose, and
   `SBXLOOP_DAEMON__LOG_FORMAT=json` renders one JSON object per line for a
   log shipper.

`systemctl --user stop` sends SIGTERM: the daemon stops claiming work, asks
the in-flight run to cancel at its next task boundary, waits up to
`[daemon].shutdown_grace_s`, and exits. The interrupted run stays resumable
and is picked up on the next start. After upgrading sbxloop, `systemctl --user restart sbxloop-daemon` so the new code (and a fresh github-ops
sandbox) is used.

## The other two units

`sbx-sandboxd.service` supervises the sandbox backend. Without it `sbx daemon start` is a bare process: if it dies nothing restarts it, and every run fails
with no systemd trace. The daemon unit `Requires=` it, so a manual
`systemctl --user start sbxloop-daemon` brings the backend up first.

`github-runner.service` runs a GitHub Actions runner as the *same user*, which
is what lets a workflow do `systemctl --user restart sbxloop-daemon`. That is
the deploy pipeline in [docs/deploy.md](../../docs/deploy.md) — merge to `main`,
and the release that follows installs itself here and restarts the service.
Both are user units, so `loginctl enable-linger "$USER"` covers all three.

## Upgrading

Automated, via that pipeline. By hand it is two commands as the daemon's user:

```bash
~/.sbxloop-venv/bin/pip install --upgrade 'sbxloop[discord]==X.Y.Z' 'sbxloop-worker==X.Y.Z'
systemctl --user reset-failed sbxloop-daemon && systemctl --user restart sbxloop-daemon
```

`reset-failed` matters: `StartLimitBurst=5` per 600s leaves a unit that
crash-looped in `failed`, where a plain `restart` will not revive it. Pause
first (`sbxloop daemon ctl pause`, wait for `current: idle`) to avoid
interrupting a run — and note the daemon comes back **unpaused** regardless,
since pause is in-memory only.
