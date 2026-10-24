# RoboDog ESP-Claw — Standalone On-Device Agent (Design)

**Status**: design — not yet implemented.
**Goal**: run [ESP-Claw](https://github.com/espressif/esp-claw) (`edge_agent`)
standalone on the Waveshare ESP32-S3 RLCD-4.2. The board calls **all** model
endpoints (ASR, LLM, TTS) on its own over LAN WiFi. No `voice_bridge.py`, no
Hermes gateway, no XiaoZhi protocol. The RLCD presentation layer stays exactly
as PR #278 defined it: text-only, no face/emote, ~14 px font, left-aligned
multiline wrap filling the 400×300 panel, status line on top.

## Problem (current state)

Today's voice mode is a three-process, two-firmware stack:

```
board mic --Opus/WS :8000--> voice_bridge (VAD, faster-whisper ASR)
    --"user" text--> Hermes brain WS (:8101, xiaozhi platform plugin)
    <--Kokoro WAV--  Hermes auto-TTS (spark-5307:8004)
board speaker <--paced 24 kHz Opus-- voice_bridge
RLCD <--{"type":"stt"/"tts"} frames over the same WS-- bridge-driven
```

The RLCD renders nothing the bridge didn't send (`stt` / `tts sentence_start`
frames → upstream XiaoZhi `Display::SetChatMessage`; layout from
`robodog_client/voice/robodog_hermes_display.patch`). Every hop except
mic/speaker is off-device, and the board is a dumb audio terminal.

## Target architecture

```
┌──────────── Waveshare ESP32-S3 RLCD-4.2 — ESP-Claw edge_agent ───────────┐
│ ES8311 mic → VAD/turn-taking ──user text──▶ claw_core agent loop         │
│ BOOT btn / RD_LISTEN console ──▶ VAD                                       │
│ claw_core ──reply text──▶ RLCD chat UI (text-only, PR #278 look)         │
│ claw_core ──PCM──▶ ES8311 speaker                                        │
└──────┬───────────────┬──────────────────────────┬───────────────────────┘
       │ POST /v1/audio/transcriptions          │ POST /v1/audio/speech
       ▼               │ /v1/chat/completions    ▼
 Whisper endpoint  OpenAI-style LLM       Kokoro TTS endpoint
 (user's own,      endpoint (gx10 vLLM    (user's own,
  LAN, URL NVS-cfg) or cloud key, NVS-cfg) spark-5307:8004, af_heart)
```

Survives unchanged: the acoustic test rig + corpus, `capture.py` (RD_SHOT
format), the calendar firmware, and the user's own LAN model services —
**Kokoro TTS** (`kokoro_server.py` / `kokoro-tts.service`, `spark-5307:8004`,
`af_heart`) and a **Whisper ASR** endpoint — which the board calls directly.
Exact endpoint URLs/model names are NVS-configured, not baked in (user's
deployment; names TBD).

Deleted at cutover: XiaoZhi firmware + 3 patches, `voice_bridge.py` (including
its in-process faster-whisper — replaced by the standalone Whisper endpoint),
`hermes-platform/xiaozhi/`, `lmao-robodog-voice.service`, OTA/WS board
provisioning.

## Work items

### 1. Fetch/build infra — `robodog_client/esp-claw/` (this folder, LMAO-owned)

Mirror the XiaoZhi precedent (`fetch_xiaozhi.sh`); no vendoring (AGENTS.md):

- `fetch_esp_claw.sh` — sha256-pinned tarball of `espressif/esp-claw` at a
  fixed commit → gitignored `.esp-claw/` (cache `~/.cache/lmao-esp-claw-src`).
- `overlay/` — LMAO-owned board package + voice component +
  `sdkconfig.defaults.robodog_claw`, copied into the fetched tree at build
  time (additive; no upstream files modified).
- `build_esp_claw.sh` — Docker `espressif/idf:v5.5.4` (ESP-Claw's recommended
  IDF, **not** the v6.0.2 XiaoZhi image),
  `idf.py bmgr -c ./boards -b lmao/robodog_rlcd_4_2 && idf.py build`.
- Bazel: `//robodog_client:build_robodog_claw`, `:flash_robodog_claw` (host
  esptool via the sanctioned `robodog_client/flash.sh` pattern — this board
  is the one esptool-allowed target).

