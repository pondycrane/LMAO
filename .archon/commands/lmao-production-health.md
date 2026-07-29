---
description: LMAO production health check — confirm the Cardputer resumed normal messaging after E2E before declaring success
argument-hint: (no arguments - polls the lmao-server journal for fresh Cardputer messages)
---

# LMAO Production Health Check

**Workflow ID**: $WORKFLOW_ID

## ⚠️ Hardware Safety (MANDATORY — from AGENTS.md)

- **NEVER run esptool on the Cardputer or the RNode** — recovery steps below use `bazel run //cardputer_client:flash` or the LoRa REBOOT command only.
- Do not open serial sessions to the Cardputer while checking health — observe via the server journal.

---

## Your Mission

After the hardware E2E gate, the production Cardputer must be back on its normal duty cycle: sending `Hello from Cardputer` every `INTERVAL_SECONDS` (default 60 s, clamped minimum 10 s). The AGENTS.md rule is that a test session is not complete until the device is verifiably talking again. Confirm this via the `lmao-server` journal before anyone declares success.

This node also writes the `$ARTIFACTS_DIR/.gate-head` marker that lets later gate-chain runs fast-pass when no commits landed in between.

---

## Phase 0: FAST-PASS — Skip If Nothing Changed

```bash
if [ -f "$ARTIFACTS_DIR/.gate-head" ] && [ "$(cat "$ARTIFACTS_DIR/.gate-head")" = "$(git rev-parse HEAD)" ]; then
  echo "FAST-PASS"
fi
```

If `FAST-PASS` printed: this exact HEAD already passed the full gate chain including this health check. Output:

```markdown
## Production Health ✅ (fast-pass — no changes since last gate)
```

and STOP.

---

## Phase 1: SKIP WHEN HARDWARE WAS ABSENT

```bash
grep -l "SKIPPED — HARDWARE NOT DETECTED" "$ARTIFACTS_DIR/hardware-e2e.md" 2>/dev/null
```

If the E2E gate skipped (no hardware attached), there is nothing to health-check:

1. Write the marker: `git rev-parse HEAD > "$ARTIFACTS_DIR/.gate-head"` (the gate chain is complete for this HEAD).
2. Write `$ARTIFACTS_DIR/production-health.md` with `Status: ⏭ SKIPPED (no hardware)`.
3. Output the skip note and STOP (success).

---

## Phase 2: SERVER CHECK

```bash
systemctl is-active lmao-server 2>&1
```

If the unit is not found or not active, the Cardputer's messages cannot be observed from this host (the server may run elsewhere, or be down):

1. Write the marker: `git rev-parse HEAD > "$ARTIFACTS_DIR/.gate-head"`.
2. Write `$ARTIFACTS_DIR/production-health.md` with `Status: ⚠️ UNVERIFIABLE (lmao-server not active on this host)` and the `systemctl` output.
3. Output a **loud** warning that production health could not be verified, and STOP (success — do not fail workflows on hosts without the service, but never stay silent about it).

---

## Phase 3: POLL FOR FRESH MESSAGES

The Cardputer sends every ~60 s. Poll the journal for up to ~4 minutes:

```bash
SEEN=""
for i in $(seq 1 16); do
  if journalctl -u lmao-server --since "-90s" --no-pager 2>/dev/null | grep -qi "hello from cardputer"; then
    SEEN="yes"; echo "PRODUCTION HEALTHY: fresh Cardputer message observed"; break
  fi
  sleep 15
done
[ -z "$SEEN" ] && echo "NO FRESH MESSAGES within timeout"
```

---

## Phase 4: VERDICT

### HEALTHY (fresh message seen)

1. Write `$ARTIFACTS_DIR/production-health.md`:

```markdown
# Production Health Check

**Generated**: {YYYY-MM-DD HH:MM}
**Workflow ID**: $WORKFLOW_ID
**Status**: ✅ HEALTHY

Fresh `Hello from Cardputer` observed in the lmao-server journal.
Last seen: {journal timestamp}
```

2. Write the marker: `git rev-parse HEAD > "$ARTIFACTS_DIR/.gate-head"`.
3. Output:

```markdown
## Production Health ✅ HEALTHY

The Cardputer resumed normal messaging after the E2E session (fresh `Hello from Cardputer` in the server journal).
```

### UNHEALTHY (no fresh message within timeout)

1. Write `$ARTIFACTS_DIR/production-health.md` with `Status: ❌ UNHEALTHY` and the last 20 relevant journal lines.
2. Do **NOT** write the marker.
3. Output a loud failure and FAIL the node:

```markdown
## Production Health ❌ UNHEALTHY

The Cardputer has not sent a message for >4 minutes after the E2E session.
Production device was NOT left running — do not declare this work complete.

Recovery (in order — NEVER esptool):
1. Verify the device is powered and the LoRa antenna is seated.
2. `bazel run //cardputer_client:flash -- --verify-only` to check the REPL is alive.
3. Re-flash the production client: `bazel run //cardputer_client:flash` (re-injects DEST_HASH).
4. If the REPL is unreachable but LoRa works: `bazel run //lmao_server:send_command -- --target <node_identity_hex> --action reboot`.
5. Last resort: physical RESET button. See README troubleshooting (#74, #78).
```

---

## Success Criteria

- **MESSAGES_FLOWING**: fresh `Hello from Cardputer` observed post-E2E, OR
- **HONEST_SKIP**: skip (no hardware / no server) is written into the artifact and output — never silent
- **MARKER_WRITTEN**: `.gate-head` updated on every successful completion, never on failure
