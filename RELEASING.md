# Releasing

Releases are **fully automated and batched**. A merge/push to `main` wakes
`Release`, which waits for **three minutes without another observed merge**,
or **thirty minutes from its first observation**, whichever comes first.
One isolated PR waits for the short quiet window; a burst shares one patch
version. Runner queueing and release checks add to that time.

A reconciliation run at minutes 13 and 43 catches missed push events and
changes left behind when a run first finishes an older partial publication.
Already-published `main` is a no-op. Normal pushes still start their own
short quiet window without waiting for that schedule.

CI still checks every PR and push to `main`. Each release batch tests its
own frozen commit before tagging or publishing. Both `sbxloop` and
`sbxloop-worker` keep the same version; nothing is committed back to `main`.

## How it works

1. [`.github/workflows/release.yml`](.github/workflows/release.yml) observes
   the current tip of `main`, rather than the triggering event's commit.
   A changed tip resets the quiet window, never the thirty-minute limit.
   At the deadline it freezes one SHA. Subsequent merges belong to the next
   batch. An already published tip exits before waiting or running checks.
2. The full release suite checks that SHA: formatting, lint, typing,
   security, and tests with coverage. The release job checks out the same
   SHA, never a moving branch.
3. A new batch reserves the next patch tag. An existing reservation whose
   publication is incomplete is finished first, at its original version
   and commit, even if `main` has advanced. A latest tag outside `main`'s
   history fails closed. The tag must resolve to the tested SHA.
4. `hatch-vcs` derives both versions from the tag. The host build hook
   ([`packages/sbxloop/hatch_build.py`](packages/sbxloop/hatch_build.py))
   vendors the matching worker wheel and injects the exact worker pin.
   The workflow verifies the vendored wheel before publishing.
5. The two wheels and two source distributions are staged on a draft
   GitHub Release. `release-manifest.json`, uploaded last, records their
   SHA-256 hashes, version, and tested commit. PyPI publication cannot
   start before that marker exists.
6. Both distributions publish through **Trusted Publishing (OIDC)**.
   Only after the PyPI uploads succeed does the draft become a published
   GitHub Release. Publication attestations are attached too.
7. Every successful workflow uploads a `release-result` artifact containing
   the version, commit, and whether publication occurred. Deployment reads
   that exact run's result; it never infers the release from the triggering
   event's SHA. An explicit no-op causes no upgrade.

A concurrency group with `cancel-in-progress: false` serializes the whole
release, including batching and validation. `queue: max` keeps a push from
replacing a pending manual request. Redundant wakeups exit without another
version when their changes have already shipped. GitHub caps the queue at
100 pending runs.

## Everyday use

Merge to `main`. The next quiet window publishes one patch version for the
accumulated changes. To release without the batching delay:

```bash
gh workflow run release.yml --ref main
```

This still waits for the serialized release slot and checks. Manual runs
must target `main`. Deploying an already published version is a separate
operation that creates no packages:

```bash
gh workflow run deploy.yml --ref main -f version=X.Y.Z
```

The deploy workflow waits for the daemon's task to finish, then refreshes
the selected release unless an explicit version was requested. Automatic
upgrades have a thirty-minute cooldown; manual deployment bypasses it.
A periodic reconciliation retries deferred deployments even without another
merge. See [self-deploy.md](docs/self-deploy.md) for recovery and notices.

## Retrying a failed publication

Re-run `Release`, or dispatch it manually on `main`. A draft carrying the
manifest reuses its original files and verifies their hashes before upload.
It never rebuilds that version, including when one package reached PyPI and
the other did not. Existing uploads are skipped. A draft without a manifest
was interrupted before PyPI publication could start, so staging can be rebuilt.
Missing or corrupt files after the marker exists fail closed for repair.

Do not delete or edit reserved tags or staged files to force a retry. A
legacy incomplete release created before this staging protocol needs manual
inspection: recover any already published bytes before retrying it. If `main`
has moved while an older reservation is completed, a queued push or the
reconciliation schedule releases those later changes. Manual dispatch can
release them sooner.

## Cutting a minor or major release

Automatic batches increment the patch segment. To reserve a minor or major
version, tag the tip of `main` and dispatch `Release` on `main`:

```bash
git tag -a v2.0.0 -m "Release v2.0.0"
git push origin v2.0.0
gh workflow run release.yml --ref main
```

The reserved tag is released at its exact commit. If you omit dispatch,
the next merge wakes the workflow, which finishes that reservation first.
A completed release at the selected tip is a no-op, not another publication.

## Setup and policy

The existing PyPI Trusted Publishers for both projects use repository
`brettbergin/sbxloop`, workflow `release.yml`, and environment `pypi`.
Those identities are preserved; no new publication tokens are needed.
The workflow's `GITHUB_TOKEN` needs `contents: write` to reserve tags and
stage releases, and `id-token: write` for PyPI Trusted Publishing.

The three-minute quiet window, thirty-minute batching limit, and
thirty-minute deployment cooldown are repository workflow policy in
`scripts/release_pipeline.py`; they are not daemon configuration options.
The generic deployment example remains a standalone PyPI upgrade example.

GitHub's [concurrency documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
describes pending queues. Scheduled reconciliation is best-effort; see
[workflow scheduling](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Versioning and local builds

Package versions are not stored in `pyproject.toml`; both packages declare
`dynamic = ["version"]`. `hatch-vcs` computes `X.Y.Z` on a tagged commit and
a development version between tags. Generated `_version.py` files are ignored.
GitHub Release notes cover the batch's commits. Keep CHANGELOG entries for
changes worth explaining beyond their commit titles.

```bash
make build
uv run sbxloop --version
unzip -l dist/sbxloop-*.whl | grep _vendor
```
