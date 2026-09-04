# GitHub permissions

What the credential in the github-ops sandbox must be allowed to do, and
the feature that first needs each permission. This is the table
`sbxloop doctor` checks a repository's token against (#696); it lives in
code as `sbxloop.gh.permissions.NEEDS`, and the README and `.env.example`
point here rather than restating it.

| Permission    | Level | Classic PAT scope | Needed for                                                                                                                                                         |
| ------------- | ----- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Metadata      | read  | `repo`            | looking the repository up                                                                                                                                          |
| Contents      | write | `repo`            | delivering the run's branch and commits, merging                                                                                                                   |
| Pull requests | write | `repo`            | opening, reviewing, un-drafting, updating and merging the pull request                                                                                             |
| Issues        | write | `repo`            | polling issues for work, claiming them, driving the lifecycle labels, filing follow-ups                                                                            |
| Checks        | read  | `repo`            | waiting for check runs at the CI and landing stages                                                                                                                |
| Actions       | read  | `repo`            | reading workflow runs and the failed jobs' logs at the CI stage                                                                                                    |
| Workflows     | write | `workflow`        | **optional** — delivering changes under `.github/workflows/`; without it GitHub refuses a delivery that touches a workflow file, and every other run is unaffected |

`public_repo` stands in for `repo` on a public repository only.

## Creating the credential

- **Fine-grained PAT** — repository access: the repositories sbxloop works
  on; repository permissions as in the table (Metadata is granted
  implicitly). Export it as `GH_TOKEN`.
- **GitHub App** — the same repository permissions on the App, installed
  on the repositories; see the README's *GitHub App auth*.
- **Classic PAT** — `repo`, plus `workflow` if runs may edit workflow
  files.

The agent sandbox's credential (`COPILOT_GITHUB_TOKEN`, *Copilot Requests*;
or `ANTHROPIC_API_KEY`) is a different token with none of these — it never
reaches GitHub's REST API.

## What doctor checks

`sbxloop doctor --probe` boots one github-ops sandbox per credential and
judges the token from whichever source describes it:

- a **GitHub App** installation token's mint carries the installation's
  `permissions` map — compared directly;
- a **classic PAT** answers every request with an `X-OAuth-Scopes` header
  (read from `GET /rate_limit`, which costs nothing) — compared to the
  scope column;
- a **fine-grained PAT** reports neither, so doctor asks one read per
  permission (`/issues`, `/pulls`, `/commits/{base}/check-runs`,
  `/actions/runs`, `/commits?sha={base}`): a 401/403 is "not granted",
  anything else — an empty list, a 404 on an empty repository — is
  "granted". Write levels come from the repository payload's `push` bit.
  `workflows:write` cannot be verified this way and is not reported.

A required permission the token lacks is a FAIL row on the repository,
naming the permission and the feature that needs it. `workflows:write`
missing is a WARN row (`github repo <r> workflows`). A `github repo <r> ci`
row says how many Actions workflows the repository has and the latest run
on the delivery base — or that there are none, in which case the CI stage
has nothing of the repository's own to wait for and passes on the delivered
head (check runs another app reports still count).
