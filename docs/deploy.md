# Deploying the daemon

Targets the current sbxloop release; check the installed version with `pip show sbxloop`.

Every merge to `main` auto-releases a patch to PyPI ([RELEASING.md](../RELEASING.md)). This
pipeline carries that release the last mile: onto the host running `sbxloop daemon`, with the
service restarted and health-checked, so a running daemon never silently drifts behind its
own releases.

```
merge to main → Release (tag + PyPI, ~4 min) → Deploy to db (self-hosted runner)
                                                 ├─ pause + drain the in-flight run
                                                 ├─ pip install the exact released version
                                                 ├─ systemctl --user restart sbxloop-daemon
                                                 ├─ health check, or roll back
                                                 └─ restore pause state + tell Discord
```

`.github/workflows/deploy.yml` is the whole pipeline. It runs on a self-hosted runner on the
daemon host and takes **no** checkout — it installs from PyPI and needs nothing from the tree.

## What it does

01. **Resolves the version** — the tag on the commit `Release` just published (matched via
    `/tags`, whose `.commit.sha` dereferences the annotated tag), falling back to the latest
    release when `Release`'s ancestor guard skipped tagging. `workflow_dispatch` can name one.
02. **Short-circuits** if the host already runs that version, so re-runs and skipped releases
    cost nothing.
03. **Fetches the release wheels** with `gh release download`, rather than installing from
    PyPI. `Release` attaches the same `dist/` it uploads to PyPI, and they exist the moment
    that workflow finishes — whereas the PyPI simple index is Fastly-cached with
    `max-age=600`, so for up to ten minutes pip can be served an index page that predates
    the upload. Two deploys died exactly there: v0.7.17 on `sbxloop`, then v0.7.18 on
    `sbxloop-worker`, which is a separate project with its own independently cached page.
