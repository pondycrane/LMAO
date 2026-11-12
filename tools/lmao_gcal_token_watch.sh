#!/bin/bash
# lmao_gcal_token_watch.sh -- alert on lmao-calendar-sync OAuth failures.
#
# The hourly lmao-calendar-sync CronJob fails with Google `invalid_grant`
# when the authorized-user refresh token is expired or revoked.  This host
# side watcher (user crontab) detects that, mints a FRESH OAuth authorization
# URL (state + PKCE via hermes' google-workspace setup.py) and sends it to
# the user's Matrix DM room so they can re-authorize from Element on Android.
#
# Alert policy: one alert per failed job (deduped by job name in the state
# file), so each hourly failing run re-alerts without duplicates from cron
# re-checks.  A successful run clears the state.
#
# Canonical copy: tools/lmao_gcal_token_watch.sh in the LMAO repo.
# Installed to ~/.hermes/scripts/ and scheduled via crontab:
#   */10 * * * * /home/pondycrane/.hermes/scripts/lmao_gcal_token_watch.sh >> \
#     /home/pondycrane/.hermes/lmao_gcal_token_watch.log 2>&1
#
# Test modes:
#   lmao_gcal_token_watch.sh <JOB_NAME> --dry-run   # detect, print, don't send
#   lmao_gcal_token_watch.sh --test-send            # send a test ping to Matrix

set -u

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
CRONJOB="lmao-calendar-sync"
STATE_FILE="$HERMES_HOME/lmao_gcal_alert.state"
MATRIX_TARGET="matrix:!OkOieXtKSAJZTacsVf:selfhost.tailab9655.ts.net"
SETUP_PY="$HERMES_HOME/hermes-agent/skills/productivity/google-workspace/scripts/setup.py"
VENV_PY="$HERMES_HOME/hermes-agent/venv/bin/python"
HERMES_BIN="$HOME/.local/bin/hermes"

if [ "${1:-}" = "--test-send" ]; then
    "$HERMES_BIN" send --to "$MATRIX_TARGET" \
        "lmao gcal watch: test ping — the token-failure alert path is wired up."
    exit $?
fi

JOB="${1:-}"
DRY_RUN=false
[ "${2:-}" = "--dry-run" ] && DRY_RUN=true

if [ -z "$JOB" ]; then
    # Newest job across the cron's own jobs and manual one-off runs: a
    # successful manual run must clear the alert epoch too.
    JOB=$(kubectl get jobs -o json 2>/dev/null | python3 -c "
import json, sys
jobs = json.load(sys.stdin)['items']
mine = [j for j in jobs if j['metadata']['name'].startswith('lmao-calendar-sync-')
        or j['metadata']['name'].startswith('lmao-cal-sync-manual')]
if not mine:
    sys.exit(0)
mine.sort(key=lambda j: j['metadata']['creationTimestamp'])
print(mine[-1]['metadata']['name'])
")
    [ -z "$JOB" ] && exit 0
fi

complete=$(kubectl get job "$JOB" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}' 2>/dev/null)
if [ "$complete" = "True" ]; then
    rm -f "$STATE_FILE"
    exit 0
fi

failed=$(kubectl get job "$JOB" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null)
if [ "$failed" != "True" ]; then
    exit 0   # still running or condition not surfaced yet
fi

logs=$(kubectl logs -l batch.kubernetes.io/job-name="$JOB" --tail=80 2>/dev/null)
if ! echo "$logs" | grep -q "invalid_grant"; then
    exit 0   # failed for some other reason; not the token epoch
fi

# Dedupe: one alert per failed job name.
if [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE" 2>/dev/null)" = "$JOB" ]; then
    exit 0
fi

# Mint a fresh authorization URL (overwrites google_oauth_pending.json with
# new state + PKCE verifier; only one re-auth flow is live at a time).
url=$(PYTHONPATH="$HERMES_HOME/hermes-agent" HERMES_HOME="$HERMES_HOME" \
      "$VENV_PY" "$SETUP_PY" --auth-url 2>/dev/null | tail -1)
case "$url" in
    https://accounts.google.com/*) ;;
    *) echo "$(date -Is) ERROR: auth-url mint failed: $url" >&2; exit 1 ;;
esac

msg="The lmao-calendar-sync CronJob failed: the Google token is expired/revoked (invalid_grant).

Calendar edits and the RoboDog panels' per-event weather stay frozen until you re-authorize. Approve this link:

$url

After approving, the browser lands on http://localhost:1/?code=... (ERR_UNSAFE_PORT — expected). Paste that full URL back to Hermes and the code will be exchanged for a new token. (failed job: $JOB)"

if $DRY_RUN; then
    echo "WOULD SEND to $MATRIX_TARGET:"
    echo "$msg"
    exit 0
fi

if "$HERMES_BIN" send --to "$MATRIX_TARGET" "$msg"; then
    echo "$JOB" > "$STATE_FILE"
    echo "$(date -Is) alerted for $JOB"
else
    echo "$(date -Is) ERROR: hermes send failed for $JOB" >&2
    exit 1
fi
