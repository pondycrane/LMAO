---
description: Smart-irrigation hardware E2E gate — Cardputer/RNode tests (reusing the LMAO gate) plus Atom Lite flash/REPL checks when attached. Loud skip when hardware is absent; results feed the PR body.
argument-hint: (no arguments - detects attached hardware, runs E2E targets)
---

# Smart Irrigation — Hardware E2E Gate

**Workflow ID**: $WORKFLOW_ID

## ⚠️ HARDWARE SAFETY — READ FIRST (violating these can brick devices)

1. **NEVER run esptool on any device** (`/dev/ttyACM*`, `/dev/ttyUSB*`) — no
   flashing, no `chip_id`, no probing. Cardputer recovery needs a physical
   unplug/replug; the RNode only flashes via https://flasher.rnode.network/.
2. **The only commands allowed to touch the Cardputer/RNode are the existing
   `bazel test` E2E targets** (they use the MicroPython raw REPL). No ad-hoc
   serial scripts, no `screen`/`minicom` while tests run.
3. **The Atom Lite is OPT-IN only**: act on it only when `IRRIGATION_PORT` is
   set, or when `lma_core/device_detect.py` has a verified Atom Lite
   fingerprint. **Never auto-probe serial ports** to discover it — the classic
   Atom Lite USB bridge (CP2104) shares VID/PID `10c4:ea60` with the RNode's
   CP2102 and blind probing can disturb the production RNode path.
4. **Leave the production Cardputer running** — restore production config and
   never interrupt a flash in progress.