04. **Pauses and drains.** `daemon ctl pause` stops new claims; it then polls `ctl status`
    every 15 s for up to 20 minutes until `current: idle`. The 15 s floor is deliberate —
    `status()` mutates the circuit breaker (#309). On timeout it restarts anyway: cancellation
    is honored at the next task boundary and an interrupted run resumes, it just spends one of
    the item's resume-budget slots.
05. **Upgrades** by installing both local wheels together. The host wheel pins
    `sbxloop-worker==X` exactly, and naming the local worker wheel satisfies that pin without
    the index being consulted for it at all. `[discord]` is required for the daemon's bridge.
    Everything else (pydantic, discord.py) still resolves from PyPI, which is fine — those are
    not racing a just-published version.
06. **Restarts**, after `systemctl --user reset-failed` — without that, a previously
    crash-looping unit sits `failed` and is not restartable (`StartLimitBurst=5` per 600 s).
07. **Health-checks**: unit active, `--version` matches, `sbxloop doctor` exits 0 (never
    `--deep`, which boots a microVM), `daemon ctl status` answers (exit 2 means no daemon came
    up), then a 45 s settle to prove it is not crash-looping.
08. **Rolls back** to the previously installed version on any failed check, restarts, and fails
    the job loudly. Rollback installs from PyPI: the previous version has been published for a
    while, so its index page is long since warm and there is no race to lose.
09. **Restores the pause state** it recorded at the start. This is required, not cosmetic:
    pause is in-memory only (#308), so *every* restart otherwise comes back claiming work
    autonomously. It runs after a rollback too — whatever version is live, operator intent
    survives.
10. **Reports to Discord** in the control channel, using the bot already there.

## Relationship to `version_status`

The concierge's `version_status` tool and the startup drift line report when a host is
behind PyPI; this pipeline is what stops it happening. They stay complementary: the
concierge deliberately cannot upgrade anything, and on a host without a runner
(anyone following `contrib/systemd/` by hand) its `pip install --upgrade` advice is
exactly right. On a host *with* the pipeline, a drift line means the deploy did not
run or did not succeed — check the **Deploy to db** workflow before upgrading by hand,
or the next deploy will roll you somewhere you did not expect.

## Host layout it assumes

Set once, on the daemon host (`db`), by the user the daemon runs as:

|             |                                                                                                   |
| ----------- | ------------------------------------------------------------------------------------------------- |
| Service     | user unit `~/.config/systemd/user/sbxloop-daemon.service`                                         |
| Working dir | `~/sbxloop-work` — `ctl` resolves `state_dir` from the cwd, so every `ctl` call runs here         |
| Interpreter | venv at `~/.sbxloop-venv`                                                                         |
| Wrapper     | `~/.local/bin/sbxloop` sources `~/.config/sbxloop/env.sh` (PATH incl. `/usr/sbin`, dbus, secrets) |
| Secrets     | `~/.config/sbxloop/secrets.env`, mode 0600                                                        |

All of these are `env:` values at the top of the deploy job — a host layout change is a
one-line edit.

Two supporting user units live alongside the daemon:

- **`github-runner.service`** — the Actions runner, as a *user* unit rather than via
  `svc.sh`. A system unit has no `XDG_RUNTIME_DIR`/`DBUS_SESSION_BUS_ADDRESS`, so
  `systemctl --user restart sbxloop-daemon` fails with "Failed to connect to bus" from one.
  Inside the user manager it just works, and `loginctl enable-linger` keeps it alive across
  logout and reboot. `KillMode=process` so stopping the runner never cuts off a deploy
  mid-restart.
- **`sbx-sandboxd.service`** — the sandbox backend. It used to be a bare unsupervised
  process; if it died, nothing restarted it and every run failed with no systemd trace. The
  daemon unit `Requires=` and is ordered `After=` it.

## Setting up the runner

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -fsSLO https://github.com/actions/runner/releases/download/v<X>/actions-runner-linux-x64-<X>.tar.gz
echo "<sha256>  actions-runner-linux-x64-<X>.tar.gz" | sha256sum -c -
tar xzf actions-runner-linux-x64-<X>.tar.gz

# registration tokens expire in an hour; mint one at use time
./config.sh --url https://github.com/brettbergin/sbxloop \
  --token "$(gh api -X POST repos/brettbergin/sbxloop/actions/runners/registration-token --jq .token)" \
  --name db --labels db --work _work --unattended --replace

cp contrib/systemd/github-runner.service ~/.config/systemd/user/
cp contrib/systemd/sbx-sandboxd.service  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sbx-sandboxd github-runner
loginctl enable-linger "$USER"
```

`self-hosted`, `Linux` and `X64` are added automatically; `--labels db` is the one that makes
`runs-on: [self-hosted, db]` resolve. Confirm with
`gh api repos/brettbergin/sbxloop/actions/runners --jq '.runners[].status'`.

To move the daemon to a different host, register a runner there with the same `db` label —
or change the label in `deploy.yml` and in the `runs-on` line together.

## Security

This repository is **public** and the runner's user has passwordless root on the host, so:

> **No job carrying the `self-hosted` label may be reachable from a fork-triggerable event** —
> `pull_request`, `pull_request_target`, or `issue_comment`.

A fork-triggered job on this runner would be a full host compromise. `deploy.yml` uses only
`workflow_run` (which fires solely from `Release`, itself push-to-main only, and always in
base-repo context) and `workflow_dispatch` (which needs write access). The invariant is
repeated as a comment at the top of the workflow so it survives future edits.

Supporting controls: `main` is protected with required status checks and `enforce_admins`;
default workflow permissions are `read` and the deploy job narrows to `contents: read`; and
*Fork pull request workflows from outside collaborators* should be set to **require approval
for all external contributors**. Runner groups are org-only, so per-workflow runner scoping is
not available on a personal repo — the label invariant is the control.

The job also sets `GH_TOKEN` explicitly on any step calling `gh`. The host's `secrets.env`
exports its own `GH_TOKEN`, and the `sbxloop` wrapper sources it with `set -a`; without the
override, a host PAT would be the identity for Actions API calls.

## 1.0 cutover

The 1.0 pipeline (one run from issue to merged PR; no self-filed audits,
post-mortems or backlog issues; landing under `[landing]`) changes what the
daemon keeps on disk and which config keys exist. Because this pipeline
deploys unattended, none of that may fail the restart:

- **State.** A pre-1.0 `state.db` carries the old lanes' tables and item
  kinds. On its first start the new daemon moves the whole file aside to
  `state.db.pre-1.0` (plus `-wal`/`-shm`; a timestamp is appended if that
  name is taken), logs `store.archived_legacy`, tells Discord, and starts
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
  installing 1.0, or the daemon will not start (the deploy pipeline's
  health check then rolls back).
- **Issues and labels.** The old loop's `sbxloop:backlog` / `sbxloop:audit`
  issues are closed by hand at cutover (`gh issue close --reason "not planned"`), those two labels and `sbxloop:delivered` deleted, and
  `sbxloop:blocked` created. Any of the old loop's PRs still open
  (`gh pr list --search "head:sbxloop/ is:open"`) are merged or closed by
  hand — their items went with the archived state.

## Operating it

```bash
# deploy a specific version (also the rollback path)
gh workflow run deploy.yml -f version=0.7.15

# deploy whatever is latest on PyPI
gh workflow run deploy.yml

gh run watch                                            # from the repo
ssh db 'journalctl --user -u sbxloop-daemon -f'         # from the host
ssh db 'systemctl --user status github-runner sbx-sandboxd sbxloop-daemon'
```

Rolling back is just deploying the older version — the workflow pins exactly, so
`-f version=0.7.14` downgrades. If a deploy fails *and* its rollback fails, the job says
`ROLLBACK ALSO FAILED — db needs a human`; fix by hand with
`~/.sbxloop-venv/bin/pip install 'sbxloop[discord]==<good>' 'sbxloop-worker==<good>'` and
`systemctl --user reset-failed sbxloop-daemon && systemctl --user restart sbxloop-daemon`.
