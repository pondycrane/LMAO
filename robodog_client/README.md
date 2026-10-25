# robodog_client

Native **ESP-IDF** firmware for the **RoboDog** family-calendar display:
a Waveshare **ESP32-S3** dev board driving the Waveshare **RLCD-4.2**
(**ST7305**, 400×300 reflective 1-bit B&W LCD).  No RNS/LXMF here: the device
reads the family calendar from a synchronous gRPC call on the LMAO server
and renders it in 1-bit B&W on the ST7305.

```
┌─ LMAO server (lma.LMAO/GetCalendar gRPC, :50051) ───────┐
│   HTTP/2 unary: CalendarRequest -> CalendarBundle       │
└──────────────────────────────────────────────────────────┘
            ▲ gRPC/HTTP2, single request/reply per sync
ESP32-S3 ───┘   → decode → merge δ into NVS cache → render ST7305
                → light-sleep ~30 min → repeat
```

## Board & wiring

Board: Waveshare **ESP32-S3-DevKitC-1 (N16R8)**-class module (no hardware
bring-up was done for this code yet — first-run validation required, see
below).  LCD: Waveshare **4.2" E-Ink-style RLCD (ST7305)**, driven over a
single SPI bus.

| Signal | GPIO | Notes |
|--------|------|-------|
| MOSI   | 12   | SPI data in |
| SCK    | 11   | SPI clock   |
| DC     | 5    | data/command |
| CS     | 40   | chip select  |
| RST    | 41   | hardware reset |
| TE     | 6    | tearing-effect (not used by the driver) |

Pins are defined once in `main/config.h` (`RD_*_PIN`).

## Build & flash

Requires an ESP-IDF install (`idf.py` on `PATH`) — **ESP-IDF ≥ 5.1** (the
driver uses the `esp_sntp` component and 5.x WiFi APIs; the reference
sdkconfig.defaults from the Waveshare example was generated on 5.5.1).  From
this directory:

```sh
idf.py set-target esp32s3
idf.py build
idf.py flash monitor        # attach a serial monitor, e.g. /dev/ttyACM0
```

The project expects 16 MB flash / 8 MB octal PSRAM
(`sdkconfig.defaults`); the custom partition table allocates an 8M app
partition (`partitions.csv`).  The vendored `u8g2` and `u8g2_st7305`
components under `components/` are third-party and copied verbatim.

**Configuration** — bake WiFi / server before flashing by editing
`main/config.h`, or leave `RD_WIFI_SSID_DEFAULT`/`RD_WIFI_PASS_DEFAULT` empty
and set them once at runtime in NVS:

| NVS key (`robodog` namespace) | Purpose |
|---|---|
| `wifi_ssid`  (string) | STA SSID (fallback: `RD_WIFI_SSID_DEFAULT`) |
| `wifi_pass`  (string) | STA passphrase (fallback: `RD_WIFI_PASS_DEFAULT`) |
| `server_host`(string) | LMAO server host/IP (fallback: `RD_SERVER_HOST_DEFAULT`, `192.168.50.67`) |
| `kid_name`   (string) | Per-board kid assignment (fallback: `RD_KID_NAME_DEFAULT`, `""` = family).  Non-empty → kid-dedicated panel: the request carries `participant=<kid>`, the server returns only that kid's events, and the header shows the kid name LEFT + live clock RIGHT.  Set at flash time with `--kid-name` — see below. |

Other knobs (`RD_CAL_GRPC_PORT`, `RD_SYNC_INTERVAL_SECONDS`,
`RD_SNTP_SERVER`, timeouts) are build-time macros in `main/config.h`.

## Wire contract (implemented exactly)

