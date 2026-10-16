---
description: RoboDog hardware E2E gate — build + flash the real Waveshare ESP32-S3 RLCD, then SCREEN-CAPTURE the exact ST7305 pixels from the live u8g2 buffer and read the render back with the vision model (Qwen2.5-VL). Writes the canonical $ARTIFACTS_DIR/hardware-e2e.md the PR gate consumes.
argument-hint: (no arguments - detects the robodog board, builds, flashes, captures + vision-verifies)
---

# RoboDog Hardware E2E Gate (screen capture + vision model)

**Workflow ID**: $WORKFLOW_ID

## ⚠️ HARDWARE SAFETY — READ FIRST (violating these can brick physical devices)

1. **This gate flashes the robodog board** — the Waveshare ESP32-S3 RLCD-4.2
   (ST7305), verified fingerprint VID `0x303A` PID `0x1001` (plain ESP32-S3
   USB-Serial-JTAG; see `lma_core/device_detect.py`). The ONLY flash path is
   `bazel run //robodog_client:flash_firmware -- --port <port>`
   (`robodog_client/flash.sh` — host esptool, because Docker ttyACM RTS
   passthrough breaks the button-free reset). Never hand-esptool the board.
2. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) — no flashing, no
   `chip_id`, no probing. esptool kills the USB-Serial-JTAG interface; recovery
   needs a physical replug. The Cardputer is flashed ONLY via
   `bazel run //cardputer_client:flash`. **This gate never touches the
   Cardputer's port** — the detection below ONLY matches the robodog
   `0x303A:0x1001` fingerprint (or an explicit `ROBODOG_PORT`).
3. **NEVER flash the RNode via esptool** (`/dev/ttyUSB*`) — web flasher only.
4. After this gate the NEW firmware owns the panel (the robodog board is the
   dev/kiosk display; unlike the Cardputer there is no production client to
   restore). Leaving the new build on the panel is the intended product state.
5. `capture.py --reset` issues only an esptool reset on the robodog board port
   (the same sequence `flash.sh` uses) — safe; it never reflashes flash content.

**Gate-chain safety**: **NEVER write `$ARTIFACTS_DIR/.gate-head`** — only
`robodog-production-health` owns that marker (issue #100 guard).

## What "verification" means here (the point of this gate)

There is no re-render and no model guess in this gate. The firmware streams its
**exact full-frame u8g2 buffer** (304×50 tiles = 300×400, the literal ST7305
payload) over USB-JTAG; `capture.py` reconstructs that buffer into
`panel_raw_portrait.png` / `panel_landscape.png`, and `--describe` sends the
landscape frame to the same self-hosted vision model hermes uses (Qwen2.5-VL at
`spark-eebf`, `~/.hermes/config.yaml "vision"`; overridable with
`RD_VISION_BASE_URL` / `RD_VISION_MODEL`) which **transcribes** the header clock
and every row. You reason from that transcription — it is what the panel really
shows.

---

## Phase -1: LOCATE THE RUN TREE (MANDATORY — do this before anything else)

Your shell's default directory is the ORIGINAL project checkout, which may be
on a different branch than this run. Every git and bazel command — including
the `.gate-head` marker comparison — MUST run in the tree that holds this run's
branch:

```bash
git worktree list --porcelain | grep -E "^(worktree|branch)"
```

- If a worktree exists for this run's branch (typically under
  `~/.archon/workspaces/*/worktrees/archon/task-*`), use it.
- Otherwise (a `--no-worktree` run), use the current checkout.

Set it once and stick to it:

```bash
WT=/absolute/path/to/run-tree
cd "$WT"
```

NEVER mix trees (issue-#91 regression: `git rev-parse HEAD` in the wrong
checkout silently corrupts fast-pass marker logic).

---

## Phase 0: FAST-PASS — Skip If Nothing Changed

```bash
cd "$WT"
if [ -f "$ARTIFACTS_DIR/.gate-head" ] && [ "$(cat "$ARTIFACTS_DIR/.gate-head")" = "$(git rev-parse HEAD)" ] \
   && [ -f "$ARTIFACTS_DIR/hardware-e2e.md" ]; then
  echo "FAST-PASS"
fi
```

