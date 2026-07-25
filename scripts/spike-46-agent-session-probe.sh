#!/usr/bin/env bash
# Field probe for issue #46 — ROUND 2.
#
# Round 1 results (sbx v0.35.0) are in issue #96: custom-secret placeholder
# env is NOT visible to exec (plain/login/tty) when the secret is registered
# AFTER sandbox creation, and `sbx run -- -c` one-shots return no command
# output (unknown whether the command even ran). Round 1 could not test the
# two paths that now matter; this round targets them:
#
#   R0  Leftover cleanup + `sbx secret ls` (round 1's cleanup used a rm
#       syntax v0.35 rejects, leaking two dummy secrets).
#   R1  ORDERING: global secret registered BEFORE sandbox creation — the
#       set-custom CLI hint ("You may need to update environment variable
#       ... inside existing sandboxes") implies env is stamped at creation.
#   R2  Sandbox-scoped secret + stop/auto-restart — does restart rehydrate
#       env (0.35 release notes say it rehydrates the github credential)?
#   R3  Does `sbx run --name X -- -c "cmd"` execute at all? (side-effect
#       files read back via exec; also captures the session's env)
#   R4  THE LINCHPIN: does the egress proxy rewrite a custom-secret
#       placeholder in requests from an EXEC'd process? The host always
#       knows the placeholder (it sets/parses it), so if rewriting is
#       process-agnostic, sbxloop can write the PLACEHOLDER into the env
#       file instead of the real token — proxy-held secrets on the existing
#       exec backend, no session backend needed. Verified observably:
#       curl to httpbin.org/headers with the placeholder in a header; the
#       echoed request shows either the real (dummy) value (rewrite fired)
#       or the placeholder (it did not).
#   R4b Same rewrite check from inside a `sbx run` session, via files.
#
# Usage (on the sbx-capable machine, from the repo root):
#
#   ./scripts/spike-46-agent-session-probe.sh [report-file]
#
# Append-only report (default: spike-46-report.txt) — paste into issue #46.
#
# Safety properties:
#   - Never touches real tokens: dummy secret values only, under unique env
#     names (sbx keys custom secrets by env name).
#   - Everything it creates carries the spike46 prefix and is removed in a
#     cleanup trap; cleanup uses the rm syntax v0.35 actually accepts
#     (--host is required alongside --env).
#   - Session steps are timeboxed; nothing here can hang the terminal.
#   - The only network egress tested is HTTPS to httpbin.org with dummy
#     values, via a per-sandbox policy allow.

set -u  # NOT -e: probes are expected to fail; every result is data.

REPORT="${1:-spike-46-report.txt}"
APP_NAME="${SBX_APP_NAME:-}"          # empty = the user's normal sbx state
SBX=(sbx)
[ -n "$APP_NAME" ] && SBX=(sbx --app-name "$APP_NAME")

STAMP="$(date +%s)"
SANDBOX="spike46r2-$STAMP"
R1_ENV="SPIKE46_R1_TOKEN"
R1_HOST="httpbin.org"
R1_VALUE="spike46-r1-dummy-$STAMP"    # dummy; never a real credential
R2_ENV="SPIKE46_R2_TOKEN"
R2_HOST="example.com"
R2_VALUE="spike46-r2-dummy-$STAMP"
WORKSPACE="$(mktemp -d "${TMPDIR:-/tmp}/spike46.XXXXXX")"

