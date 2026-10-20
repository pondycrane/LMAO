---
description: RoboDog hermes (XiaoZhi voice firmware) hardware E2E gate — build + flash the self-built hermes image (capture hook compiled in), then SCREEN-CAPTURE the exact ST7305 pixels (RD_SHOT frames over USB-JTAG, decode + vision readback via capture.py) to confirm the voice board really renders on the RLCD. Loud skip when the board is not attached. Never writes .gate-head.
argument-hint: (no arguments - detects the robodog board, builds/flashes hermes, captures + vision-verifies the panel)
---

# RoboDog Hermes Hardware E2E Gate (screen capture + vision model)

**Workflow ID**: $WORKFLOW_ID

## ⚠️ HARDWARE SAFETY — READ FIRST

1. This gate flashes the **hermes firmware** (XiaoZhi voice app, self-built
   from pinned upstream source with the RoboDog capture hook) onto the
   **robodog board** — the Waveshare ESP32-S3 RLCD-4.2 (ST7305), verified
   fingerprint `0x303A:0x1001`. The ONLY flash path is
   `bazel run //robodog_client:flash_robodog_hermes -- --port <port>`
   (`robodog_client/xiaozhi_flash.sh`, host esptool — button-free reset;
   Docker ttyACM RTS passthrough breaks it). This replaces the calendar app
   (reflash `//robodog_client:flash_firmware` to restore it).
2. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) or flash the RNode
   (`/dev/ttyUSB*`) — this gate's detection below ONLY matches the robodog
   `0x303A:0x1001` fingerprint (or an explicit `ROBODOG_PORT`).
3. `capture.py --reset` issues only an esptool reset on the robodog board port
   (the same sequence `flash.sh` uses) — safe; it never reflashes flash content.
4. **Never write `$ARTIFACTS_DIR/.gate-head`** — only `robodog-production-health`
   owns that marker (issue #100 guard).

## What "verification" means here

Same pixel contract as the calendar gate, but for the hermes client: the rebuilt
XiaoZhi firmware streams its **exact full-frame ST7305 panel memory** (400×300
landscape, 15000 B) as `RD_SHOT` frames over USB-JTAG; `capture.py` reconstructs
`panel_landscape.png` (inverting the upstream `custom_lcd_display.cc` landscape
LUT) and `--describe` reads it back with the self-hosted vision model
(Qwen2.5-VL at `spark-eebf`, `~/.hermes/config.yaml "vision"`; overridable with
`RD_VISION_BASE_URL` / `RD_VISION_MODEL`). You reason from that transcription —
it is what the panel really shows.

The voice loop itself (ASR → Hermes brain → Kokoro TTS → speaker) is verified
functionally; this gate proves the hermes client **renders on the RLCD** and
that the capture pipeline works for it.

## Phase 1: HOST PREREQUISITES

```bash
python3 -c "import serial; import PIL"        # capture.py needs both
python3 -m esptool --help >/dev/null 2>&1     # flash.sh host esptool
docker version >/dev/null 2>&1                # build.sh ESP-IDF image runner
curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 http://spark-eebf:8000/v1/models  # vision endpoint
```

Missing `serial`/`PIL`/`esptool` → install and re-check; capture cannot run
without them. Vision endpoint timeout is not fatal: capture still writes the
PNGs, `--describe` just fails per-frame (record `VISION=UNVERIFIABLE`).

## Phase 2: DETECT — the robodog board

An explicit `ROBODOG_PORT` wins; otherwise auto-detect ONLY the verified
robodog fingerprint — never touch `0x303A:0x8120` (Cardputer) or `0x10C4:0xEA60`
(RNode):

```bash
PORT="${ROBODOG_PORT:-}"
if [ -z "$PORT" ]; then
  PORT=$(python3 - <<'EOF'
import serial.tools.list_ports
for p in serial.tools.list_ports.comports():
    if p.vid == 0x303A and p.pid == 0x1001:
        print(p.device)
        break
EOF
)
fi
echo "ROBODOG_PORT=${PORT:-NONE}"
```

## Phase 3: NO BOARD → LOUD SKIP

If `ROBODOG_PORT` is NONE:
1. Try offline decode evidence: if a saved hermes `robodog_client/shots/frame*.i1`
   (15000 B) exists from a prior run, decode it:
   ```bash
   mkdir -p "$ARTIFACTS_DIR/robodog-hermes-shots"
   python3 robodog_client/capture.py --i1 robodog_client/shots/frame0.i1 \
     --describe --out "$ARTIFACTS_DIR/robodog-hermes-shots"
   ```
   Record `VISION=PASS offline`, `E2E=SKIPPED` (no live flash).
2. Write `$ARTIFACTS_DIR/hermes-hardware-e2e.md` with `Status: ⚠️ SKIPPED —
   ROBODOG BOARD NOT DETECTED` and STOP (node succeeds; the skip must be loud).

## Phase 4: BUILD (first build is long — IDF v6.0 image + full XiaoZhi tree)

```bash
bazel run //robodog_client:build_robodog_hermes
```

If it FAILS: fix and re-run; a red build must not flash anything. Record the
commit for the report: `git rev-parse HEAD`.

## Phase 5: FLASH

```bash
bazel run //robodog_client:flash_robodog_hermes -- --port "$PORT"
```

(skips rebuild when the image is already built). On USB-Serial-JTAG wedge:
physical unplug/replug, retry once.

## Phase 6: CAPTURE — stream the exact panel pixels

```bash
mkdir -p "$ARTIFACTS_DIR/robodog-hermes-shots"
python3 robodog_client/capture.py --port "$PORT" --reset --n 8 --timeout 180 \
  --describe --out "$ARTIFACTS_DIR/robodog-hermes-shots"
```

- `--reset` reboots into a fresh boot (boards dump a frame ~every 4 s — the
  throttle ensures dumps never interleave). `--n 8` spans boot → WiFi connect →
  OTA check → idle, so later frames show the connected status UI.
- Outputs per frame: `frameN.i1` (raw 15000-byte buffer), `panel_landscape.png`
  (400×300 as drawn), `frameN_analysis.txt` (vision transcription).
- If `no RD_SHOT frames captured` → the flashed image lacks the capture hook or
  is wedged. FATAL for this gate — re-flash, retry once, then FAIL.
- To capture a mid-conversation frame, press BOOT and speak while capturing
  (the display shows STT/TTS subtitle state); the boot/idle frames alone prove
  the render path.

## Phase 7: VISION VERIFY — read the render back

Report each frame's transcription VERBATIM in the artifact (that is the
evidence; never paraphrase from memory of the code). Assert on the LAST frame:

