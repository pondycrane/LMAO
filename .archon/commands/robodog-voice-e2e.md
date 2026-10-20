---
description: RoboDog hermes autonomous voice-loop E2E gate (issue #267) — opens the voice session with the RD_LISTEN software button over USB-JTAG, drives a pinned command WAV into the board mic through the test speaker, asserts the bridge journal (ASR transcript + Hermes reply), captures the reply on the test mic for ring-closure re-ASR, and reads the STT/TTS subtitle back off the RLCD with the vision model. No human, no wake word. Loud skip when the board (0x303A:0x1001) or the ALSA test-sound card is missing. Never writes .gate-head.
argument-hint: (no arguments - detects the board + test audio, drives the autonomous voice loop, returns PASS/FAIL with verbatim evidence)
---

# RoboDog Hermes Voice-Loop E2E Gate (autonomous, acoustic replay)

**Workflow ID**: $WORKFLOW_ID

## ⚠️ HARDWARE SAFETY — READ FIRST

1. This gate **drives audio** (test speaker into the board mic, test mic onto
   the board speaker) and opens the voice session with the **RD_LISTEN
   software button** over the SAME USB-JTAG console the capture hook uses.
   The board (Waveshare ESP32-S3 RLCD-4.2, `0x303A:0x1001`) must already be
   flashed with the self-built hermes image (see `robodog-hermes-e2e.md` for
   build/flash) — this gate does not reflash.
2. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) or flash the RNode
   (`/dev/ttyUSB*`). `capture.py --reset` is the only esptool call here (host
   reset on `0x303A:0x1001`, the same sequence flash.sh uses) and is not used
   by the voice loop (RD_LISTEN needs no reboot).
3. The test speaker/mic is the USB "PnP Audio Device" (ALSA card id `Device`,
   `plughw:Device,0`). The host user needs the `audio` group:
   `sudo gpasswd -a $(whoami) audio`. `hermes_voice_e2e.py` runs audio ops
   through `sg audio` so the change applies without a re-login.