Synchronous gRPC unary `lma.LMAO/GetCalendar(CalendarRequest) returns
(CalendarBundle)` over HTTP/2 to `LMAO_SERVER_HOST:RD_CAL_GRPC_PORT`
(default `50051` — the LMAO server's gRPC port; `192.168.10.19` LAN).

The ESP32 client implements a minimal HTTP/2 + gRPC transport for this single
RPC: connection preface, SETTINGS/ACK, a constant HPACK header block (literal
without indexing), and a DATA frame carrying the gRPC-framed `CalendarRequest`
(1-byte compression flag + 4-byte BE length + protobuf).  The reply DATA is
stripped of its gRPC header and the `CalendarBundle` decoded by `lma_proto`.
The wire format is verified live against the LMAO server.

- **Client → server** payload = serialized `CalendarRequest` protobuf
  (`calendar_id=1` string, `since_ms=2` varint u64, `max_events=3` varint u32,
  `participant=4` string — kid name, omitted when empty/family).
  A 0-length payload = the documented defaults `{calendar_id:"family",
  since_ms:0}` (no participant = family filter).
- **Server → client** payload = serialized `CalendarBundle` protobuf
  (`format=1` varint u32, `calendar_id=2` string, `events=3` repeated
  `CalEvent`, `watermark_ms=4` varint u64).
- `CalEvent`: `uid=1` string, `title=2` string, `notes=3` string,
  `start_ms=4` varint u64, `end_ms=5` varint u64, `deleted=6` varint bool,
  `pic=7` string.

The device sends `since_ms = last watermark` (persisted in NVS), so the reply
is a small delta; it merges the delta into the NVS-persisted event cache
(union by `uid`, `deleted` tombstones remove), stores the new watermark, and
renders the merged set sorted by start time.  The tiny hand-rolled protobuf
codec is `main/lma_proto.c` (dependency-free — no malloc, no lwip — so it is
host-testable).

### Host self-test of the wire layer

```sh
cc -std=c11 -Wall -Wextra main/lma_proto_selftest.c main/lma_proto.c \
   -o /tmp/lma_selftest && /tmp/lma_selftest
```

`lma_proto_selftest.c` is host-only (defines its own `main`) and is not part
of the IDF build.

## Voice mode (XiaoZhi voice firmware + local bridge)

The same board can be reflashed into an always-on **AI voice assistant** — a
separate firmware plus host-side glue under `robodog_client/voice/`. This is
independent of the calendar firmware; `//robodog_client:flash_firmware`
restores the calendar app.

### Firmware flash (self-built from pinned upstream source, capture compiled in)

```sh
bazel run //robodog_client:flash_robodog_hermes   # build (Docker, if needed) + flash
```

This is the board's **only** sanctioned esptool path (button-free reset; the
Docker ttyACM RTS passthrough breaks it). The firmware is **no longer a
prebuilt blob**: `//robodog_client:build_robodog_hermes` fetches the *source*
of the exact XiaoZhi v2.4.2 release (`78/xiaozhi-esp32` @ pinned commit
`e8d8a40`, sha256-verified — `voice/fetch_xiaozhi.sh`) into the gitignored
`.xiaozhi/`, applies the **RoboDog capture hook**
(`voice/robodog_hermes_capture.patch` — streams the exact 400×300 ST7305 panel
memory as `RD_SHOT` frames over USB-JTAG, so the voice board is verifiable
pixel-for-pixel exactly like the calendar app), and builds with the ESP-IDF
**v6.0.x** Docker image (`XIAOZHI_IDF_IMG` overrides). Nothing vendored — the
staged source tree is never committed.

For an always-readable transcript the stock XiaoZhi screen (a centered
emoji/character **face** plus a single-line 30 px scrolling subtitle) is
replaced by the **RoboDog hermes text display**
(`voice/robodog_hermes_display.patch`): the face is removed, the chat prompt
and response use the **smaller 14 px noto font** and the full panel as their
canvas, and — with `CONFIG_USE_MULTILINE_CHAT_MESSAGE=y` baked into
`sdkconfig.defaults.robodog` — the text wraps onto many lines with real line
breaks instead of truncating, so the full prompt/response is visible on screen.

