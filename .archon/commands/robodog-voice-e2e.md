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
bazel run //robodog_client:hermes_voice_e2e -- --corpus count \
  --artifacts "$ARTIFACTS_DIR"
# E2E_RUNS default 3; `E2E_RUNS=…` still overrides (bazel run env).
# the `count` corpus ("count slowly from twenty to thirty") is the long-reply
# corpus (issue #274): a deterministic 11-number list the brain reads as a
# reliably ~7-10 s spoken VOICE reply whose replies reliably exceed
# the 3.5 s discriminative floor so READOUT_COMPLETE is decisive, instead of
# `time`-class ~3.0 s replies that the ~0.5 s truncation delta washes out.
# Equivalently, direct venv: E2E_RUNS=3 ~/xiaozhi/.venv/bin/python \
#   robodog_client/voice/hermes_voice_e2e.py --corpus count --artifacts "$ARTIFACTS_DIR"
```

- Default 3 consecutive loops (acceptance criterion from issue #267). Loop 1
  opens with its own `RD_LISTEN`; every later loop reuses the listen the
  PREVIOUS loop's `POST_READOUT_RELISTEN` armed a few seconds earlier (a fresh
  `RD_LISTEN` into an already-listening board is a measured no-op) — with a
  short `RD_BETWEEN_LOOP_SETTLE=3` drain between runs (the old flat 12 s
  settle is gone).
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
   wake word). From loop 2 onward this is the `RD_LISTEN` the PREVIOUS loop's
   `POST_READOUT_RELISTEN` fired (one open per boundary — no double-open).
2. **ASR_MATCH** — journal `ASR text='...'` normalized-match the pinned corpus
   transcript (the acoustic test-speaker→board-mic leg).
3. **BRAIN_REPLY** — journal `Hermes voice|reply reply:` non-empty (Hermes
   brain + auto-TTS).
4. **READOUT_COMPLETE** (issue #274) — the full active-play span of the reply
   on the test mic (below the ambient floor) vs the bridge's reported TTS
   duration: `played_fraction = span / reply_secs >= 0.85` proves the reply was
   read out to the end (a read-out truncated at ~2.4 s by the decode-queue drop
   that #272's pacing removes FAILs instead of passing as RING "audible").
   SKIP only when `reply_secs < 3.5 s` (too short to discriminate); the `count`
   corpus makes that rare. `reply_secs` is refreshed from the bridge's exact
   `tts streamed full <N>` byte count polled up to the reply's `tts stop` (the
   0.1 s-windowed play-span metric keeps the ~0.85 discriminant real — an
   80%-truncated reply measures below it).
5. **POST_READOUT_RELISTEN** (issue #274) — after the bridge logs the reply's
   `session=<id> tts stop`, a fresh `RD_LISTEN` must produce a `listen start`
   within ~15 s (the shrunken replacement for the old flat 12 s settle between
   loops). **FAIL (not SKIP) when the running bridge does not log the `tts
   stop` anchor** — that is the deployment prerequisite this gate must catch:
   redeploy `robodog_client/voice/voice_bridge.py` and restart
   `systemctl --user lmao-robodog-voice`. The **realtime auto-relisten** normal
   conversation hits is NOT observable here: the board returns to idle after
   TTS with no reliable wake trigger and the E2E drives `RD_LISTEN`
   (`mode=manual`) for every open — the fresh `RD_LISTEN` right after tts-stop
   is the reachable proxy.
6. **RING** — the test mic captured the board speaker's TTS and the host
   re-ASR aligns with the reply/subtitle (acoustic TTS→speaker→test-mic→ASR
   closure); loud note when only "audible" (weak placement).
7. **SUBTITLE** — `capture.py --describe` readback of a panel dump carries the
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
**Corpus**: count ('count slowly from twenty to thirty')  **Runs**: 3
| Assertion | loop 1 | loop 2 | loop 3 |
|---|---|---|---|
| OPEN_SESSION | ✅ | ✅ | ✅ |
| ASR_MATCH | ✅ | ... | ... |
| BRAIN_REPLY | ... | ... | ... |
| READOUT_COMPLETE | ... | ... | ... |
| POST_READOUT_RELISTEN | ... | ... | ... |
| RING | ... | ... | ... |
| SUBTITLE | ... | ... | ... |
## Verbatim transcripts + re-ASR + read-out spans per loop (the evidence)
```