If `FAST-PASS` printed: the full gate chain already completed on this exact HEAD
**and** this gate's own artifact exists, so re-running would re-flash identical
code. Output:

```markdown
## RoboDog Hardware E2E ✅ (fast-pass — no changes since last gate)
```

and STOP.

---

## Phase 1: HOST PREREQUISITES

```bash
python3 -c "import serial; import PIL"        # capture.py needs both
command -v tesseract                          # orientation OCR (capture.py dihedral)
python3 -m esptool --help >/dev/null 2>&1     # flash.sh host esptool
docker version >/dev/null 2>&1                # build.sh ESP-IDF image runner
curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 http://spark-eebf:8000/v1/models  # vision endpoint
```

If `serial`/`PIL`/`tesseract` are missing, install them (`pip install pyserial
Pillow`, `apt install tesseract-ocr`) and re-check — capture.py cannot decode the
frames or pick the orientation without them. The vision endpoint timeout is not
fatal: capture still writes the PNGs, `--describe` just fails per-frame (record
`VISION=UNVERIFIABLE` and fall back to your own reading of the PNG).

---

## Phase 2: DETECT — The RoboDog Board

An explicit `ROBODOG_PORT` always wins; otherwise auto-detect ONLY the verified
robodog fingerprint (`0x303A:0x1001`). Never touch `0x303A:0x8120` (Cardputer)
or `0x10C4:0xEA60` (RNode):

```bash
PORT="${ROBODOG_PORT:-}"
if [ -z "$PORT" ]; then
  PORT=$(python3 - <<'EOF'
import serial.tools.list_ports
for p in serial.tools.list_ports.comports():
    # only the plain ESP32-S3 USB-Serial-JTAG — the Waveshare devkit.
    # 0x8120 is the M5Stack Cardputer and is NEVER probed here.
    if p.vid == 0x303A and p.pid == 0x1001:
        print(p.device)
        break
EOF
)
fi
echo "ROBODOG_PORT=${PORT:-NONE}"
```

---

## Phase 3: NO BOARD → LOUD SKIP (+ offline decode evidence)

If `ROBODOG_PORT` is NONE:

1. Try **offline decode evidence** — a `robodog_client/shots/frame*.i1` dump
   from a previous run exercises the pixel-decode + vision pipeline without the
   board. If one exists:
   ```bash
   mkdir -p "$ARTIFACTS_DIR/robodog-shots"
   python3 robodog_client/capture.py --i1 "$WT"/robodog_client/shots/frame*.i1 \
     --describe --out "$ARTIFACTS_DIR/robodog-shots"
   ```
   Record the readback as *offline* evidence (`VISION=PASS offline`)
   with `E2E=SKIPPED` (no live flash).
2. Write `$ARTIFACTS_DIR/hardware-e2e.md`:
   ```markdown
   # RoboDog Hardware E2E Results

   **Generated**: {YYYY-MM-DD HH:MM}
   **Workflow ID**: $WORKFLOW_ID
   **Status**: ⚠️ SKIPPED — ROBODOG BOARD NOT DETECTED

   > ⚠️ **WARNING**: No robodog board (0x303A:0x1001) was attached when this ran,
   > so the mandatory pixel-exact display verification did NOT execute live. This
   > change has **no hardware render verification**. Attach the board and re-run
   > `archon workflow run robodog-client-dev "…"` (or the gate command:
   > `bazel run //robodog_client:build_firmware && bazel run //robodog_client:flash_firmware
   > && python3 robodog_client/capture.py --port <port> --reset --n 4 --timeout 120 --describe`)
   > before merging if the change touches the renderer, pictures, wire or data path.
   ```
3. Output the same warning prominently (it is copied into the PR body).
4. STOP here — the node SUCCEEDS (missing hardware must not block the workflow,
   but the skip must be impossible to miss).

---

## Phase 4: BUILD THE FIRMWARE

