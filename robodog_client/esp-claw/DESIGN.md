# RoboDog ESP-Claw — Standalone On-Device Agent (Design)

**Status**: Phase 0 (spike) + **Phase 1 (board + display)** + **Phase 2 (voice loop)** complete — see Phase 0/1/2 results below. Phase 3 (cutover) pending.
**Goal**: run [ESP-Claw](https://github.com/espressif/esp-claw) (`edge_agent`)
standalone on the Waveshare ESP32-S3 RLCD-4.2. The board calls **all** model
endpoints (ASR, LLM, TTS) on its own over LAN WiFi. No `voice_bridge.py`, no
Hermes gateway, no XiaoZhi protocol. The RLCD presentation layer stays
exactly as PR #278 defined it: text-only, no face/emote, ~16 px font,
left-aligned, multiline wrap filling the 400×300 panel, status line on top
(the font is pure black on the white reflective panel for max contrast).

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
  LAN, URL build-  or cloud key,          spark-5307:8004, af_heart;
  time Kconfig)    NVS-cfg)               URL build-time Kconfig)
```

Survives unchanged: the acoustic test rig + corpus, `capture.py` (RD_SHOT
format), the calendar firmware, and the user's own LAN model services —
**Kokoro TTS** (`kokoro_server.py` / `kokoro-tts.service`, `spark-5307:8004`,
`af_heart`) and a **Whisper ASR** endpoint — which the board calls directly.
Endpoint configuration (not hardcoded): the **LLM** base URL/model are
NVS-provisioned (`flash_esp_claw.sh` writes the `app` namespace — WiFi +
`llm_base_url` / `llm_model`); the **ASR/TTS** endpoint URLs/model/voice are
build-time Kconfig, baked per-flash from `CLAW_ASR_URL` / `CLAW_TTS_URL` /
`CLAW_*_MODEL` / `CLAW_TTS_VOICE` env in `build_esp_claw.sh` (so LAN IPs are
never committed; changing them requires a rebuild, not a runtime edit).
Defaults documented in `Kconfig` match the user's current LAN deployment
(names TBD elsewhere).

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
- Theme: no emote/`emoji_box`, noto-sans-basic-16-equivalent font (Montserrat-16,
  pure black — the LVGL theme default is a mid-grey that binarizes to thin
  greys on the 1-bpp panel), left-aligned, full-panel wrap
  (`CONFIG_USE_MULTILINE_CHAT_MESSAGE` semantics), ASCII-sanitize before render
  (same glyph constraint as the bridge's `display_text_ascii()`), status bar
  with WiFi/agent state.
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

**Complementary voice-verification gate** (`robodog-claw-voice-verify`,
`robodog_claw_e2e.py --voice-verify`): acoustic completeness of the CHUNKED
reply — asserts the FULL reply was queued AND read out to its end:
- CHUNKED_QUEUE: every console `voice chunk i/n` has its own `TTS start`…
  `TTS stop` bracket (deterministic console evidence; the queue ran 1..n)
- GAP_VERIFY: the feature's actual effect — each inter-chunk gap
  `TTS stop(i) → TTS start(i+1)` measured from the console's ms timestamps
  must be < 250 ms.  Pre-fix that gap was the ~1-3 s blocking Kokoro fetch on
  the critical path; prefetch collapses it to a few ms (next chunk buffered).
  Emits the measured per-gap ms values into the gate artifact.
- FULL_READOUT: whole-reply test-mic capture re-ASRs ≥2 of the reply's LAST
  12 tokens — nothing was cut off at the end (PASS-WEAK tolerated for marginal
  speaker→mic coupling; CHUNKED_QUEUE is the authoritative leg)

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
| 1 — Board + display | Board package, `esp_lcd_st7305`, text-only UI parity, RD_SHOT | `capture.py --describe` readback — **DONE, hardware-verified** (Phase 1) |
| 2 — Voice loop | RD_LISTEN → ASR → agent → TTS → speaker, all device-initiated | `robodog-claw-e2e` full chain — **DONE, hardware-verified** (Phase 2) |
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

## Phase 0 — spike results (implemented)

Status of the Phase 0 gate: **all four deliverables proven on hardware.**

LMAO-owned tooling in this folder (all in `robodog_client/esp-claw/`):

- `fetch_esp_claw.sh` — sha256-pinned espressif/esp-claw tarball
  (`9b23c15bc28703369b4b7fad3e810f8cb535fb59`) → gitignored `../.esp-claw/`;
  stamp-guarded (re-fetch skipped when the pinned commit is already staged).
- `rewrite_component_paths.py` — rewrites root local-source `path:` deps to
  absolute container paths (see build fix below).
- `patches/0001-rdshot.patch` — `setup_device.c` (the Waveshare board's
  custom ST7305 `esp_lcd_panel`): publishes the 15000 B monochrome
  framebuffer from `rlcd_flush_frame()` via `robodog_rdshot_publish()` +
  layout-identical local `robodog_display_lcd_{config,handles}_t` types.
- `patches/0002-init.patch` — `main.c` calls `robodog_init()`.
- `overlay/components/robodog_phase0/` — LMAO component: RD_SHOT publisher
  (self-ticking, 4 s cadence — see capture seam below), console commands
  `rdshot` / `rd_asr` / `rd_tts` / `rd_chat`, embedded corpus WAV for ASR.
- `build_esp_claw.sh` — Docker `espressif/idf:v5.5.4`, generates
  `sdkconfig.defaults.robodog` (model endpoints via `CLAW_*` env),
  `idf.py bmgr -c ./boards -b waveshare/waveshare_ESP32_S3_RLCD_4_2 && idf.py build`.
- `flash_esp_claw.sh` — host esptool via the sanctioned `flash.sh` pattern:
  writes bootloader/app/partition/system/storage **and provisions NVS** at
  `0xA000` (WiFi `Hankerker`, LLM openai_compatible, `cfg_ver=1`).
- Bazel: `//robodog_client:build_robodog_claw` + `:flash_robodog_claw`.

