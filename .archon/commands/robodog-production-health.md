---
description: RoboDog production health check — after the E2E flash the panel must still render a live sync, and the LMAO server the display depends on must be serving CalendarBundle. Owns the $ARTIFACTS_DIR/.gate-head marker.
argument-hint: (no arguments - captures one non-destructive frame + checks the calendar server)
---

# RoboDog Production Health Check

**Workflow ID**: $WORKFLOW_ID

## ⚠️ Hardware Safety (MANDATORY — from AGENTS.md + robodog_client/README.md)

- **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) or the RNode.
- This node only ever READS the robodog board's serial output with `capture.py`
  (one frame, no reset, no flash) — non-destructive. If the board is absent,
  nothing is touched.
- The robodog board is the dev/kiosk display — the firmware left by the E2E gate
  is the intended product state; there is no production client to restore (unlike
  the Cardputer).

---

## Your Mission

After the hardware E2E gate confirmed the changed firmware renders on the panel,
confirm the resulting display keeps working at rest: the freshly flashed board
still draws a live sync, and the LMAO server the display depends on is serving
`CalendarBundle`. Honest UNVERIFIABLE beats silent optimism.

This node writes `$ARTIFACTS_DIR/.gate-head` — the marker that lets later
gate-chain runs fast-pass when no commits landed in between. NO OTHER NODE may
write it (issue #100 guard).

---

## Phase -1: LOCATE THE RUN TREE (MANDATORY — do this before anything else)

```bash
git worktree list --porcelain | grep -E "^(worktree|branch)"
```

- If a worktree exists for this run's branch (typically under
  `~/.archon/workspaces/*/worktrees/archon/task-*`), use it.
- Otherwise (a `--no-worktree` run), use the current checkout.

Set it once:

```bash
WT=/absolute/path/to/run-tree
cd "$WT"
```