1. **PANEL_RENDERED** — readback shows a non-empty screen (text/icons/face).
   A `BLANK` panel = display not driving → FAIL.
2. **RECOGNIZABLE** — the transcription reflects the XiaoZhi status UI (a
   character/status line/subtitle), not noise.
3. If the bridge (`lmao-robodog-voice.service`) is up and the board connects,
   later frames should show the *connected* state (vs. `no wifi`-type boot
   frames) — record what the panel shows either way.

## Phase 8: ARTIFACT — write the hermes hardware E2E results

Write `$ARTIFACTS_DIR/hermes-hardware-e2e.md` (distinct from the calendar gate's
`hardware-e2e.md`; this gate is for the voice board and NEVER writes
`.gate-head`):

```markdown
# RoboDog Hermes Hardware E2E Results (screen capture + vision model)

**Generated**: {YYYY-MM-DD HH:MM}   **Workflow ID**: $WORKFLOW_ID
**Status**: ✅ PASS | ⚠️ SKIPPED (see Phase 3) | ❌ FAIL
**Flashed HEAD**: {git sha}   **Board**: Waveshare ESP32-S3 RLCD-4.2 (0x303A:0x1001)
**Builder**: `bazel run //robodog_client:build_robodog_hermes`
**Flasher**: `bazel run //robodog_client:flash_robodog_hermes -- --port <port>`
**Capture**: `capture.py --reset --n 8 --timeout 180 --describe`

| Assertion | Result |
|-----------|--------|
| Frame captured from live panel (RD_SHOT/15000 B) | ✅ / ❌ |
| PANEL_RENDERED (non-blank readback) | ✅ / ❌ |
| RECOGNIZABLE (XiaoZhi status UI in readback) | ✅ / ❌ |
| Vision endpoint (Qwen2.5-VL readback) | ✅ {model} |

## Captured frames (verbatim vision readback)
- frame0: ...
- frameN (last): ...
```
