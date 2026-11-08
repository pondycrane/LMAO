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
    <--Qwen3-TTS WAV-- Hermes auto-TTS (spark-5307:8004)
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
 Whisper endpoint  OpenAI-style LLM       Qwen3-TTS endpoint
 (user's own,      endpoint (gx10 vLLM    (user's own,
  LAN, URL build-  or cloud key,          spark-5307:8004, explicit
  time Kconfig)    NVS-cfg)               language + native timbre;
                                          URL build-time Kconfig)
```

Survives unchanged: the acoustic test rig + corpus, `capture.py` (RD_SHOT
format), the calendar firmware, and the user's own LAN model services —
**Qwen3-TTS** (`spark-5307:8004`, `qwen3-tts.service`) and a **Whisper ASR**
endpoint — which the board calls directly.
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
- `gen_font_tc_16.sh` — rasterises the sha256-pinned Noto Sans CJK TC OTF
  (tag `Sans2.004`) into an LVGL 16 px/1 bpp font with `lv_font_conv@1.5.2`,
  staged into the overlay component at build time (cache
  `~/.cache/lmao-fonts`; the generated C is never committed). Requires
  Node.js ≥ 18 + network on the first build only.
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
- Theme: no emote/`emoji_box`, 16 px Noto Sans CJK TC (generated at build
  time from the sha256-pinned OTF by `gen_font_tc_16.sh`; ASCII + CJK
  punctuation + CJK Unified Ideographs, so Traditional Chinese replies render
  as real glyphs rather than `?`), pure black — the LVGL theme default is a
  mid-grey that binarizes to thin greys on the 1-bpp panel; left-aligned,
  full-panel wrap (`CONFIG_USE_MULTILINE_CHAT_MESSAGE` semantics),
  UTF-8-sanitize before render (keep every drawable codepoint, replace only
  emoji/exotic scripts with `?`, never split a multi-byte sequence), status
  bar with WiFi/agent state.
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
   `spark-73dd:8000/v1` `qwen3.8-flash-next` (192.168.50.3) — the stable,
   always-on LLM endpoint, on the board's own subnet.
   No Hermes, no brain WS, no adapter. ESP-Claw structured memory/skills are
   available but out of scope for parity.
5. **TTS**: POST `/v1/audio/speech` to the user's own Qwen3-TTS endpoint
   (today: `spark-5307:8004`, voice **empty** — the server picks a native
   speaker from the reply text's script; see "Voice selection" below) →
   stream 24 kHz WAV → PCM → I2S TX.
   Blocking I2S write gives real-time pacing for free — the Opus
   `DOWNLINK_PACE` / 40-frame decode queue problem (#267/#272) disappears
   with the protocol.

### 5. E2E — `robodog-claw-e2e` gate

Adapt `robodog_client/voice/hermes_voice_e2e.py` phases, assertions
retargeted from the bridge journal to the **device serial console** (plus
Whisper/Qwen3-TTS endpoint logs):

- OPEN_SESSION: RD_LISTEN + console listen log
- ASR_MATCH: corpus WAV via test speaker → console `ASR text=` fuzzy match
  (≥75 % token overlap; Latin words + one token per CJK ideograph)
- ASR_LANG: console `ASR language=` matches the corpus language (`zh`/`en`) —
  the endpoint auto-detected it (no `language` field is sent)
- REPLY_LANG: console `voice reply language=` shows the directive the turn
  chose (`zh-TW` for a Chinese utterance, `en` otherwise)
- REPLY: console agent-reply log (a Chinese corpus must yield a CJK reply)
- RING: test-mic capture → re-ASR of the board's spoken reply
- SUBTITLE: RD_SHOT → `capture.decode_hermes` → Qwen2.5-VL readback must
  transcribe the reply

**Complementary voice-verification gate** (`robodog-claw-voice-verify`,
`robodog_claw_e2e.py --voice-verify`): acoustic completeness of the CHUNKED
reply — asserts the FULL reply was queued AND read out to its end:
- CHUNKED_QUEUE: every console `voice chunk i/n` has its own `TTS start`…
  `TTS stop` bracket (deterministic console evidence; the queue ran 1..n)
- PIPELINE_AHEAD: every boundary AFTER the first logged
  `pipeline chunk i/n buffered=yes` — the deterministic proof that the next
  sentence was already synthesized when its turn came, so no endpoint fetch
  sits on the play path (the "long delay between response segments"
  regression).  Emits the per-boundary buffered/ahead series.
- GAP_VERIFY: the measured inter-chunk gap `TTS stop(i) → TTS start(i+1)`
  from the console's ms timestamps must be < 250 ms.  Pre-fix that gap was the
  endpoint fetch on the critical path; the pipeline collapses it to a few ms
  (next chunk already buffered).  Emits the per-gap ms values.
- FULL_READOUT: whole-reply test-mic capture re-ASRs ≥2 of the reply's LAST
  12 tokens — nothing was cut off at the end (PASS-WEAK tolerated for marginal
  speaker→mic coupling; CHUNKED_QUEUE is the authoritative leg)

New `.archon` workflow `robodog-claw-dev` (clone of the hermes gates, minus
bridge health checks); `robodog-hermes-*` workflows stay until cutover.

### 6. Cutover & cleanup (after claw E2E passes)

Clean cutover per repo contract: delete `voice_bridge.py`,
`hermes-platform/xiaozhi/`, `lmao-robodog-voice.service`, the three XiaoZhi
patches + `build_xiaozhi.sh` / `fetch_xiaozhi.sh`, hermes E2E/workflow docs.
Qwen3-TTS (`qwen3-tts.service` on spark-5307) stays — it **is** the user's
TTS endpoint; the Whisper endpoint is the user's own, external to this repo.
`kokoro_server.py` / `kokoro-tts.service` in this repo are the superseded
engine and are no longer the serving unit. Update `robodog_client/README.md`, `docs/archon-workflows.md`,
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
  (self-ticking, 10 s cadence — see capture seam below), console commands
  `rdshot` / `rd_asr` / `rd_tts` / `rd_chat`, embedded corpus WAV for ASR.
  **Console input is ASCII-only**: the IDF console's linenoise runs in dumb
  mode on USB-Serial/JTAG and its `sanitize()` drops every byte where
  `isprint((char)c)` is false — all UTF-8 lead/continuation bytes — so typed
  Chinese never reaches a command's argv.  The *display* path is UTF-8 (reply
  text from the LLM, ASR text over HTTP), so Chinese is exercised by asking in
  ASCII for a Chinese reply (`rd_chat Reply in Traditional Chinese: …`) or via
  the voice/HTTP paths.
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
  `llm_base_url=http://192.168.50.3:8000/v1`, `llm_model=qwen3.8-flash-next`,
  `llm_auth_type=none`, `llm_sup_tools=true` (false → the agent refuses with
  "Selected backend does not support tool calls" even though the backend is
  OpenAI-compatible), `llm_sup_vis=false`.
- **Current endpoint topology (no forwards).** spark-5307 now resolves to
  **192.168.50.27**, i.e. the BOARD's own subnet, so the device reaches
  Whisper (`:8001`) and Qwen3-TTS (`:8004`) **directly** — the workspace socat
  forwards on `11801`/`11804` (which used to bridge the old
  `192.168.10.37` livecam address) are gone.  The committed defaults are the
  LAN IP, not the host name, because the board's resolver does not know the
  LAN names: `CLAW_ASR_URL=http://192.168.50.27:8001/v1/audio/transcriptions`,
  `CLAW_TTS_URL=http://192.168.50.27:8004/v1/audio/speech`.