5. **NEVER write `$ARTIFACTS_DIR/.gate-head`** — only `irrigation-production-health`
   owns that marker (issue #100 guard).

---

## Your Mission

Run the mandatory hardware E2E checks for the current change set and record
results in `$ARTIFACTS_DIR/hardware-e2e.md`. Missing hardware is a **loud
skip**, never a silent pass. A FAIL here stops the workflow — no PR may be
created with a red hardware gate.

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

The `.gate-head` fast-pass comparison MUST use `git -C "$WT" rev-parse HEAD`.

---

## Phase 0: FAST-PASS

```bash
cd "$WT"
if [ -f "$ARTIFACTS_DIR/.gate-head" ] && \
   [ "$(cat "$ARTIFACTS_DIR/.gate-head")" = "$(git rev-parse HEAD)" ] && \
   [ -f "$ARTIFACTS_DIR/hardware-e2e.md" ]; then
  echo "FAST-PASS"
fi
```

If `FAST-PASS` prints, output:

```markdown
## Hardware E2E ✅ (fast-pass — no changes since last gate)
```

and STOP.

---

## Phase 1: CHANGE RELEVANCE

```bash
SHARED_CHANGED=$( { git diff --name-only "$BASE_BRANCH"...HEAD; git ls-files --others --exclude-standard; } \
  | grep -vE '^smart_irrigation/' | grep -E '\.(py|c|h|proto|bazel|toml|txt)$' || true )
IRRIGATION_FW_CHANGED=$( { git diff --name-only "$BASE_BRANCH"...HEAD; git ls-files --others --exclude-standard; } \
  | grep -E '^smart_irrigation/(firmware|stm32-dtu|e2e|tests)/' || true )
```

- Shared LMAO code changed → the Cardputer E2E is **required** when a Cardputer
  is attached (server/client/protocol paths are in play).
- Only `smart_irrigation/` docs/scripts changed → Cardputer E2E may be recorded
  as N/A (host-only change), but say so loudly in the artifact.
- Irrigation firmware/DUT/E2E code changed → the Atom Lite checks below become
  required when an Atom Lite is attached; UNVERIFIABLE otherwise.

---

## Phase 2: DETECT HARDWARE

```bash
python3 - <<'EOF'
import serial.tools.list_ports
cardputer = rnode = None
for p in serial.tools.list_ports.comports():
    if p.vid == 0x303A and p.pid == 0x8120:
        cardputer = p.device
    if p.vid == 0x10C4 and p.pid == 0xEA60:
        rnode = p.device
print(f"CARDPUTER={cardputer or 'NONE'}")
print(f"RNODE={rnode or 'NONE'}")
EOF

# Atom Lite: explicit opt-in only (no auto-probe!)
if [ -n "${IRRIGATION_PORT:-}" ] && [ -e "$IRRIGATION_PORT" ]; then
  echo "IRRIGATION=$IRRIGATION_PORT (from IRRIGATION_PORT)"
else
  # Optional: a verified fingerprint may have been added to device_detect.py
  IRRIGATION=$(python3 - <<'EOF' 2>/dev/null || true
try:
    from lma_core.device_detect import detect_devices
    d = detect_devices()
    port = getattr(d, "atom_lite_port", None) or getattr(d, "irrigation_port", None)
    print(port or "")
except Exception:
    print("")
EOF
)
  echo "IRRIGATION=${IRRIGATION:-NONE}"
fi
```

If the fingerprint does not exist yet, that is expected — the Atom Lite
port must be supplied via `IRRIGATION_PORT` until a verified fingerprint is
committed. Do **not** guess it from VID/PID alone.

---

## Phase 3: CARDPUTER / RNODE E2E (reuse the LMAO gate)

Execute **Phases 1–4c of `.archon/commands/lmao-hardware-e2e.md` verbatim**
(read that file; it contains the exact detection, flash E2E, LoRa E2E, and the
production-path fallback scripts for when the RNode lives on the K8s node):

- `CARDPUTER != NONE` → run
  `bazel test //tests:test_cardputer_e2e --test_output=all --cache_test_results=no`
  (generous timeout — flashing takes minutes; never interrupt).
- `RNODE != NONE` → run
  `bazel test //tests:test_cardputer_lora_e2e --test_output=all --cache_test_results=no`
  (pass through `E2E_SENSOR_TYPE` when set).
- `RNODE == NONE` but `CARDPUTER != NONE` → production-path verification
  (Phases 4a–4c of that file: server journal + JetStream consumer health).
- `CARDPUTER == NONE` → Cardputer checks are a loud SKIP (Phase 4 below).
- Failure handling: consult the README troubleshooting, retry **once**, then
  FAIL this gate.

Record the Cardputer/RNode results exactly as that command's table does.

---

## Phase 4: ATOM LITE / IRRIGATION CHECKS

Run only when `IRRIGATION != NONE`; otherwise record
`⚠️ UNVERIFIABLE — Atom Lite not attached (set IRRIGATION_PORT)`.

### 4a. Find irrigation E2E targets

```bash
bazel query 'kind("py_test", //smart_irrigation/...)' 2>/dev/null || true
```

If an E2E target exists (named/tagged for hardware E2E), run it uncached:

```bash
bazel test //smart_irrigation/e2e:<target> --test_output=all --cache_test_results=no
```

### 4b. Otherwise: flash + boot verification via the project tool

If no E2E target exists yet but the flash tool exists
(`smart_irrigation/firmware/flash.py` or `//tools:install_all` with irrigation
support), use **only** that tool (raw REPL — never esptool):

- Upload the current firmware files.
- Verify the node boots, prints its node identity, and the REPL is alive.
- If the LMAO server path is live, capture one SensorReport arriving
  (`kubectl logs deployment/lmao-server` or the local `lmao-server` journal)
  and the node's sensor readings for the changed feature.
- Record exactly what was verified, with raw evidence snippets.

If the flash tool does not exist yet → `⚠️ UNVERIFIABLE — irrigation flash
tooling not implemented yet`.

### 4c. STM32WLE5CC DTU

If a DTU/PlatformIO build exists, run the build-only check
(`pio run` in `smart_irrigation/stm32-dtu/`). Any on-air LoRaWAN verification
is `⚠️ UNVERIFIABLE` unless a DTU is attached and documented. Never claim DTU
E2E without it.

---

## Phase 5: STATUS AGGREGATION

Compute one status:

1. Any executed check FAILED → **❌ FAIL** (stop the workflow; no PR).
2. No devices attached at all → **⚠️ SKIPPED — HARDWARE NOT DETECTED** (loud).
3. Cardputer checks passed and irrigation checks N/A (host-only diff) →
   **✅ PASS (host-only change)** with the N/A stated.
4. Cardputer checks passed and irrigation checks executed and passed →
   **✅ PASS**.
5. Else (attached but untestable, e.g. missing tooling) → **⚠️ UNVERIFIABLE**
   with the reason — never a silent pass.

---

## Phase 6: ARTIFACT — `$ARTIFACTS_DIR/hardware-e2e.md`

```markdown
# Hardware E2E Results

**Generated**: {YYYY-MM-DD HH:MM}
**Workflow ID**: $WORKFLOW_ID
**Status**: {✅ PASS | ✅ PASS (host-only change) | ❌ FAIL | ⚠️ SKIPPED — HARDWARE NOT DETECTED | ⚠️ UNVERIFIABLE}

## Devices

| Device | Port | VID:PID | Source |
|--------|------|---------|--------|
| Cardputer | {/dev/ttyACM0 or NONE} | 303a:8120 | auto-detect |
| RNode | {/dev/ttyUSB0 or NONE} | 10c4:ea60 | auto-detect |
| Atom Lite | {port or NONE} | {known fingerprint or n/a} | IRRIGATION_PORT / detect_devices |

## Results

| Test | Result | Evidence |
|------|--------|----------|
| Cardputer flash E2E | ✅ / ❌ / ⏭ SKIP | {summary} |
| LoRa E2E (local RNode) | ✅ / ❌ / ⏭ SKIP | {summary} |
| LoRa production path | ✅ / ❌ / ⚠️ UNVERIFIABLE / N/A | {server log + JetStream} |
| Atom Lite flash/boot | ✅ / ⚠️ UNVERIFIABLE / N/A | {tool used, boot output} |
| Atom Lite SensorReport → server | ✅ / ⚠️ UNVERIFIABLE / N/A | {evidence} |
| STM32 DTU build/on-air | ✅ build / ⚠️ UNVERIFIABLE / N/A | {pio result} |

Production Cardputer restored with server `DEST_HASH`: {yes/no/N-A}

## Why anything is UNVERIFIABLE / SKIPPED

{explicit reason per row — never omitted}
```

Then output the same table prominently. If the status is SKIPPED or
UNVERIFIABLE, include this warning verbatim in the output so it lands in the PR:

> ⚠️ **WARNING**: Parts of the mandatory hardware E2E did NOT execute. This
> change has no hardware verification for those rows. Attach the devices (and
> set `IRRIGATION_PORT`) and re-run this gate before relying on them.

---

## Success Criteria

- **DETECTED_OR_LOUD_SKIP**: absence of any device is written into both artifact and output
- **NO_ESPTOOL**: no esptool or ad-hoc serial access anywhere
- **CLEAN_CARDPUTER_PATH**: when shared code changed and hardware was attached, the LMAO E2E ran uncached and green
- **IRRIGATION_HONESTY**: Atom Lite/DTU results are PASS only with raw evidence; otherwise UNVERIFIABLE/N/A
- **ARTIFACT_WRITTEN**: `hardware-e2e.md` has devices + results + reasons
- **NO_MARKER**: `.gate-head` untouched
