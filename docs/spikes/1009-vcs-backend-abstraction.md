# Spike: a configurable version-control backend (GitHub, GitLab, Gitea)

Status: **design proposal — tracked as an epic, no implementation landed
yet.** The epic is
[issue #1009](https://github.com/brettbergin/sbxloop/issues/1009); its
child issues follow the sequence at the end of this document, with the
field-verification questions filed as their own gate between the
forge-agnostic steps and the first backend.

Code-seam claims below were read off this tree and are reproducible with the
commands quoted beside them. **Every claim about GitLab and Gitea API
behaviour is field-unverified** — desk knowledge, not checked against a live
instance — and is marked as such where it is load-bearing.

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

**Field-unverified for the GitLab and Gitea columns.** Confirming this table
against live instances is the first task of any implementation, and several
rows below are the ones most likely to be wrong.

| Capability                     | GitHub                 | GitLab                | Gitea             |
| ------------------------------ | ---------------------- | --------------------- | ----------------- |
| merge queue / train            | merge queue            | merge trains (paid)   | none              |
| resolvable review threads      | yes                    | discussions           | weak              |
| draft change                   | `draft` flag           | `Draft:` title prefix | draft flag        |
| request-changes review         | yes                    | paid tier             | yes               |
| host-minted short-lived token  | GitHub App             | none                  | none              |
| remote commit (no checkout)    | git data API           | commits API + actions | contents API      |
| required-checks introspection  | protection + rulesets  | protected branches +  | branch protection |
|                                | + PR rollup            | approval rules        |                   |
| bot identity signal            | `[bot]` / `__typename` | `bot` user flag       | none              |
| API-created commits are signed | App credential only    | no                    | no                |
| author may approve own change  | policy-dependent       | forbidden             | policy-dependent  |

Two rows carry most of the risk. **Bot identity** is what "one round for
bots" depends on; with no signal, Gitea either treats every reviewer as human
(safe, slower) or needs an operator-supplied list of bot logins. **Author may
approve own change** feeds `BaseRequirements.blockers()`, whose current
reasons are written in GitHub's terms and will need per-backend phrasing.

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
each needs its own implementation behind the same three methods. This is the
least portable corner of the system.

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
  than an assumption.

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
the capability model has to carry it.

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