NEVER mix trees: `.gate-head` MUST be written from this run's tree HEAD
(issue-#91 regression).

---

## Phase 0: FAST-PASS — Skip If Nothing Changed

```bash
cd "$WT"
if [ -f "$ARTIFACTS_DIR/.gate-head" ] && [ "$(cat "$ARTIFACTS_DIR/.gate-head")" = "$(git rev-parse HEAD)" ] \
   && [ -f "$ARTIFACTS_DIR/production-health.md" ]; then
  echo "FAST-PASS"
fi
```

If `FAST-PASS` printed: this exact HEAD already passed the full gate chain
including this health check **and** this gate's artifact exists. Output:

```markdown
## RoboDog Production Health ✅ (fast-pass — no changes since last gate)
```

and STOP.

---

## Phase 1: SKIP WHEN THE E2E GATE SKIPPED

```bash
grep -l "SKIPPED — ROBODOG BOARD NOT DETECTED" "$ARTIFACTS_DIR/hardware-e2e.md" 2>/dev/null
```

If the E2E gate skipped (no board, or it only produced offline `--i1` evidence),
there is no live panel to health-check:

1. Write the marker **from `"$WT"`**:
   `git -C "$WT" rev-parse HEAD > "$ARTIFACTS_DIR/.gate-head"`
   (the gate chain is complete for this HEAD).
2. Write `$ARTIFACTS_DIR/production-health.md` with `Status: ⏭ SKIPPED (no board)`.
3. Output the skip note and STOP (success).

---

## Phase 2: SERVER CHECK — The Calendar Source

The panel's data comes from the LMAO server's `lma.LMAO/GetCalendar` gRPC.
If the server is reachable from this host (`LMAO_SERVER_HOST:RD_CAL_GRPC_PORT`,
default `50051`), do a lightweight probe with a short timeout; otherwise record
UNVERIFIABLE. Also try the in-cluster server when kubectl is available
(`kubectl cluster-info` + `kubectl logs deployment/lmao-server`)
— the server may run in the K8s cluster (see AGENTS.md deployment section).

```bash
# Best-effort: gRPC GetCalendar probe (needs the generated stubs + reachable server)
python3 - <<'EOF' 2>/dev/null || echo "CALENDAR_SERVER=UNVERIFIABLE (probe failed/absent)"
import grpc
from proto import lma_messages_pb2 as lm, lma_grpc_pb2 as lg
ch = grpc.insecure_channel(f"{LMAO_SERVER_HOST:-192.168.50.67}:50051")
events = lg.LMAOStub(ch).GetCalendar(lm.CalendarRequest(calendar_id="family")).events
print(f"CALENDAR_SERVER=PASS ({len(events)} events)")
EOF
```

A `PASS` proves the display's data source is alive. `UNVERIFIABLE` is recorded
loudly but does not by itself stop the workflow — a live panel capture in Phase 3
is the stronger signal.

---

## Phase 3: PANEL CHECK — One Non-Destructive Frame

Detect the board exactly like the E2E gate (never the Cardputer):

```bash
PORT="${ROBODOG_PORT:-}"
if [ -z "$PORT" ]; then
  PORT=$(python3 - <<'EOF'
import serial.tools.list_ports
for p in serial.tools.list_ports.comports():
    if p.vid == 0x303A and p.pid == 0x1001:   # robodog board only
        print(p.device); break
EOF
)
fi
```

If a board is attached, read exactly ONE frame **without reset and without
flash** (the panel is already showing the E2E build; this confirms it keeps
rendering at rest):

```bash
if [ -n "$PORT" ]; then
  mkdir -p "$ARTIFACTS_DIR/robodog-shots"
  python3 robodog_client/capture.py --port "$PORT" --n 1 --timeout 60 \
    --describe --out "$ARTIFACTS_DIR/robodog-shots" \
    && cat "$ARTIFACTS_DIR/robodog-shots/frame0_analysis.txt"
else
  echo "PANEL=UNVERIFIABLE (no robodog board attached)"
fi
```

The single frame's readback should show the header date/time (live sync) and
rows. A BLANK header here is expected only if the board just powered on and
WiFi/sync has not landed (~40 s) — one retry with a longer timeout before
judging.

---

## Phase 4: VERDICT

### HEALTHY (panel renders a synced frame — with `CALENDAR_SERVER` PASS or UNVERIFIABLE recorded)

1. Write `$ARTIFACTS_DIR/production-health.md`:

```markdown
# RoboDog Production Health Check

**Generated**: {YYYY-MM-DD HH:MM}
**Workflow ID**: $WORKFLOW_ID
**Status**: ✅ HEALTHY

Fresh panel capture shows the header clock + calendar rows (live sync).
Frame readback (verbatim):
{frame0_analysis.txt}

Calendar server: {PASS | UNVERIFIABLE}
```

2. Write the marker **from `"$WT"`**:
   `git -C "$WT" rev-parse HEAD > "$ARTIFACTS_DIR/.gate-head"`.
3. Output:

```markdown
## RoboDog Production Health ✅ HEALTHY

The panel still renders a live sync after the E2E flash. Calendar server: {PASS | UNVERIFIABLE}.
```

### UNHEALTHY / UNVERIFIABLE-WITH-BOARD-PRESENT (board attached but no synced frame)

1. Write `$ARTIFACTS_DIR/production-health.md` with
   `Status: ❌ UNHEALTHY` (or `⚠️ UNVERIFIABLE`) and the last capture readback
   verbatim + any capture stderr.
2. Do **NOT** write the marker.
3. FAIL the node loudly if the board is attached but repeatedly shows no sync
   (WiFi/Wi-Fi AP, server host or `GetCalendar` regression suspected):

```markdown
## RoboDog Production Health ❌ UNHEALTHY

The panel did not show a live sync re-capture after the E2E session.
Check, in order:
1. WiFi reachability from the board (2.4 GHz AP; NVS `wifi_ssid`/`wifi_pass`).
2. The server is up and serving GetCalendar (Phase 2 probe).
3. Re-run the capture loop: `capture.py --port <port> --reset --n 4 --timeout 120 --describe`.
```

### UNVERIFIABLE-NO-BOARD (no board attached)

1. Write `$ARTIFACTS_DIR/production-health.md` with `Status: ⚠️ UNVERIFIABLE (no board)`.
2. Write the marker **from `"$WT"`** (chain complete for this HEAD) and output a
   loud note — the panel is the dev display; its absence is an environment
   condition, not a code failure.

---

## Success Criteria

- **PANEL_ALIVE**: a fresh non-destructive capture shows the header clock +
  rows, OR
- **HONEST_UNVERIFIABLE**: no-board / no-server states are written to the
  artifact and output loudly, never silent
- **MARKER_WRITTEN**: `.gate-head` updated on every successful completion, never
  on a failing health verdict
