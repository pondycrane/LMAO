---
description: RoboDog ESP-Claw autonomous voice-loop E2E gate (DESIGN.md Phase 2) — drives the corpus WAV into the board mic through the ALSA test speaker, asserts each voice-loop leg from the DEVICE SERIAL CONSOLE (rd_listen -> listen started -> ASR text= -> agent reply -> TTS start/stop), captures the board's spoken reply on the test mic for ring-closure re-ASR, and reads the rendered reply back off the RLCD with the vision model. No bridge, no Hermes — the assertions come from the board itself. Loud skip when the board or the ALSA test card is missing. Never writes .gate-head.
argument-hint: (no arguments - detects the board + test audio, drives the autonomous voice loop, returns PASS/FAIL with verbatim evidence)
---

# RoboDog ESP-Claw Voice-Loop E2E Gate (autonomous, device-direct)

**Workflow ID**: $WORKFLOW_ID

## ⚠️ HARDWARE SAFETY — READ FIRST

1. This gate **drives audio** (test speaker into the board mic, test mic onto
   the board speaker) and opens a voice turn with the **`rd_listen`** console
   command over the SAME USB-JTAG console the capture hook uses. The board
   (Waveshare ESP32-S3 RLCD-4.2, `0x303A:0x1001`) must already be flashed with
   the self-built esp-claw image (see `build_esp_claw.sh` / `flash_esp_claw.sh`
   — this gate does not reflash).
2. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) or flash the RNode
   (`/dev/ttyUSB*`). This board (`0x303A:0x1001`) is the ONLY esptool-allowed
   target, and only via `flash_esp_claw.sh` / `flash.sh`.
3. The test speaker/mic is the bidirectional USB "PnP Audio Device" = ALSA
   **card 2 (`hw:2`)** on the workspace host; there is **no `default` alias**.
   Node needs the `audio` group; `robodog_claw_e2e.py` runs audio ops through
   `sg audio`. Override with `RD_ALSA_DEVICE`. A `default`-named card appears
   where a real ALSA default exists — the script parametrizes either way.
4. **Never write `$ARTIFACTS_DIR/.gate-head`** — only `robodog-production-health`
   owns that marker (issue #100 guard).

## What this gate proves

The full **device-initiated, bridge-free** voice loop (DESIGN.md Phase 2):

```
host rd_listen (USB-JTAG) -> board voice task -> ES7210 mic subscriber opened
   -> energy-VAD (VAD_ENERGY=300, VAD_SILENCE_MS=800)
   -> test speaker -> corpus WAV -> board mic
   -> board -> Whisper /v1/audio/transcriptions (host-forward) -> ASR text
   -> board claw_agent_mgr -> gx10 LLM -> reply rendered on RLCD
   -> board -> Kokoro /v1/audio/speech -> 24 kHz WAV -> 16 kHz resample
   -> ES8311 speaker (TTS start/stop)
   -> test mic captures the reply -> host re-ASR (ring closure)
   -> RD_SHOT frame of the rendered reply -> vision readback (subtitle)
```

Every leg is asserted from the **device serial console** logs
(`listen started`, `ASR text="..."`, `agent reply: ...`, `TTS start` /
`TTS stop`) plus one RD_SHOT frame readback — there is no bridge journal.

## Phase 1: HOST PREREQUISITES

```bash
python3 -c "import serial; import PIL"      # orchestrator + capture
ffmpeg -version >/dev/null                  # audio play/record
getent group audio | grep -q "$(whoami)"    # ALSA test speaker/mic access
# Whisper/Kokoro reachable on the board's subnet via the host-forward socat:
curl -s -m 8 http://192.168.50.153:11801/v1/audio/transcriptions -o /dev/null -w '%{http_code}\n'
# (the board's ASR/TTS URLs are baked by build_esp_claw.sh from CLAW_ASR_URL/CLAW_TTS_URL)
```

## Phase 2: DETECT — board + test audio

`robodog_claw_e2e.py` detects the board (`ROBODOG_PORT` wins, else
`0x303A:0x1001`) and the ALSA USB test card. Absent either → LOUD SKIP (writes
`SKIPPED.txt`, returns 0, never writes `.gate-head`).

## Phase 3: RUN THE LOOP

```bash
python3 robodog_client/esp-claw/robodog_claw_e2e.py --corpus capabilities \
  --artifacts "$ARTIFACTS_DIR"
# Volume default RD_PLAY_VOL=0.8; RD_PLAY_SETTLE=3 (ms after `listen started`);
# corpus 'capabilities' = "what can you do". E2E_RUNS=N overrides (default 1:
# each full loop is ~60-80 s; the voice loop is slow by design).
```

- The gate reads the RD_SHOT frame **from the same console handle** (a second
  open races the self-tick flood) and **skips truncated forced frames** (the
  `rdshot` force-publish races the flood; the next self-tick frame is complete).
- `RING` may be `PASS-WEAK` when the board speaker → test-mic acoustic
  coupling is marginal (it still proves audible TTS); a real FAIL only when the
  test mic captured nothing.

## Phase 4: ASSERT + ARTIFACT

Assertions (PASS/FAIL/SKIP, verbatim evidence written back to the artifact):

1. **OPEN_SESSION** — after `rd_listen`, device logs `listen started`.
2. **ASR_MATCH** — device `ASR text="..."` normalized-matches the corpus
   transcript (>= 0.75 token overlap). Empty / missing ASR → **FAIL (no ASR)**
   and the rest of the turn is SKIP'd (fail-fast, no 180 s hang).
3. **REPLY** — device `agent reply: <text>` non-empty (native agent + LLM).
4. **TTS** — device `TTS start` then `TTS stop` (ES8311 playback leg).
5. **RING** — test mic captured the board speaker; host re-ASR (same Whisper
   endpoint) overlaps the reply (or audible-only → PASS-WEAK).
6. **SUBTITLE** — RD_SHOT frame -> `capture.decode_hermes` -> Qwen2.5-VL
   readback carries >= 2 distinct reply tokens (the reply renders on the RLCD).

Write `$ARTIFACTS_DIR/robodog-claw-e2e/robodog-claw-e2e.md` (under the run
subdir where the verbatim transcripts also live), plus a top-level
`$ARTIFACTS_DIR/hardware-e2e.md` summary for the generic gate-chain/finalize
contract. Never writes `.gate-head`:

```markdown
# RoboDog ESP-Claw Voice-Loop E2E Results
**Generated**: {YYYY-MM-DD HH:MM}   **Workflow ID**: $WORKFLOW_ID
**Status**: ✅ PASS | ⚠️ SKIPPED (see Phase 2) | ❌ FAIL
**Board**: Waveshare ESP32-S3 RLCD-4.2 (0x303A:0x1001)
**Corpus**: capabilities ('what can you do')   **Runs**: 1
- OPEN_SESSION: PASS/FAIL
- ASR_MATCH:    ...
- REPLY:        ...
- TTS:          ...
- RING:         ...
- SUBTITLE:     ...
## Verbatim transcripts + subtitle readback (the evidence)
```
