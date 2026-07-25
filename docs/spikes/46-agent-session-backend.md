# Spike: native agent-session execution backend (issue #46)

Status: **feasibility report — no implementation commitment yet.**
Desk research done 2026-07-24 against the `docker/docs` source tree and
`docker/sbx-releases` (sbx 0.37.0). Field round 1 done 2026-07-25 on sbx
v0.35.0 (results:
[issue #96](https://github.com/brettbergin/sbxloop/issues/96); analysis in
the round-1 section below). Round 2: run
[`scripts/spike-46-agent-session-probe.sh`](../../scripts/spike-46-agent-session-probe.sh)
on an sbx-capable machine and paste the report into
[issue #46](https://github.com/brettbergin/sbxloop/issues/46).

## Why

Field testing (0.1.8–0.1.9, sbx 0.35) showed sbx's proxy secret injection
not reaching the worker under `sbx exec`, so provisioning auto-falls-back to
a plain in-VM env file (`sandbox.secret_env_fallback`). That concedes sbx's
core security property: the Copilot PAT sits in cleartext inside a VM whose
whole job is to run untrusted agent tool calls.

## Where a backend would plug in (code seams, verified)

The abstraction point already exists and is narrow:

- `PhaseRunner` (engine/phases.py) is the only producer of agent work, and
  it talks exclusively to `WorkerClient.submit(JobRequest) -> JobResult`
  (worker/client.py). Every phase goes through `_agent_job()`; VERIFY uses
  `shell()` on the same client.
- `JobRequest`/`JobResult` (sbxloop_worker/protocol.py) already model
  everything a session needs: prompt, system_message, model,
  permission_mode, expect, cwd, resume_session_id.
- The in-VM worker owns protocol framing: JSONL events + authoritative
  result file; host stdout is telemetry only.

So any variant is a **host-side transport/launch swap, not a protocol
change**, selectable as `[sbx] backend = "exec" | "agent-session"` next to
the existing `secret_strategy`/`worker_transport` knobs, defaulting to the
current behavior.

## What desk research established (docs source + release notes + issues)

All from the `docker/docs` markdown source (`content/manuals/ai/sandboxes/`,
`data/sbx_cli/*.yaml`) and `docker/sbx-releases` issues — the rendered docs
pages are JS-only, as previously established. **Every load-bearing claim
below still needs field confirmation** (project standing rule); the probe
script maps to these directly.

### Session surface

- `sbx run [flags] [AGENT] [PATH...] [-- AGENT_ARGS...]` — runs an agent in
  a sandbox, creating it if absent. Native agents: `claude, codex, copilot, cursor, docker-agent, droid, gemini, kiro, opencode, shell`. Each has a
  default startup command — notably `claude --dangerously-skip-permissions`,
  `copilot --yolo`, `shell` → `bash -l` (sbx runs agents unattended-style by
  default, same trust model as our `approve_all` inside the microVM).
- Args after `--`: a leading `-` flag is **appended** to the default
  command; a bare word **replaces** it. Documented one-shots:
  `sbx run copilot -- -p "review this PR"` (print mode) and
  `sbx run shell -- -c "cmd"` (arbitrary command as the session).
- Re-attach: `sbx run --name <sandbox>` (0.33+). Re-attaching re-launches
  the agent entrypoint; conversation resume is the agent's own job
  (`-- --continue` for claude). Sandboxes persist after the agent exits.
- No headless session API: no `--prompt/--json` at the sbx level, sessions
  appear PTY-based (TTY-size issues #63/#220), the sandboxd HTTP API's
  OpenAPI spec is unpublished (open issue #139). Docker's own documented
  CI/headless recipe uses `sbx exec`, not `run`.
- New alternative channel: experimental SSH endpoint (0.34, expanded 0.37)
  supports one-shot non-interactive commands (`ssh -T <name>@sbx sh`).

### Secrets in sessions vs exec — including a conflict with our field data

- Mechanism (security/credentials.md): the env var inside the sandbox is a
  **placeholder** (custom secrets: e.g. `sbx-cs-<rand>`; service secrets:
  shaped like the real thing, e.g. `GH_TOKEN=gho_sbxproxymanaged...` per
  issue #231 — the shape is deliberate because agents sniff prefixes). When
  the placeholder appears **anywhere in a request** to a configured host,
  the egress proxy substitutes the real value. Real credential never enters
  the VM.
- **`--placeholder` is settable for custom secrets** (the Amp kit tutorial
  uses `--placeholder "sgamp-{rand}"`). This defuses what we thought was
  the sharpest risk: the Copilot SDK's client-side token-format check
  (`gho_`/`ghu_`/`github_pat_` prefixes) can be satisfied by giving
  `COPILOT_GITHUB_TOKEN` a `github_pat_…`-shaped placeholder. The PAT→token
  exchange request goes to `api.github.com` (the configured host), the
  proxy rewrites the placeholder in flight, and the exchanged Copilot
  bearer comes back into SDK memory as a real token — the proxy never needs
  to touch `api.githubcopilot.com` traffic.
- **Discrepancy to resolve**: issue #348's repro observes custom-secret
  placeholder env via `sbx exec -it <sb> bash`, i.e. placeholders ARE set
  sandbox-wide and visible to exec — while our 0.1.8/0.1.9 field tests
  (also sbx 0.35) found the env invisible to exec, which is the entire
  reason the plain-env fallback exists. Candidate explanations: `-it` vs
  plain exec, secret scoping/timing (`-g` secrets only apply to sandboxes
  created afterward; sandbox-scoped apply immediately), version drift, or
  the #348 name-collision bug family. Issue #252 shows the opposite split
  too (env visible in exec shells but NOT in the `sbx run` agent process) —
  env sourcing genuinely differs per channel. **The probe's P3 matrix
  (run-session / exec-tty / exec-plain / exec-login) settles this.**
- Related sharp edges: custom secret env names colliding with built-in
  service vars silently don't inject (#348, open); since 0.35 host env vars
  no longer auto-inject (`sbx secret import` instead); `sbx inspect` (0.35+)
  lists injected secrets — useful for doctor/#52.

### Arbitrary entrypoints (the "worker as session" path)

Two documented paths make our own process the session entrypoint:

1. **Shell agent one-shot**: `sbx run shell -- -c "<worker cmd>"` — and the
   shell agent's docs explicitly frame it with proxy-held credentials
   ("credentials are never stored inside the VM"). Injection keys on
   destination host, not on which process is the entrypoint.
2. **Custom agent kits** (customize/build-an-agent.md): a `spec.yaml` with
   `sandbox.entrypoint.run: [any, argv]`, `environment.proxyManaged: [VAR]`,
   `network.serviceDomains`, `credentials.sources` — run via
   `sbx run --kit ./dir/ <name>`, re-attachable by name since 0.35. Kit
   startup commands run with no terminal attached.

## Revised feasibility picture

The issue's Q1/Q2/Q4 are now answered on paper; what remains is narrower
and mostly a single fork:

**If P3 shows placeholder env visible to plain `sbx exec`** (as #348
suggests), the entire feature may collapse into the *current* backend:
register `COPILOT_GITHUB_TOKEN` with a token-shaped `--placeholder`, keep
launching the worker via exec, and the plain-env fallback simply stops
triggering (`_verify_secret_env` already passes the moment the env becomes
visible — that forward-compat path was built in). Proxy-held secrets with a
~10-line provisioning change. This would be the best possible outcome and
makes running the probe worth it regardless of #46's fate.

**If P3 confirms exec never sees placeholders**, the viable architectures,
ranked by how much of the existing stack survives:

1. **Session-wrapped worker** — launch `python -m sbxloop_worker` as the
   session via `sbx run --name <agent-sbx> -- -c "<worker argv>"` (shell
   agent) or a small sbxloop kit. Protocol, transcripts, result files,
   read-only critic enforcement, checkpoint/resume all unchanged; only the
   launch path in `WorkerClient` forks on the backend config. Needs P2
   (non-TTY `run`) to pass; the SSH channel (0.37) is a fallback launch
   transport with possibly different env semantics.
2. **Native copilot-agent session driven by the host** — pipe phase prompts
   through `sbx run copilot -- -p "..."`. Loses the worker protocol
   (transcript fidelity, structured JSON extraction, result files,
   read-only SCRUTINIZE enforcement) and gains nothing over (1). Not
   recommended.
3. **PTY driver** for strictly-interactive sessions — only if P2 fails;
   high complexity, likely not worth it versus plain-env + per-phase
   network policy (#49).

Checkpoint/resume interacts cleanly with (1): sandboxes persist after
session exit and re-attach re-launches the entrypoint, which matches the
existing model (resume = fresh pair + re-run uncommitted phases; sandboxes
stay cattle).

## Field-verification protocol

`scripts/spike-46-agent-session-probe.sh` — dummy-secret-only, timeboxed,
self-cleaning; runnable on the sbx machine or the e2e workflow environment.

| Probe | Verifies                                                                        | Gates             |
| ----- | ------------------------------------------------------------------------------- | ----------------- |
| P1    | Installed flag surface (`run`, `set-custom --placeholder`, `inspect`, `kit`)    | everything        |
| P2    | `sbx run --name X -- -c "cmd"` with no TTY                                      | architecture 1    |
| P3    | Placeholder visibility matrix: run-session / exec -it / exec plain / exec login | the fork above    |
| P4    | Token-shaped `--placeholder` accepted                                           | SDK compatibility |
| P5    | Sandbox/session lifecycle after exit                                            | resume model      |

One follow-up needs a real PAT and is deliberately not in the probe script:
end-to-end SDK-through-proxy (register the real PAT with a
`github_pat_…{rand}` placeholder, run one minimal Copilot SDK call in
whichever channel P3 says sees the placeholder, confirm the exchange
succeeds). Do this only after P1–P4 look right.

## Field results — round 1 (sbx v0.35.0, 2026-07-25, issue #96)

What round 1 settled:

- **Custom-secret placeholder env is NOT visible to exec** — plain, login
  shell, or TTY — *when the secret is registered after sandbox creation*
  (which is both what the probe did and what sbxloop's provisioner does).
  This reconfirms the 0.1.8 finding; sbx-releases#348's `-it` repro did not
  reproduce. `inspect` even reported `Secrets: none` right after a
  successful sandbox-scoped `set-custom` — attach semantics on 0.35 are
  murky.
- **`sbx run --name X -- -c "cmd"` exits 0 but returns no command output**
  (banners only), so round 1 proved nothing about whether the command ran
  or what env a session sees. Bare `run` without a TTY hangs until killed
  (`inspect exec: context deadline exceeded`). Consequence either way: a
  session-launched worker could only use the poll transport (events file
  tailed via exec) — the stream transport's stdout channel does not exist
  over `run`.
- **Token-shaped `--placeholder` works**: `github_pat_spike46{rand}` was
  accepted and the CLI printed the generated value — confirming both SDK
  prefix-check compatibility and that **the host always knows the
  placeholder**.
- The shell template stamps built-in cred env names (`ANTHROPIC_API_KEY`
  etc., with `SBX_CRED_*_MODE` markers) visible to exec even with auth "not
  configured" — a creation-time env-stamping mechanism exists; custom
  secrets just didn't participate post-creation.
- `sbx secret rm <scope> --env NAME` alone is rejected on v0.35 (`--host`
  or `--placeholder` required). Round 1's cleanup used exactly that form,
  so its two dummy secrets leaked (round 2's R0 removes them). Side
  finding for provision.py: the env-only rm candidate in the
  replace-on-exists ladder can never succeed on v0.35 (harmless —
  best-effort — but dead).

What round 1 could **not** test, and round 2 targets:

1. **Ordering** (R1): `set-custom`'s own output says *"You may need to
   update environment variable … inside existing sandboxes to \<placeholder>"*
   — implying env is stamped at creation. A global secret registered
   *before* `create` is the doc-blessed path and is untested. R2 adds the
   stop/auto-restart rehydration variant.
2. **The placeholder-in-env-file design** (R4, the linchpin): since the
   host chooses/knows the placeholder, sbxloop can write the *placeholder*
   into `~/.sbxloop/env.sh` instead of the real token — the VM never holds
   the credential, the egress proxy substitutes it in flight, and **env
   stamping stops mattering entirely**. This works on the existing exec
   backend with the existing env-file mechanics if and only if the proxy
   rewrites placeholders in requests from exec'd processes. That is
   expected (the proxy is a network-level MITM at 172.17.0.0:3128 with
   sandbox-global `HTTP_PROXY`/CA env, and credentials.md says "when a
   sandboxed process sends a request" — process-agnostic wording), and it
   is observably testable with dummy values: curl `httpbin.org/headers`
   with the placeholder in a header and see which value the server echoes
   back.
3. **Whether `run -- -c` executes at all** (R3, via side-effect files).

## Relationship to other work

- **#52 (doctor→sbx conformance suite)**: permanent home for P1–P5 once
  answered; `sbx inspect` is a ready-made doctor data source.
- **#49 (per-phase network policy)**: complementary either way; the main
  mitigation if this all fails.
- The `plain-env` fallback and `secret_strategy` config stay regardless.

## Recommendation (updated after round 1)

Run the round-2 probe before writing any backend code. Decision tree:

- **R4 rewrite fires for exec'd processes** (expected) → skip the session
  backend entirely. Implement **placeholder-in-env-file**: the provisioner
  registers `COPILOT_GITHUB_TOKEN` with a host-generated token-shaped
  placeholder and writes the *placeholder* (never the token) into the env
  file. The `sandbox.secret_env_fallback` security concession disappears;
  #46's actual goal (proxy-held secrets) is met on the exec backend.
  Follow up with the real-PAT SDK end-to-end, then close #46.
- **R4 fails but R1 ordering stamps env AND R3 shows sessions execute** →
  architecture 1 (session-wrapped worker, poll transport only) behind
  `[sbx] backend`, small scoped PR.
- **R4 fails and sessions are undrivable** → close #46 as infeasible for
  now, fold findings into #49/#52, revisit when sandboxd's HTTP API is
  published (issue #139) or the SSH channel goes GA.
