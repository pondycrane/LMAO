---
description: Smart-irrigation production health gate — confirms the production Cardputer resumed messaging and the irrigation ingest path is healthy (or honestly N/A). Sole owner of the .gate-head fast-pass marker.
argument-hint: (no arguments - polls server logs / cluster health)
---

# Smart Irrigation — Production Health

**Workflow ID**: $WORKFLOW_ID

## ⚠️ Rules (MANDATORY)

- Observe via server logs / `kubectl` / journald only — **never open serial
  sessions** to any device, never esptool.
- **Only this command may write `$ARTIFACTS_DIR/.gate-head`** — it signals the
  full gate chain completed. Never write it on FAIL.

---

## Your Mission

After the hardware E2E gate, confirm the production systems are back to normal:
the production Cardputer is still sending, and (when applicable) irrigation
telemetry is flowing. Then write the gate marker.

---

## Phase -1: LOCATE THE RUN TREE (MANDATORY)

```bash
git worktree list --porcelain | grep -E "^(worktree|branch)"
```

Use this run's worktree (`~/.archon/workspaces/*/worktrees/archon/task-*`);
fall back to the current checkout for `--no-worktree` runs.

```bash
WT=/absolute/path/to/run-tree
cd "$WT"
```

Marker comparisons/writes MUST use `git -C "$WT" rev-parse HEAD`.

---

## Phase 0: FAST-PASS

```bash
cd "$WT"
if [ -f "$ARTIFACTS_DIR/.gate-head" ] && \
   [ "$(cat "$ARTIFACTS_DIR/.gate-head")" = "$(git rev-parse HEAD)" ] && \
   [ -f "$ARTIFACTS_DIR/production-health.md" ]; then
  echo "FAST-PASS"
fi
```

If `FAST-PASS` prints, output:

```markdown
## Production Health ✅ (fast-pass — no changes since last gate)
```

and STOP.

---

## Phase 1: HARDWARE-ABSENT SHORT-CIRCUIT

```bash
grep -l "SKIPPED — HARDWARE NOT DETECTED" "$ARTIFACTS_DIR/hardware-e2e.md" 2>/dev/null
```

If the E2E gate skipped entirely (no hardware attached):

1. `git -C "$WT" rev-parse HEAD > "$ARTIFACTS_DIR/.gate-head"`
2. Write `production-health.md` with `Status: ⏭ SKIPPED (no hardware)` and the
   E2E skip reason.
3. Output the skip note. STOP (success).

If the E2E gate ran (even partially), continue.

---

## Phase 2: CARDPUTER PRODUCTION CHECK

The Cardputer sends `Hello from Cardputer` every `INTERVAL_SECONDS` (~60 s).
Find its traffic via the local service **or** the K8s deployment:

```bash
# Option A — local service
systemctl is-active lmao-server 2>&1

# Option B — K8s deployment (issue #93: RNode lives on tp4)
kubectl get deployment lmao-server 2>&1 >/dev/null && echo "K8S=yes" || echo "K8S=no"
```

Poll up to ~4 minutes (16 × 15 s). Local:

```bash
journalctl -u lmao-server --since "-90s" --no-pager 2>/dev/null | grep -qi "hello from cardputer"
```

K8s:

```bash
kubectl logs deployment/lmao-server --since=3m 2>/dev/null | grep -qi "message received"
```

Record the source used. If neither the service nor the cluster is reachable on
this host → `⚠️ UNVERIFIABLE (no server visibility)` — write the artifact and
marker (the gate chain is complete; the skip is honest and loud).

---

## Phase 3: IRRIGATION PATH CHECK (when applicable)

Skip this phase with `N/A — no irrigation node deployed yet` when:
- `smart_irrigation/firmware/` does not exist, or
- `smart_irrigation/docs/development-plan.md` shows Phase 6 (LoRaWAN
  integration) not yet complete, or
- the hardware E2E recorded the Atom Lite as not attached/unverified.

Otherwise verify, when the infrastructure allows:

| Check | How | Result handling |
|-------|-----|-----------------|
| Irrigation SensorReports arriving | server logs mention the irrigation node (`LMAO_Irrigation` / node id) | PASS / FAIL |
| DuckDB ingest | `irrigation_sensor_readings` row count grows (kubectl exec into `iot-ingest-consumer`, or query API) | PASS / WARN / UNVERIFIABLE |
| JetStream consumer | `LMAO_MESSAGES` consumer `iot-ingest`: `last_seq == ack_floor`, `num_pending == 0` (same script as `lmao-hardware-e2e.md` Phase 4b) | PASS / WARN / FAIL |

Aggregate exactly like the LMAO gate: any FAIL → **❌ FAIL** (no marker, fail
the node); UNVERIFIABLE → **⚠️ UNVERIFIABLE** (marker OK, loud); PASS/WARN →
**✅ PASS**.

---

## Phase 4: VERDICT + MARKER

### Healthy / honestly unverifiable

1. Write `$ARTIFACTS_DIR/production-health.md`:

```markdown
# Production Health Check

**Generated**: {YYYY-MM-DD HH:MM}
**Workflow ID**: $WORKFLOW_ID
**Status**: {✅ HEALTHY | ⚠️ UNVERIFIABLE (reason) | ⏭ SKIPPED (reason)}

| Check | Result | Evidence |
|-------|--------|----------|
| Cardputer resumed messaging | ✅/⚠️ | {source + timestamp} |
| Irrigation SensorReports | ✅/N/A/⚠️ | {evidence} |
| DuckDB irrigation ingest | ✅/WARN/N/A/⚠️ | {row count} |
| JetStream consumer | ✅/WARN/N/A/⚠️ | {last_seq/ack_floor} |
```

2. Write the marker **from `"$WT"`**:
   `git -C "$WT" rev-parse HEAD > "$ARTIFACTS_DIR/.gate-head"`
3. Output the verdict.

### Unhealthy (a required check failed)

1. Write `production-health.md` with `Status: ❌ UNHEALTHY` + the failing
   evidence (last 20 relevant log lines).
2. Do **NOT** write the marker.
3. FAIL the node with the recovery guidance from
   `.archon/commands/lmao-production-health.md` (verify power/antenna, verify
   the REPL via the flash tool, re-flash production client, REBOOT command,
   physical reset last — never esptool).

---

## Success Criteria

- **MESSAGES_FLOWING** or **HONEST_SKIP/UNVERIFIABLE** recorded with evidence
- **IRRIGATION_HONESTY**: irrigation rows are N/A/UNVERIFIABLE rather than invented when not deployed
- **MARKER_WRITTEN**: `.gate-head` updated on every non-FAIL completion (including honest skips), never on failure
- **ONLY_OWNER**: no other gate command writes the marker
