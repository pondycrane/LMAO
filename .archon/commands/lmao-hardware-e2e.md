---
description: LMAO hardware E2E gate — mandatory Cardputer flash + LoRa tests on real devices (per AGENTS.md)
argument-hint: (no arguments - detects attached hardware, runs bazel e2e targets)
---

# LMAO Hardware E2E Gate

**Workflow ID**: $WORKFLOW_ID

## ⚠️ HARDWARE SAFETY — READ FIRST (violating these can brick physical devices)

1. **NEVER run esptool on the Cardputer** (`/dev/ttyACM*`) — no flashing, no `chip_id`, no probing of any kind. esptool kills the USB-Serial-JTAG interface; recovery requires physical USB unplug/replug. The Cardputer is flashed ONLY via `bazel run //cardputer_client:flash` or the E2E test targets below (both use the MicroPython raw REPL).
2. **NEVER flash the RNode via esptool** (`/dev/ttyUSB*`) — the only supported flashing method is the web tool at https://flasher.rnode.network/. An interrupted esptool flash bricks the device.
3. **The ONLY commands allowed to touch the devices in this gate are the two `bazel test` targets below.** No ad-hoc serial scripts, no `screen`/`minicom`, no REPL experiments while the tests run.
4. **Leave the production Cardputer running** — the E2E targets restore the production client (with the server's `DEST_HASH`) when they finish. Do not interrupt them mid-flash.

---

## Your Mission

Run the mandatory hardware E2E tests required by AGENTS.md before any PR is created, and record the results in `$ARTIFACTS_DIR/hardware-e2e.md`. This artifact is consumed by:

- the PR-creation step (the PR body MUST include the results table), and
- `lmao-production-health` (decides whether a health check is possible).

Hardware missing is a **loud skip**, never a silent pass.

---

## Phase -1: LOCATE THE RUN TREE (MANDATORY — do this before anything else)

Your shell's default directory is the ORIGINAL project checkout, which may be
on a different branch than this run. Every git and bazel command in this
document — including the `.gate-head` marker comparison — MUST run in the
tree that holds this run's branch:

```bash
git worktree list --porcelain | grep -E "^(worktree|branch)"
```

- If a worktree exists for this run's branch (typically under
  `~/.archon/workspaces/*/worktrees/archon/task-*`), use it.
- Otherwise (a `--no-worktree` run), use the current checkout.

Set it once and stick to it for the whole command:

```bash
WT=/absolute/path/to/run-tree
cd "$WT"
```

