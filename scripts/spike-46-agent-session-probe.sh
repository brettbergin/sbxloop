#!/usr/bin/env bash
# Field probe for issue #46: native agent-session execution backend.
#
# Desk research (docs/spikes/46-agent-session-backend.md) answered the broad
# surface questions from the docker/docs source; this probe field-verifies
# the specific behaviors the backend design hinges on, per the project rule
# that every sbx-semantics assumption is validated only in the field:
#
#   P1  `sbx run` surface as installed: agents list, --name re-attach,
#       `--` arg composition, kit/inspect/set-custom flags (--placeholder?).
#   P2  Non-interactive driving: does `sbx run --name X -- -c "cmd"` work
#       with no TTY (stdin from /dev/null, stdout piped)?
#   P3  THE DISCREPANCY: docs+issue #348 say custom-secret placeholder env
#       is visible in `sbx exec -it` shells; our 0.1.8/0.1.9 field tests saw
#       it invisible to exec. Matrix: run-session vs exec-tty vs exec-plain
#       vs exec-login-shell.
#   P4  Placeholder shape: default (`sbx-cs-<rand>`?) and whether
#       `--placeholder` accepts a token-shaped template (gho_/github_pat_).
#   P5  Lifecycle: sandbox state after session exit; re-attach behavior.
#
# Usage (on the sbx-capable machine, from the repo root):
#
#   ./scripts/spike-46-agent-session-probe.sh [report-file]
#
# Append-only report (default: spike-46-report.txt) — paste into issue #46.
#
# Safety properties:
#   - Never touches real tokens: the custom secret uses a DUMMY value with a
#     unique env name (SPIKE46_PROBE_TOKEN), so it cannot collide with
#     COPILOT_GITHUB_TOKEN (sbx keys custom secrets by env name; issue #348
#     is exactly a name-collision bug).
#   - Everything it creates carries the spike46- prefix and is removed in a
#     cleanup trap (sandboxes + secrets), even on failure.
#   - Session/run steps are timeboxed so a PTY-wanting session can't hang.
#   - Does NOT run `sbx setup ssh` (modifies user ssh config) — help only.

set -u  # NOT -e: probes are expected to fail; every result is data.

REPORT="${1:-spike-46-report.txt}"
APP_NAME="${SBX_APP_NAME:-}"          # empty = the user's normal sbx state
SBX=(sbx)
[ -n "$APP_NAME" ] && SBX=(sbx --app-name "$APP_NAME")

STAMP="$(date +%s)"
SANDBOX="spike46-$STAMP"
PROBE_ENV="SPIKE46_PROBE_TOKEN"
PROBE_ENV2="SPIKE46_SHAPED_TOKEN"
PROBE_HOST="example.com"
PROBE_VALUE="spike46-dummy-value-$STAMP"   # dummy; never a real credential
WORKSPACE="$(mktemp -d "${TMPDIR:-/tmp}/spike46.XXXXXX")"
MARKER="echo SPIKE46_ENV_CHECK:\${$PROBE_ENV:-UNSET}:\${$PROBE_ENV2:-UNSET}"

log()   { printf '\n===== %s =====\n' "$*" >>"$REPORT"; printf '>> %s\n' "$*"; }
run()   { printf '\n$ %s\n' "$*" >>"$REPORT"; "$@" >>"$REPORT" 2>&1; printf '[exit %d]\n' $? >>"$REPORT"; }
# Timeboxed + TTY-less variant for anything that might open a session.
run_t() { local t="$1"; shift; printf '\n$ timeout %s %s   (stdin=/dev/null, stdout piped)\n' "$t" "$*" >>"$REPORT"; timeout "$t" "$@" </dev/null 2>&1 | cat >>"$REPORT"; printf '[exit %d]\n' "${PIPESTATUS[0]}" >>"$REPORT"; }

cleanup() {
  log "CLEANUP"
  run "${SBX[@]}" rm --force "$SANDBOX"
  run "${SBX[@]}" rm --force "$SANDBOX-fresh"
  for env in "$PROBE_ENV" "$PROBE_ENV2"; do
    run "${SBX[@]}" secret rm "$SANDBOX" --env "$env"
    run "${SBX[@]}" secret rm -g --env "$env"
  done
  rm -rf "$WORKSPACE"
}
trap cleanup EXIT