log()   { printf '\n===== %s =====\n' "$*" >>"$REPORT"; printf '>> %s\n' "$*"; }
run()   { printf '\n$ %s\n' "$*" >>"$REPORT"; "$@" >>"$REPORT" 2>&1; printf '[exit %d]\n' $? >>"$REPORT"; }
# Timeboxed + TTY-less variant for anything that might open a session.
run_t() { local t="$1"; shift; printf '\n$ timeout %s %s   (stdin=/dev/null, stdout piped)\n' "$t" "$*" >>"$REPORT"; timeout "$t" "$@" </dev/null 2>&1 | cat >>"$REPORT"; printf '[exit %d]\n' "${PIPESTATUS[0]}" >>"$REPORT"; }
# Capture variant: logs like run() but also leaves output in $CAP.
cap()   { printf '\n$ %s\n' "$*" >>"$REPORT"; CAP="$("$@" 2>&1)"; local rc=$?; printf '%s\n[exit %d]\n' "$CAP" "$rc" >>"$REPORT"; }
note()  { printf '%s\n' "$*" >>"$REPORT"; printf '>> %s\n' "$*"; }

# Verdict for R4: did the echoed request contain the real value or the
# placeholder? (Both are dummies — safe to print.)
verdict() { # label output
  local label="$1" out="$2"
  if printf '%s' "$out" | grep -qF "$R1_VALUE"; then
    note "VERDICT[$label]: REWRITE FIRED — echoed request contains the real (dummy) value"
  elif [ -n "${PH1:-}" ] && printf '%s' "$out" | grep -qF "$PH1"; then
    note "VERDICT[$label]: NO REWRITE — echoed request still contains the placeholder"
  else
    note "VERDICT[$label]: INCONCLUSIVE — neither value present (request failed?)"
  fi
}

cleanup() {
  log "CLEANUP"
  run "${SBX[@]}" rm --force "$SANDBOX"
  run "${SBX[@]}" secret rm -g --host "$R1_HOST" --env "$R1_ENV"
  run "${SBX[@]}" secret rm "$SANDBOX" --host "$R2_HOST" --env "$R2_ENV"
  run "${SBX[@]}" secret rm -g --host "$R2_HOST" --env "$R2_ENV"
  log "CLEANUP: what remains"
  run "${SBX[@]}" secret ls
  rm -rf "$WORKSPACE"
}
trap cleanup EXIT

: >"$REPORT"
log "spike-46 probe ROUND 2 $(date -u +%Y-%m-%dT%H:%M:%SZ) — sbx version"
run "${SBX[@]}" version

# ---------------------------------------------------------------------------
log "R0: secret surface + round-1 leftover cleanup (issue #96: rm --env alone is rejected)"
run "${SBX[@]}" secret ls
run "${SBX[@]}" secret rm --help
for scope in "spike46-1784995061" "-g"; do
  for env in SPIKE46_PROBE_TOKEN SPIKE46_SHAPED_TOKEN; do
    run "${SBX[@]}" secret rm "$scope" --host example.com --env "$env"
  done
done
run "${SBX[@]}" secret ls

# ---------------------------------------------------------------------------
log "R1: ORDERING — global secret registered BEFORE sandbox creation"
cap "${SBX[@]}" secret set-custom -g --host "$R1_HOST" --env "$R1_ENV" \
  --value "$R1_VALUE" --placeholder "github_pat_spike46{rand}"