Hardware-verified results:

| Deliverable | Result |
|---|---|
| ESP-Claw builds for the board | `edge_agent.bin` 3.0 MB builds green with all patches; board flashes + boots, **all 24 capabilities registered, root agent ready**, ES8311+ES7210 audio init |
| Screen capture shows text | RD_SHOT pixel frame → `capture.py --describe` readback: `WI-FI ON \| AP esp-claw-DD6DF3 / 16:46 / Sat, 24 Oct` — the esp-claw LVGL home screen renders on the ST7305 |
| Chat-injection API identified | native seam = `claw_agent_mgr_post_session_message` (async) / `claw_agent_mgr_start_root_run_text` + `claw_agent_mgr_receive_root_for` (per-turn); exercised via `rd_chat` |
| Device→Whisper round-trip | `rd_asr POST http://192.168.50.153:11801/v1/audio/transcriptions → 200, text="What can you do?"` |
| Device→Kokoro round-trip | `rd_tts POST http://192.168.50.153:11804/v1/audio/speech → 200, 165644 B WAV (RIFF: yes)` |
| Device→LLM (agent seam) | `rd_chat "7×8?" → reply: 56` |

Upstream/build fixes that were required (documented for Phase 1 reuse):

- **IDF pin = `espressif/idf:v5.5.4`, not 5.5.1.** ESP-IDF 5.5.1's
  component-manager kconfig pass misses required keys from skipped optional
  registry components (`espressif/knob` → `ESP_BOARD_DEV_KNOB_SUPPORT`,
  `espressif/usb_host_uac` → `CODEC_UAC_SUPPORT`) → fatal "Missing required
  kconfig option after retry"; 5.5.4 resolves the rest, and the two symbols
  are defined `n` in the overlay Kconfig.
- **Absolute component paths.** CM 2.x records root local-source requirements
  by the raw `path:` string, so sibling `../x` deps hash differently than
  self-root absolute paths → "Already defined root local requirement". The
  manifest rewrite to `/workspace/...` absolute paths (from the container
  root) unifies them.
- **Local struct types for the LCD config.** `CONFIG_ESP_BOARD_DEV_DISPLAY_LCD_SUPPORT`
  is silently dropped from sdkconfig (board-manager Kconfig unknown at
  defconfig-merge time), so `dev_display_lcd.h` is unavailable; layout-
  identical local types (config only feeds `esp_board_device_override_config`,
  init ignores it) replace it. The `setup_device.c` FRAME-BUFFER writes go to
  a buffer in **SPIRAM**, keeping the 15000 B out of internal RAM.
- **NVS schema key is mandatory.** `app_config_init()` requires
  `cfg_ver` = the app's `APP_CONFIG_SCHEMA_VERSION` ("1"); if missing, it
  **erases the whole `app` namespace on first boot**, wiping provisioned keys.
  `flash_esp_claw.sh` writes `cfg_ver=1` first so the schema is accepted.