: >"$REPORT"
log "spike-46 probe $(date -u +%Y-%m-%dT%H:%M:%SZ) — sbx version"
run "${SBX[@]}" version

# ---------------------------------------------------------------------------
log "P1: installed command surface (desk research says: run/create/exec/ls/inspect/kit/secret)"
run "${SBX[@]}" --help
run "${SBX[@]}" run --help            # agents list + flag surface
run "${SBX[@]}" create --help
run "${SBX[@]}" exec --help           # -it / -e / -w flags?
run "${SBX[@]}" secret set-custom --help   # --placeholder present?
run "${SBX[@]}" inspect --help
run "${SBX[@]}" kit --help
run "${SBX[@]}" setup --help          # ssh channel exists? (NOT running it)
run "${SBX[@]}" skills --help

# ---------------------------------------------------------------------------
log "SETUP: create probe sandbox (shell agent) + dummy custom secret (sandbox-scoped, like sbxloop)"
run "${SBX[@]}" create --name "$SANDBOX" shell "$WORKSPACE"
run "${SBX[@]}" secret set-custom "$SANDBOX" --host "$PROBE_HOST" --env "$PROBE_ENV" --value "$PROBE_VALUE"
run "${SBX[@]}" ls
run "${SBX[@]}" inspect "$SANDBOX"    # 0.35+: should list injected secrets

log "P4: token-shaped custom placeholder (docs: Amp kit uses --placeholder 'sgamp-{rand}')"
run "${SBX[@]}" secret set-custom "$SANDBOX" --host "$PROBE_HOST" --env "$PROBE_ENV2" \
  --value "$PROBE_VALUE-2" --placeholder "github_pat_spike46{rand}"

# ---------------------------------------------------------------------------
log "P3: placeholder-visibility matrix (resolves the docs-vs-0.1.8 discrepancy)"
log "P3a: exec, plain (sbxloop's current invocation shape)"
run "${SBX[@]}" exec "$SANDBOX" sh -c "$MARKER"
log "P3b: exec, login shell (sbxloop's worker wrapper shape)"
run "${SBX[@]}" exec "$SANDBOX" sh -lc "$MARKER"
log "P3c: exec with a TTY (issue #348's repro shape used -it bash)"
run_t 30 "${SBX[@]}" exec -it "$SANDBOX" sh -lc "$MARKER"
log "P3d: for comparison — full env var names visible in exec (names only, no values)"
run "${SBX[@]}" exec "$SANDBOX" sh -lc "env | cut -d= -f1 | sort"

# ---------------------------------------------------------------------------
log "P2: non-interactive session driving via the shell agent"
log "P2a: re-attach by name with a one-shot command (docs: args after -- compose/replace)"
run_t 90 "${SBX[@]}" run --name "$SANDBOX" -- -c "$MARKER"
log "P2b: same, checking a session env-name listing (names only)"
run_t 90 "${SBX[@]}" run --name "$SANDBOX" -- -c "env | cut -d= -f1 | sort"
log "P2c: fresh-sandbox one-shot form documented in agents/shell.md"
run_t 90 "${SBX[@]}" run --name "$SANDBOX-fresh" shell "$WORKSPACE" -- -c "echo SPIKE46_FRESH_RUN_OK"
log "P2d: does an interactive session tolerate missing TTY at all? (expect immediate exit or error)"
run_t 30 "${SBX[@]}" run --name "$SANDBOX"

# ---------------------------------------------------------------------------
log "P5: lifecycle after sessions — sandbox still up? state preserved?"
run "${SBX[@]}" ls
run "${SBX[@]}" exec "$SANDBOX" sh -c "echo persisted > /tmp/spike46-state && cat /tmp/spike46-state"
run_t 90 "${SBX[@]}" run --name "$SANDBOX" -- -c "cat /tmp/spike46-state"

# ---------------------------------------------------------------------------
log "SUMMARY: every SPIKE46_ENV_CHECK line (value = placeholder, real dummy, or UNSET)"
summary="$(grep -h "SPIKE46_ENV_CHECK" "$REPORT" | sort -u || true)"
printf '%s\n' "$summary" >>"$REPORT"

log "DONE — paste $REPORT into issue #46"
echo "Report written to $REPORT"