- **LLM = qwen3.8-flash-next on spark-73dd (192.168.50.3:8000/v1)** — the
  stable, always-on endpoint, on the board's subnet too.  It is the default in
  `flash_esp_claw.sh` (`llm_base_url`/`llm_model` NVS keys), replacing the old
  `gx10-f156:8000/v1` `deepseek-ai/DeepSeek-V4-Flash-0731`, which is NOT
  running (a board pointed there fails the agent run with
  `agent run failed (ESP_ERR_TIMEOUT)` and ends the turn in `error`).
  Host-side gates use the same endpoints (the e2e re-ASR default is
  `http://192.168.50.27:8001/v1/audio/transcriptions`).
- **TTS engine is Qwen3-TTS on `:8004` (2026-11-02 cutover).** Same
  OpenAI-shaped contract the firmware speaks (POST `/v1/audio/speech`
  `{model,input,voice,...}` -> 24 kHz mono s16 WAV). The firmware now sends
  `language`, `voice`, `seed` and `instruct` per request (see "Voice
  selection" below): `CLAW_TTS_MODEL` (`qwen3-tts`), per-language
  `CLAW_TTS_VOICE_EN`/`CLAW_TTS_VOICE_ZH`, `CLAW_TTS_SEED`,
  `CLAW_TTS_INSTRUCT_ZH`, and `CLAW_TTS_VOICE` only as a global pin. Warm, a
  2-4 s sentence chunk takes ~1.5-5 s (measured 1.2-1.3 s for a 1.2 s
  utterance up to ~5 s for 5 s of audio, i.e. RTF ≈ 1) — and the endpoint
  returns the WAV whole, so the pipeline keeps several chunks synthesized
  ahead instead of a one-step lookahead. Under load (a busy GPU host) a
  single chunk was measured at 14-54 s, which is exactly the case the N-slot
  pipeline absorbs. Kokoro's `af_heart` remains accepted as an alias, but it
  resolves to a **Chinese-native** timbre and gives English replies a Chinese
  accent; the explicit per-language timbre avoids it.

### Voice selection — per-language native timbres, fixed seed (2026-11-04)

**The board and the bridge name the language and the timbre explicitly.**
Qwen3-TTS timbres have their native language baked in: `aiden`/`ryan` are
English-native, `serena`/`vivian`/`uncle_fu`/`dylan`/`eric` Chinese-native
(`dylan` Beijing, `eric` Chengdu). `instruct` cannot remove that accent —
measured, Serena with `instruct: "neutral American English accent"` moved its
spectral envelope by 0.09 (13.618 -> 13.704, i.e. no change). The previous
one-voice pin (`QWEN_TTS_VOICE=serena` for every reply) made **English replies
carry a heavy Mandarin accent**, so it is gone: the server runs unpinned and
the callers send the explicit language + timbre. English -> `aiden`, Chinese
-> `dylan` (Beijing-native).

**Chinese uses one fixed Beijing-native speaker.** Taiwan has several Mandarin
varieties, so a generic "standard Mandarin" instruction still let the voice
drift between sections. `dylan` is the model's Beijing-native timbre and the
request names the Beijing register explicitly in `instruct`
(請用標準北京腔的普通話說話…不要帶台灣腔、粵語或南方口音). There is no
"accent parameter" on CustomVoice — the timbre *is* the accent.

The explicit `language` also fixes per-chunk drift: the server would otherwise
infer each sentence independently, so a short English-only chunk inside a
Chinese reply could flip to the English-native timbre mid-readout. The turn
sets the language once from the ASR result and every chunk carries it.

**A fixed seed makes synthesis deterministic.** Every request carries
`seed: 8` (firmware `CONFIG_ROBODOG_TTS_SEED`, bridge `TTS_SEED`). Qwen3-TTS
samples codec tokens, so without a seed the same text re-realized prosody on
every call. Measured: the same request now returns a **byte-identical WAV**
(md5 equal across calls), and the same speaker (`dylan`) is used for every
Chinese sentence. Note what a seed does and does not do: it pins the sampler
for a given token sequence, so identical text is identical audio; different
sentences still differ (different text = different tokens), but they are all
the same fixed speaker with the same named accent — that is what stops the
"different accent in different sections" drift.

An explicit `voice` in a request still overrides the per-language choice, and
the voice-design service (:8005) remains the escape hatch for a described
voice (it is not deployed: its briefs drifted Cantonese/Hong-Kong-flavoured).

**Replies are Mandarin, never Cantonese.** The model mirrors the input's
register: a Cantonese (or Cantonese-flavoured) utterance made it answer in
Cantonese (唔/係/嘅/咗/佢/嗰/咁/啲/冇/睇/嚟/攰/喇) about half the time, and the
TTS then voiced those characters — the board appeared to switch dialect
mid-conversation. Both reply directives now name the vocabulary explicitly
(firmware `VOICE_STYLE_ZH`, Hermes `platform_hint`), because "繁體中文" alone
does not exclude 粵語 and "reply in the user's language" actively invites it.
Verified over 6 samples with a Cantonese utterance: all Mandarin, no 粵語
particles.

`voice_turn_is_chinese()` also accepts `yue`/`cmn` tags, not just `zh`:
faster-whisper can tag Cantonese as `yue`, and the non-Chinese branch would
have answered a Cantonese utterance in English.

### Voice selection history — native English and native Chinese (2026-11-02)

Qwen3-TTS-1.7B-CustomVoice has 9 fixed timbres whose native language is baked
in: `aiden`/`ryan` are English-native; `serena`/`vivian`/`uncle_fu`/`dylan`/
`eric` are Chinese-native; `ono_anna` Japanese; `sohee` Korean. A
Chinese-native timbre reading an English reply sounds like fluent English with
a Chinese accent, and `instruct` does **not** remove it — measured against a
native American-English reference, `instruct: "neutral American English
accent"` on Serena moved its spectral envelope by 0.09 (13.618 -> 13.704,
i.e. no change). So voice choice must follow the language.

The server infers the spoken language from the **reply text's script** so
pronunciation is right (kana -> Japanese, hangul -> Korean, any >=2 CJK chars
-> Chinese, else English). The board and the bridge now send an explicit
`language` + timbre instead of relying on that inference; an explicit
`language` field wins, and naming a timbre in `voice` pins it.

The mixed-script rule is deliberately asymmetric: **any** real Chinese clause
(>= 2 CJK chars) selects Chinese even when Latin letters outnumber it, because
an English-native timbre mangles Chinese (`好的` came out `Hara`) while a
Chinese-native timbre reads an embedded English clause verbatim
(`好的,turn on the lights please` transcribed exactly). Kana wins over CJK,
hangul wins over both.

Verified on the board-shaped request (`{"model":"qwen3-tts","input":…,
"voice":""}`): English -> ASR `en` 0.99, Chinese -> ASR `zh` 0.99; the old
`"voice":"af_heart"` request produced English at only 0.70 language
confidence (the accent). The same cutover also removed Whisper's forced
`language="en"`, which had been transcribing every Chinese utterance as a
plausible English sentence; the endpoint now auto-detects and returns
`language`/`probability`, so the board's `ASR text=` log shows the detected
language in the JSON it already parses.

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
- **Frame source**: `robodog_phase0`'s rdshot publisher self-ticks every 10 s
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
  `file`+`model=whisper-1` POST to `/v1/audio/transcriptions`; Qwen3-TTS
  takes JSON `{model, input, voice, language, seed, instruct}` to
  `/v1/audio/speech` and streams WAV. The voice turn sends the explicit
  language + native timbre + fixed seed (see "Voice selection").
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
- **Deterministic text-only UI** (`robodog_ui.c`): no face/emote, 16 px
  **Noto Sans CJK TC** face (generated at build time by
  `gen_font_tc_16.sh` from the sha256-pinned `noto-cjk` OTF, cached in
  `~/.cache/lmao-fonts` — never committed; Montserrat-16 stays enabled as
  LVGL's default), **pure-black text on an opaque white label background**. The
  LVGL-theme labels otherwise render against a black screen with a mid-grey
  text that binarizes thin on the 1-bpp panel, so the status + body labels
  force `bg=white COVER` + `text=black` for crisp black-on-white. Left-aligned
  full-panel wrap, status bar on top
  (`WIFI ON/OFF | <agent state>`). Registers its own LVGL display via
  `esp_lv_adapter` (not esp-claw's gated display_service), so it comes up on
  **every** boot.
- **Traditional Chinese text** (`robodog_utf8.c`): the transcript, session
  title and chunk splitter are UTF-8 aware — drawable codepoints are copied
  byte-for-byte, only undrawable ones (emoji/exotic scripts) become `?`, and
  no buffer/chunk boundary ever splits a multi-byte sequence. The generated
  font is rasterised at 16 px / 1 bpp with compact 20 px rows (the OTF's
  natural 30 px row would fit only 8 transcript rows).
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
(RD_LISTEN → mic → Whisper ASR → agent LLM → Qwen3-TTS → speaker → RLCD render),
and it is **dual-language**: Mandarin Chinese speech is heard, answered,
displayed and spoken in Chinese (the English path is unchanged).

### Dual-language voice (English + Traditional Chinese)

The ASR upload carries **no `language` field**, so the Whisper endpoint
auto-detects the spoken language and returns `language`/`probability` with the
`text` — Chinese speech is transcribed as Chinese instead of being forced
through an English decode. The device surfaces that detection as
`ASR language=<tag> prob=<p>` (the gate's `ASR_LANG` leg), then:

- picks the reply directive from the tag (CJK script detection is the fallback
  when an older endpoint omits it), logging `voice reply language=zh-TW|en`
  (the gate's `REPLY_LANG` leg). A Chinese turn gets a Traditional-Chinese
  directive: *reply in 繁體中文 (Taiwan usage), Traditional characters only,
  spoken prose, no markdown*; an English turn gets *reply in English, plain
  prose, no markdown*. **Both name the language explicitly** — the session
  memory carries earlier turns, so after a Chinese conversation the model
  answered a later ENGLISH question in Chinese until the directive said
  "reply in English" (measured on the English gate). Whisper may return
  Simplified, so the Traditional rule must be explicit too, or the model
  mirrors the transcript. This rides the same per-turn directive seam as the
  existing "no markdown" rule, so the upstream base prompt is never rewritten.
- renders the reply with the build-time Noto Sans CJK TC font, keeps UTF-8
  byte-for-byte (`robodog_utf8.c`; only emoji/exotic scripts become `?`), and
  the chunk splitter cuts at CJK sentence breaks (。！？；…) on codepoint
  boundaries, so no Chinese character is ever split between TTS chunks.
- speaks it through Qwen3-TTS with the turn's **explicit language and
  per-language native timbre** (`dylan`, Beijing-native, for Chinese;
  `aiden` for English), plus a **fixed seed** and the Beijing `instruct`. The
  server would otherwise infer each chunk independently and re-sample prosody
  per call; a short English-only chunk in a Chinese reply could flip timbre,
  and the same Chinese reply re-realized its accent on every chunk. Taiwan has
  several Mandarin varieties, so the instruct names the Beijing accent rather
  than a generic "standard Mandarin". The turn sets the language once from the
  ASR result (logged as
  `TTS request language=<lang> voice=<timbre> seed=<n>`) and every chunk
  carries it, so the read-out is one voice from start to end.
- titles the session CJK-aware (9 Han chars ≈ 18 Latin widths in the 170 px
  top-middle label) instead of falling back to `S#<id>` on a Chinese first
  question.

Gate: `robodog_claw_e2e.py --corpus zh_hello` (pinned `你好世界`, spoken by
`serena` in the input fixture, `language: chinese`; the fixture timbre is
unrelated to the reply timbre, which is `dylan`). The phrase uses only
characters identical in Simplified
and Traditional because faster-whisper may emit either script; a
script-dependent phrase would make ASR_MATCH flap. Asserts: `ASR_LANG=zh`,
`REPLY_LANG=zh-TW`, `REPLY` contains CJK, `SUBTITLE` readback contains the
Chinese reply (the vision prompt forbids translation).

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

- **"Hit listen -> straight back to idle": the utterance buffer's single
  1.44 MB allocation.** `voice_capture` used to `malloc` the WHOLE
  `VOICE_MAX_SECS` window (45 s = 1.44 MB contiguous SPIRAM) on the first
  speech frame, and `robodog_voice_asr` then built a second contiguous
  ~1.44 MB multipart body.  The first turn after a boot could satisfy both;
  every later turn could not — the previous turn's buffers (two 1.5 MB TTS
  slots, the ASR response, LVGL, WiFi/Lua) leave the heap with no free block
  that large, so the capture malloc failed, `voice_capture` returned zero
  samples and the turn ended in `idle` after ~2.6 s.  Reproduced 4/4 turns:
  `no SPIRAM for 720000-sample capture buffer` -> `no usable utterance
  captured (0 samples)` -> idle (the reported symptom).  Fixed by removing
  BOTH big allocations:
  - the utterance buffer now **grows in `CONFIG_ROBODOG_VOICE_CAPTURE_STEP_SECS`
    steps** (2 s = 64 KB) as audio arrives (`capture_grow`/`capture_append`),
    so the typical 2-6 s utterance never allocates more than 64-192 KB and no
    single huge contiguous block is ever needed; a growth failure mid-utterance
    ends the utterance with everything already buffered (still transcribed)
    instead of dropping the turn;
  - the ASR multipart body is **streamed** (`esp_http_client_open` + four
    `esp_http_client_write` calls + `fetch_headers` + a direct body read) so
    the captured PCM is never copied into a second contiguous buffer.
  A capture fault (the first allocation failing) now ends the turn in the
  visible **`error`** state, never as a clean `idle` — the panel must not
  claim "user said nothing" when the board could not buffer the audio.  The
  turn also logs `spiram free=/largest=` at every listen so this class of
  fault is attributable from the console.
- **The BOOT button swallowed quick taps and could fire a session reset.**
  The old ANYEDGE ISR debounced *every* edge, so a tap released inside 300 ms
  of its press edge was dropped entirely (the button did nothing unless held
  > 300 ms), and a release edge arriving without an accepted press edge fell
  through to the duration branch with a stale press timestamp — which is
  >= 1.5 s by construction — and fired `EVT_RESET` (clear session) instead of
  a listen.  Now the debounce applies to PRESS edges only, a release is
  honoured only when a press is armed (20 ms contact-bounce floor), and a
  stray release is ignored: every tap starts a listen, a >= 1.5 s hold still
  clears the session.
- **The UI no longer dumps an RD_SHOT frame per command.** Every UI command
  used to end with `robodog_rdshot_capture_now()`, i.e. a 15000 B hex dump
  (~30 KB, ~2.6 s at 115200) on the UI task's critical path.  A chunked reply
  issues ~2 commands per chunk, so a 6-chunk answer left the UI queue ~30 s
  behind: the status label still showed `idle` seconds into the NEXT listen,
  which made a working listen look like "hit listen -> back to idle" (the
  panel lied, not the state machine).  The flush already publishes the
  retained frame (`robodog_rdshot_publish`), and captures are on demand
  (`rdshot` / the self-tick), so the UI now only renders + refreshes; the
  panel tracks the live state within one command.
- **The RD_SHOT self-tick no longer starves the console.** The publisher
  dumped a 15000 B frame (≈30 KB of hex, ~2.6 s of link time) every 4 s, i.e.
  ~65% of the 115200 USB-JTAG link, so every real log line (ASR text, agent
  reply, TTS brackets) arrived seconds late — and a host that stopped reading
  for a few seconds (playing the corpus, re-ASR of the reply) could lose them
  outright, which is how a gate ends up reporting a false "session ended
  before ASR".  On-demand captures are immediate (`rdshot`, and the UI's
  forced dump after every render), so the self-tick only needs to guarantee a
  frame exists for a caller that never asks: it is now 10 s, still well inside
  `capture.py`'s 30 s window, and leaves the link to the logs.  The host
  gates additionally drain the console continuously in a background thread
  (`ConsoleTail` in `robodog_claw_e2e.py`), so no line can be lost while the
  host is busy with audio.
- **A noise blip no longer closes the listen window.** The trailing-silence
  rule (800 ms) ended the utterance as soon as speech stopped — but ANY frame
  above the energy threshold counted as speech, so a chair creak / click / door
  closed the listen ~1 s in, typically BEFORE the user spoke.  The turn then
  captured ~1.5 s of room noise, ASR returned no text, and the panel went
  straight back to `idle` — indistinguishable from the reported "hit listen,
  it switches right back to idle", and it also produced the intermittent empty
  ASR in the multi-session gate.  `CONFIG_ROBODOG_VOICE_MIN_SPEECH_MS`
  (default 100 ms = 5 frames; a click/creak is 1-2 frames) now gates the
  trailing-silence verdict: below it the buffered audio is discarded as a
  blip (`ignoring a <n> ms blip ...; still listening`) and the window stays
  open, still bounded by the no-speech timeout and the hard caps.  The verdict
  has ONE settle point in the capture loop, reached whether or not new audio
  arrived, so a quiet ring still ends a real utterance and a blip is retired
  even when the stream stalls.  The floor must stay LOW: on this rig's
  marginal speaker→mic coupling a short corpus utterance ("what time is it")
  only crosses the energy threshold for ~180-200 ms, so a 300 ms floor
  discarded genuine speech and the listen bailed with 0 samples (measured on
  hardware — see the `ignoring a <n> ms blip` console line).
- **A stalled capture stream is a fault, not silence.** If the listen window
  delivered no PCM at all (I2S RX overrun / wedged codec / capture task
  stopped), the old code reported `no usable utterance` and ended in a clean
  `idle` — the panel claimed "nothing heard" for a broken capture path.  It
  now logs `capture stream produced no audio` and ends the turn in `error`.
- **The idle capture subscriber no longer floods the console.** Keeping the
  APP subscriber open across turns (F3) means it is never drained between
  turns, so upstream `audio_capture` logged a ring-overflow warning on EVERY
  20 ms frame — ~30 KB/s (12.7 MB dropped observed) on the 115200 USB-JTAG
  console, which saturates it and can block the capture task inside
  `ESP_LOGW` while a turn is listening (dropped audio -> VAD misses speech).
  `patches/0004-capture-drop-log-rate-limit.patch` rate-limits that warning to
  one line per 5 s carrying the running total.

- **Two per-turn SPIRAM leaks — the other half of "later turns go silent".**
  Even with the capture cliff gone, every turn after the first still ended in
  `error` with the reply rendered but nothing spoken.  Measured on the
  console (`spiram free=`): only ~1 MB free after a handful of turns.  Two
  leaks, both fixed:
  - the TTS **double-buffer slots were never freed on the normal (prefetch)
    path**: `rx[0]`/`rx[1]` (2 x `CONFIG_ROBODOG_VOICE_TTS_MAX_RESPONSE`
    = ~3 MB) were allocated per turn, and only the `!dbl` sequential fallback
    released them.  The prefetch branch therefore leaked ~3 MB per voiced
    turn; after the first reply the heap could never satisfy the next turn's
    slots (`TTS: no memory for response buffers ... nothing played`) -> the
    turn ended in `error`, and the leaked megabytes also starved the next
    turn's capture buffer -> `idle`.  The slots are now freed right after the
    prefetch task ACKs the abort (the only point where nothing can still be
    writing them).
  - `claw_core_response_t` (`text` / `error_message` / `target_*`) is owned
    heap memory the caller must release with `claw_core_response_free()` (the
    upstream `app_claw_cli.c` does).  `voice_turn` and `rd_chat` skipped it,
    leaking the reply text every turn.  Both now free it on every exit path.
  With both leaks closed the per-listen `spiram free=/largest=` line stays
  flat across turns, and the multi-session stress gate's NO_COLLAPSE +
  CHUNKED_QUEUE legs prove turns 2..N behave like turn 1.

- **The reply is CHUNKED at sentence breaks and queued, not truncated.** The
  esp-claw root agent is verbose by default (a full "what can you do" dump is
  60+ s of speech), so `voice_turn()` splits the FULL reply via
  `voice_split_reply()` into SENTENCE-ALIGNED chunks of
  `CONFIG_ROBODOG_VOICE_TTS_MAX_CHARS` bytes preferred (default 96) — the cut
  lands at the last `.!?;`/`。！？；…` inside the budget; when the window ends
  mid-sentence the splitter runs on to the NEXT sentence ender, bounded by
  `CONFIG_ROBODOG_VOICE_TTS_OVERFLOW_CHARS` (default 24), so a chunk still
  fits one pipeline slot's WAV buffer (384 KB = ~8.2 s). A sentence too long
  for that window falls back to the last CLAUSE break (`,` `，` `、` `：`) —
  a natural pause, not a word cut — then a space, then a hard cut at the
  budget. A newline is only a weak fallback for unpunctuated text. Chunks also
  never exceed the 479-byte RLCD command field (R5: on-screen == spoken).
  Each chunk is queued in order: appended to the on-screen reply
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
- **Inter-chunk TTS latency is hidden by an N-SLOT PIPELINE (measured, not
  assumed).** Qwen3-TTS on the LAN synthesizes at roughly real time and
  returns each chunk's WAV WHOLE — `Content-Length` set, time-to-first-byte ==
  total time (measured 1.3 s for a 1.2 s utterance, 4.4 s for a 4.9 s one),
  so a chunk cannot start playing before its synthesis finished.  A ONE-step
  lookahead (fetch i+1 while i plays) therefore only hides the fetch when that
  fetch is shorter than the previous chunk's playback; when the endpoint is
  busy (measured 14-54 s for a 3-4 s chunk while the GPU host was loaded) the
  reply stalls for seconds at every sentence boundary — the reported "long
  delay between response segments".  The queue is now fed by a real pipeline:
  `CONFIG_ROBODOG_VOICE_TTS_SLOTS` (5) persistent slot buffers,
  `CONFIG_ROBODOG_VOICE_TTS_LOOKAHEAD` (3) CONCURRENT background fetch workers
  that keep that many chunks armed ahead, and a play loop that waits only when
  the next chunk is genuinely not ready.  Concurrency is not decoration: the
  endpoint serializes synthesis, but every request still costs the board a
  fixed ~0.5 s of HTTP setup + WAV download.  With ONE worker that cost lands
  between requests (the server idles while the board reads the previous WAV),
  and the measured device-side rate was ~1.1x real time — slower than playback,
  which is what produced the seam gaps.  Overlapping fetches hide the transfer
  under the server's next synthesis.  The TTS request also uses a 4 KB HTTP RX
  buffer (a 512-byte one made the board's effective synthesis rate ~1.0x while
  the same request from a host ran at ~0.88x — the read overhead was letting
  playback overtake the fetch).  Synthesis thus runs ahead of the speaker
  and consecutive sentences join without a fetch on the play path.

  Three further things make it sound like ONE answer rather than clips:
  - **One mixer track per turn.** The APP track is opened ONCE and every chunk
    is written into it back-to-back; the old per-chunk `flush`+`close` drained
    the ring and re-opened the codec at each boundary, adding a silence of its
    own.
  - **Boundary silence trim** (`voice_pcm.c`, host-tested): Qwen3-TTS pads each
    WAV with a lead-in and a tail (measured 150-400 ms).  Played verbatim, each
    seam doubles that padding.  Leading silence is trimmed entirely on every
    chunk after the first (`TTS_LEAD_KEEP_MS` keeps the opening breath), and
    trailing silence is trimmed to `TTS_TAIL_KEEP_MS` on all of them.
  - **SENTENCE-ALIGNED, near-uniform chunk lengths** (`TTS_FIRST_CHARS ==
    TTS_MAX_CHARS == 96 bytes`, ~6.4 s of speech; a chunk overflows the budget
    only by OVERFLOW_CHARS=24 to finish a sentence).  The reported "cuts in
    mid-sentence, voice sounds weird" defect came from `cap` being treated as a
    HARD limit: a sentence longer than 96 bytes was cut at the last space, so
    the speaker stopped mid-clause and resumed mid-clause.  `cap` is now a
    preferred budget: the seam is a sentence end whenever one fits the bounded
    window, else a clause break (comma), else a space.  Newlines are a WEAK
    fallback only — treating them as sentence ends cut at the agent's line
    wrapping, which produced the same defect.

    The overflow bound is not cosmetic: without it a 479-byte sentence became
    a ~30 s WAV and the fetch failed with `response buffer grow failed`
    (394886 B > the 384 KB slot buffer), dropping chunks.  Measured on the
    board after bounding: 4 chunks, every seam a sentence/clause end, no
    buffer failures.  Length variance is absorbed by the deep pipeline: with
    RTF ~1.0 and
    endpoint serializes requests and synthesizes at ~1.0x real time on the
    board, so the fetch worker cannot start chunk 2 until chunk 1 is done.
    The seam gap at chunk k+1 is `Σ synth(2..k+1) - Σ play(1..k)`, which is
    ~0 for UNIFORM chunks but `synth(chunk2) - play(chunk1)` for a short first
    chunk — a 96-byte opener followed by 144-byte chunks produced a measured
    7 s silence at the first seam, while uniform 96-byte chunks produced ~0 ms
    seams.  A tighter first budget only buys first-audio latency at the price
    of that gap; keep the two equal.  Sentence alignment does not change this
    rule: a long sentence overflows to its own end, so the first chunk must
    not be shorter than the budget or the first seam is exposed.

  Memory invariants: the slots are `SLOTS x TTS_MAX_RESPONSE` (5 x 384 KB ≈
  1.9 MB of PSRAM), allocated ONCE for the firmware's lifetime and reset in
  place via `robodog_http_rx_reset` — no per-turn/per-chunk alloc/free (the R7
  free-churn panic site cannot re-run) and no free under the fetch task's
  writer.  `K <= SLOTS-2` keeps one slot free for the synchronous fallback
  fetch even while another slot is playing.

  Console contract (unchanged plus one new marker): `voice chunk i/n` then
  `TTS start`…`TTS stop` per chunk, in order; plus
  `pipeline chunk i/n buffered=yes|no ahead=k` logged before each chunk plays.
  `buffered=yes` proves the chunk was already synthesized when its turn came.
  On a failed/wedged fetch the chunk is fetched synchronously — never skipped,
  reordered or double-fetched (the old code fired a SECOND request for the
  same text after a 3 s wait).  If fewer than two slots can be allocated the
  whole turn degrades to the sequential single-buffer path.

  ⚠️ **Slot bookkeeping is TURN-LOCAL; the worker only reports.** The fetch
  task never touches the turn's pipeline model (it lives on the voice task's
  stack): it signals completion through a per-slot event bit + a per-slot
  result code, and the play loop applies `commit()`/`fail()` when it consumes
  the chunk.  Teardown sets the abort flag, drains the job queue and waits for
  the worker's IDLE ack before the turn returns — the slots themselves are
  never freed, so a mid-flight fetch cannot be freed under its writer.
  Host regression guards: `//robodog_client:test_voice_split` drives the exact
  firmware call sequence (`test_voice_pipeline.c`) and the trimmer
  (`test_voice_pcm.c`); the gate's **PIPELINE_AHEAD** leg asserts every
  boundary after the first logged `buffered=yes` (no endpoint fetch on the play
  path; `ahead=k` is emitted as a diagnostic), and **GAP_VERIFY** keeps
  measuring `TTS stop(i) → TTS start(i+1)` < 250 ms.

  Measured on hardware (same corpus, same reply shape), the two changes that
  mattered for the reported "long delay between response segments":
  | build | inter-chunk gaps (console) |
  |---|---|
  | one-step lookahead, per-chunk flush/close, 512 B HTTP reads | 4108, 3, 980, 3, 785, 3, 953, 3, **10141**, 2, 4, 3, 2 ms |
  | uniform 96-B chunks, 3 concurrent fetch workers, 4 KB HTTP reads, one track per turn | 3, 3, 4, 3, 3, 3, 3, 3, 3, 3, 6, 5, 4 ms |

  The per-request overhead is the key: the device's `fetched chunk i/n in N ms`
  log showed ~0.5 s of HTTP setup + WAV transfer per chunk on top of the
  endpoint's synthesis, so a serial worker let the server idle between requests
  and the device-side rate fell to ~1.1x real time (slower than playback).
- **Multi-session gapless-prefetch stress.** The mandatory gate runs ≥ 5
  consecutive voice sessions (`E2E_RUNS ≥ 5`), each asking a DIFFERENT
  question (corpus slugs time/joke/greeting/capabilities/count cycled so no
  two consecutive sessions match), every session must reach `TTS stop` with a
  complete readout (CHUNKED_QUEUE + FULL_READOUT PASS, no `voice truncated`),
  and the whole run must show NO board reset/panic (watchdog scans for
  `rst:*` / tlsf / heap-corruption — the R7 soak regression the prefetch's
  new sustained-load path must not re-trigger; `robodog_claw_stress.py`
  drives it, additive, and never writes `hardware-e2e.md`/`.gate-head`).
  It also carries the **NO_COLLAPSE** leg: a session that never reaches ASR
  because the board could not capture/buffer the audio (`no usable utterance`,
  `capture fault`, `no SPIRAM`, `capture buffer growth failed`) FAILs — that
  is exactly the "first turn works, every later one drops back to idle"
  regression class, which a single-session gate cannot see.
  Run it with `bazel run //robodog_client:claw_stress -- --runs 5`.
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
- The test rig: the bidirectional USB PnP Audio device is **card 2** on the
  workspace host; there is **no ALSA `default` alias**.  The raw `hw:2` node
  returns `EIO` on the current kernel — the working node is **`plughw:2`**.
  The bench host (op4) has **no USB audio at all**: only card 0 `sndi2s4`
  (onboard ES8389, not coupled to the RoboDog board) and card 1
  `allwinnerhdmi`; benchd's defaults (`BENCH_AUDIO_PLAYBACK`/
  `BENCH_AUDIO_CAPTURE` = `default:CARD=sndi2s4`) therefore play into the
  onboard codec and record from it — a 3 s capture there measures
  `mean_volume −76.5 dB`, `max_volume −45.6 dB`, i.e. the noise floor.
  `robodog_claw_e2e.py` probes candidate nodes (`plughw:CARD=<id>`, `plughw:2`,
  `hw:2`, `default`, each with and without the `sg audio` wrapper) with a 0.3 s
  null-source playback and drives the first that opens; `RD_ALSA_DEVICE` pins
  one.  It logs `/dev/snd`, `aplay -l`, the chosen node and every probe result,
  and LOUD-SKIPS when no node opens — a playback failure is never silently
  reported as "the board heard nothing" (issue #301).  The skip is split by
  verdict: **no board** keeps the documented loud-skip convention (`SKIPPED.txt`,
  exit 0), while **board present but no ALSA node opens** is a **rig fault** —
  it writes `RIG_UNUSABLE.txt` and exits **4**, because benchd maps rc==0 to
  `passed` and a missing acoustic rig must never read as a green gate (a node
  that opens but is not coupled to the board still fails honestly on the ASR
  legs).

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

## Phase 2 fix round — "hit listen, the board switches right back to idle"

Reported symptom: many listens after a button press returned to `idle`
immediately instead of opening the voice window.  Root causes (all reproduced
on hardware, all fixed — see the bullets above for the mechanism of each):

| # | Cause | Fix | Evidence |
|---|---|---|---|
| 1 | Utterance buffer = one 1.44 MB contiguous SPIRAM malloc on the first speech frame; only turn 1 after boot could satisfy it | grows in 2 s steps (`CONFIG_ROBODOG_VOICE_CAPTURE_STEP_SECS`) | pre-fix 4/4 turns: `no SPIRAM for 720000-sample capture buffer` -> `no usable utterance` -> idle; post-fix 5/5 turns voice normally |
| 2 | ASR multipart body = a second ~1.44 MB contiguous copy | streamed upload (`esp_http_client_open` + 4 writes) | same runs above |
| 3 | TTS double-buffer slots (~3 MB) never freed on the prefetch path | freed after the prefetch task ACKs the abort | pre-fix `spiram free=1053340` after 2 turns + `TTS: no memory for response buffers` -> `error`; post-fix flat `spiram free=4200xxx largest=4194304` across 5 turns |
| 4 | `claw_core_response_t` strings never released | `claw_core_response_free()` on every exit path (voice + rd_chat) | same leak measurement |
| 5 | A noise blip tripped the energy VAD and the 800 ms trailing silence closed the window before the user spoke | `CONFIG_ROBODOG_VOICE_MIN_SPEECH_MS` (100 ms) gates the trailing-silence verdict; a blip is discarded and listening continues | console: `ignoring a 180 ms blip (< 100 ms speech); still listening` -> then a real utterance proceeds |
| 6 | BOOT button: every edge debounced (quick taps swallowed) and a stray release fired `EVT_RESET` from a stale timestamp | press-only debounce; release honoured only when a press is armed | shared `EVT_LISTEN` path proven via `rd_listen`; the ISR branch is by-inspection (the physical press cannot be driven remotely) |
| 7 | UI forced a 30 KB RD_SHOT dump per UI command (~2.6 s each), putting the UI queue tens of seconds behind — the panel showed `idle` mid-listen | UI renders + refreshes only; dumping is the publisher's job (self-tick 10 s + on demand) | RD_SHOT frame captured during a listen reads `listening`; state transitions now logged (`state -> listening`) |
| 8 | Console: idle capture subscriber flooded ~30 KB/s of ring-overflow warnings; 4 s RD_SHOT self-tick ate ~65% of the link | `patches/0004-capture-drop-log-rate-limit.patch` (1 line / 5 s) + 10 s self-tick + host-side continuous drain (`ConsoleTail`) | gate no longer loses ASR/reply/TTS lines while the host plays audio or re-ASRs |
| 9 | A stalled capture stream was reported as "user silent" -> clean `idle` | `capture stream produced no audio` -> turn ends in `error` | console + panel state |

Verification on the board (`/dev/ttyACM0`, Waveshare ESP32-S3 RLCD-4.2,
esp-claw @ pinned commit, IDF v5.5.4):

```
# multi-session stress (5 sessions, different question each)
bazel run //robodog_client:claw_stress -- --runs 5
  session 1 'what time is it'          ASR 1.00 / 1 chunk  / FULL_READOUT 0.86
  session 2 'tell me a joke'           ASR 1.00 / 1 chunk  / FULL_READOUT 0.72
  session 3 'how are you today'        ASR 1.00 / 1 chunk  / FULL_READOUT 0.87
  session 4 'what can you do'          ASR 1.00 / 4 chunks / FULL_READOUT 0.61
  session 5 'count slowly ... thirty'  ASR 0.83 / 1 chunk  / FULL_READOUT 0.91
  NO_COLLAPSE 5/5 PASS, NO_RESET 5/5 PASS -> Overall: PASS

# standard voice-loop gate
python3 robodog_client/esp-claw/robodog_claw_e2e.py --corpus capabilities
  OPEN_SESSION / ASR_MATCH / REPLY / TTS / RING / SUBTITLE / NO_RESET -> Overall: PASS
```

Panel + state, captured with the RD_SHOT → `capture.decode_hermes` → vision
readback path: a press with no speech shows `listening` on the status row and
ends in the intended `no speech within 12000 ms` bail (`state -> idle`), while
a press followed by speech runs the full chain and renders the reply.

## Phase 3 — omni realtime voice path (standalone, no bridge)

The Phase 2 loop is three serial HTTP calls, each returning a WHOLE response
before the next can start. Measured on the board (`/dev/ttyACM0`, corpus
`count`, 4.16 s WAV played into the mic):

| Stage | Measured |
|---|---|
| VAD trailing silence | 0.8 s |
| Whisper `/v1/audio/transcriptions` | 7.8–12.5 s (4.2 s fixed + 0.67 × utterance) |
| claw_core reply (whole, no stream) + 3.6 s memory-extract stage note | 4.2–7.0 s |
| Qwen3-TTS first chunk (TTFB == total, RTF ≈ 1) | 8.8–15.0 s |
| **end-of-speech → first audio** | **24.3–32.5 s** |
| read-out to `idle` | 11.6–14.2 s |

Phase 3 replaces those three hops with ONE WebSocket session to a vLLM-Omni
OpenAI-Realtime endpoint (cluster-ops `omni-realtime/`). The board streams its
mic into the session **as it captures** (no utterance upload at all), commits
when its own energy VAD says the user stopped, and receives transcript and PCM
deltas as the model generates.

**Measured on the same corpus, same board, warm session:**

| Leg | Phase 2 | Phase 3 |
|---|---|---|
| end-of-speech → first audio | 24.3–32.5 s | **0.83 s** |
| session handshake | n/a | 74–99 ms |
| first audio → readout complete (9.2 s of audio) | ~11.6 s | 5.1 s |

### Two per-turn shapes (esp-claw decides, per turn)

The omni model answers and speaks a turn itself, but that path has no
skills/MCP/web access. So the board picks the shape PER TURN:

| Shape | Path | Cost |
|---|---|---|
| `OMNI` | omni answers + speaks (`modalities ["audio","text"]`) | ~0.8 s to first audio; **no esp-claw tools** |
| `AGENT` | omni transcribes (`modalities ["text"]`), claw_core answers with skills/MCP/web/self-programming, Qwen3-TTS speaks | full capability, Phase-2 latency |

`AUTO` (the default) routes with `voice_route.c` — a pure-C, host-tested
keyword table, so the decision is auditable on the console
(`RT route=agent reason=search`) and tunable per household. Anything that
isn't a capability intent stays on the fast path. `rd_voice_mode
[classic|auto|omni]` pins a path at runtime for A/B measurement, and a
realtime fault ends the turn visibly (never a re-capture).

**The transcript comes from an explicit ASR call, not the realtime event.**
The deployed Qwen3-Omni lane never emits
`conversation.item.input_audio_transcription.completed` (bench-probed: it
accepts and echoes `input_audio_transcription` in `session.update`, then emits
only `input_audio_buffer.committed` / `conversation.item.*` / `response.*`), and
the fallback the code used to claim in `rt_dispatch` never existed. So for the
AGENT shape the board POSTs the captured utterance to
`<realtime host>/v1/chat/completions` as `input_audio` (same model, no new
service, `modalities:["text"]`), logs `RT ASR text="..."` (gate regex unchanged)
and `RT ASR language=<tag> prob=0.00`, renders the transcript on the RLCD, and
routes on it. The ASR prompt asks for `{"language":...,"text":...}` so the
language tag is the model's own detection rather than a script guess; the pure-C
`voice_asr.c` parser (host-tested) accepts a plain transcript too.

That makes the AUTO turn **one** response, not two: capture → explicit ASR →
route → commit → `response.create` with the shape's modalities. The omni shape
streams and plays the model's audio; the agent shape gets a text-only response
(kept so the session history has the model's answer) and then claw_core answers
with Qwen3-TTS. The unused text-only reply is neither rendered nor
think-suppressed (its audio is ignored before decode).

### Session protocol (what the board sends)

`session.update` → `session.created` → paced `input_audio_buffer.append`
(40-60 ms PCM16 24 kHz frames, only once speech begins) →
`input_audio_buffer.commit` → `response.create`. The session opens with
`turn_detection: null` and `auto_response: false` so the turn boundary is
the BOARD's (a server VAD would answer mid-sentence). The input audio is
resampled 16 kHz → 24 kHz (the handler hardcodes `SAMPLE_RATE_HZ = 24000`).

### Capture pump: never block the reader (issue #301)

The first implementation resampled + base64'd + sent each 20 ms frame inline in
the capture loop, with a 10 s send timeout, so a slow socket could stall the
reader — the pump is decoupled from the network below as hardening.

The bench evidence then showed the reported "stalled pump" was a misreading.
The subscriber ring `total` grew at the full 32 KB/s *minus ~600 frames per
turn*: the reader consumed exactly the 12 s the `RT no speech within 12000 ms`
bail allows and then stopped. The turn had ENDED on "no speech" while the
still-open APP subscriber overflowed the ring for the rest of the gate's 90 s
ASR wait, and the missing stage reset left the watchdog reporting
`stage=capture` for a turn that was over. What the board actually lacked was
speech: the gate drove the corpus into a device string that was not a PCM node
(the ALSA `null` plugin's *description*; see the rig note below), so the mic
only ever saw its noise floor.

The reader now only fills a 64-slot ring of **raw 16 kHz frames** (PSRAM,
1.28 s). A dedicated `robodog_rt_up` sender task drains it, resamples, base64s,
coalesces up to 3 frames into ONE `input_audio_buffer.append` (one JSON message
per 40-60 ms) and sends with a **250 ms** timeout. A full ring drops the OLDEST
frame and counts it; the reader never blocks on the network. The once-per-second
pump log carries `frames`, `captured` bytes, `up_dropped`, `ever_speech`,
`speech_ms`, `silence_ms`, `vad_max`/`peak` (the highest frame mean-abs and
sample the VAD saw) and the sender's `last/avg/max` build+send wall time, which
distinguishes "pump too slow" from "socket blocked" from "the mic is silent" in
one run. The turn
drains the ring before `commit` so the whole utterance is sealed (a timed-out
drain logs and commits what was sent).

**No idle capture.** The APP subscriber is closed at the end of each RT turn
(the capture task then idles with the codec left open, so there is no codec
churn and no ring for the idle gap to overflow). Every early turn exit resets
the stage to `idle`, so the watchdog can only report a stage that is actually
being held.

**`response.listen` is not a turn end.** MiniCPM-o's native turn-taking can
answer a commit with "keep listening" (measured: a 2.1 s committed utterance
produced `listen`, then `speak` immediately after one explicit
`response.create`). A committed utterance must still be answered, so the board
re-asks ONCE; a second `listen` verdict is honoured (the user really did pause
mid-sentence).

### Device-side implementation

`robodog_realtime.c` — WS client (`esp_websocket_client`, already a compiled
dependency) + fragment reassembly + JSON dispatch + a playback task that
writes decoded PCM deltas to the shared APP mixer track.

- **No per-delta allocation**: the playback slots (`RT_QUEUE_DEPTH` ×
  `RT_DELTA_MAX_BYTES`) and the base64 decode scratch are allocated ONCE at
  init, so the Phase 2 R7 tlsf-free panic site cannot recur. An oversized
  delta is dropped with a log rather than growing a buffer mid-stream.
- **No sleep pacing**: the mixer ring is the backpressure (a short write is
  retried), which is what paces playback at the codec rate. The Phase 2 loop
  slept 19 ms per 20 ms frame and ran ~5 % fast.
- **One session per boot**: `robodog_rt_connect()` is idempotent, so turns 2..N
  reuse the admitted session (`duplex_session.max_sessions` counts admitted
  sessions, and the server reports `resource_exhausted` beyond it).
- **AEC**: `esp_audio_effects` in this tree ships ALC/DRC/EQ/mixer/rate-cvt but
  no echo canceller, so the board stays half-duplex (mic not read while audio
  plays) exactly as in Phase 2. MiniCPM-o supports barge-in; the hardware
  does not.

Shared helpers extracted so the two paths cannot drift:
`robodog_speaker.c` (PA + DAC unmute + volume — the reply is silent until all
three are asserted) and `robodog_pcm_resample()` in `voice_pcm.c` (used by both
the TTS path and the realtime path; the omni endpoint declares its output rate
per delta, so a wrong rate assumption would play a 16 kHz clip slow).

### Server side (cluster-ops `omni-realtime/`)

`vllm-omni serve openbmb/MiniCPM-o-4_5 --omni --deploy-config <overlay>` on
spark-5307 (GB10, 121 GiB unified). The packaged profile budgets 0.55 + 0.15 +
0.18 of the DEVICE, which assumes the node owns the GPU; the site overlay
(`minicpmo_4_5_shared_gpu.yaml`) trims it so the omni endpoint coexists with
Whisper (:8001) and Qwen3-TTS (:8004). Three install gaps found on the way:

1. **`vllm-omni` does not depend on `vllm`** — install both at the same version.
2. **Code2Wav needs `stepaudio2-minicpmo`**, and its bundled `cosyvoice2`
   implementation is only registered under the upstream module name inside
   `stepaudio2.Token2wav.__init__` — which vLLM-Omni's `StepAudio2Token2WavCore`
   never calls. Without the `cosyvoice2.py` alias shim in site-packages, Stage 2
   dies with `ImportError: There is no such class as
   cosyvoice2.flow.flow.CausalMaskedDiffWithXvec`. (`hyperpyyaml` and
   `speechbrain` are also needed.)
3. **The vocoder assets live under `assets/token2wav/`** in the HF repo, not at
   the snapshot root, so a plain `hf download` of the model leaves Stage 2
   without `flow.pt`/`hift.pt`.

Also: GB10 memory is UNIFIED, so the page cache counts as USED in vLLM's
free-memory check — a warm cache can make a legal budget look impossible.
Drop caches before starting the engine.

### The session recipe that actually works (hardware-verified)

Getting a turn answered took four findings, each reproduced on the board:

1. **`session.type` is MANDATORY.** OpenAI's schema spells the session object
   `RealtimeSessionCreateRequest`, whose `type` field is required. Omitting it
   makes the server reject `session.update` with `invalid_event` and keep its
   defaults — the session still opens, so the failure is silent (no
   instructions, no turn behaviour change).
2. **Wait for `session.updated` before streaming audio.** The server applies
   the update asynchronously and RESETS the input buffer when it lands, so
   anything appended before that event is discarded and the commit then
   produces nothing at all.
3. **Send the MINIMAL session payload.** Measured on hardware: adding
   `modalities`, `turn_detection`, `input_audio_format`, `overlap_policy`,
   `playback_commit_policy` or `extra_body` makes the update succeed but then
   silently drop the commit. The accepted shape is
   `{type:"session.update", session:{type:"realtime", model, instructions}}`.
4. **Input audio is 24 kHz mono PCM16**, not 16 kHz — the OpenAI-compatible
   handler hardcodes `SAMPLE_RATE_HZ = 24000`. The board's capture hub runs
   16 kHz, so every frame is resampled up before the append; sending raw
    16 kHz makes the model hear the utterance 1.5x fast.

Then `input_audio_buffer.commit` + `response.create` with
`{"modalities":["audio","text"]}` produces `response.created`,
`response.output_audio_transcript.delta` (the RLCD subtitle) and
`response.output_audio.delta` (PCM16 at 24 kHz, played through the shared APP
mixer track).

**Device-side WS sizing.** One audio delta is ~54 KB of JSON (base64 of ~40 KB
of PCM16) on the MiniCPM-o lane, but the Qwen lane sends the WHOLE reply in ONE
delta (~214 KB of PCM for a 4.5 s answer). With a 4 KB `esp_websocket_client`
buffer the message fragments and its bytes are lost whenever the handler does
work between reads — measured as every audio delta failing to parse. The buffer
is therefore 64 KB, i.e. one event per message; the decode scratch is 256 KB
(the whole-reply delta) and the decoded PCM is split across the playback slots.
The queue is 16 x 64 KB (~1 MB) rather than 48 slots (3 MB, which exhausted the
~3.2 MB of free SPIRAM and made the websocket client fail to init).

Measured on the board after these fixes (`count` corpus, test speaker into the
board mic): **6.3 s from end-of-speech to first audio** (device-internal
commit→first-audio 5.4 s on that run), reply text streamed to the RLCD and
audio played — against 24-32 s for the Phase 2 chain. The remaining gap is
first-request model warmup plus the 800 ms VAD tail; the host probe on a warm
engine measures 1.1-2.6 s.

### Why turns sometimes ended silent (root causes, all fixed)

The board "often hit idle and no response". Three distinct causes, each
reproduced on hardware:

1. **The Talker stage died.** When the Thinker emits a `think` block with no
   `<|tts_bos|>` span, Stage 1 raises
   `ValueError: MiniCPM-o Talker requires tensor tts_token_ids and
   tts_hidden_states conditioning` and is evicted. The API server keeps
   answering `/health` **200**, but every realtime request then fails with
   `OmniEngineDeadError: Stage-1 has no live replica` — forever, because
   `Restart=always` only fires when the process *exits*. Fixed server-side by
   `omni-realtime-watchdog.timer` (cluster-ops): it probes the real WS
   handshake, skips the cold-load grace window, and restarts after three
   consecutive failures.
2. **The board re-captured after a failed turn.** On any realtime fault the
   Phase 2 chain opened a *second* listen window; the user had already spoken,
   so it found silence and ended in a clean `idle` — the symptom looked like
   "the assistant ignored me". Now a realtime fault ends in a visible `error`
   and the next turn reconnects (`rt_send` drops the cached session on
   failure).
3. **Nothing was heard.** A VAD miss ended in a plain `idle`, indistinguishable
   from a broken turn. It now shows the `no speech` state and *says*
   "Sorry, I didn't hear that." so the failure is audible.

### Reasoning blocks ("it tries to pronounce think")

MiniCPM-o sometimes emits `think ... /think` and the Talker **voices it** —
ASR of such a turn heard `"Gate, rot, zoo, rot, okay, 21, 22, ..."`, i.e. the
model pronouncing the tag before the answer. Measured facts:

- **No server-side switch removes it.** `chat_template_kwargs.enable_thinking
  = false` is accepted by the realtime handler and ignored (identical text and
  audio with and without it, whether passed in `extra_body` or at the request
  root). `use_tts_template` does not change it either.
- **Re-asking does not clear it** (a second `response.create` on the same
  committed turn emits the block again).
- It appears on a **repeated question within one session**; a fresh session
  never emitted one in 8 varied turns.

Device-side handling (`voice_think.c`, pure C, host-tested):

- `robodog_think_strip()` removes every block from the accumulated text — the
  scan runs over the whole text because the streaming deltas split the tag
  across events (`<`, `think`, `>` arrive separately).
- When a block is present the turn's **server audio is discarded** (it
  contains the spoken tag) and the CLEAN text is re-spoken through Qwen3-TTS.
  The log carries `think=1 dropped=<n>` so the E2E can prove it.
- A reply that is *entirely* a block strips to empty; rather than going
  silent, the board says "Sorry, I lost that one. Please say it again."
- The session is then **reset** (fresh history), which is the state that never
  emits a block — verified: the turn after a think turn is clean and fast
  (1.38 s to first audio).

Playback is gated on the first transcript delta (`s_play_go`): audio deltas can
arrive BEFORE the text (measured: the first audio delta came 14.5 s after
commit, before the text), so a timed fallback would start playing a reasoning
block the stripper has not seen yet. The turn releases the gate at
`response.done` so a text-less stream cannot mute the turn or hang the drain.