Model configuration (NVS, namespace `app`):

- `wifi_ssid` / `wifi_password` → Hankerker on the 192.168.50.0/24 LAN.
- `llm_backend=openai_compatible`, `llm_endpoint=static`,
  `llm_base_url=http://gx10-f156:8000/v1`, `llm_model=deepseek-ai/DeepSeek-V4-Flash-0731`,
  `llm_auth_type=none`, `llm_sup_tools=true` (false → the agent refuses with
  "Selected backend does not support tool calls" even though the backend is
  OpenAI-compatible), `llm_sup_vis=false`.
- Whisper/Kokoro are on the **livecam 192.168.10.0/24 subnet**, which the
  board (192.168.50.0/24) cannot route to. The workspace host forwards them:
  `socat TCP-LISTEN:11801,TCP:192.168.10.37:8001` and `:11804 → :8004`
  (Kokoro). Build with `CLAW_ASR_URL=http://192.168.50.153:11801/v1/audio/transcriptions`
  `CLAW_TTS_URL=http://192.168.50.153:11804/v1/audio/speech` and the overlay
  POSTs through the forwards. `dev_display_lcd`-independent of the board.

### RD_SHOT capture seam — finding for Phase 1 (root cause + implemented seam)

Phase-0 observed the board-flush RD_SHOT hook was flaky ("sometimes zero
frames for a whole boot"). Phase 1 **pinned the root cause**: esp-claw's own
display_service / system_ui are gated behind `CONFIG_ESP_BOARD_DEV_DISPLAY_LCD_SUPPORT`,
which the esp_board_manager Kconfig **silently drops for this custom-typed
panel** (the board declares `display_lcd` as `type: custom`, so only
`ESP_BOARD_DEV_CUSTOM_SUPPORT` is set; `display_service_load_board_display()`
therefore compiled out / returns `ESP_ERR_NOT_SUPPORTED`). On a normal boot
**nothing initialises the RLCD at all** — no LVGL, no flush, no frame. (That is
why the boot appeared to "park the panel in dummy mode"; it was simply never
brought up.)

Phase 1 therefore does **not** lean on esp-claw's display stack for the UI or
the capture. The LMAO overlay brings the panel + LVGL up itself and the RD_SHOT
frame is sourced deterministically:

- **UI owns the display**: `robodog_ui` starts the custom RLCD panel through
  the board manager (`robodog_board_display_get`, in the LMAO-owned
  `setup_device.c`), registers an LVGL display via `esp_lv_adapter` directly
  (no display_service / system_ui), and loads its text-only screen. This is
  deterministic — it comes up on every boot.
- **Frame source**: `robodog_phase0`'s rdshot publisher self-ticks every 4 s
  and force-publishes on the `rdshot` command. Normally it publishes the
  retained frame from the last panel flush; as a **deterministic fallback it
  reads the panel's live 1-bpp `frame_buf` directly**
  (`robodog_board_display_frame`, overlay `setup_device.c`) — which is always
  valid (blank white on a fresh boot, current content after any render), so a
  capture at any point always has a 15000 B frame. `capture.py decode_hermes`
  (400×300 ST7305 landscape) works unmodified.
- **Capture is still the exact panel LUT**: no RGB565→1bpp re-binarization —
  the panel's own `rlcd_panel_draw_bitmap` is the single source, so the
  published frame is bit-for-bit the ST7305 layout (zero duplication).
- The UI forces a synchronous LVGL refresh + immediate RD_SHOT dump after each
  text change, so the gate capture (`rd_chat` → reply) reads the fresh frame.

No `capture.py` changes were needed.

### Chat-injection seam (Phase 0 resolution)

Native, no loopback HTTP needed (the seam the web console uses is
HTTP-to-localhost; the C entry points exist and are the right seam for the
voice component):

- per-turn: `claw_agent_mgr_start_root_run_text(agent_id, text, flags)` (+
  `claw_agent_mgr_receive_root_for(...)` to await the reply);
- queued: `claw_agent_mgr_post_session_message(...)`.

Phase 1's voice component should use the per-turn variant and render
`receive_root_for`'s reply — identical roles/timing to `SetChatMessage("user"|"assistant")` today. ESP-Claw's own LLM-RUN path does the HTTP round-trip to
an OpenAI-style endpoint natively (proven: latest frame carries the clock,
tox-Free fixtures confirm the endpoint shapes).

### Phase 1 notes / deltas from the design above

- `rd_asr`/`rd_tts` prove the endpoint shapes: Whisper takes a multipart
  `file`+`model=whisper-1` POST to `/v1/audio/transcriptions`; Kokoro takes
  JSON `{model, voice, input}` to `/v1/audio/speech` and streams WAV.
- ES8311 + ES7210 audio devices initialize on boot (board port, no
  extra work) — good groundwork for Phase 2's native voice loop.
- The board boots the RoboDog **text-only chat UI** (Phase 1) directly —
  see the Phase 1 results below; capture.py's hermes decode path reads the
  RD_SHOT frame as-is.

## Phase 1 — results (implemented)

Phase 1 delivers the **core text-only presentation layer + deterministic
RD_SHOT**, hardware-verified on the board.

### Changes

- **Board file moved to the overlay** (no fragile patch). The whole
  `setup_device.c` (ST7305 custom panel + the RD_SHOT capture hook + the new
  `robodog_board_display_get` / `robodog_board_display_frame` helpers) is now
  an LMAO-owned copy at
  `boards/waveshare_ESP32_S3_RLCD_4_2/setup_device.c` in the overlay,
  wholesale-copied over the upstream waveshare copy at build time.
  `patches/0001-rdshot.patch` is deleted; only `0002-init.patch` (wires
  `robodog_init()`) remains.
- **Deterministic text-only UI** (`robodog_ui.c`): no face/emote, ~16 px
  Montserrat-16 ASCII font (ships with LVGL — no vendoring; noto-sans-basic
  equivalent), **pure-black text on an opaque white label background**. The
  LVGL-theme labels otherwise render against a black screen with a mid-grey
  text that binarizes thin on the 1-bpp panel, so the status + body labels
  force `bg=white COVER` + `text=black` for crisp black-on-white. Left-aligned
  full-panel wrap, status bar on top
  (`WIFI ON/OFF | <agent state>`). Registers its own LVGL display via
  `esp_lv_adapter` (not esp-claw's gated display_service), so it comes up on
  **every** boot.
- **Agent rendering (stale-caption-safe)**: `rd_chat` (the per-turn
  chat-injection seam Phase 2's voice component will also use) renders the
  user transcript (`Style "> <text>"`) before the run and the assistant reply
  when it finalizes; the status bar shows `thinking` mid-run and `idle` after.
  A single local render owner means the transcript only ever accumulates what
  actually completed — no stale "previous reply" replay.
- **RD_SHOT now deterministic**: the rdshot publisher self-ticks plus reads
  the panel's live `frame_buf` as a fallback, so a capture has a frame at any
  time (not only after a flush / text change). The exact panel 1-bpp layout is
  preserved (no LUT duplication), so `capture.py --describe` works unmodified.

### Gate (hardware-verified)

`rd_chat 6*7` → the screen renders `> 6*7` then the reply `42`; the captured
RD_SHOT frame's vision readback transcribes it (`capture.py --describe`):

```
- WIFI ON | ...
- 42
```

Deterministic: the answer is present in every captured frame (the earlier
`rd_chat 2+2` → `4` also read back), and a fresh-boot capture with **no** text
change still returns a frame (the direct-frame fallback). A single local
render owner (text appended only when a turn's reply actually finalizes)
satisfies the stale-caption rule by construction.

Phase-0 probes re-passed with no regression: `rd_asr`
(device→Whisper 200, `text="What can you do?"`), `rd_tts`
(device→Kokoro 200, 165644 B RIFF WAV), `rd_chat` (reply via the native seam,
rendered to the UI).

**Stable capture invocation**: because the rdshot publisher self-ticks and can
read the panel frame directly, the no-reset live-listen path always returns a
frame; the reset path works too (a frame is seeded on every boot). Either works

```sh
python3 robodog_client/capture.py --port /dev/ttyACM0 --n 1 --timeout 30 --describe
python3 robodog_client/capture.py --port /dev/ttyACM0 --reset --n 1 --timeout 45 --describe
```

### Deterministic display bring-up (root cause fix)

esp-claw never inits the RLCD because `APP_CLAW_SYSTEM_UI_ENABLE` depends on
`ESP_BOARD_DEV_DISPLAY_LCD_SUPPORT`, which the board-manager Kconfig drops for
this custom-typed panel. The Phase-1 overlay bypasses that entirely:
`robodog_ui` calls `robodog_board_display_get()` → board manager lazy-inits
the custom `display_lcd` device → panel ready → LVGL adapter drives it. This is
the same panel port Phase 0 proved on a "good" boot, now brought up on **every**
boot deterministically.

## Phase 2 — results (implemented)

Phase 2 delivers the **device-initiated voice loop**, hardware-verified end-to-end
(RD_LISTEN → mic → Whisper ASR → agent LLM → Kokoro TTS → speaker → RLCD render).

### Seam changes vs. the design above

- **Audio access via the shared `audio_hub`, not raw I2S.** The board's ES8311
  (speaker) + ES7210 (mic) codecs are brought up by `esp_board_manager` and
  exposed through the managed `audio_hub` component (started by
  `app_claw_audio_start_service`) as `dev_audio_codec_handles_t`. Opening the
  codec a second time would conflict with audio_hub's ownership, so the voice
  loop subscribes instead:
    - mic (`robodog_voice.c voice_capture`): `audio_capture_open_subscriber(
      ..., AUDIO_CAPTURE_SUB_APP, {16000,1,16}, ...)` then
      `audio_capture_sub_read`.
    - speaker (`voice_speak`): `audio_mixer_open_track(..., AUDIO_MIXER_TRACK_APP,
      ...)` + `audio_mixer_track_write` / `_track_flush`. The natural APP
      subscriber/track are free at boot (only the Lua `audio` module claims them,
      and only when a skill runs). Half-duplex by construction: the mic
      subscriber is open only for the listen phase and closed before TTS.
- **Energy VAD, not ESP-SR.** `espressif/esp-sr` (VADNet) is not a dependency in
  the tree and is not offline-fetchable; DESIGN's fallback is used, matching the
  bridge exactly: `VAD_ENERGY=300` (16-bit mean-abs), `VAD_SILENCE_MS=800`,
  `voice_capture()` ends the utterance 800 ms after the last speech frame.
- **Capture-buffer hardening (stress round R7).** Under sustained voice-loop
  load the voice-loop teardown panicked the board on
  `assert failed: tlsf_free tlsf.c:630 ("block already marked as free")` →
  `Rebooting… rst:0xc`.  The 9-turn claw stress soak reproduced it repeatedly
  (3 of 9 turns failed with no `ASR text=`; the same crash was forced
  standalone).  Backtraces fixed the fault at the capture teardown free, but
  the assert tripped on DIFFERENT blocks run-to-run (`heap_caps_free(pcm)` on
  the old all-at-once 1.4 MB buffer; the audio_hub subscriber's 5 KB ring free
  after the lazy-alloc change) — the signature of shared-PSRAM TLSF metadata
  corruption whose *writer* lives outside the LMAO-owned paths (all overlay
  buffers/frees verified in range; comprehensive heap poisoning changes the
  assert, not the failure).  The in-repo hardening removes every per-turn
  capture teardown free so the confirmed panic site cannot execute:
  1. **F1 — the capture buffer is allocated lazily on the FIRST speech frame**
     (grow-on-speech): a no-speech bail returns `NULL` with zero samples,
     having allocated and freed nothing.  Pre-speech silence is dropped; from
     the first speech frame everything (trailing silence included) is buffered
     as before.
  2. **F2 — continuous-speech cap**: `CONFIG_ROBODOG_VOICE_SPEECH_MAX_SECS`
     (default 30) bounds the "speech" phase so a saturated/echoing mic that
     never goes 800 ms quiet cannot burn the whole `VOICE_MAX_SECS` window and
     transcribe half a minute of noise.
  3. **F3 — persistent capture subscriber.** `voice_capture` opens the APP
     capture subscriber ONCE and keeps the ring + rate-conversion buffers
     across turns (no per-turn open/close), flushing the ring at each listen
     start.  The per-listen teardown free (the ring-delete panic site) is gone,
     and a fresh flush also discards post-TTS echo that previously kept the
     energy-VAD "busy" (the stuck-listen).  Trade-off: the APP capture role is
     claimed by the voice loop for the board's lifetime (Lua `audio` capture is
     not available) — correct for this standalone-agent board.
  The remaining open item is the original external PSRAM corruptor; next step
  if it ever resurfaces is a heap-trace/poison build run to name the writer
  (likely the esp_ae 24k→16k converter or an LWIP/LVGL buffer) or a PSRAM
  speed check — tracked pending longer soak.

- **The reply is CHUNKED at sentence breaks and queued, not truncated.** The
  esp-claw root agent is verbose by default (a full "what can you do" dump is
  60+ s of speech), so `voice_turn()` splits the FULL reply via
  `voice_split_reply()` into sentence-bounded chunks of at most
  `CONFIG_ROBODOG_VOICE_TTS_MAX_CHARS` (default 240, `range 1 479` so a
  spoken chunk always fits the RLCD command field — the on-screen sentence is
  the same text the voice reads, R5) — cut at the first
  `.!?;`/newline break within the window, else the last space, else a hard
  cutoff — and each chunk is queued in order: appended to the on-screen reply
  (`robodog_ui_append_assistant`, so the whole reply accumulates on the RLCD)
  then spoken (`voice_speak` per chunk; `CONFIG_ROBODOG_VOICE_TTS_MAX_CHUNKS`
  bounds the queue).  The read-out voice therefore finishes EVERY word of the
  reply — the old "response always truncated to the first ~240 chars" failure
  is gone.  Every chunk logs `voice chunk i/n: ...` on the console so the host
  voice-verification gate can count that the full queue was voiced.
  The one reachable exception is honest: when a reply exceeds
  `CONFIG_ROBODOG_VOICE_TTS_MAX_CHUNKS`, the tail is dropped but never
  silently — the device logs `voice truncated: ...` and the turn ends in a
  distinct **`truncated`** terminal state (not the false-complete `idle`), and
  the `robodog-claw-voice-verify` gate FAILs on that marker, so a truncated
  readout can never pass as a full one.  The splitter itself lives in the
  pure, host-tested module `voice_split.c` (R6) — lossless / ordered / ≤cap
  invariants are proven under Bazel against the exact source the firmware
  compiles.
- **Markdown is handled at BOTH ends of the turn — no "asterisk asterisk".**
  The agent replies in multi-line markdown (`**` headers, bullets, inline-code
  spans, tables, links) that Kokoro would voice literally and the text-only
  panel would show as glyph noise.  Two complementary layers:
  - **Source (per-turn directive, not a global base-prompt rewrite):** the
    voice turn prefixes the agent input with a terse "reply in plain spoken
    prose, no markdown" instruction (applied after the transcript render, so
    it never appears on the RLCD).  This is scoped to the voice seam rather
    than the upstream-owned IM-oriented base prompt — a global override via
    `claw_agent_mgr_update_core_config` would force re-embedding the whole
    upstream `APP_SYSTEM_PROMPT` string into the overlay (the fragile
    hardcode we avoid) and would change other surfaces.
  - **Deterministic backstop (output normalizer):** `robodog_voice_md_strip()`
    (pure C, host-tested `voice_md.c`, R8) runs over the reply BEFORE the
    TTS queue.  The source directive is probabilistic — the model can still
    emit markers (and tool/web results pasted into the reply carry their own),
    so the strip is the guarantee that no `*`/`#` reaches the speaker.  It
    keeps every word and natural punctuation (incl. ordered-list `1.` and a
    leading `-5`), drops emphasis/code/strike markers, link URLs, table
    pipes, and whole-line noise (HR / table separators / fence delimiters),
    and collapses whitespace — so the chunks that feed BOTH the speaker and
    the RLCD are clean prose.  The raw reply is still logged in
    `agent reply: ...` for the host gates.
- **24 kHz native audio hub (hermes parity) — no software resample.** The
  shared `audio_hub` (patched via `patches/0003-audio-hub-24k.patch`) runs the
  ES8311/ES7210 at **24 kHz**, exactly like the hermes stack: Kokoro's native
  24 kHz WAV is written to the APP mixer track **untouched and zero-copy**
  (`voice_speak()` detects the mixer rate via `audio_mixer_track_info` and
  passes the WAV straight through when the rates match).  The mic path stays
  16 kHz for VAD/ASR via the capture hub's per-subscriber `esp_ae_rate_cvt`.
  A WAV at any other rate is interpolated to the mixer rate; the old hardcoded
  "24k assumed everywhere" path silently down-converted a 16 kHz WAV to ~10.7k
  and played it slow with very low pitch (the "read out too slow / pitch very
  low" report).
- **TTS playback is manually paced — the blocking-write assumption is wrong.**
  `audio_mixer_track_write` does NOT block for real-time playback: it only
  waits `AUDIO_MIXER_WRITE_TIMEOUT_MS` (500 ms) for its ~8-frame (5 KB) ring to
  drain, then returns short. Writing a whole long reply at once therefore
  saturated the ring and timed out after ~0.5 s — the speaker got only a
  fraction of the audio and the reply was inaudible (TTS `start→stop` ≈ 0.7 s
  for 16.5 s of audio). `voice_speak()` now feeds the track in ~20 ms frames
  with a ~19 ms `vTaskDelay` step so playback rides the 16 kHz codec rate
  (verified on-device: 14.5 s of audio plays over 14.5 s). Do NOT "simplify"
  this back to a single `track_write`.
- **Inter-chunk TTS fetch latency is hidden by a ONE-STEP LOOKAHEAD
  PREFETCH.** The chunk queue used to block on the Kokoro HTTP round-trip
  (~1-3 s) BEFORE each sentence's paced playback, so every inter-sentence
  boundary had dead air. `robodog_voice_speak` is now split into a fetch
  phase and a play phase, and while chunk i plays a small background prefetch
  task fetches chunk i+1's WAV into the free slot of a TWO-SLOT double-buffer
  (exactly one fetch in flight; at most two
  `CONFIG_ROBODOG_VOICE_TTS_MAX_RESPONSE` slots ≈ 3 MB SPIRAM, allocated once
  per turn and reset in place via `robodog_http_rx_reset` — no per-chunk
  alloc/free, so the R7 free-churn panic site cannot re-run; no deeper
  pipeline). The play/fetch split keeps the console contract byte-identical
  (`voice chunk i/n` + per-chunk `TTS start/stop` in order), the R5 render
  fires at each chunk's playback START (never at prefetch-commit — the
  prefetch task never touches the UI), fail-stop error semantics and the
  truncated/error terminals are unchanged, and the slot-exchange bookkeeping
  is pure C + host-tested (`voice_lookahead.c`, R9). On a lookahead-fetch
  failure the chunk falls back to the synchronous fetch-then-play path — it
  never pre-fails, skips or reorders; if the prefetch machinery is
  unavailable the whole turn degrades to the sequential single-buffer path.

  ⚠️ **Slot bookkeeping is TURN-LOCAL — commit on observe.** The prefetch task
  reports only `s_pf_result`; the `slot_ix` table that `prefetched()` scans
  lives in `robodog_voice_speak`'s stack `la` and the task cannot reach it.
  The array loop MUST call `robodog_voice_lookahead_commit(&la)`/`fail(&la)`
  right after the DONE wait — the missing commit silently sent EVERY chunk
  through the redundant synchronous re-fetch (the exact ~1-3 s gap the feature
  removes) while the queue still played 1..n correct. The host
  `GAP_VERIFY` leg is the regression guard: it asserts every
  `TTS stop(i) → TTS start(i+1)` gap is < 250 ms and reports which path the
  console markers show (`prefetch chunk i/1 ready` = gapless swap,
  `prefetch ... failed` = sync fallback).
- **Multi-session gapless-prefetch stress.** The mandatory gate runs ≥ 5
  consecutive voice sessions (`E2E_RUNS ≥ 5`), each asking a DIFFERENT
  question (corpus slugs time/joke/greeting/capabilities/count cycled so no
  two consecutive sessions match), every session must reach `TTS stop` with a
  complete readout (CHUNKED_QUEUE + FULL_READOUT PASS, no `voice truncated`),
  and the whole run must show NO board reset/panic (watchdog scans for
  `rst:*` / tlsf / heap-corruption — the R7 soak regression the prefetch's
  new sustained-load path must not re-trigger; `robodog_claw_stress.py`
  drives it, additive, and never writes `hardware-e2e.md`/`.gate-head`).
- **Speaker output needs explicit re-assert before playback.** Even paced,
  this board's ES8311 was silent until `voice_speak` itself calls
  `esp_codec_dev_set_out_mute(dac, false)` + `esp_codec_dev_set_out_vol(dac, 80)`
  + raises the PA (GPIO46, `voice_enable_pa()`; the board-manager peripheral
  init leaves the amp pin at the struct-default LOW). With those three the
  reply is audibly reproduced — verified by capturing the board speaker on the
  test rig's mic and re-ASR'ing it back to real words. `rd_audio_diag` dumps
  the live ES8311 DAC registers (power/DAC-enable/mute/volume) and plays a
  1 kHz tone for quick output bring-up.
- **HTTP shared helper extracted** to `robodog_http.c/h` (was static in
  `robodog_overlay.c`'s probes) so `voice_asr` / `voice_speak` reuse the exact
  body-buffering path. `robodog_http_rx_t` grows in SPIRAM as the body lands.
- **Keep the overlay in the `robodog_phase0` component** (now also holding the
  Phase 1 UI + Phase 2 voice) rather than a separate `robodog_voice` component —
  a second claw-path manifest would re-trigger the component-manager relative-
  path hash collision the build already works around, for no isolation benefit.
  The voice loop is a module (`robodog_voice.c`) in the same LMAO-owned component.
- **Presentation-layer UX** (this round): the RLCD shows a **live state indicator
  top-right** (`idle` / `listening` / `processing` / `responding` / `error`) driven
  by the voice loop next to `WIFI ON` on the left; a **BOOT button (GPIO0) mirrors
  `rd_listen`** (debounced falling-edge ISR) so the user can push-to-talk without
  the console; and the transcript is **bounded to the current exchange**
  (cap 1600 chars, ~drop-oldest for older user lines) so the current exchange
  fills the visible panel instead of a stale older turn sitting below the fold.
- **The reply shown syncs with what the voice is reading.** Each sentence
  chunk is rendered (`robodog_ui_append_assistant`) **at the instant its TTS
  playback starts** — `voice_speak()` appends the chunk right after the
  TTS fetch, immediately before the paced speaker write, so the on-screen
  sentence is exactly the one being spoken (no queue-time lead). The panel
  SNAPS TO THE BOTTOM as each chunk lands (`s_chunked` mode, canonical
  `lv_obj_scroll_to_y(..., LV_COORD_MAX, ...)` bottom idiom) and keeps the
  NEWEST content when the transcript overflows — subtitles. Single-shot
  replies (`rd_chat`, `UI_CMD_ASSISTANT`) keep the Phase-1 head-pin: the full
  answer's beginning fills the screen. (The old universal top-pin appended
  later chunks below the visible fold, and a first attempt that scrolled by
  `lv_obj_get_scroll_bottom()` blanked the whole body on this LVGL build —
  both "read out but never displayed".)
- **The state indicator never lies about VOICE progress.** `UI_CMD_ASSISTANT`
  no longer forces the state to `idle` when the text finalizes (which
  announced "response done" while the TTS round-trip + playback hadn't even
  fully started). `voice_turn()` drives `processing -> responding -> idle`
  and `rd_chat` sets its own terminal `idle`.
- **Readability**: 16 px Montserrat, and the labels force opaque-white background +
  pure-black text so the 1-bpp reflective panel is crisp black-on-white (no grey).

### Working endpoints / fixed bugs

- Two real firmware bugs fixed during bring-up, both invisible to the Phase-0
  probes:
    1. **JSON escaping in `voice_speak`**: the agent's multi-line reply was
       embedded raw in the TTS JSON → malformed JSON → FastAPI **422**.
    2. **UAF in `voice_speak`**: `esp_http_client_set_post_field` stores a
       pointer (does not copy); freeing the request JSON before `perform()`
       sent freed-memory as the body → Kokoro **400**. Both now fixed.
- The test rig: the bidirectional USB PnP Audio device is **card 2 (`hw:2`)** on
  the workspace host; there is **no ALSA `default` alias**, so the E2E drives
  `hw:2` explicitly (`RD_ALSA_DEVICE`).

### Gate (hardware-verified)

`python3 robodog_client/esp-claw/robodog_claw_e2e.py --corpus capabilities`

```
OPEN_SESSION: PASS listen started
ASR_MATCH:    PASS overlap=1.00 asr="What can you do?"
REPLY:        PASS reply="I'm ESP-Claw, an on-device AI agent that turns your intent into actions..."
RING:         PASS-WEAK re-asr="You" (audible TTS on test mic; verbatim overlap weak)
SUBTITLE:     PASS  (vision readback transcribed the rendered reply on the RLCD)
Overall:      PASS
```

Console hooks the gate asserts on (all device-side): `listen started`, `ASR
text="..."`, `agent reply: ...`, `voice chunk i/n: ...` (one per queued
sentence chunk, before its TTS bracket), `TTS start` / `TTS stop`, `RD_SHOT`
frame.

- `SUBITITLE` reads an RD_SHOT **frame from the same console handle** (a second
  open races the self-tick flood) and **skips truncated forced frames** (the
  `rdshot` force-publish races the flood and can arrive < 15000 B; the next
  self-tick frame is always complete).
- `RING` is PASS-WEAK because the board speaker → test-mic acoustic coupling is
  marginal on the current rig (the test mic hears "You" but not the whole
  reply). It still proves audible TTS; the leg is not a firmware fault.

Phase-0 probes re-passed with no regression (the HTTP refactor): `rd_asr` →
device→Whisper 200 / `text="What can you do?"`.
