# Releasing

Releases are **fully automated**. Every merge/push to `main` runs the check
suite, bumps the patch version, tags it, builds both distributions, and
publishes them to PyPI — no manual version edits, no release PRs, no tokens.

## How it works

1. A merge lands on `main` → [`.github/workflows/release.yml`](.github/workflows/release.yml) runs.
1. The full check suite must pass (ruff, mypy, pytest — the gate).
1. The workflow finds the latest `vX.Y.Z` tag and computes the next **patch**
   version (`v0.4.0` → `v0.4.1`). If `HEAD` is already tagged (a manual
   minor/major bump, or a re-run), that version is released as-is.
1. It creates and pushes the tag. [`hatch-vcs`](https://github.com/ofek/hatch-vcs)
   derives **both** package versions (`sbxloop`, `sbxloop-worker`) from that
   one tag, so the lockstep invariant holds by construction and nothing is
   committed back to `main`.
1. `uv build` produces the sdist + wheel for each package. The host build hook
   ([`packages/sbxloop/hatch_build.py`](packages/sbxloop/hatch_build.py))
   vendors the worker wheel into the host wheel and injects the exact
   `sbxloop-worker==X.Y.Z` pin into the wheel metadata. A guard step fails the
   release if the vendored wheel is missing or at the wrong version.
1. Both distributions are published to PyPI via **Trusted Publishing (OIDC)**
   (environment `pypi`) and attached to an auto-generated GitHub Release.

Pull requests are tested separately by [`.github/workflows/ci.yml`](.github/workflows/ci.yml)
across Python 3.13–3.14, so broken code never reaches `main`.

## Everyday use

Just merge to `main`. That's it — a new patch version of both packages ships
automatically.

## Cutting a minor or major release

The workflow only auto-bumps the **patch** segment. To move the minor or
major, push the tag yourself and run the **Release** workflow manually from
the Actions tab (`workflow_dispatch`) — it detects that `HEAD` is already
tagged and publishes that exact version:

```bash
git tag -a v0.5.0 -m "Release v0.5.0"
git push origin v0.5.0
```

(If you skip the manual dispatch, the tag still takes effect on the next
merge: the workflow continues from the newest tag, so the next release is
`v0.5.1`.)

## One-time setup (already done)

- **PyPI Trusted Publishing** is configured for both projects (`sbxloop` and
  `sbxloop-worker`): repo `brettbergin/sbxloop`, workflow `release.yml`,
  environment `pypi`. No API token secrets exist or are needed.
- The workflow pushes tags with the built-in `GITHUB_TOKEN` (granted
  `contents: write`). If a tag protection rule is ever added, allow `v*` tags
  to be created by Actions.

## Versioning notes

- Package versions are **not** stored in the repo. `pyproject.toml` declares
  `dynamic = ["version"]` and hatch-vcs computes the version from git: exactly
  `X.Y.Z` on a tagged commit, `X.Y.(Z+1).devN` on commits in between.
- `_version.py` is generated into each package at build/sync time (gitignored)
  so `sbxloop.__version__` / `sbxloop_worker.__version__` report the real
  version at runtime, including inside sandboxes.
- The CHANGELOG is no longer the release trigger; per-release notes are
  auto-generated on the GitHub Release. Keep using CHANGELOG.md for anything
  worth narrating beyond commit titles.

## Local sanity check

```bash
make build          # versions come from `git describe`; a dev tree -> X.Y.Z.devN
uv run sbxloop --version
unzip -l dist/sbxloop-*.whl | grep _vendor    # worker wheel made it into the host wheel
```