### 2. Board package — `boards/lmao/robodog_rlcd_4_2/` (in `overlay/`)

`board_info.yaml`, `board_devices.yaml`, `board_peripherals.yaml`,
`setup_device.c`, `sdkconfig.defaults.board`, declaring:

- **ST7305 RLCD** (SPI: MOSI 12, SCK 11, DC 5, CS 40, RST 41, TE 6 — from
  `robodog_client/main/config.h`) via a new LMAO-owned `esp_lcd_st7305`
  component, modeled on ESP-Claw's per-board `esp_lcd_fl7707n` /
  `esp_lcd_nv3051f` examples. Pixel-packing/GRAM addressing ports from the
  proven `u8g2_st7305` logic in the calendar firmware.
- **ES8311 codec + onboard mic** — pins extracted from the upstream XiaoZhi
  board definition (fetched by `fetch_xiaozhi.sh`; exact values live there,
  not in this repo — Phase 0 confirms). Driver via `esp_codec_dev` from the
  IDF component manager, not hand-rolled.
- **BOOT button** (listen trigger) and **USB-Serial/JTAG primary console**
  (`CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`) for RD_LISTEN / RD_SHOT.

### 3. Presentation layer (the parity contract)

ESP-Claw renders via `display_service` + LVGL; its default UI is
emote-centric, so parity is explicit work, not config:

- Monochrome: LVGL `I1` color format, 400×300, 15000 B framebuffer —
  identical geometry to what `capture.py` (`decode_hermes`) already decodes.
- LMAO-owned UI component (registered from the board glue) subscribing to
  agent events (Event Router, verbose `agent_stage`) and rendering: user
  transcript on ASR completion, assistant reply as it finalizes — same
  roles/timing as `SetChatMessage("user"|"assistant", …)` today.