The board provisions itself over WiFi by POSTing the OTA URL configured in its
NVS by `tools`-style offline provisioning (external `provision_nvs.py`: reads
the WiFi PSK from netplan via sudo, writes the 16 KB NVS at `0x9000` with
`ssid`/`password` and `ota_url=http://<bridge-host>:8003/xiaozhi/ota/`). The
OTA reply gives the board the bridge WebSocket URL, so no xiaozhi.me
dependency.

### Screen-capturing the voice board (RD_SHOT)

Because the capture hook is compiled into the hermes firmware, the same
`capture.py` pipeline reads the voice board's panel:

```sh
bazel run //robodog_client:flash_robodog_hermes     # first time / after changes
python3 robodog_client/capture.py --port /dev/ttyACM0 --reset --n 8 --timeout 180 --describe
#   shots/frameN.i1               raw 15000-byte ST7305 panel memory (landscape)
#   shots/panel_landscape.png     400x300 — the logical screen the app drew
#   shots/frameN_analysis.txt     vision transcription (--describe)
```

Frames are auto-detected by their `RD_SHOT` header/buffer size (15000 B =
voice board 400×300 landscape LUT; 15200 B = calendar app 300×400 portrait),
and the voice-board vision prompt transcribes the XiaoZhi status/subtitle text
instead of calendar rows. Capture is throttled to one full frame per ~4 s so
the serial dump never interleaves or starves the realtime audio.

### Software button (RD_LISTEN)

The wake word is not reliable on this board, so the voice session is opened
with a **software button**: a small firmware hook (`voice/robodog_listen.patch`)
reads lines over the same USB-JTAG console and calls `Application::StartListening()`
(the exact event the BOOT button raises) when it sees `RD_LISTEN`:

```sh
python3 robodog_client/capture.py --port /dev/ttyACM0 --listen
# bridge journal: device connected session=... + listen start (mode=manual)
```

This requires the **primary console to be USB-Serial/JTAG** (full duplex,
input + output) rather than the IDF default UART0; that is baked into
`sdkconfig.defaults.robodog` in `voice/build_xiaozhi.sh` because the IDF
"secondary USB-Serial/JTAG" console is output-only. The trigger works from the
`idle` state only, and the host must keep the port open ~1 s before writing so
the USB-Serial/JTAG RX is armed (`--listen` already does this). `RD_LISTEN` is
compiled into every hermes build, like the capture hook.

### Voice backend (`robodog_client/voice/`)

