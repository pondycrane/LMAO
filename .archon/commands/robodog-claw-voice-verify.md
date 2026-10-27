---
description: RoboDog ESP-Claw dedicated voice verification gate — acoustic completeness of the CHUNKED reply. The esp-claw root agent reply is split at sentence breaks into a queue of <= TTS_MAX_CHARS chunks (voice_split_reply), each rendered progressively on the RLCD and spoken in order, so the WHOLE reply is read out instead of the old "always truncated to ~240 chars" behaviour. This gate drives the corpus WAV into the board mic, asserts from the DEVICE SERIAL CONSOLE that every queued `voice chunk i/n` had its own TTS start->stop bracket (CHUNKED_QUEUE — the authoritative, deterministic proof the full reply was voiced), then captures the whole multi-chunk playback on the test mic and re-ASRs it for the reply's TAIL tokens (FULL_READOUT — nothing was cut off at the end). No bridge, no Hermes, pixel/render leg intentionally NOT here (that is robodog-claw-e2e's job). Loud skip when the board or the ALSA test card is missing. Never writes .gate-head or hardware-e2e.md.
argument-hint: (no arguments - detects the board + test audio, drives the autonomous chunked voice loop, returns PASS/FAIL with verbatim evidence)
---

# RoboDog ESP-Claw Voice Verification Gate (chunked-readout completeness)

**Workflow ID**: $WORKFLOW_ID

## What this gate proves

The response audio is **not truncated**: the full LLM reply is split into
sentence-bounded chunks (each ≤
`CONFIG_ROBODOG_VOICE_TTS_MAX_CHARS`, default 240), queued in order, rendered
progressively on the RLCD (`robodog_ui_append_assistant`) and spoken
(`voice_split_reply` + per-chunk `voice_speak`) — the voice reaches the LAST
word of the reply.

Two evidence legs:

```
CHUNKED_QUEUE : device `voice chunk i/n:` then `TTS start` ... `TTS stop`
                for EVERY chunk — the whole reply was queued AND bracketed.
                (deterministic console evidence, no acoustic ambiguity)
FULL_READOUT  : the test mic, recording across ALL chunk brackets, captured
                the playback and host re-ASR carries the reply's TAIL tokens —
                the read-out reached the end of the reply.
```

## ⚠️ HARDWARE SAFETY — READ FIRST

1. This gate **drives audio** (test speaker into the board mic, test mic onto
   the board speaker) and opens a voice turn with **`rd_listen`** over the
   same USB-JTAG console the capture hook uses. The board (Waveshare
   ESP32-S3 RLCD-4.2, `0x303A:0x1001`) must already be flashed with the
   self-built esp-claw image (`build_esp_claw.sh` + `flash_esp_claw.sh` — this
   gate does not reflash, nor does `robodog-claw-e2e`; re-flash via
   `flash_esp_claw.sh` when the firmware changed).
2. **NEVER run esptool on the Cardputer** (`0x303A:0x8120`) or flash the RNode
   (`/dev/ttyUSB*`). This board is the ONLY esptool-allowed target, and only
   via `flash_esp_claw.sh` / `flash.sh`.
3. The test speaker/mic is the bidirectional USB "PnP Audio Device" = ALSA
   **card 2 (`hw:2`)** on the workspace host; there is **no `default` alias**.
   Audio ops run through `sg audio` (`E2E_AUDIO_SG`); override with
   `RD_ALSA_DEVICE`.
4. **Never write `$ARTIFACTS_DIR/.gate-head`** — only
   `robodog-production-health` owns that marker. Do **not** write
   `$ARTIFACTS_DIR/hardware-e2e.md` either — that contract slot belongs to the
   regular `robodog-claw-e2e` node.

## Phase 1: HOST PREREQUISITES

```bash
python3 -c "import serial; import PIL"
ffmpeg -version >/dev/null
getent group audio | grep -q "$(whoami)"
curl -s -m 8 http://192.168.50.153:11801/v1/audio/transcriptions -o /dev/null -w '%{http_code}\n'
```