4. **Never write `$ARTIFACTS_DIR/.gate-head`** — only `robodog-production-health`
   owns that marker (issue #100 guard).

## What this gate proves

The full voice loop **with no human and no wake word** (RD_LISTEN is the
deterministic software button — the WN9 wake word is unreliable on this board):

```
host RD_LISTEN (USB-JTAG) -> board StartListening() -> WS session opens
   -> test speaker -> pinned command WAV -> board mic
   -> bridge faster-whisper ASR -> Hermes brain -> Kokoro TTS -> board speaker
   -> test mic captures the reply (ring closure) -> host re-ASR
   -> STT/TTS subtitle rendered on the RLCD -> vision readback
```

Complementary to `robodog-hermes-e2e.md` (which proves the board *renders* via
RD_SHOT + vision): this gate proves the whole loop including the acoustic
legs. If real-speech ASR is flaky the gate degrades to the documented `E2E_STT`
injection fallback (`POST /xiaozhi/inject` — covers brain→TTS→speaker→display,
skipping only the mic leg) and says so loudly.

## Phase 1: HOST PREREQUISITES

```bash
python3 -c "import serial; import PIL"      # orchestrator + capture
~/xiaozhi/.venv/bin/python -c "import faster_whisper, numpy, PIL, serial"
systemctl --user is-active lmao-robodog-voice   # bridge must log (BRIDGE_LOG_FILE
                                                # -> ~/xiaozhi/voice_bridge.log,
                                                # the orchestrator's assertion source)
test -f ~/xiaozhi/voice_bridge.log             # the tail the gate polls
getent group audio | grep -q "$(whoami)"        # ALSA test speaker/mic access
ffmpeg -version >/dev/null                       # audio play/record
curl -s -m 8 http://127.0.0.1:8003/healthz      # bridge OTA/inject port
```

The orchestrator must run under the bridge venv (faster-whisper, numpy, PIL,
pyserial): `~/xiaozhi/.venv/bin/python`. It tails the bridge's INFO log file
(`BRIDGE_LOG_FILE`, default `~/xiaozhi/voice_bridge.log`) — never hammers
journalctl during the realtime audio leg.

## Phase 2: DETECT — board + test audio

The orchestrator detects the board (`ROBODOG_PORT` wins, else fingerprint
`0x303A:0x1001`) and the ALSA USB test card (id `Device`), then checks bridge
health. Absent anything → LOUD SKIP:

- No board → gate cannot exercise the hardware loop.
- No USB audio card → no test speaker/mic for the acoustic legs.
- Bridge healthz down → no journal/WS to assert against.

## Phase 3: RUN THE LOOP

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
E2E_RUNS=3 ~/xiaozhi/.venv/bin/python robodog_client/voice/hermes_voice_e2e.py \
  --corpus time --artifacts "$ARTIFACTS_DIR"
```

- Default 3 consecutive loops (acceptance criterion from issue #267) — fresh
  RD_LISTEN each loop, 12 s settle so the board idles/WS drains between runs.
- Volume default `RD_PLAY_VOL=0.7`: louder saturates the board mic so its VAD
  never sees silence and the ASR buffer explodes (measured live).
- Per-loop evidence under `$ARTIFACTS_DIR/robodog-voice-e2e/loopN/`:
  `mic_ring.wav` + `reply_burst.wav` (test-mic captures), `shots/frame*.i1`
  panel dumps + `subtitle_readback.txt`, `ring_reasr.txt`.
- `E2E_STT_TEXT=<canned>` runs the injection fallback (skips the mic leg).

Fallback within a mic run: if the ASR transcript doesn't match the pinned
corpus on an attempt, the run re-executes that loop through `/xiaozhi/inject`
and marks `ASR_MATCH: FAIL (mic ASR) -> E2E_STT fallback` — the gate still
completes brain→TTS→speaker→display.

## Phase 4: ASSERT + ARTIFACT

Assertions (each PASS/FAIL/SKIP, logs verbatim):

1. **OPEN_SESSION** — `device connected session=...` + `listen start
   (mode=manual)` in the bridge journal right after `RD_LISTEN` (no button, no
   wake word).
2. **ASR_MATCH** — journal `ASR text='...'` normalized-match the pinned corpus
   transcript (the acoustic test-speaker→board-mic leg).
3. **BRAIN_REPLY** — journal `Hermes voice|reply reply:` non-empty (Hermes
   brain + auto-TTS).
4. **RING** — the test mic captured the board speaker's TTS and the host
   re-ASR aligns with the reply/subtitle (acoustic TTS→speaker→test-mic→ASR
   closure); loud note when only "audible" (weak placement).
5. **SUBTITLE** — `capture.py --describe` readback of a panel dump carries the
   STT transcript phrase (RLCD render leg).

Write `$ARTIFACTS_DIR/robodog-voice-loop-e2e.md` (distinct from the calendar
gate's `hardware-e2e.md` and the capture gate's `hermes-hardware-e2e.md`; this
gate NEVER writes `.gate-head`):

```markdown
# RoboDog Hermes Voice-Loop E2E Results (acoustic replay)
**Generated**: {YYYY-MM-DD HH:MM}   **Workflow ID**: $WORKFLOW_ID
**Status**: ✅ PASS | ⚠️ SKIPPED (see Phase 2) | ❌ FAIL
**Board**: Waveshare ESP32-S3 RLCD-4.2 (0x303A:0x1001)
**Mode**: real-microphone | E2E_STT injection
**Corpus**: time ('what time is it')  **Runs**: 3
| Assertion | loop 1 | loop 2 | loop 3 |
|---|---|---|---|
| OPEN_SESSION | ✅ | ✅ | ✅ |
| ASR_MATCH | ✅ | ... | ... |
| BRAIN_REPLY | ... | ... | ... |
| RING | ... | ... | ... |
| SUBTITLE | ... | ... | ... |
## Verbatim transcripts + re-ASR per loop (the evidence)
```
