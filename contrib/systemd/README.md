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
   ```

`systemctl --user stop` sends SIGTERM: the daemon stops claiming work, asks
the in-flight run to cancel at its next task boundary, waits up to
`[daemon].shutdown_grace_s`, and exits. The interrupted run stays resumable
and is picked up on the next start. After upgrading sbxloop, `systemctl --user restart sbxloop-daemon` so the new code (and a fresh github-ops
sandbox) is used.