PH1="$(printf '%s' "$CAP" | sed -n 's/.*placeholder "\([^"]*\)".*/\1/p' | head -1)"
note "parsed placeholder: ${PH1:-<PARSE FAILED>}"

run "${SBX[@]}" create --name "$SANDBOX" shell "$WORKSPACE"
log "R1a: env visible now? (plain and login shell)"
run "${SBX[@]}" exec "$SANDBOX" sh -c  "echo R1_CHECK:\${$R1_ENV:-UNSET}"
run "${SBX[@]}" exec "$SANDBOX" sh -lc "echo R1_CHECK:\${$R1_ENV:-UNSET}"
log "R1b: does inspect list the global secret? (round 1: 'Secrets: none' after a scoped set-custom)"
run "${SBX[@]}" inspect "$SANDBOX"

# ---------------------------------------------------------------------------
log "R2: sandbox-scoped secret AFTER creation (round-1 shape) + stop/auto-restart rehydration"
run "${SBX[@]}" secret set-custom "$SANDBOX" --host "$R2_HOST" --env "$R2_ENV" --value "$R2_VALUE"
run "${SBX[@]}" exec "$SANDBOX" sh -lc "echo R2_CHECK_PRE_RESTART:\${$R2_ENV:-UNSET}"
run "${SBX[@]}" stop "$SANDBOX"
log "R2a: exec auto-restarts a stopped sandbox (per exec --help); env after restart:"
run "${SBX[@]}" exec "$SANDBOX" sh -lc "echo R2_CHECK_POST_RESTART:\${$R2_ENV:-UNSET}; echo R1_CHECK_POST_RESTART:\${$R1_ENV:-UNSET}"

# ---------------------------------------------------------------------------
log "R3: does 'sbx run --name X -- -c CMD' execute? (side-effect files; round 1 saw no stdout)"
run_t 90 "${SBX[@]}" run --name "$SANDBOX" -- -c \
  "echo RAN > /tmp/spike46-ran.txt; env | grep '^SPIKE46' > /tmp/spike46-env.txt 2>&1; env | cut -d= -f1 | sort > /tmp/spike46-envnames.txt"
log "R3a: read the side-effect files back via exec"
run "${SBX[@]}" exec "$SANDBOX" sh -c "cat /tmp/spike46-ran.txt 2>&1"
run "${SBX[@]}" exec "$SANDBOX" sh -c "echo '--- SPIKE46 env in session:'; cat /tmp/spike46-env.txt 2>&1; echo '--- all env names in session:'; cat /tmp/spike46-envnames.txt 2>&1"

# ---------------------------------------------------------------------------
log "R4: LINCHPIN — placeholder rewrite in requests from an EXEC'd process"
run "${SBX[@]}" policy allow network "$R1_HOST" --sandbox "$SANDBOX"
if [ -n "${PH1:-}" ]; then
  log "R4a: header carrying the placeholder → httpbin.org/headers echoes what the SERVER received"
  cap "${SBX[@]}" exec "$SANDBOX" sh -lc \
    "curl -sS --max-time 30 -H 'Authorization: Bearer $PH1' -H 'X-Spike46: $PH1' https://$R1_HOST/headers"
  verdict "exec/header" "$CAP"
  if printf '%s' "$CAP" | grep -qi 'certificate\|ssl\|tls'; then
    log "R4a-retry: TLS trouble — retry with -k (MITM proxy CA not picked up by curl)"
    cap "${SBX[@]}" exec "$SANDBOX" sh -lc \
      "curl -sSk --max-time 30 -H 'Authorization: Bearer $PH1' -H 'X-Spike46: $PH1' https://$R1_HOST/headers"
    verdict "exec/header/-k" "$CAP"
  fi
  log "R4c: placeholder in a POST body (docs say 'anywhere in the request'; help says headers)"
  cap "${SBX[@]}" exec "$SANDBOX" sh -lc \
    "curl -sS --max-time 30 -d 'token=$PH1' https://$R1_HOST/post"
  verdict "exec/body" "$CAP"

  log "R4b: same header check from inside a run session (response via file)"
  run_t 90 "${SBX[@]}" run --name "$SANDBOX" -- -c \
    "curl -sS --max-time 30 -H 'Authorization: Bearer $PH1' https://$R1_HOST/headers > /tmp/spike46-session-curl.txt 2>&1"
  cap "${SBX[@]}" exec "$SANDBOX" sh -c "cat /tmp/spike46-session-curl.txt 2>&1"
  verdict "session/header" "$CAP"
else
  note "R4 SKIPPED: could not parse the generated placeholder from set-custom output"
fi

# ---------------------------------------------------------------------------
log "SUMMARY: all check + verdict lines"
summary="$(grep -hE 'R1_CHECK|R2_CHECK|VERDICT\[' "$REPORT" | sort -u || true)"
printf '%s\n' "$summary" >>"$REPORT"

log "DONE — paste $REPORT into issue #46"
echo "Report written to $REPORT"