NEVER mix trees: a `git rev-parse HEAD` in the wrong checkout silently
corrupts the fast-pass marker logic (observed in the issue-#91 live test).

---

## Phase 0: FAST-PASS — Skip If Nothing Changed

```bash
cd "$WT"
if [ -f "$ARTIFACTS_DIR/.gate-head" ] && [ "$(cat "$ARTIFACTS_DIR/.gate-head")" = "$(git rev-parse HEAD)" ]; then
  echo "FAST-PASS"
fi
```

If `FAST-PASS` printed: the full gate chain already completed on this exact HEAD. Output:

```markdown
## Hardware E2E ✅ (fast-pass — no changes since last gate)
```

and STOP.

---

## Phase 1: DETECT — What Hardware Is Attached?

```bash
python3 - <<'EOF'
import serial.tools.list_ports
cardputer = rnode = None
for p in serial.tools.list_ports.comports():
    if p.vid == 0x303A and p.pid == 0x8120:
        cardputer = p.device   # M5Stack Cardputer ADV (ESP32-S3 USB-Serial-JTAG)
    if p.vid == 0x10C4 and p.pid == 0xEA60:
        rnode = p.device       # Heltec RNode (CP2102 UART bridge)
print(f"CARDPUTER={cardputer or 'NONE'}")
print(f"RNODE={rnode or 'NONE'}")
EOF
```

(These VID/PIDs are the verified fingerprints from `lma_core/device_detect.py`.)

---

## Phase 2: NO HARDWARE → LOUD SKIP

If `CARDPUTER=NONE` (the LoRa test also needs `RNODE`):

1. Write `$ARTIFACTS_DIR/hardware-e2e.md`:

```markdown
# Hardware E2E Results

**Generated**: {YYYY-MM-DD HH:MM}
**Workflow ID**: $WORKFLOW_ID
**Status**: ⚠️ SKIPPED — HARDWARE NOT DETECTED

> ⚠️ **WARNING**: No Cardputer/RNode was attached when this ran, so the
> mandatory AGENTS.md hardware E2E tests did NOT execute. This change has
> **no hardware verification**. Attach the devices and re-run
> `bazel test //tests:test_cardputer_e2e //tests:test_cardputer_lora_e2e --test_output=all`
> before merging if the change touches the client, server, protocol, or flash tooling.
```

2. Output the same warning prominently (it will be copied into the PR body).
3. STOP here — the node SUCCEEDS (missing hardware must not block the workflow, but the skip must be impossible to miss).

---

## Phase 3: FLASH E2E (Cardputer)

```bash
bazel test //tests:test_cardputer_e2e --test_output=all --cache_test_results=no
```

- `--cache_test_results=no` is **required**: a cached pass never touched the hardware.
- Flashing ~40 files over the raw REPL takes minutes — use a generous timeout (15 min). Do not interrupt a run in progress.
- **If it FAILS**: read the output against the README troubleshooting table (wedged-device recovery is built into the flash tooling). Retry **once**. Still failing → write the artifact with `Status: FAIL` and STOP the workflow with a failing status. **No PR may be created with a red hardware gate.**

**Record result**: ✅ Pass / ❌ Fail

---

## Phase 4: LORA E2E — Local RNode or Production Path

### Decision: Which path to take?

- **If `RNODE != NONE`**: run the existing `bazel test //tests:test_cardputer_lora_e2e` against the local RNode (unchanged behavior — skip Phases 4a-4c, go directly to Phase 5).
- **If `RNODE == NONE` but `CARDPUTER != NONE`**: run production-path verification (Phases 4a-4c below).
- **If `CARDPUTER == NONE`**: this is handled by Phase 2 (both absent → loud SKIP). Phase 4 does not execute.

### PATH A: Local RNode Available

```bash
bazel test //tests:test_cardputer_lora_e2e --test_output=all --cache_test_results=no
```

- Pass through `E2E_SENSOR_TYPE` if it is set in the environment (e.g. `E2E_SENSOR_TYPE=DHT20` when an external humidity sensor is attached).
- Waits for real LoRa traffic (default interval 60 s) — generous timeout (15 min).
- Same handling: consult troubleshooting, retry once, then FAIL the node on persistent failure.

**Record result**: ✅ Pass / ❌ Fail

### PATH B: Production LoRa Path Verification (no local RNode)

When the Cardputer is attached but the RNode is on the K8s cluster (issue #93),
verify the production LoRa pipeline — Cardputer→LoRa→pod→NATS→DuckDB —
is actually live. Skip is never silent.

---

### Phase 4a: Production Server Log Verification

```bash
# Check kubectl availability first
if ! command -v kubectl >/dev/null 2>&1; then
  echo "LORA_PROD=UNVERIFIABLE (kubectl not found)"
else
  # Check if cluster is reachable
  if ! kubectl cluster-info >/dev/null 2>&1; then
    echo "LORA_PROD=UNVERIFIABLE (cluster unreachable)"
  else
    # Look for fresh "Message received" from a Cardputer in server logs.
    # Poll loop: 6 iterations × 30s sleep = 3 min, catches ≥2 Cardputer
    # intervals (default 60s each).
    LORA_SEEN=""
    for i in $(seq 1 6); do
      if kubectl logs deployment/lmao-server --since=3m 2>/dev/null | grep -qi "message received"; then
        LORA_SEEN="yes"
        break
      fi
      sleep 30
    done
    if [ -n "$LORA_SEEN" ]; then
      echo "LORA_PROD=PASS"
    else
      echo "LORA_PROD=FAIL (no messages in server logs)"
    fi
  fi
fi
```

---

### Phase 4b: JetStream Consumer Health Check

```bash
if kubectl cluster-info >/dev/null 2>&1; then
  # The iot-ingest-consumer pod already has nats-py and runs on the cluster
  # network. Use a single kubectl exec to query stream + consumer info.
  JS_SCRIPT='
import asyncio, json, sys

async def main():
    import nats
    nc = await nats.connect("nats://nats-server.default.svc.cluster.local:4222")
    js = nc.jetstream()

    result = {}
    try:
        info = await js.stream_info("LMAO_MESSAGES")
        result["last_seq"] = info.state.last_seq
    except Exception as e:
        result["stream_error"] = str(e)

    try:
        cinfo = await js.consumer_info("LMAO_MESSAGES", "iot-ingest")
        result["consumer_ack_floor"] = cinfo.ack_floor.stream_seq if cinfo.ack_floor else 0
        result["consumer_num_pending"] = cinfo.num_pending
        result["consumer_num_ack_pending"] = cinfo.num_ack_pending
    except Exception as e:
        result["consumer_error"] = str(e)

    await nc.close()
    print(json.dumps(result))

asyncio.run(main())
'
  JS_INFO=$(kubectl exec deployment/iot-ingest-consumer -- python3 -c "$JS_SCRIPT" 2>/dev/null || echo "")
  if [ -n "$JS_INFO" ]; then
    LAST_SEQ=$(echo "$JS_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('last_seq',0))" 2>/dev/null)
    CONSUMER_ACK=$(echo "$JS_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('consumer_ack_floor',0))" 2>/dev/null)
    NUM_PENDING=$(echo "$JS_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('consumer_num_pending',-1))" 2>/dev/null)
    NUM_ACK_PENDING=$(echo "$JS_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('consumer_num_ack_pending',-1))" 2>/dev/null)
    echo "JS_LAST_SEQ=$LAST_SEQ"
    echo "JS_CONSUMER_ACK=$CONSUMER_ACK"
    echo "JS_NUM_PENDING=$NUM_PENDING"
    echo "JS_NUM_ACK_PENDING=$NUM_ACK_PENDING"
    if [ "$LAST_SEQ" != "0" ] && [ "$CONSUMER_ACK" != "0" ] && [ "$LAST_SEQ" = "$CONSUMER_ACK" ] && [ "$NUM_PENDING" = "0" ] && [ "$NUM_ACK_PENDING" = "0" ]; then
      echo "JETSTREAM=PASS (last_seq=$LAST_SEQ, consumer_ack=$CONSUMER_ACK, pending=0)"
    elif [ "$LAST_SEQ" != "0" ] && [ "$CONSUMER_ACK" != "0" ]; then
      echo "JETSTREAM=WARN (last_seq=$LAST_SEQ, consumer_ack=$CONSUMER_ACK, pending=$NUM_PENDING, ack_pending=$NUM_ACK_PENDING)"
    else
      echo "JETSTREAM=FAIL (no JetStream consumer progress)"
    fi
  else
    echo "JETSTREAM=UNVERIFIABLE (iot-ingest exec failed)"
  fi
fi
```

---

### Phase 4c: Aggregate LoRa Result

Combine the results from Phase 4a (production server logs) and Phase 4b (JetStream health):

- If `LORA_PROD=PASS` AND `JETSTREAM=PASS` → **✅ PASS**
- If `LORA_PROD=PASS` AND `JETSTREAM=WARN` → **✅ PASS with warning** (traffic flowing, consumer catching up)
- If `LORA_PROD=FAIL` OR `JETSTREAM=FAIL` → **❌ FAIL** (loud — stop the workflow)
- If both `UNVERIFIABLE` → **⚠️ UNVERIFIABLE** (loud skip — cluster unreachable or kubectl missing)

Record the aggregated result. FAIL must stop the workflow — the same "no PR may be created with a red hardware gate" rule from Phase 3 applies.

---

## Phase 5: ARTIFACT — Write Results

Write to `$ARTIFACTS_DIR/hardware-e2e.md`:

```markdown
# Hardware E2E Results

**Generated**: {YYYY-MM-DD HH:MM}
**Workflow ID**: $WORKFLOW_ID
**Status**: {PASS | FAIL | UNVERIFIABLE}

## Devices

| Device | Port | VID:PID |
|--------|------|---------|
| Cardputer | {/dev/ttyACM0} | 303a:8120 |
| RNode | {/dev/ttyUSB0} | 10c4:ea60 |

## Results

| Test | Result | Duration |
|------|--------|----------|
| `//tests:test_cardputer_e2e` (flash + boot) | ✅ PASS | {N}s |
| `//tests:test_cardputer_lora_e2e` (LoRa comms) | ✅ PASS / ✅ PASS (production path) / ❌ FAIL (production path) / ⚠️ UNVERIFIABLE / ⏭ SKIP (no RNode) | {N}s |

Production client restored with server `DEST_HASH`: {yes}

## Production LoRa Evidence (when RNODE absent — production path verified)

| Hop | Evidence | Result |
|-----|----------|--------|
| Cardputer → LoRa uplink | Server logs: `Message received` from Cardputer | {✅ / ❌} |
| Server → NATS JetStream | `LMAO_MESSAGES` last_seq={N} | {✅ / ❌} |
| NATS → DuckDB | `iot-ingest` consumer ack_floor={M}, num_pending=0 | {✅ / ❌} |
```

---

## Phase 6: OUTPUT — Report Results

```markdown
## Hardware E2E {✅ PASS | ❌ FAIL | ⚠️ UNVERIFIABLE}

| Test | Result |
|------|--------|
| Cardputer flash E2E | ✅ |
| LoRa communication E2E | ✅ (production path) / ❌ (production path: no traffic) / ⚠️ UNVERIFIABLE (cluster unreachable) |

Artifact: `$ARTIFACTS_DIR/hardware-e2e.md`
Next: production health check (`lmao-production-health`).
```

A FAIL here FAILS the node — do not report success, do not continue to PR creation.

---

## Success Criteria

- **DETECTED_OR_LOUD_SKIP**: hardware absence is written into the artifact and the output, never silent
- **FLASH_E2E_PASS**: `//tests:test_cardputer_e2e` executed (not cached) and green when a Cardputer is attached
- **LORA_E2E_PASS**: `//tests:test_cardputer_lora_e2e` executed (not cached) and green when both devices are attached
- **NO_ESPTOOL**: no esptool or ad-hoc serial access was used
- **ARTIFACT_WRITTEN**: `$ARTIFACTS_DIR/hardware-e2e.md` contains the devices + results table
