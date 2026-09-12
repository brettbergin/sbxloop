# Live forges

GitLab CE and Gitea in Docker, seeded into the shape the conformance suite
and the field questions of #1016 need. Everything here is opt-in: without the
environment below, every live test skips and says what is missing.

## Bring them up

Needs Docker with about 6 GB of memory for GitLab, `openssl` on `PATH`, and
the workspace installed (`make install`). GitLab's first boot takes three to
five minutes.

```bash
uv run python -m tests.live.harness up
uv run python -m tests.live.seed_gitea
uv run python -m tests.live.seed_gitlab
```

`harness up` mints a throwaway CA and a `localhost` certificate under
`tests/live/.state/certs`: both forges serve HTTPS only, because the worker
transport refuses a plain-http API root. The seeds are idempotent; run them
again after a restart. They print the URLs, users, token variable names and
repository slugs, and write the values to `tests/live/.state/live.env`. That
directory is git-ignored; nothing secret is printed, logged or passed on a
command line.

| Forge  | API root                        | Repository     | Tokens                                                                                                                           |
| ------ | ------------------------------- | -------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Gitea  | `https://localhost:3000/api/v1` | `acme/widgets` | `GITEA_TOKEN` (write collaborator), `GITEA_REVIEWER_TOKEN`, `GITEA_BOT_TOKEN` (a `--user-type bot` account), `GITEA_ADMIN_TOKEN` |
| GitLab | `https://localhost:8929/api/v4` | `acme/widgets` | `GITLAB_TOKEN` (Developer), `GITLAB_REVIEWER_TOKEN`, `GITLAB_BOT_TOKEN` (project access token), `GITLAB_ADMIN_TOKEN`             |

## Run against them

```bash
SBXLOOP_LIVE_ENV_FILE=tests/live/.state/live.env uv run pytest -n0 tests/live tests/conformance
```

`SBXLOOP_LIVE_ENV_FILE` names the env file; variables already in the
environment win over it. A forge is live when its API root
(`SBXLOOP_LIVE_GITLAB_URL`, `SBXLOOP_LIVE_GITEA_URL`) and its token
(`GITLAB_TOKEN`, `GITEA_TOKEN`) are set and `/version` answers with that
token; `SBXLOOP_LIVE_CA_FILE` names the CA to trust.

`tests/live/test_field_verify.py` asks the questions again and holds each
forge to the answers the spike records. To regenerate the evidence
transcripts the spike quotes:

```bash
SBXLOOP_LIVE_ENV_FILE=tests/live/.state/live.env uv run python -m tests.live.fieldverify --out tests/live/.state/evidence
```

Probes that write work on a fresh branch each run and never move `main`.

## Take them down

```bash
uv run python -m tests.live.harness down            # keep the seeded data
uv run python -m tests.live.harness down --volumes  # forget it
```