```bash
cd "$WT"
bazel run //robodog_client:build_firmware
```

- ESP-IDF Docker build (image `espressif/idf:v5.3.1`); u8g2 upstream is fetched
  by `u8g2_prepare.sh` (never vendored). Takes minutes — do not interrupt.
- If it FAILS: fix (it is a compile error in your branch), re-run; a red build
  must not flash anything.
- Record the commit flashed for the report: `git -C "$WT" rev-parse HEAD`.

---

## Phase 5: FLASH THE BOARD

```bash
bazel run //robodog_client:flash_firmware -- --port "$PORT"
```

- Host esptool via `flash.sh` (button-free `default_reset`). `@flash_args` is
  re-read from the build output, so the freshly built image goes onto the panel.
- Any failure on the USB-Serial-JTAG interface: if the port wedges, a physical
  unplug/replug is the recovery (record it, retry once).

---

## Phase 6: CAPTURE — Stream The Exact Panel Pixels

```bash
mkdir -p "$ARTIFACTS_DIR/robodog-shots"
python3 robodog_client/capture.py --port "$PORT" --reset --n 4 --timeout 120 \
  --describe --out "$ARTIFACTS_DIR/robodog-shots"
```

- `--reset` reboots the app (fresh frame dumps); `--n 4 --timeout 120` is wide
  enough that the WiFi connect + first gRPC sync land (~40 s post-boot) — so the
  later frames show the synced state, while frame 0 is the pre-sync boot.
- Outputs per frame: `frameN.i1` (raw 15200-byte buffer), `panel_raw_portrait.png`
  (300×400), `panel_landscape.png` (400×300, orientation auto-detected by OCR so
  the clock reads upright), `frameN_analysis.txt` (vision transcription when
  `--describe` worked).
- If `no RD_SHOT frames captured` → the flashed firmware lacks the capture hook
  or is wedged. FATAL for this gate (capture is compiled into every build per the
  README) — re-flash, retry once, then FAIL.

---

## Phase 7: VISION VERIFY — Read The Render Back

Read each frame's state from `frameN_analysis.txt` (optionally open the PNGs).
The transcription looks like:

```
- 2026-10-15 21:25      ← header: expects a real date/time
- RoboDog play date 14:00   ← rows
- BLANK
```

**Reading the device state from the screenshot (README):**
- Header `YYYY-MM-DD HH:MM` present → WiFi up + gRPC sync healthy.
- Header **BLANK** + `no wifi` beside the first row → not connected yet:
  pre-sync boot frame, or WiFi/AP unreachable.
- `stale` → connected but the last sync failed (showing the NVS cache).

**Assertions (on the LAST frame captured):**

1. **HEADER_PRESENT** — the readback shows a date+time line (non-BLANK). A BLANK
   header means no sync → PASS? NO: FAIL (unless the change under test is
   explicitly about the no-sync state, in which case assert the intended hint).
2. **ROWS_RENDERED** — at least one calendar row is transcribed (real
   `GetCalendar` data reaches the panel).
3. **NO_UNEXPECTED_STATUS** — the final frame does not show `no wifi`/`stale`
   (again, unless that status IS the feature under test — then assert exactly that).
4. **ROW_MATCH (best effort)** — cross-check the transcribed titles against the
   LMAO server's `CalendarBundle`. Try the gRPC ground truth:
   ```bash
   # needs the generated stubs + a reachable server; else mark UNVERIFIABLE.
   python3 - <<'EOF' 2>/dev/null || echo "ROW_MATCH=UNVERIFIABLE (server CalendarBundle unreachable)"
   import grpc
   from proto import lma_messages_pb2 as lm, lma_grpc_pb2 as lg
   ch = grpc.insecure_channel(f"{server_host}:50051")
   r = lg.LMAOStub(ch).GetCalendar(lm.CalendarRequest(calendar_id="family"))
   print("ROW_MATCH=PASS (server titles match readback)" if r.events else "ROW_MATCH=FAIL")
   EOF
   ```
   When unreachable, rely on assertions 1–3 + a note that the row titles came
   from the panel's live sync; never claim a match you could not check.