- Theme: no emote/`emoji_box`, noto-sans-basic-14-equivalent font,
  left-aligned, full-panel wrap (`CONFIG_USE_MULTILINE_CHAT_MESSAGE`
  semantics), ASCII-sanitize before render (same glyph constraint as the
  bridge's `display_text_ascii()`), status bar with WiFi/agent state.
- **Stale-caption rule preserved**: never show the previous reply during the
  current turn (bridge solved it with the `_caption_queue` split; here the
  single local render owner makes it trivial — E2E assert stays).
- **RD_SHOT**: hook the LVGL flush callback, dump the 15000 B buffer as
  `RD_SHOT_BEGIN bufsize=15000 w=400 h=300` + hex + `RD_SHOT_END`, 4 s
  throttle. `capture.py` then works unmodified.

### 4. Voice loop component — `components/robodog_voice/` (in `overlay/`)

The piece ESP-Claw does **not** ship (it is chat/IM-first; `lua_module_audio`
is script-level PCM I/O, not a voice pipeline). All C, on-device:

1. **Trigger**: BOOT button or `RD_LISTEN` console line → start listening
   (same surface as `robodog_listen.patch`, now native).
2. **Capture**: I2S RX from ES8311, 16 kHz/16-bit/mono. End-of-utterance via
   ESP-SR VADNet (component manager) with 800 ms trailing silence to match
   current turn semantics; energy-VAD fallback. Half-duplex (mic muted during
   TTS playback) for v1 — AEC/barge-in is a later option (`esp-sr` AFE, as
   `CONFIG_USE_DEVICE_AEC` does today).
3. **ASR**: multipart POST to the user's own Whisper endpoint
   (OpenAI-compatible `/v1/audio/transcriptions` assumed — Phase 0 confirms
   the exact request/response shape and model name). Log `ASR text=` to
   console (E2E hook), render as user message, inject into the agent.
4. **LLM**: native `claw_core` — OpenAI-style custom endpoint,
   NVS-configured (`base_url`, key, model). Default: the existing
   `gx10-f156:8000/v1` DeepSeek-V4-Flash the bridge already falls back to.
   No Hermes, no brain WS, no adapter. ESP-Claw structured memory/skills are
   available but out of scope for parity.
5. **TTS**: POST `/v1/audio/speech` to the user's own Kokoro endpoint
   (today: `spark-5307:8004`, voice `af_heart` — the bridge already talks to
   it with this exact OpenAI-shaped API) → stream 24 kHz WAV → PCM → I2S TX.
   Blocking I2S write gives real-time pacing for free — the Opus
   `DOWNLINK_PACE` / 40-frame decode queue problem (#267/#272) disappears
   with the protocol.

### 5. E2E — `robodog-claw-e2e` gate

Adapt `robodog_client/voice/hermes_voice_e2e.py` phases, assertions
retargeted from the bridge journal to the **device serial console** (plus
Whisper/Kokoro endpoint logs):

- OPEN_SESSION: RD_LISTEN + console listen log
- ASR_MATCH: corpus WAV via test speaker → console `ASR text=` fuzzy match
  (≥75 % token overlap)
- REPLY: console agent-reply log
- RING: test-mic capture → re-ASR of the board's spoken reply
- SUBTITLE: RD_SHOT → `capture.decode_hermes` → Qwen2.5-VL readback must
  transcribe the reply

New `.archon` workflow `robodog-claw-dev` (clone of the hermes gates, minus
bridge health checks); `robodog-hermes-*` workflows stay until cutover.

### 6. Cutover & cleanup (after claw E2E passes)

Clean cutover per repo contract: delete `voice_bridge.py`,
`hermes-platform/xiaozhi/`, `lmao-robodog-voice.service`, the three XiaoZhi
patches + `build_xiaozhi.sh` / `fetch_xiaozhi.sh`, hermes E2E/workflow docs.
Kokoro (`kokoro_server.py` / `kokoro-tts.service`) stays — it **is** the
user's TTS endpoint; the Whisper endpoint is the user's own, external to this
repo. Update `robodog_client/README.md`, `docs/archon-workflows.md`,
AGENTS.md workflow table.

## Phasing

| Phase | Deliverable | Gate |
|---|---|---|
| 0 — Spike | ESP-Claw builds for the board; ST7305 shows LVGL text; chat-injection API for ASR text identified; device→Whisper and device→Kokoro HTTP round-trips proven | screen capture shows text |
| 1 — Board + display | Board package, `esp_lcd_st7305`, text-only UI parity, RD_SHOT | `capture.py --describe` readback |
| 2 — Voice loop | RD_LISTEN → ASR → agent → TTS → speaker, all device-initiated | `robodog-claw-e2e` full chain |
| 3 — Cutover | Bridge/Hermes/patches deleted, docs + workflows updated | green CI + production-health |

## Risks / open questions

- **Chat injection seam** (Phase 0 must resolve): the exact internal API to
  submit a user turn to `claw_core` from C. The web console's JSON API proves
  one exists; if it is HTTP-only we call loopback, but a native entry point
  is preferred.
- **ESP-Claw maturity**: young, fast-moving API → pin the fetch commit,
  expect upgrade churn.
- **Mono LVGL in `display_service`**: if its session management assumes
  color/emote, fall back to a minimal LMAO-owned renderer on the raw
  `esp_lcd` handle — the presentation is simple enough that this is a
  contained Plan B, not a redesign.
- **Endpoint API shapes**: both are OpenAI-shaped in the current stack
  (`/v1/audio/transcriptions` for Whisper, `/v1/audio/speech` for Kokoro) and
  the bridge already speaks to Kokoro that way — Phase 0 only needs to
  confirm the user's standalone Whisper endpoint matches and pin the exact
  URLs/model names (TBD) into NVS defaults.
- **TLS/heap**: HTTPS to a cloud LLM from the S3 is supported but heap-tight
  alongside Lua+LVGL; LAN HTTP endpoints (vLLM/Whisper/Kokoro) avoid it —
  another reason to default to the local endpoints.
- **ES8311 pinout** is inferred from the upstream XiaoZhi board definition
  [INFERENCE] — confirmed in Phase 0 from the fetched source.

## Scope decision

"Board calls all model endpoints on its own" = the **full voice loop stays**
(mic → ASR endpoint → LLM endpoint → TTS endpoint → speaker), with ASR/TTS as
LAN HTTP services. ESP-Claw has no onboard ASR/TTS. A text-only variant
(web-console/IM chat on the RLCD, no mic) would shrink Phase 2 to nothing —
rejected for now; the RD_LISTEN/corpus/ring E2E exists to prove voice.