## Phase 2: DETECT — board + test audio

`robodog_claw_e2e.py --voice-verify` detects the board (`ROBODOG_PORT` wins,
else `0x303A:0x1001`) and the ALSA USB test card. Absent either → LOUD SKIP
(writes `SKIPPED.txt`, returns 0).

## Phase 3: RUN THE VOICE VERIFICATION

```bash
python3 robodog_client/esp-claw/robodog_claw_e2e.py --voice-verify \
  --corpus capabilities --artifacts "$ARTIFACTS_DIR"
# 'capabilities' = "what can you do" — the esp-claw root agent answers with a
# verbose multi-sentence reply, so the splitter must emit 2+ chunks (the
# queue actually engages).  RD_VOICE_CAP_SECS (default 240) must cover the
# whole multi-chunk playback; RD_PLAY_VOL / RD_PLAY_SETTLE as in the E2E.
```

- The device console hooks asserted are the new `voice chunk i/n:` log added
  by the chunk queue, plus the existing `listen started`, `ASR text=`,
  `agent reply:`, `TTS start`/`TTS stop`.
- This gate intentionally does **not** run the RLCD subtitle leg — the render
  is verified by the sibling `robodog-claw-e2e` node (SUBITTLE leg).

## Phase 4: ASSERT + ARTIFACT

Assertions (PASS / PASS-WEAK / FAIL / SKIP, verbatim evidence written back):

1. **OPEN_SESSION** — after `rd_listen`, device logs `listen started`.
2. **ASR_MATCH** — device `ASR text="..."` normalized-matches the corpus
   transcript (≥ 0.75 token overlap). Missing → FAIL, rest SKIP (fail-fast).
3. **REPLY** — device `agent reply: <text>` non-empty.
4. **CHUNKED_QUEUE** — every `voice chunk i/n` seen with a contiguous
   1..n index AND `TTS start`/`TTS stop` counts ≥ n: **the FULL reply was
   queued and every chunk played**. This is the authoritative anti-truncation
   proof (deterministic, console-side). No chunk logs → FAIL (queue did not
   run — regression of the chunker).
5. **FULL_READOUT** — mic capture spans all chunk brackets; re-ASR carries ≥ 2
   of the reply's LAST 12 tokens → PASS (the read-out reached the end). Audible
   but tail absent → PASS-WEAK (marginal speaker→mic coupling; CHUNKED_QUEUE is
   the authoritative evidence). No capture → FAIL.
6. **TTS** — at least one `TTS start` + `TTS stop` bracket observed.

Write `$ARTIFACTS_DIR/robodog-claw-voice-verify/robodog-claw-voice-verify.md`
(per-loop `speaker.wav` + `ring_reasr.txt` also recorded). Never writes
`.gate-head` or `hardware-e2e.md`:

```markdown
# RoboDog ESP-Claw Voice Verification Results
**Generated**: {YYYY-MM-DD HH:MM}   **Workflow ID**: $WORKFLOW_ID
**Status**: ✅ PASS | ⚠️ SKIPPED (see Phase 2) | ❌ FAIL
**Board**: Waveshare ESP32-S3 RLCD-4.2 (0x303A:0x1001)
**Corpus**: capabilities ('what can you do')   **Runs**: 1
- OPEN_SESSION:   PASS
- ASR_MATCH:      ...
- REPLY:          ...
- CHUNKED_QUEUE:  PASS N chunk(s), all TTS-bracketed (start=.. stop=..)
- FULL_READOUT:   PASS tail tokens read out (overlap=..)
- TTS:            PASS start+stop
## Verbatim transcripts + chunk queue + mic re-ASR (the evidence)
```

## Relationship to the other claw gates

- `robodog-claw-e2e` — full voice loop INCLUDING the RLCD subtitle readback;
  the author of `hardware-e2e.md`. Runs first.
- `robodog-claw-voice-verify` — acoustic completeness of the CHUNKED reply
  only; corroborates that the reply reached its end. Runs after claw-e2e.
- `robodog-production-health` — .gate-head owner; runs last.