The agent must report the transcription VERBATIM in the artifact — that is the
evidence. Do not paraphrase the panel content from memory of the code.

---

## Phase 8: ARTIFACT — Write The Canonical Hardware E2E Results

Write `$ARTIFACTS_DIR/hardware-e2e.md` (this exact filename is the PR gate that
`lmao-finalize-pr` checks — the robodog gate produces the same contract with
robodog capture evidence):

```markdown
# RoboDog Hardware E2E Results (screen capture + vision model)

**Generated**: {YYYY-MM-DD HH:MM}
**Workflow ID**: $WORKFLOW_ID
**Status**: ✅ PASS | ⚠️ SKIPPED (see Phase 3) | ❌ FAIL

**Flashed HEAD**: {git sha}   **Board**: Waveshare ESP32-S3 RLCD-4.2 (0x303A:0x1001)
**Builder**: `bazel run //robodog_client:build_firmware`
**Flasher**: `bazel run //robodog_client:flash_firmware -- --port <port>`
**Capture**: `capture.py --reset --n 4 --timeout 120 --describe` (→ `$ARTIFACTS_DIR/robodog-shots/`)

| Assertion | Result |
|-----------|--------|
| Frame captured from live panel | ✅ |
| HEADER_PRESENT (sync date/time in readback) | ✅ / ❌ |
| ROWS_RENDERED (≥1 calendar row) | ✅ / ❌ |
| NO_UNEXPECTED_STATUS (no wifi/stale absent on final frame) | ✅ / ❌ |
| ROW_MATCH vs server CalendarBundle | ✅ / UNVERIFIABLE |
| Vision endpoint (Qwen2.5-VL readback) | ✅ {model} |

## Captured frames (verbatim vision readback)

- `frame0_analysis.txt`: `BLANK / RoboDog play date 14:00 / no wifi`   ← pre-sync boot
- `frameN_analysis.txt`: `2026-10-15 21:25 / RoboDog play date 14:00 / BLANK`  ← synced ✅

## Evidence files

- `$ARTIFACTS_DIR/robodog-shots/panel_landscape.png` (400×300 as seen in landscape)
- `$ARTIFACTS_DIR/robodog-shots/panel_raw_portrait.png` (300×400 panel memory)
- `$ARTIFACTS_DIR/robodog-shots/frameN_analysis.txt` (vision transcription)
```

Copy the same content into the node output (the PR body embeds it verbatim).

---

## Phase 9: OUTPUT — Report Results

### If All Pass:

```markdown
## RoboDog Hardware E2E Complete ✅

Captured {N} live frames from the ST7305 panel and verified via the vision
model readback: header clock present (sync healthy), rows rendered, no
unexpected status. Latest frame transcription:

    {verbatim frameN_analysis.txt}

Artifact: `$ARTIFACTS_DIR/hardware-e2e.md`
Next: production health (`robodog-production-health`).
```

### If Blocked (unfixable):

```markdown
## RoboDog Hardware E2E Blocked ❌

**Failed assertion**: {name} — {evidence / readback verbatim}
**Attempts**: {what was tried}
**Required action**: {what a human must do}
```

A failed/blocked gate FAILS this node — do not continue to PR creation.

---

## Success Criteria

- **CAPTURED**: ≥1 live frame streamed from the panel buffer (or loud skip + offline `--i1` evidence)
- **HEADER_PRESENT**: final frame readback has a date/time (or the intended no-sync state assert)
- **ROWS_RENDERED**: the transcriber saw real calendar rows on the panel
- **NO_UNEXPECTED_STATUS**: no `no wifi`/`stale` on the synced frame (unless intentional)
- **EVIDENCE**: `$ARTIFACTS_DIR/hardware-e2e.md` with verbatim transcriptions + PNG paths; loud-skip variant when no board
- **NON_DESTRUCTIVE**: esptool only ever ran on the robodog board port; Cardputer/RNode untouched
