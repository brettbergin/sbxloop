# Spike: a configurable version-control backend (GitHub, GitLab, Gitea)

Status: **tracked as an epic; the forge-agnostic steps have landed and the
field questions are answered.** The epic is
[issue #1009](https://github.com/brettbergin/sbxloop/issues/1009); its
child issues follow the sequence at the end of this document. Steps 1-5
landed as #1022-#1027 and #1029. The six field questions (#1016) were
answered on 2026-09-12 against GitLab CE 19.3.2 and Gitea 1.24.7 running in
Docker; the evidence, the corrected capability matrix and the decisions they
force are in [Field verification](#field-verification-1016).

Code-seam claims below were read off this tree and are reproducible with the
commands quoted beside them. GitLab and Gitea claims are **verified** where
the matrix and the field-verification section say so, with the version they
were verified on; anything not exercised there is labelled
**field-unverified** where it appears. Two limits apply to every verified
row: GitLab was the free tier (CE, `enterprise: false`), so a Premium or
Ultimate fact is field-unverified; and both instances ran their default
configuration, so an instance-level setting (commit signing, a site-wide
token policy) can change a row.

## The outcome

An operator points sbxloop at a repository on GitHub, GitLab or Gitea by
setting one config key, and the run works: an ask becomes a landed change on
a review-protected base, with the same chronology, the same steering, and the
same credential split in every case.

Where a forge cannot do something sbxloop relies on, the loop says so and
degrades on purpose — it never guesses, and it never silently drops a
guarantee the operator thinks they have.

## Why

Two reasons, and the second is the larger one.

**Goal 1 says "work on most software projects."** Today that reads "most
GitHub projects". The default customer in `CLAUDE.md` — an organisation
repository with a protected base and a service-backed suite — describes a
GitLab shop exactly as well as a GitHub one, and sbxloop cannot serve it.
There is a concrete GitLab target motivating this work.

**But the abstraction is worth landing even if no second backend ever
ships.** The reason is `raw()`. The host's `GithubOps` exposes an escape
hatch for arbitrary REST calls, and seven modules outside the backend package
use it 50 times to build GitHub paths by hand:

```
$ grep -rn "ops\.raw(\|raw_lookup(ops\|raw_pages(ops" packages/sbxloop/src --include="*.py" \
    | grep -v "src/sbxloop/gh/ops.py" | cut -d: -f1 | sort | uniq -c | sort -rn
  12 deliver.py
  12 daemon/concierge.py
  11 daemon/sources.py
   6 engine/engine.py
   4 engine/landing.py
   3 cli/doctor.py
   2 engine/issue_lookup.py
```

Every one of those is a piece of GitHub knowledge living outside the module
that owns GitHub knowledge, untyped and untestable as an operation. That is a
defect at the current single-backend scale — it is why `deliver.py` and
`sources.py` each have to know GitHub's issue-state vocabulary — and closing
it pays for itself before the word GitLab is mentioned.

So the sequencing argument is: the forge-agnostic cleanup comes first, is
independently valuable, and is roughly 60% of the total cost. A second
backend is what proves it, not what motivates it.

## Where it plugs in (code seams, verified)

### The transport seam already exists and is in the right place

The host never talks to a forge directly — the credential split the project
is built on. Every operation is a `JobRequest(kind="github.op", op=…)`
executed inside a github-role sandbox that holds the token
(`daemon/github.py`, `gh/ops.py:_op`). The worker's registry is 14 named ops
plus `raw.api`
(`packages/sbxloop-worker/src/sbxloop_worker/githubops.py`).

**This seam does not move.** What a forge-neutral worker needs is three
parameters, not a redesign:

- base URL — already a knob (`[github] api_url`, for GHES)
- auth header style — `Authorization: Bearer` / `PRIVATE-TOKEN` / `token`
- pagination style — Link header / `X-Next-Page`

Keep `checks.failed_logs` and `blobs.create_many` as real ops, since they
stream and batch respectively. The `gh` CLI transport becomes a GitHub-only
optimisation; the stdlib urllib path is the universal one. Estimated worker
change: a few hundred lines.

### The host-side seam does not exist yet

`gh/ops.py` is 1777 lines and 38 public methods, and it *is* the GitHub
vocabulary rather than an implementation of an interface — 6 GraphQL
documents (review threads, resolve-thread, ready-for-review, checks rollup,
merge-queue enqueue, queue state) and REST paths inline. There is no
`VcsOps` protocol to implement against; it has to be extracted first.

Nine modules annotate `GithubOps` directly: `daemon/sources.py`,
`daemon/concierge.py`, `daemon/github.py`, `engine/landing.py`,
`engine/reconcile.py`, `engine/issue_lookup.py`, `engine/engine.py`,
`engine/checks.py`, `cli/doctor.py`, plus `deliver.py`.

### What is already neutral and must stay that way

The decision logic is in better shape than the vocabulary, and none of it
should move:

- `engine/checks.py` — the baseline comparison (is this red *ours*?) is pure
  and takes a folded verdict, not an API payload.
- `engine/landing.py` — the wait/hold state machine.
- `engine/review.py` — the review round, including one-round-for-bots.
- `gh/protection.py`'s `BaseRequirements` — the *shape* is already
  forge-neutral, including its three-state `source` field.
- The agent prompts. The agent never touches the forge; the host does. This
  is the single biggest reason the change is tractable.

## What "configurable" has to mean: capabilities, not URLs

Forges differ less in paths than in what exists at all. A design that only
swaps URLs will produce a loop that silently does the wrong thing on Gitea.

Because "fail closed on could not tell" is a project principle, a capability
cannot be a boolean. It needs the three states `BaseRequirements.source`
already uses:

- `SUPPORTED` — the backend does this.
- `UNSUPPORTED` — a real answer. Gitea has no merge queue, so the landing
  merges directly instead of enqueuing. Not an error.
- `UNKNOWN` — the probe could not decide. Names what it needed and stops.

The distinction between the last two is the whole point: an absent feature is
a design input, an unreadable one is a halt.

### Capability matrix

Verified 2026-09-12 against GitLab CE 19.3.2 (revision `34042bf7d00`,
`enterprise: false`) and Gitea 1.24.7, with the credential a run would hold:
a Developer on GitLab and a write collaborator on Gitea. The GitHub column is
what the GitHub backend already does. The evidence for each row is in
[Field verification](#field-verification-1016); the name in parentheses is
the one in `sbxloop.vcs.protocol.CAPABILITIES`.

| Capability                                                      | GitHub                            | GitLab CE 19.3.2                                                                                                                                         | Gitea 1.24.7                                                                                                                           |
| --------------------------------------------------------------- | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| merge queue / train (`merge_queue`)                             | merge queue                       | **none on CE**, verified: `GET /merge_trains` is 404, `merge_trains_enabled: null`. Premium merge trains are field-unverified                            | **none**, verified: no queue path; `merge_when_checks_succeed` is auto-merge, not a queue                                              |
| resolvable review threads (`review_threads`)                    | yes                               | **yes**, verified: resolve and reopen by `PUT .../discussions/:id`, reply by `POST .../discussions/:id/notes`; the id survives pushes                    | **no**, verified: no resolve and no reply path in the API; `resolver` is read-only                                                     |
| draft change (`draft_changes`)                                  | `draft` flag                      | **`Draft:` title prefix**, verified: sets `draft: true` and `detailed_merge_status: draft_status`; retitling clears it                                   | **`WIP:` title prefix**, verified: no draft field on create; the prefix sets `draft: true`; retitling clears it                        |
| request-changes review (`request_changes_review`)               | yes                               | **recorded, not enforced on CE**, verified: `reviewer_state=requested_changes` is accepted and the change stays `mergeable`. Premium is field-unverified | **yes**, verified: a `REQUEST_CHANGES` review blocks the merge when `block_on_rejected_reviews` is on                                  |
| host-minted short-lived token (`short_lived_token`)             | GitHub App                        | **none**, verified: a PAT or project access token, with an `expires_at` the token reads itself. OAuth application tokens are field-unverified            | **none**, verified: tokens have no expiry at all                                                                                       |
| remote commit, no checkout (`remote_commit`)                    | git data API                      | **yes**, verified: `POST /repository/commits` with `actions`; atomic, binary-safe, a new branch via `start_branch`                                       | **yes**, verified: `POST /contents` with `files`; atomic, binary-safe, a new branch via `new_branch`                                   |
| required-checks introspection (`required_checks_introspection`) | protection + rulesets + PR rollup | **yes, with no named checks on CE**, verified: a Developer reads the protected branch and "pipeline must succeed"; every status in the pipeline counts   | **yes**, verified: a write collaborator reads required contexts and approvals from `GET /branches/{b}`; the other flags are admin-only |
| bot identity signal (`bot_identity`)                            | `[bot]` / `__typename`            | **yes**, verified: `bot: true` on `GET /users/:id`, readable by a Developer; absent from a note's `author`                                               | **none**, verified: a `--user-type bot` account reads exactly like a human; the schema has no type field                               |
| API-created commits are signed (`signed_api_commits`)           | App credential only               | **no on a default install**, verified: `GET .../commits/:sha/signature` is 404. An instance with signing configured is field-unverified                  | **no on a default install**, verified: `verification.verified: false`, `gpg.error.not_signed_commit`                                   |
| author may approve own change                                   | policy-dependent                  | **allowed on CE**, verified: the author's `POST .../approve` is 201 and counts. The Premium setting that forbids it is field-unverified                  | **forbidden**, verified: 422 `approve your own pull is not allowed`                                                                    |

Three rows moved from the desk version. **Author may approve own change** was
"forbidden" on GitLab and is allowed on CE; it was "policy-dependent" on Gitea
and is forbidden. **Request-changes review** on GitLab was "paid tier"; CE
records the state and does not enforce it, so the backend reports the
capability unsupported rather than read a recorded state as a gate.
**Resolvable review threads** on Gitea was "weak" and is absent from the API.

Two rows still carry most of the risk. **Bot identity** is what "one round
for bots" depends on; GitLab has a signal and Gitea has none (decision below).
**Author may approve own change** feeds `BaseRequirements.blockers()`, whose
reasons need per-backend phrasing: on Gitea the loop can never approve its
own change, and on GitLab CE an approval never gates a merge at all.

## The interface

Six role protocols rather than one object, because they have different
consumers and different capability profiles:

```
RepoOps      repo_get, default_branch, ref_lookup, contents_read,
             branch_delete, merge_base
IssueOps     create, comment, label_add/remove, close, comments, events, search
ChangeOps    create, get, comment, set_title/body, request_reviewers,
             update_branch, merge, mark_ready, enqueue, queue_state
ReviewOps    submit_review, inline_comments, threads, reply, resolve
ChecksOps    head_verdict, failed_logs, required_contexts
PolicyOps    base_requirements
ContentOps   the remote, no-local-checkout commit path
```

`gh/` becomes `vcs/github/`; `vcs/gitlab/` and `vcs/gitea/` sit beside it.

One rule makes the whole thing hold:

> **`raw()` is private to `vcs/<backend>/`.** If engine or daemon code needs
> a path, it needs a named operation.

Without that rule the protocol is decorative — callers route around it the
moment something is missing, which is precisely how the current 50 sites
accumulated.

`ContentOps` deserves its own note. `deliver.py` builds commits remotely
through GitHub's git data API (`/git/trees`, `/git/commits`, `/git/refs`) for
the no-local-checkout path. GitLab and Gitea expose nothing shaped like it;
each needs its own implementation. This is the least portable corner of the
system. Verified (V5): both forges take a whole changeset in one atomic
call, so the role is reshaped around that call rather than around GitHub's
three methods.

## The domain model has to shed GitHub words

The shared types are mostly right. Four leak:

| Today                                       | Neutral form                                              |
| ------------------------------------------- | --------------------------------------------------------- |
| `PostedFinding.thread_node_id` (GraphQL)    | opaque backend-minted `thread_id: str`                    |
| `QueueEntry.state` (`MergeQueueEntryState`) | `queued\|testing\|mergeable\|blocked\|removed`            |
| `ReviewComment.side: LEFT\|RIGHT`           | stays as neutral *input*; backend resolves its own anchor |
| issue close `state_reason: "completed"`     | neutral close reason; not every forge has one             |

`ReviewComment` is subtler than it looks: GitLab positions a discussion with
base/start/head shas plus old and new path and line. The caller should keep
saying `(path, line, side)` and the backend should do that translation, or
the anchor vocabulary leaks into `engine/review.py`.

`MergeOutcome`, `CheckState`, `ChecksVerdict` and `BaseRequirements` need no
change — each backend maps its own status codes and rule model into them.
`ghids.py` gains `gl:` and `gt:` prefixes alongside `gh:`, with the legacy
bare `gh:<n>` form still normalising.

## Configuration

```toml
[vcs]
kind = "gitlab"          # github | gitlab | gitea
api_url = "..."          # generalises the existing github.api_url

[[vcs.repos]]
repo = "group/project"
kind = "github"          # per-repo override
```

Per-repo `kind` is worth having rather than a global-only switch:
`CompositeSource` already routes work items by id prefix, so one daemon
tending repositories on two forges falls out of the id-grammar change almost
for free. *(Assumption, stated so it can be rejected: if a mixed fleet is
explicitly not wanted, the per-repo key should be dropped rather than shipped
untested.)*

`[github]` stays as a deprecated alias mapping to `[vcs] kind = "github"`.
There is precedent: `GithubConfig._normalise_repos` already folds legacy
`github.repo` into `github.repos`.

Every new key lands in three places (model, `sbxloop.toml.example`,
user-guide table) per the standing rule, and `doctor` gains a capability row
per configured backend so an `UNKNOWN` is visible before a run hits it rather
than after.

## Credentials: an accepted reduction, stated plainly

GitHub App installation auth (`gh/appauth.py`, 407 lines) mints a
short-lived token on the host, signed with a private key the sandbox never
sees, and `Provisioner.gh_refresher` rotates it into a live sandbox.

**GitLab and Gitea have no equivalent.** The closest is a long-lived
project or group access token.

The proposal is to accept this rather than gate on it, and to say so
out loud:

- The credential split is unchanged. The token still lives only inside the
  github-role sandbox; the host holds it only long enough to write the env
  file, exactly as a PAT works today.
- What is lost is expiry. A leaked GitHub App token is useful for about an
  hour; a leaked GitLab project token is useful until someone revokes it.
- The mitigation is scope and rotation discipline, both the operator's:
  a project-scoped token, not a personal one, with an expiry date set at
  creation.
- `doctor` should report the credential kind and, where the forge exposes it,
  the token's expiry — so "this token never expires" is a visible fact rather
  than an assumption. Verified (V6): a GitLab token reads its own
  `expires_at`; a Gitea token has no expiry to read or set.

This is a real reduction in posture for non-GitHub backends. It is accepted
because the alternative is not shipping, and because the blast radius is
bounded by the sandbox boundary that already exists.

## Testing

This is what sets the cost, given the standing rule that forge behaviour is
tested against a fake and the ops layer is never stubbed around it.

- **A conformance suite** — one parameterised module running identical
  scenarios against every backend's fake, with capability-gated skips. This
  is the forge analogue of `tests/fixtures/ecosystems/`, and it is what makes
  "supported" a testable claim rather than a table in a document.
- **`fake_gitlab.py` and `fake_gitea.py`** at `fake_github.py`'s fidelity
  (894 lines). This is unavoidable and is a large share of the work.
- **The 27 existing test modules that ride `fake_github.py`** should mostly
  end up backend-agnostic, asserting against `ChecksVerdict` and
  `BaseRequirements` directly. How many resist that conversion is the
  feedback signal: if the decision logic cannot be tested without a forge,
  the seam is in the wrong place and the design should be revised before a
  second backend is written.
- `tests/unit/test_code_run_trail.py` must stay byte-identical through the
  whole forge-agnostic phase. A change in that fixture during steps 1–3 means
  something leaked.

## Sequence and cost

| Step  | Work                                                                | Backend added |
| ----- | ------------------------------------------------------------------- | ------------- |
| 1     | Remove `raw()` from outside the backend package — 50 call sites     | none          |
| 2     | Extract `VcsOps` + capability model; `gh/` → `vcs/github/`          | none          |
| 3     | Neutralise the domain model (thread id, queue state, review anchor) | none          |
| 4     | `[vcs] kind`, per-repo override, `[github]` alias, doctor rows      | none          |
| 5     | Conformance suite; GitHub passes it                                 | none          |
| 6–9   | GitLab backend                                                      | GitLab        |
| 10–11 | Gitea backend                                                       | Gitea         |

Steps 1–5 change no behaviour, add no backend, and are roughly 60% of the
total cost. Rough sizing, one engineer with agent help: steps 1–5 about three
to four weeks, GitLab three to four, Gitea one and a half to two. Gitea is
cheaper in API surface but more expensive in fail-closed paths, because it
has the weakest introspection of the three.

Order within 6–9 should be: read paths first (repo, issue, checks), then
review, then landing, then the remote-commit path — landing last because it
is where an unverified capability assumption does visible damage.

## What must be field-verified before writing backend code

Each of these can invalidate part of the design above.

| ID  | Question                                                                         | What it changes                |
| --- | -------------------------------------------------------------------------------- | ------------------------------ |
| V1  | Can a GitLab MR discussion be resolved by API, and is the id stable?             | `ReviewOps.resolve`            |
| V2  | What does GitLab report as required before merge, readable by a non-owner?       | `PolicyOps`, fail-closed paths |
| V3  | Is there any bot-identity signal on GitLab and Gitea?                            | one-round-for-bots             |
| V4  | Does Gitea expose branch protection richly enough to avoid gating on everything? | `BaseRequirements.source`      |
| V5  | Can a commit be created remotely, without a checkout, on both?                   | `ContentOps` feasibility       |
| V6  | Do project access tokens expose an expiry readable by the token itself?          | the doctor row above           |

V5 is the one that could force a structural answer: if the no-local-checkout
path cannot be built on GitLab or Gitea, that path becomes GitHub-only and
the capability model has to carry it. All six are answered in the next
section, and V5 did not force it.

## Field verification (#1016)

Run 2026-09-12 against two forges in Docker, from the harness in
`tests/live/` (see its `README.md`): `gitlab/gitlab-ce:19.3.2-ce.0` and
`gitea/gitea:1.24.7`, both serving HTTPS from a throwaway CA. `GET /api/v4/version` answered `{"version": "19.3.2", "revision": "34042bf7d00"}`
and `GET /api/v4/metadata` added `"enterprise": false`; `GET /api/v1/version`
answered `{"version": "1.24.7"}`.

The seeds built the same shape on both: `acme/widgets` with a README on
`main`, a second branch with an open change and a review comment on it, a
labelled open issue and a closed issue; `main` protected (no direct push, one
approval, required checks `ci` and `lint`, or on GitLab CE the nearest
settings: pipeline must succeed, discussions must be resolved); statuses on
both heads. The credentials are the ones a run would hold, not an owner's:

| Identity                | GitLab                                                | Gitea                                                                 |
| ----------------------- | ----------------------------------------------------- | --------------------------------------------------------------------- |
| the run's credential    | `dev-alice`, Developer, personal access token `api`   | `dev-alice`, write collaborator, token `write:repository,write:issue` |
| a second human reviewer | `rev-bob`, Developer                                  | `rev-bob`, write collaborator                                         |
| a bot                   | project access token at Developer (`project_1_bot_*`) | `ci-bot`, `gitea admin user create --user-type bot`                   |
| setup only              | `root`                                                | `sbx-admin`, site admin                                               |

`tests/live/fieldverify.py` is every request below, runnable again;
`tests/live/test_field_verify.py` holds both forges to these answers.
Responses are trimmed to the fields a finding rests on.

### V1: can a review thread be resolved by API, and is its id stable?

**GitLab: yes, and yes.** As `rev-bob` (Developer):

```
POST /api/v4/projects/1/merge_requests/2/discussions
{"body": "beta looks wrong",
 "position": {"base_sha": "33da92ad...", "start_sha": "33da92ad...", "head_sha": "9ddd6f5e...",
              "position_type": "text", "old_path": "v1-986c31.txt", "new_path": "v1-986c31.txt", "new_line": 2}}
-> 201 {"id": "193bff4c510488bf6c79352ff0e86478e48599d9", "individual_note": false,
        "notes": [{"id": 3, "type": "DiffNote", "resolvable": true, "resolved": false, ...}]}
```

As `dev-alice` (Developer, the change's author):

```
POST /api/v4/projects/1/merge_requests/2/discussions/193bff4c.../notes {"body": "fixed in the next push"} -> 201
PUT  /api/v4/projects/1/merge_requests/2/discussions/193bff4c... {"resolved": true}
-> 200 {"id": "193bff4c...", "notes": [{"id": 3, "resolvable": true, "resolved": true,
        "resolved_by": {"username": "dev-alice", ...}}, ...]}
GET  /api/v4/projects/1/merge_requests/2 -> 200 {"blocking_discussions_resolved": true, ...}
```

Then two pushes to the source branch through the commits API, one touching
another file and one rewriting the discussed line. After each, `GET .../discussions/193bff4c...` answered the same id, still `"resolved": true`;
after the rewrite GitLab had moved the note's `position.head_sha` to the new
head (`6362d378...`). `PUT ... {"resolved": false}` as the reviewer reopened
it. The discussion id is a 40-hex string, the note ids are integers.

**Gitea: no.** The review comment carries a `resolver` field
(`GET /api/v1/repos/acme/widgets/pulls/1/reviews/1/comments -> 200 [{"id": 2, "path": "one.txt", "position": 2, "resolver": null, ...}]`), but the API has no
way to set it: the 1.24.7 swagger document has no path naming "resolve", and
the only review paths under `/pulls/{index}/` are `requested_reviewers`,
`reviews`, `reviews/{id}`, `reviews/{id}/comments`, `reviews/{id}/dismissals`
and `reviews/{id}/undismissals`. There is no reply-to-comment path either.
Resolution exists in Gitea's web UI only.

**Consequence.** `ReviewOps.resolve` is a real operation on GitLab keyed by
the discussion id, which is the opaque `thread_id` the neutral model already
carries (#1018). On Gitea `review_threads` is `UNSUPPORTED`: the loop posts
findings and answers them with change-level comments, and never waits on a
resolution it cannot make.

### V2: what does GitLab report as required before a merge, to a non-owner?

Every read below was made as `dev-alice`, Developer.

```
GET /api/v4/metadata -> 200 {"version": "19.3.2", "enterprise": false}
GET /api/v4/projects/1/protected_branches/main
-> 200 {"name": "main", "push_access_levels": [{"access_level": 0, "access_level_description": "No one"}],
        "merge_access_levels": [{"access_level": 30, "access_level_description": "Developers + Maintainers"}],
        "allow_force_push": false}
GET /api/v4/projects/1
-> 200 {"only_allow_merge_if_pipeline_succeeds": true, "only_allow_merge_if_all_discussions_are_resolved": true,
        "allow_merge_on_skipped_pipeline": false, "merge_method": "merge",
        "permissions": {"project_access": {"access_level": 30}}}
GET /api/v4/projects/1/approvals                         -> 404
GET /api/v4/projects/1/approval_rules                    -> 404
GET /api/v4/projects/1/external_status_checks            -> 404
GET /api/v4/projects/1/merge_requests/1/approval_state   -> 404
GET /api/v4/projects/1/merge_requests/1/approvals
-> 200 {"approved": false, "approved_by": [], "user_can_approve": true, "user_has_approved": false}
```

The same 404s answered the administrator, and the seed's attempts to require
an approval (`POST /projects/1/approvals`, `POST /projects/1/approval_rules`)
were 404 too: on CE these are not forbidden, they do not exist.

A fresh merge request with no discussions, read after each status was posted
(`POST /api/v4/projects/1/statuses/:sha`, as the same Developer):

| Head pipeline                                               | `detailed_merge_status` |
| ----------------------------------------------------------- | ----------------------- |
| no status reported                                          | `ci_must_pass`          |
| `ci` running                                                | `ci_still_running`      |
| `ci` success, `lint` never reported                         | `mergeable`             |
| a `docs` status failed, posted with `"allow_failure": true` | `ci_must_pass`          |
| `lint` failed                                               | `ci_must_pass`          |

`allow_failure` on an external status is ignored: the response echoed
`"allow_failure": false`. `PUT .../merge_requests/3/merge` with a red status
answered `405 {"message": "405 Method Not Allowed"}`, the same refusal shape
`MergeOutcome.blocked` already reads. The seeded merge request, whose
discussion was open, reported `discussions_not_resolved`.

**Answer.** On CE a Developer can read everything that gates the merge: the
protected branch, the two project settings, and the merge request's own
verdict in `detailed_merge_status`. What CE cannot express is a *named*
required check. When "pipeline must succeed" is on, every status and job the
head pipeline reports is required (CI jobs marked `allow_failure` are the
documented exception; not exercised here, no runner, **field-unverified**).
Required approvals do not exist on CE. Premium approval rules and external
status checks were not available to test and are **field-unverified**.

**Does a backend have to gate on everything?** No. The forge itself gates on
the whole pipeline, so "the pipeline" is exactly what the base requires, and
the baseline comparison still works: a red on the change that is also red on
the base's latest pipeline is not the loop's, even though GitLab will refuse
the merge until someone fixes it (the landing holds for a human instead of
looping on it).

**`BaseRequirements` on GitLab (#1017).**

- `source = "protected_branch+project"` when both reads answered;
  `"unknown"` when either did not (a 401, a 403, or a project the token
  cannot see), with `unread` naming which.
- A new flag, `all_checks_required`, carries "pipeline must succeed": the
  required set is whatever the change's pipeline reports, so
  `required_contexts` is `()` and a caller treats every reported check as
  required. With the setting off, nothing is required and `source` still
  says the settings were read.
- `approvals_required = 0` when `/metadata` says `enterprise: false`: a real
  answer, CE cannot require one. On an enterprise instance the approval-rule
  read decides, and an unreadable one leaves the count `None`.
- `conversation_resolution` from `only_allow_merge_if_all_discussions_are_resolved`.

### V3: is there a bot-identity signal?

**GitLab: yes, on the user, one lookup away.**

```
GET /api/v4/user                  (as the project access token) -> 200 {"id": 4, "username": "project_1_bot_05b4100c...", "name": "ci-bot", "bot": true}
GET /api/v4/users/4               (as dev-alice, Developer)     -> 200 {"id": 4, "username": "project_1_bot_05b4100c...", "bot": true}
GET /api/v4/users/3               (as dev-alice)                -> 200 {"id": 3, "username": "rev-bob", "bot": false}
GET /api/v4/users?username=project_1_bot_05b4100c...            -> 200 [{"id": 4, "username": "...", "name": "ci-bot", "state": "active"}]
POST /api/v4/projects/1/merge_requests/1/notes (as the bot)     -> 201 {"author": {"id": 4, "username": "project_1_bot_...", "state": "active", ...}}
GET /api/v4/projects/1/members/all (as dev-alice)               -> 200 [{"username": "root", "access_level": 50}, ...]
```

The `bot` flag is on `GET /users/:id` and nowhere a review is read from: not
on a note's `author`, not on a user search, not on a member list. The
username pattern `project_<id>_bot_<hex>` is GitLab's convention for project
access tokens; it corroborates, it is not the signal. Group access tokens and
Premium service accounts were not exercised (**field-unverified**).

**Gitea: no.** `GET /api/v1/users/ci-bot` and `GET /api/v1/users/rev-bob`,
both as the write collaborator, return the same keys (`active`,
`avatar_url`, `created`, `description`, `email`, `followers_count`, ...,
`visibility`, `website`), and no value differs but the identity and its timestamps; the swagger
`User` definition has no type field. `GET /api/v1/users/gitea-actions` is
`404 user redirect does not exist`. What an Actions-created status or comment
reports as its author needs a runner and is **field-unverified**.

**Decision (V3).**

- **GitLab: the signal.** A reviewer is a bot when `GET /users/:id` says
  `bot: true`, looked up once per distinct author per read and cached for the
  run. A lookup that fails leaves the kind `None`, which the identity model
  already treats as "not known", never as "human".
- **Gitea: an operator-supplied list, empty by default, so every reviewer is
  human until listed.** Treating a bot as human costs rounds; treating a
  human as a bot drops their feedback after one answer, which is the failure
  the project's one-round rule must never cause. The list is a per-repository
  config key with a `[github]`-level default and lands with the Gitea backend
  (#1021), in the three places.

### V4: does Gitea expose branch protection richly enough?

As `dev-alice`, write collaborator (`"permissions": {"admin": false, "push": true, "pull": true}`):

```
GET /api/v1/repos/acme/widgets/branch_protections      -> 403 {"message": "user should be an owner or a collaborator with admin write of a repository"}
GET /api/v1/repos/acme/widgets/branch_protections/main -> 403 (same message)
GET /api/v1/repos/acme/widgets/branches/main
-> 200 {"name": "main", "protected": true, "required_approvals": 1, "enable_status_check": true,
        "status_check_contexts": ["ci", "lint"], "user_can_push": false, "user_can_merge": true,
        "effective_branch_protection_name": ""}
```

The rest of the rule is admin-only. As the site administrator the same rule
also says `block_on_rejected_reviews`, `block_on_official_review_requests`,
`block_on_outdated_branch`, `dismiss_stale_approvals`,
`require_signed_commits`, `protected_file_patterns`,
`enable_approvals_whitelist` and `block_admin_merge_override`.

A fresh pull request, with the merge attempted as the write collaborator
after each step (`POST /api/v1/repos/acme/widgets/pulls/{n}/merge {"Do": "merge"}`):

| Statuses on the head                                | `GET /commits/{sha}/status` | Merge                                           |
| --------------------------------------------------- | --------------------------- | ----------------------------------------------- |
| none                                                | `state: ""`, no statuses    | `405 Not all required status checks successful` |
| `ci` success, `docs` success, `lint` never reported | `state: "success"`          | `405 Not all required status checks successful` |
| `lint` failure added                                | `state: "failure"`          | `405 Not all required status checks successful` |

**Answer.** Yes. The required contexts and the approval count are readable
without admin, from the branch rather than the protection endpoint, and the
required set is distinguishable from what ran: the combined status said
`success` while a required context had never reported, and Gitea refused the
merge for exactly that. Context patterns (Gitea accepts globs in
`status_check_contexts`) were not exercised and are **field-unverified**.

**`BaseRequirements` on Gitea (#1021).** `source = "branch"` from `GET /branches/{base}`: `required_contexts` is `status_check_contexts` when
`enable_status_check`, else `()`; `approvals_required` is
`required_approvals`. The flags only an admin reads are left unknown for a
non-admin credential, with `unread = ("protection",)`, so `signed_commits`
and friends are never read as conclusively off; with an admin credential,
`source = "branch+protection"`.

### V5: can a commit be created remotely, without a checkout?

**GitLab: yes.** As `dev-alice`, Developer:

```
POST /api/v4/projects/1/repository/commits
{"branch": "v5/...", "start_branch": "main", "commit_message": "...",
 "actions": [{"action": "create", "file_path": "v5/.../a.txt", "content": "a\n"},
             {"action": "create", "file_path": "v5/.../b.bin", "content": "iVBORw0KGgoAAAANSUhEUgD//oA=", "encoding": "base64"},
             {"action": "update", "file_path": "README.md", "content": "...", "encoding": "base64"}]}
-> 201 {"id": "<sha>", "parent_ids": ["<main head>"], "stats": {...}}
POST (same branch) {"actions": [{"action": "delete", ...}, {"action": "move", "previous_path": "...b.bin", "file_path": "...c.bin"}]}
-> 201 {"parent_ids": ["<the first commit>"]}
POST (same branch) {"actions": [{"action": "create", "file_path": ".../ok.txt"}, {"action": "update", "file_path": ".../missing.txt"}]}
-> 400; the branch head did not move and ok.txt does not exist
POST {"branch": "main", "actions": [{"action": "create", ...}]}
-> 403 {"message": "403 Forbidden - You are not allowed to push into this branch"}
```

The binary file read back byte-identical (`GET .../repository/files/:path`,
base64 `content`).

**Gitea: yes.** As `dev-alice`, write collaborator:

```
POST /api/v1/repos/acme/widgets/contents
{"branch": "main", "new_branch": "v5/...", "message": "...",
 "files": [{"operation": "create", "path": "v5/.../a.txt", "content": "YQo="},
           {"operation": "create", "path": "v5/.../b.bin", "content": "iVBORw0KGgoAAAANSUhEUgD//oA="},
           {"operation": "update", "path": "README.md", "sha": "3f49aa96...", "content": "..."}]}
-> 201 {"commit": {"sha": "9d8b0135...", ...}, "files": [...]}
POST (same branch) {"files": [{"operation": "delete", "sha": "..."}, {"operation": "update", "from_path": "...b.bin", "path": "...c.bin", "sha": "...", "content": "..."}]}
-> 201, parent is the first commit
POST (same branch) {"files": [{"operation": "create", "path": ".../ok.txt"}, {"operation": "update", "path": ".../missing.txt", "sha": "000..."}]}
-> 500 with an empty body; the branch head did not move and ok.txt does not exist
POST {"branch": "main", "files": [{"operation": "create", ...}]} -> 403, empty body
POST /api/v1/repos/acme/widgets/contents/v5/.../single.txt {"branch": "v5/...", "content": "b25lCg=="} -> 201
```

The binary file read back byte-identical.

**Decision (V5): `ContentOps` on both.** Neither forge has GitHub's
blob/tree/commit/ref vocabulary, and neither needs it: both take a whole
changeset (create, update, delete, move; binary as base64) in one atomic
call, onto a new branch cut from a base, and both refuse a protected base
with 403. What the capability model must carry:

- `remote_commit` is `SUPPORTED` on all three. The GitHub-shaped methods on
  `ContentOps` (`blobs_create_many`, `tree_create`, `commit_create`,
  `ref_create`) become private to the GitHub backend, and the role gains one
  neutral operation that commits a changeset onto a branch (#1020).
- The changeset must say create versus update: GitLab refuses an `update` of
  a missing file and a `create` of an existing one, and Gitea wants the
  current blob `sha` for an update or a delete. A backend reads the base tree
  first rather than guess.
- A refusal is not always classifiable: Gitea answers a bad operation with a
  bare 500. Atomicity held on both, so the caller treats any non-2xx as
  "nothing was written" and stops, never as a transient to retry.
- Moving an existing branch to an unrelated commit (`ref_force_update` on
  GitHub) was not exercised on either forge: GitLab's `force` parameter on the
  commits API and Gitea's lack of one are **field-unverified** until #1020.

### V6: does a token expose an expiry readable by itself?

**GitLab: yes.**

```
GET /api/v4/personal_access_tokens/self (as dev-alice's personal access token)
-> 200 {"id": 2, "name": "sbxloop-live", "scopes": ["api"], "active": true, "revoked": false, "expires_at": "2026-11-11", "user_id": 2}
GET /api/v4/personal_access_tokens/self (as the project access token)
-> 200 {"id": 4, "name": "ci-bot", "scopes": ["api"], "active": true, "revoked": false, "expires_at": "2026-11-11", "user_id": 4}
GET /api/v4/projects/1/access_tokens (as the project access token, and as dev-alice) -> 401
```

The same endpoint answers for both kinds; listing the project's tokens needs
more than Developer.

**Gitea: no, because there is none to read.**

```
GET /api/v1/users/dev-alice/tokens (token auth)       -> 401 {"message": "auth required"}
GET /api/v1/users/dev-alice/tokens (basic auth)       -> 200 [{"id": 2, "name": "sbxloop-live", "scopes": [...], "created_at": "...", "last_used_at": "...", ...}]
swagger AccessToken:             created_at, id, last_used_at, name, scopes, sha1, token_last_eight
swagger CreateAccessTokenOption: name, scopes
```

A token cannot read its own record (basic auth only, even for a site-admin
token), and there is no expiry to read or to set.

**Consequence for the doctor credential row (#1019).** GitLab: read
`/personal_access_tokens/self` and report `expires_at` and `scopes`, failing
the row when `active` is false. Gitea: report "this token never expires" as
a fact of the forge, not as an unread value.

### Gate

Neither V2 nor V4 means a backend gates on every check on every run. GitLab
CE gates on the whole pipeline because the forge does, and the loop can still
tell its red from the base's; Gitea names its required contexts to a
non-admin. #1017 can start.

## Recommendation

Land steps 1–5 regardless of whether a second backend is ever approved. They
are a defect fix (`raw()` leakage), a testability improvement (capabilities
become assertable), and a doctor improvement, and none of them depends on
GitLab being funded.

Answer V1–V6 against live instances before starting step 6. If V2 or V4 come
back badly enough that a backend would have to gate on every check on every
run, say so and reconsider the scope — a loop that can never distinguish its
own red from someone else's on a given forge is a worse product than no
support for that forge.