| File | Purpose |
|------|---------|
| `fetch_xiaozhi.sh` | Fetch + sha256-verify the pinned upstream xiaozhi-esp32 **source** into the gitignored `.xiaozhi/` (no-vendoring, cache-backed). |
| `build_xiaozhi.sh` | Build the hermes firmware in the ESP-IDF v6.0 Docker image: board selection + capture hook + `idf.py build`. |
| `robodog_hermes_capture.patch` | The RD_SHOT capture hook applied to upstream `custom_lcd_display.cc` (compiled into every hermes build). |
| `robodog_hermes_display.patch` | The text-only transcript display: removes the emoji/character face, uses the smaller 14 px noto font, and lets the prompt/response wrap with line breaks over the full panel (with the `CONFIG_USE_MULTILINE_CHAT_MESSAGE=y` sdkconfig). |
| `voice_bridge.py` | XiaoZhi WebSocket bridge + OTA/`/healthz` server (`:8000` WS, `:8003` OTA). Negotiates the 16 kHz uplink for ASR, sends **24 kHz Opus** downlink (the board codec rate → no 16↔24 resample, no distortion). Server-side energy VAD finalizes a turn ~0.8 s after you stop talking — required because the device streams `realtime` and never sends `listen stop`. The **brain** is the board's Hermes session: each ASR'd utterance is submitted over a loopback brain WebSocket to the `xiaozhi` platform plugin inside the hermes-gateway (falls back to the stateless gx10 call if that's down). |
| `hermes-platform/xiaozhi/` | **Hermes platform plugin** (plugin.yaml + adapter.py + __init__.py) deployed to `~/.hermes/plugins/platforms/xiaozhi/`. Makes the board a first-class Hermes gateway client (see "Hermes gateway integration" below). |
| `lmao-robodog-voice.service` | systemd **user** unit for the bridge, run from `~/xiaozhi/` with a Python venv (`fastapi`, `uvicorn`, `websockets`, `opuslib`, `faster-whisper`, `numpy`). |
| `kokoro_server.py` + `kokoro-tts.service` | **Kokoro-82M** TTS (Apache-2.0, ~82M params, 24 kHz native) exposed as OpenAI `/v1/audio/speech` on the GPU host `spark-5307:8004`, also systemd-user-managed. |
| `lmao-robodog-voice.service` env | ASR = local faster-whisper `base`; brain = `HERMES_BRAIN_URL` (default `ws://127.0.0.1:8101/xiaozhi/brain`, the board's Hermes session; `DEVICE_ID` pins the identity, else hello device_id/mac or a persisted uuid); fallback LLM = `gx10-f156:8000/v1` (`deepseek-ai/DeepSeek-V4-Flash-0731`); TTS = `spark-5307:8004`; downlink = `DOWNLINK_PACE` (seconds per 60 ms Opus frame, default `0.0582` = ~97% real-time), so the 24 kHz reply is streamed at play-rate and the board's ~2.5 s decode pipeline never overflows (issue #267). |

Loop:

```
board --Opus/WS--> voice_bridge --PCM--> faster-whisper (local ASR)
                      --text--> xiaozhi Hermes session (memory/tools, device-id keyed)
                                + auto-TTS reply audio (Kokoro via Hermes tts.provider)
                                |__fallback__> gx10 vLLM (stateless, if brain down)
voice_bridge --24 kHz Opus--> board speaker
```

### Hermes gateway integration (xiaozhi platform)

The XiaoZhi bridge is the board's **realtime transport**; the **brain is Hermes**.
The `xiaozhi` platform plugin runs inside the hermes-gateway process and binds a
loopback brain WebSocket (default `ws://127.0.0.1:8101/xiaozhi/brain`):

```
board --Opus/WS:8000--> voice_bridge --ASR text--WS:8101--> xiaozhi adapter
                                                              |-> MessageEvent(VOICE, device_id)
                                                              |-> Hermes session + memory + tools
                                                              |-> auto-TTS reply (tts.provider: openai -> Kokoro)
voice_bridge <--voice/reply frames--WS:8101-- xiaozhi adapter <--send()/send_voice()
    |-> decode audio to 24 kHz PCM -> Opus frames -> board speaker
```

- **Identity / sessions**: the board's session and chat key is its stable Device-Id/MAC; reconnects (bridge restart, board reattach) reuse it, so the conversation history and memory survive.
- **Spoken replies**: inbound turns are VOICE-type, so Hermes auto-TTS fires by default for this platform (adapter-local `_should_auto_tts_for_chat` — the global `voice.auto_tts` stays off so telegram/matrix/signal are unaffected). `tts.provider: openai` points Hermes' TTS at **our Kokoro** (`~/.hermes/config.yaml` `tts.openai.base_url: http://spark-5307:8004/v1`, `voice: af_heart`) — the exact voice the board already uses. The reply audio returns to the bridge as a `voice` frame and is streamed to the board.
- **Fallback**: if the brain is unreachable the bridge synthesizes the `reply` text itself with Kokoro (stateless; no memory/tools). Last-resort it calls gx10 directly.
- **Config / deploy**: enable the platform in `~/.hermes/config.yaml` under `gateway.platforms.xiaozhi.enabled: true` + `plugins.enabled` (`platforms/xiaozhi`), set `XIAOZHI_ALLOW_ALL_USERS=true` (LAN device; `XIAOZHI_ALLOWED_USERS` for a device-id allow-list), then `systemctl --user restart hermes-gateway`. The board then appears in `hermes gateway status` (or the `Platforms:` line of `hermes status`) as **XiaoZhi Voice Companion**. The plugin's source of truth lives in the repo under `robodog_client/voice/hermes-platform/xiaozhi/`; deploy by copying it to `~/.hermes/plugins/platforms/xiaozhi/`.
- **Latency**: measured first-audio ~6.5 s after VAD finalize on the Hermes path (ASR + agent turn + auto-TTS), vs ~3–4 s for the thin stateless path. Accepted per the issue: the Hermes path earns sessions, multi-turn memory and tools; the stateless gx10 path stays as the fallback.

**TTS selection note.** The earlier CosyVoice2 server on `spark-5307:8002`
returns non-speech for every input on this deployment: it pins
torch 2.3.1 / transformers 4.51.3, which cannot be satisfied on the ARM64
GB10 host (runs torch 2.11 / transformers 5.20.1), and that version skew
deterministically garbles the Qwen text backbone. Kokoro-82M avoids the whole
dependency class (no torch needed for inference) and is the working voice —
verified by feeding its output back through ASR (no human round-trip).

**Usage.** Right (BOOT) button toggles listening; stop talking for ~0.8 s and
the bridge auto-processes and speaks. Full-duplex: just talk again for the
next turn. Logs: `journalctl --user -u lmao-robodog-voice -f`.

## ESP-Claw standalone agent (Phase 1 text UI + Phase 2 voice loop)

`robodog_client/esp-claw/` stages **esp-claw** (`edge_agent`) for the same
RLCD-4.2 board — the board calls its own ASR / LLM / TTS endpoints over LAN,
no bridge, no Hermes. Phase 0 (spike) + Phase 1 (PR #278 text-only chat UI +
deterministic RD_SHOT) are implemented and hardware-verified; **Phase 2 adds the
native on-device voice loop** (`rd_listen` → ES7210 mic → energy-VAD → Whisper
ASR → claw agent LLM → Kokoro TTS → ES8311 speaker → RLCD render). See
`robodog_client/esp-claw/DESIGN.md` for the full details + fixes. TL;DR:

```sh
# fetch pinned esp-claw -> gitignored robodog_client/.esp-claw/
bash robodog_client/esp-claw/fetch_esp_claw.sh

# build in espressif/idf:v5.5.4 (endpoints overridable per-build)
CLAW_IDF_IMG=espressif/idf:v5.5.4 \
CLAW_ASR_URL=http://192.168.50.153:11801/v1/audio/transcriptions \
CLAW_TTS_URL=http://192.168.50.153:11804/v1/audio/speech \
bash robodog_client/esp-claw/build_esp_claw.sh

# flash (host esptool — the board's sanctioned path) + provision NVS
# (WiFi + LLM + cfg_ver schema key) at 0xA000
CLAW_IDF_IMG=espressif/idf:v5.5.4 bash robodog_client/esp-claw/flash_esp_claw.sh
```

Bazel wrappers: `bazel run //robodog_client:build_robodog_claw` and
`:flash_robodog_claw`. The board boots a deterministic **text-only chat UI**
(PR #278 parity: no face/emote, ~14 px ASCII font, left-aligned full-panel
wrap, WiFi/agent status bar on top). The UI brings the RLCD panel + LVGL up
itself (esp-claw's own display_service is gated behind a Kconfig symbol this
board silently drops), so the RD_SHOT capture seam is **always** live:
`capture.py --describe` reliably transcribes the rendered frame (no more
"no panel frame captured yet"). After a text update the UI forces a
synchronous refresh + immediate RD_SHOT dump, so a capture reads the new
frame deterministically.

On-device probe console commands (over `/dev/ttyACM0` 115200, `app> ` REPL):

```sh
rd_asr                 # device -> Whisper via host-forward, logs text="..."
rd_tts <text>          # device -> Kokoro via host-forward, logs WAV bytes
rd_chat <question>     # native agent seam -> LLM, renders user + reply to the
                       #   text UI (e.g. `rd_chat 2+2` -> screen shows "4")
rd_listen              # Phase 2 voice loop: mic -> ASR -> agent -> TTS -> speaker
rdshot                 # force-publish the current screen frame (RD_SHOT)
```

Phase 2 full-loop hardware gate (drives the corpus WAV into the board mic via
the test rig's ALSA `hw:2` and asserts each leg from the device serial
console + an RD_SHOT vision readback):

```sh
python3 robodog_client/esp-claw/robodog_claw_e2e.py --corpus capabilities
# OPEN_SESSION / ASR_MATCH / REPLY / RING / SUBTITLE -> Overall: PASS
```

Mutually exclusive with the calendar + hermes voice firmwares — flashing
esp-claw replaces them on this board.

## Layout

```
robodog_client/
├── CMakeLists.txt            project() entry
├── sdkconfig.defaults        ESP32-S3: 16M flash, octal 8M PSRAM, wifi, PM
├── partitions.csv            roomy 8M factory app
├── components/
│   ├── u8g2/                 vendored u8g2 monochrome library (verbatim)
│   └── u8g2_st7305/          vendored Waveshare ST7305 driver (verbatim)
└── main/
    ├── main.c                app_main shim → robodog_app_main()
    ├── robodog.c             wifi/tcp/sleep loop + u8g2 rendering
    ├── lma_proto.c/.h        dependency-free protobuf codec (wire layer)
    ├── lma_proto_selftest.c  host-only self-test binary
    ├── config.h              pins, wifi/server/sync configuration
    └── CMakeLists.txt        component registration
```

## Status

Hardware-validated on the Waveshare RLCD-4.2 (built in the ESP-IDF Docker
image, flashed to `/dev/ttyACM0`, gRPC-synced live data, and the render
confirmed **pixel-exact** against the physical panel via `capture.py` — the
captured frame matched what the user sees on the RLCD exactly, orientation and
polarity included).  The header shows the live date once synced; rows are real
`GetCalendar` events.

Still environment-dependent / worth a look on real air:
- **WiFi**: credentials are NVS/built-in (`wifi_ssid`/`wifi_pass`); the board
  is 2.4 GHz-only, so the AP must be 2.4 GHz and reachable from the node.
- **Frame/width tuning**: title clipping and the icon boxes are tuned for the
  current data — adjust with the screenshot loop below. The wrapped headline
  band (top margin / meta gap) is tuned via `TITLE_TOP_MARGIN` /
  `TITLE_META_GAP` in `render_screen`. (Row count is a fixed cap: the
  headline picture + 4 compact text rows = 5 event rows at `RD_MAX_EVENT_ROWS`,
  capture-tuned yy=214 / row_stride=20.)
- **Per-board delta-safety**: the NVS event cache and watermark are owned by
  the kid the board last synced under (`wm_kid`).  Re-flashing with a
  different `--kid-name` (or set↔family) clears the cache on boot so a
  re-assigned board never shows the previous kid's cached rows in the
  pre-sync boot frame.

## Screenshot-driven validation (feature + state)

Every UI/data change is validated from the **panel pixels**, not a
re-render.  The capture hook is compiled into every build (it must confirm the
current production firmware's render — no separate opt-in build); it streams
the exact full-frame buffer after each render — for each frame it reconstructs
`panel_raw_portrait.png` (300×400 panel memory) and `panel_landscape.png` (as
seen in landscape) so a change is verifiable pixel-for-pixel.

```bash
# capture is built into every firmware — just build+flash normally:
bash ./build.sh
bash flash.sh --port /dev/ttyACM0

# --kid-name makes the board a kid-dedicated panel (one image, per-board NVS
# assignment):
bazel run //robodog_client:flash_firmware -- --port /dev/ttyACM0 --kid-name juliet
bazel run //robodog_client:flash_firmware -- --port /dev/ttyACM1 --kid-name wesley
#   non-empty --kid-name writes the board's NVS `kid_name` (robodog namespace)
#   at 0x9000 (24 KB partition, generated by IDF nvs_partition_gen.py in the
#   same Docker image that builds); ``--kid-name ""`` / no flag leaves NVS
#   untouched (family mode via the pinned RD_KID_NAME_DEFAULT build default).
#   NVS wipe caveat: writing 0x9000 erases the WHOLE NVS partition (event
#   cache + any wifi/server keys).  Desired for re-assignment delta-safety
#   (the board refetches; network config falls back to local_secrets.h
#   defines), but do not store anything else in this board's NVS.

# capture: reset the board, then read frames; make --n/--timeout wide enough
# that WiFi connect + first gRPC sync happen (sync landed ~40s post-boot here)
python3 capture.py --port /dev/ttyACM0 --reset --n 4 --timeout 100 --describe
#   shots/frameN.i1                raw 15200-byte u8g2 buffer (panel memory)
#   shots/panel_raw_portrait.png   panel-native 300×400
#   shots/panel_landscape.png      400×300 as seen in landscape
#   shots/frameN_analysis.txt      vision-model transcription (--describe)
```

**Reading the device state from the screenshot:**
- Header (family): centered `YYYY-MM-DD HH:MM` present → WiFi up + gRPC sync
  healthy.  Header (kid panel): kid name LEFT + `YYYY-MM-DD HH:MM` RIGHT on the
  same row — same health signals.  (No divider under the header.)
- Header **BLANK** + `no wifi` hint beside the first row → not connected yet:
  pre-sync boot frame, or WiFi/AP unreachable.
- `stale` hint → connected but last sync failed (showing NVS cache).
- Row set == Postgres/`GetCalendar` output → data path live; a stale/different
  set means the cache is being shown (compare timestamps against the header).
- At most 5 event rows are drawn: the headline (big picture, LEFT;
  word-wrapped `helvB18` title block + right-aligned `HH:MM-HH:MM` range meta
  beside it) plus 4 compact text rows, each title-clipped left with a
  measured,
  right-aligned `HH:MM-HH:MM` range meta; a 6th+ visible event is dropped by
  design (`RD_MAX_EVENT_ROWS` in `render_screen`) — a shorter row set is not a
  data-path error unless the header hints (`stale`/`no wifi`) say otherwise.
- Non-today events prefix their right-hand meta with a day qualifier so the
  family panel reads unambiguously: `tomorrow` for the next civil day,
  `MM/DD` for a later day in the same civil year, `YYYY/MM/DD` for a later
  day in a different year; today's events show the bare time range. The
  qualifier is keyed off the event **start** civil day (see
  `rd_datetime.h`), and the header clock (`YYYY-MM-DD HH:MM`) is the "today"
  anchor — rows render range-only while the clock is still unset (pre-first
  sync).

**Validation loop after a change** (layout, status hints, row logic,
data path): build + flash normally (capture is compiled in), run the capture
command above, and read the state from `frameN_analysis.txt` + the PNGs.

**Vision readback (so the agent can reason):** `--describe` sends the
landscape frame to the same self-hosted vision model hermes uses —
Qwen2.5-VL at `spark-eebf` (`~/.hermes/config.yaml` `vision`; override with
`RD_VISION_BASE_URL` / `RD_VISION_MODEL`) — which transcribes the header clock
and every row.  The agent reads that transcription (e.g.
"2026-10-14 23:19 / Smoke test: Dinner 00:27 / …") instead of guessing from
the XML/model, so iteration is driven by what the panel really shows.

The raw buffer is the literal ST7305 payload (304×50 tiles = 300×400), so
there is no interpretation between "what the firmware drew" and "what you see".

