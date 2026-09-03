# How sbxloop deploys itself

**This is a reference for this repository's own daemon host, not a guide.** The generic
procedure — running the daemon as a service, upgrading it by hand, automating that with a
workflow — is [docs/deploy.md](deploy.md). This page records where sbxloop's own pipeline
departs from that pattern and the facts about the host that operating it needs.

```
merge to main → Release (tag + PyPI, ~4 min) → Deploy the daemon (self-hosted runner on db)
                                                 ├─ take a named pause hold (deploy-<run id>)
                                                 ├─ wait — no cap — for the in-flight run to finish
                                                 ├─ pip install the release wheels
                                                 ├─ systemctl --user restart sbxloop-daemon
                                                 ├─ health check, or roll back
                                                 └─ restore the other holds + tell the control channel
```

Every merge to `main` auto-releases a patch to PyPI ([RELEASING.md](../RELEASING.md));
`.github/workflows/deploy.yml` carries that release the last mile onto the host running
`sbxloop daemon`, so a running daemon never silently drifts behind its own releases. Since
1.0 the loop merges its own PRs, every merge deploys, and the next queued item is usually
already running when the deploy lands — which is why the wait for idle has no cap (#534): a
capped drain that "restarts anyway" killed in-flight tasks and spent the item's resume budget
for nothing of its own doing.

## Where it departs from the example

`deploy.yml` is `contrib/workflows/deploy-daemon.yml.example` with these differences:

- **Trigger.** `workflow_run` on `Release` (which is push-to-`main` only and always runs in
  base-repository context) instead of a schedule, plus `workflow_dispatch`. Both satisfy the
  self-hosted security invariant in the generic guide.
- **Version.** The tag on the commit `Release` just published (matched via `/tags`, whose
  `.commit.sha` dereferences the annotated tag), falling back to the latest release when
  `Release`'s ancestor guard skipped tagging.
- **Wheels from the release, not PyPI.** `gh release download` fetches the same `dist/` that
  `Release` uploads to PyPI, which exists the moment that workflow finishes — whereas the
  PyPI simple index is Fastly-cached with `max-age=600`, so for up to ten minutes pip can be
  served an index page that predates the upload. Two deploys died exactly there: v0.7.17 on
  `sbxloop`, then v0.7.18 on `sbxloop-worker`, which is a separate project with its own
  independently cached page. The host wheel pins `sbxloop-worker==X` exactly, and naming
  the local worker wheel satisfies that pin without the index being consulted. Rollback
  installs from PyPI: the previous version has been published for a while, so its page is
  long since warm. Both install `[discord,slack]` (#619) so a rollback never drops an extra
  the upgrade had.
- **`GH_TOKEN` is set explicitly** on every step calling `gh`. The host's `secrets.env`
  exports its own `GH_TOKEN`, and the `sbxloop` wrapper sources it with `set -a`; without
  the override, a host PAT would be the identity for Actions API calls.
- **Working directory** is `~/sbxloop-work`, not the `~/sbxloop-runner` the systemd README
  sets up, and the tokens live in `~/.config/sbxloop/secrets.env` (mode 0600; shape: the
  repo-root [`.env.example`](../.env.example), "Daemon host layout"), sourced by the wrapper
  `~/.local/bin/sbxloop` via `~/.config/sbxloop/env.sh` — not in a cwd `.env`. The job
  itself never reads that file (#639): `ctl status --json` and `daemon notify` go through
  the wrapper.

## The host

`db`, one user, three user units alongside each other: `sbxloop-daemon`, `sbx-sandboxd`
(the sandbox backend, which the daemon unit `Requires=`), and `github-runner` (the Actions
runner, as the same user so the workflow can `systemctl --user restart`). Deploying by hand
and the layout table are in the generic guide; the paths above are the only deltas.

The runner is registered with the label `db`:

```bash
./config.sh --url https://github.com/brettbergin/sbxloop \
  --token "$(gh api -X POST repos/brettbergin/sbxloop/actions/runners/registration-token --jq .token)" \
  --name db --labels db --work _work --unattended --replace
```

Confirm with `gh api repos/brettbergin/sbxloop/actions/runners --jq '.runners[].status'`.

**The host is one variable** (#640). The workflow targets
`runs-on: [self-hosted, "${{ vars.SBXLOOP_DEPLOY_HOST || 'db' }}"]` and calls the host by
the same name in its notices. To move the daemon: set the repository variable
`SBXLOOP_DEPLOY_HOST` to the new host's runner label and register a runner there with that
label. `deploy.yml` does not change, and neither does anything `make check` runs.

## Operating it

```bash
# deploy a specific version (also the rollback path)
gh workflow run deploy.yml -f version=X.Y.Z

# deploy whatever the latest release is
gh workflow run deploy.yml

gh run watch                                            # from the repo
ssh db 'journalctl --user -u sbxloop-daemon -f'         # from the host
ssh db 'systemctl --user status github-runner sbx-sandboxd sbxloop-daemon'
```

Rolling back is just deploying the older version — the workflow pins exactly. If a deploy
fails *and* its rollback fails, the job says `ROLLBACK ALSO FAILED — db needs a human`; fix
by hand with the two commands in the generic guide, from `~/sbxloop-work`.

A `version_status` drift line from the concierge on this host means the **Deploy the
daemon** workflow did not run or did not succeed — check it before upgrading by hand.

## Cutovers

Because this pipeline deploys unattended, a change to what the deploy job needs from the
running daemon can strand it. Each one is recorded here with its manual step.

- **Structured status (#639).** The drain step now reads `ctl status --json` and
  fails closed when the daemon answers without a structured status — which a daemon older
  than that flag does. The first deploy after it lands therefore fails at "Wait for the
  daemon to go idle" *before installing anything*; upgrade once by hand (the two commands in
  the generic guide), and every deploy after that is unattended again.
- **1.0 (state and config).** The steps a 0.7.x host needs are in the
  [CHANGELOG under "1.0 cutover"](../CHANGELOG.md#10-cutover).
