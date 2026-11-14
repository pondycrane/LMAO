# robodog_client

Native **ESP-IDF** firmware for the **RoboDog** family-calendar display:
a Waveshare **ESP32-S3** dev board driving the Waveshare **RLCD-4.2**
(**ST7305**, 400×300 reflective 1-bit B&W LCD).  No RNS/LXMF here: the device
reads the family calendar over the server's **framed-TCP calendar service
(:4247)** and renders it in 1-bit B&W on the ST7305.

```
┌─ LMAO server (framed-TCP calendar read, :4247) ─────────┐
│  [u32 BE len][CalendarRequest] -> [u32 BE len][Bundle]  │
└──────────────────────────────────────────────────────────┘
            ▲ plain lwIP socket, one request/reply per sync
ESP32-S3 ───┘   → decode → merge δ into NVS cache → render ST7305
                → hold the frame ~60 min (light sleep; redraw on a new minute) → repeat
```

**Power.** The hold is not an active spin. `esp_pm_configure()` runs DFS at
80/40 MHz, and the hold loop calls `esp_light_sleep_start()` **explicitly** for
each tick when no USB host is attached, so the board sits at ~1 mA instead of
~40-80 mA between syncs (WiFi is stopped for the hold; the panel is fed from the
board rail and keeps its GRAM, so the picture stays up). Automatic light sleep
(`CONFIG_FREERTOS_USE_TICKLESS_IDLE`) is deliberately **off**: USB-Serial/JTAG
cannot survive light sleep, and the idle task would sleep during the boot dump
before the host had enumerated, which tore down the CDC interface permanently
(the board then stops enumerating until a power cycle — observed on the bench).
Sleeping explicitly only after `usb_serial_jtag_is_connected()` says "no host"
keeps the capture/console path safe. The RD_SHOT dump is likewise gated on a
host being attached: on battery it is skipped, which removes ~31 KB of console
traffic per render. **Caveat:** on battery the USB console is unavailable while
the panel sleeps (the USB-JTAG peripheral is off across light sleep); attach USB
before powering/resetting the board to capture. See `main/config.h`
(`RD_SYNC_INTERVAL_SECONDS`, `RD_CLOCK_TICK_MS`, `RD_PM_MAX_FREQ_MHZ`) and
`build.sh`'s stale-`sdkconfig` guard.

**Transport decision.** An earlier build spoke a hand-rolled HTTP/2 + gRPC
client for `lma.LMAO/GetCalendar` on `:50051`.  That bought nothing for one
unary RPC and could not decode response HEADERS, so a non-zero `grpc-status`
was indistinguishable from an empty reply; any proxy or server-side HTTP/2
change would have broken it silently.  The framed-TCP service carries the same
protobuf messages with a 4-byte length prefix, and the server assembles the
reply with the SAME shared helper as the gRPC and LXMF paths
(`lma_core/calendar_bundle.build_bundle`) — so the transports cannot drift
(the old TCP copy had no full-fetch tombstone filter and never attached
`pic_data`).

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

The NVS key names have ONE definition (`main/rd_store.h`), shared with the
flash scripts.  Other knobs (`RD_CAL_TCP_PORT`, `RD_SYNC_INTERVAL_SECONDS`,
`RD_SNTP_SERVER`, `RD_TCP_CONNECT_TIMEOUT_MS`/`RD_TCP_READ_TIMEOUT_MS`,
`RD_WDT_TIMEOUT_S`) are build-time macros in `main/config.h`.

## Wire contract (implemented exactly)

Framed-TCP to `LMAO_SERVER_HOST:RD_CAL_TCP_PORT` (default `4247`, the server's
`LMAO_CAL_TCP_PORT`; host is a LAN hostname, never an IP — tp4's wlan0 is
DHCP and has moved):

```
client -> server   [u32 BE length][CalendarRequest protobuf]
server -> client   [u32 BE length][CalendarBundle protobuf]
```

A 0-length request frame is the documented defaults (`calendar_id:"family"`,
`since_ms:0`).  Connect and read budgets are separate
(`RD_TCP_CONNECT_TIMEOUT_MS` for a non-blocking connect + `select`;
`RD_TCP_READ_TIMEOUT_MS` as both the per-recv socket timeout and a total
deadline), so a slow-drip server cannot stall a whole cycle, and a task
watchdog (`RD_WDT_TIMEOUT_S`, default 60 s) reboots a wedged board instead of
leaving a frozen panel.

- **Client → server** payload = serialized `CalendarRequest` protobuf
  (`calendar_id=1` string, `since_ms=2` varint u64, `max_events=3` varint u32,
  `participant=4` string — kid name, omitted when empty/family).
  A 0-length payload = the documented defaults `{calendar_id:"family",
  since_ms:0}` (no participant = family filter).
- **Server → client** payload = serialized `CalendarBundle` protobuf
  (`format=1` varint u32, `calendar_id=2` string, `events=3` repeated
  `CalEvent`, `watermark_ms=4` varint u64, `current=5` `Weather`).
- `CalEvent`: `uid=1` string, `title=2` string, `notes=3` string,
  `start_ms=4` varint u64, `end_ms=5` varint u64, `deleted=6` varint bool,
  `pic=7` string, `pic_data=8` bytes (1bpp **176×176** tile, 3872 B),
  `weather=9` `Weather`.  `pic_data` is served on the single headline event
  only; the decoder keeps ONE tile at bundle level (`pic_uid` binds it to its
  event) instead of a per-event 3872-byte slot.
- `Weather`: `temp_c10=1` **sint32** (zigzag; tenths of a degree Celsius, so
  −2.5 °C is `-25`) and `code=2` varint u32 (WMO 4677 weather code).
  `CalendarBundle.current` is the conditions now; `CalEvent.weather` is the
  forecast bucket containing that event's `start_ms`. **An absent `Weather`
  means "no forecast for this slot"** (weather disabled server-side, or an
  event past the 16-day hourly horizon) and the panel draws nothing — never
  a placeholder `0 °C`. `lma_proto` tracks this as `lma_weather_t.present`.

**Weather.** The panel prints the current conditions and the forecast at the
first event's start. Both ride this same reply (no extra request, no extra
connection): the server fills `CalendarBundle.current` and each
`CalEvent.weather` from Open-Meteo when `LMAO_WEATHER_LAT`/`LMAO_WEATHER_LON`
are set on the deployment. The device renders them with
`rd_weather.c` (host-tested by `rd_weather_selftest.c`): the wire carries
tenths of a degree Celsius, and the panel shows whole degrees Fahrenheit
(`44°F`) plus a short WMO label (`clear`, `partly cloudy`, `drizzle`, `snow
showers`, …). Weather is **never persisted**: `CalendarBundle.current` lives
in RAM only, so a boot render or an offline cycle never shows a stale reading.

Refresh cadence differs between the two slots, and it is worth knowing why:

- **`now`** is bundle-level, so it is NOT gated by the delta — it arrives on
  every reply, i.e. every `RD_SYNC_INTERVAL_SECONDS` (60 min).  The server's
  15-min cache means a sync triggers a fresh Open-Meteo fetch, so the on-screen
  reading is ~0-75 min old (typically 15-75).  The minute-tick redraw inside the
  hold loop only advances the header clock; the temperature does not change
  until the next sync.
- **The headline event's forecast** IS gated by the delta: a row only re-ships
  when its `updated_ms` moves.  The hourly in-cluster calendar-sync CronJob
  (`k8s/calendar-sync.yaml`) touches every in-window row on each run, so an
  event's forecast advances hourly as its date approaches.  If that CronJob is
  stopped, per-event weather freezes on the panel.

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

Frames are auto-detected by buffer size (15000 B = voice board landscape LUT;
15200 B = u8g2 tiles — the calendar app and the home env client share that
size AND the same `U8G2_R1` landscape rotation, so they decode identically).
The voice-board frame uses its own vision prompt (XiaoZhi status/subtitle text,
CJK); the u8g2 frames use one prompt that transcribes every drawn line and
lets the gate's assertions pick what matters (clock + rows for the calendar,
name + reading + status for the env panel). Capture is throttled to one full
frame per ~10 s so the serial dump never interleaves or starves the realtime
audio.

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
| `lmao-robodog-voice.service` env | ASR = local faster-whisper `base`; brain = `HERMES_BRAIN_URL` (default `ws://127.0.0.1:8101/xiaozhi/brain`, the board's Hermes session; `DEVICE_ID` pins the identity, else hello device_id/mac or a persisted uuid); fallback LLM = `192.168.50.3:8000/v1` (`qwen3.8-flash-next`, spark-73dd — the stable always-on endpoint); TTS = `spark-5307:8004`; downlink = `DOWNLINK_PACE` (seconds per 60 ms Opus frame, default `0.0582` = ~97% real-time), so the 24 kHz reply is streamed at play-rate and the board's ~2.5 s decode pipeline never overflows (issue #267). |

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
- **Spoken replies**: inbound turns are VOICE-type, so Hermes auto-TTS fires by default for this platform (adapter-local `_should_auto_tts_for_chat` — the global `voice.auto_tts` stays off so telegram/matrix/signal are unaffected). `tts.provider: openai` points Hermes' TTS at **our Qwen3-TTS** (`~/.hermes/config.yaml` `tts.openai.base_url: http://spark-5307:8004/v1`, `model: qwen3-tts`, `voice: ""`) — the same server the board calls. An empty voice makes the server pick a **native speaker of the reply's language** (English → `aiden`, Chinese → `dylan`, the Beijing-native timbre); Qwen3-TTS timbres have a native language baked in, so `af_heart`/`serena` reading English carries a Chinese accent that `instruct` cannot remove; `dylan` keeps Chinese on one fixed Beijing accent. The reply audio returns to the bridge as a `voice` frame and is streamed to the board.
- **Fallback**: if the brain is unreachable the bridge synthesizes the `reply` text itself with Qwen3-TTS (stateless; no memory/tools). Last-resort it calls gx10 directly.
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
ASR → claw agent LLM → Qwen3-TTS → ES8311 speaker → RLCD render), and it is
**dual-language**: English and Mandarin/Traditional Chinese speech are heard,
answered, displayed and spoken in their own language (see "Dual-language voice"
below). See `robodog_client/esp-claw/DESIGN.md` for the full details + fixes.
TL;DR:

```sh
# fetch pinned esp-claw -> gitignored robodog_client/.esp-claw/
bash robodog_client/esp-claw/fetch_esp_claw.sh

# The RLCD face (Noto Sans CJK TC, 16 px, 1 bpp) is generated at build time by
# esp-claw/gen_font_tc_16.sh: needs Node.js >= 18 (npx lv_font_conv@1.5.2) and
# network on the FIRST build; the OTF (sha256-pinned) + the generated font are
# cached in ~/.cache/lmao-fonts afterwards. Never committed.

# build in espressif/idf:v5.5.4 (endpoints overridable per-build)
# Endpoint defaults are committed (spark-5307 = 192.168.50.27 for Whisper/
# Qwen3-TTS, spark-73dd = 192.168.50.3 for qwen3.8-flash-next); all three are on
# the board's own subnet, so no host socat forward is needed. Override with
# CLAW_ASR_URL / CLAW_TTS_URL / CLAW_LLM_BASE_URL / CLAW_LLM_MODEL if they move.
# CLAW_TTS_VOICE defaults to EMPTY: per-language native timbres
# (English -> aiden, Chinese -> dylan, the Beijing-native timbre), so the
# board does not read English with a Chinese accent. Every TTS request carries
# CLAW_TTS_SEED (default 8) and the Beijing instruct (CLAW_TTS_INSTRUCT_ZH),
# so a chunked reply keeps one voice/accent across its sentences.
# Override with CLAW_TTS_VOICE_EN / CLAW_TTS_VOICE_ZH (per language) or
# CLAW_TTS_VOICE=<timbre> (pin one voice for every reply).
CLAW_IDF_IMG=espressif/idf:v5.5.4 bash robodog_client/esp-claw/build_esp_claw.sh

# flash (host esptool — the board's sanctioned path) + provision NVS
# (WiFi + LLM + cfg_ver schema key) at 0xA000
CLAW_IDF_IMG=espressif/idf:v5.5.4 bash robodog_client/esp-claw/flash_esp_claw.sh
```

Bazel wrappers: `bazel run //robodog_client:build_robodog_claw` and
`:flash_robodog_claw`. The board boots a deterministic **text-only chat UI**
(PR #278 parity: no face/emote, 16 px **Noto Sans CJK TC** font generated at
build time by `esp-claw/gen_font_tc_16.sh` from the sha256-pinned `noto-cjk`
OTF — Traditional Chinese renders as real glyphs, English too; left-aligned
full-panel wrap, WiFi/agent status bar on top). The UI brings the RLCD panel +
LVGL up itself (esp-claw's own display_service is gated behind a Kconfig
symbol this board silently drops), so the RD_SHOT capture seam is **always**
live:
`capture.py --describe` reliably transcribes the rendered frame (no more
"no panel frame captured yet"). After a text update the UI forces a
synchronous refresh + immediate RD_SHOT dump, so a capture reads the new
frame deterministically.

On-device probe console commands (over `/dev/ttyACM0` 115200, `app> ` REPL):

```sh
rd_asr                 # device -> Whisper, logs text="..." + language=.../prob
rd_tts <text>          # device -> Qwen3-TTS, logs WAV bytes
rd_chat <question>     # native agent seam -> LLM, renders user + reply to the
                       #   text UI (e.g. `rd_chat 2+2` -> screen shows "4")
rd_listen              # Phase 2 voice loop: mic -> ASR -> agent -> TTS -> speaker
                       #   (or press the BOOT button — same trigger)
rdshot                 # force-publish the current screen frame (RD_SHOT)
```

> The serial REPL is **ASCII-only input**: the IDF console's linenoise runs in
> dumb mode on USB-Serial/JTAG and its `sanitize()` drops every byte with
> `isprint((char)c) == 0`, i.e. all UTF-8 lead/continuation bytes — typing
> `rd_chat 你好` reaches the command as `rd_chat` (and `rd_chat 你好a` as
> `rd_chat a`).  The panel still *renders* Chinese fine (reply text is UTF-8
> from the LLM), so drive Chinese by asking in ASCII for a Chinese reply —
> `rd_chat Reply in Traditional Chinese: what is the weather in Taipei?` — or
> use the real voice/HTTP paths, which carry UTF-8 end to end.

### Dual-language voice (English + Traditional Chinese)

The board sends **no `language` field** with the ASR upload, so the Whisper
endpoint auto-detects the spoken language and returns `language`/`probability`
alongside `text` — Chinese speech is transcribed as Chinese, never forced
through an English model. The voice turn then:

1. logs `ASR language=<tag> prob=<p>` (console evidence the E2E gates on);
2. selects the reply directive by that tag (falling back to CJK script
   detection when an older endpoint omits it): a Chinese utterance gets
   **`voice reply language=zh-TW`** — *reply in Traditional Chinese (Taiwan
   usage), Traditional characters only, spoken prose, no markdown* — everything
   else gets the English prose directive. **Both directives name the language
   explicitly**: the agent's session memory carries earlier turns, so a Chinese
   conversation would otherwise make the model answer a later English question
   in Chinese (measured on the English gate). Whisper may transcribe in
   Simplified, so the Traditional rule must also be explicit or the model
   mirrors the transcript;
3. renders the reply with the generated Noto Sans CJK TC font (UTF-8 is kept
   byte-for-byte; only emoji/exotic scripts become `?`), and the chunk
   splitter never cuts a CJK character in half;
4. speaks it through Qwen3-TTS with the turn's **explicit language and
   per-language native timbre** (`dylan`, Beijing-native, for Chinese;
   `aiden` for English), plus a **fixed seed** (default 8) and the Beijing
   `instruct`. The server would otherwise infer each chunk independently and
   re-sample prosody per call: a short English-only chunk inside a Chinese
   reply could flip timbre, and the same Chinese reply re-realized its accent
   on every chunk. Taiwan has several Mandarin varieties, so the instruct
   names the Beijing accent rather than a generic "standard Mandarin". The
   turn sets the language once from the ASR result
   (`TTS request language=<lang> voice=<timbre> seed=<n>`) and every chunk
   carries it. Pin `CLAW_TTS_VOICE=<timbre>` only if one voice must serve
   every reply.

The session title (top-middle label) is built CJK-aware (9 Han chars fit the
170 px label, 18 Latin), so a Chinese first question titles the session instead
of falling back to `S#<id>`. The E2E gates this path with the pinned
`zh_hello` corpus (`你好世界`, spoken by `serena`, `language: chinese`):

```sh
python3 robodog_client/esp-claw/robodog_claw_e2e.py --corpus zh_hello
# OPEN_SESSION / ASR_MATCH / ASR_LANG(zh) / REPLY_LANG(zh-TW) / REPLY(CJK)
# / TTS / RING / SUBTITLE(CJK readback) -> Overall: PASS
```

The corpus phrase uses only characters identical in Simplified and Traditional
(`你好世界`) because faster-whisper may emit either script; a script-dependent
phrase would make ASR_MATCH flap. Regenerate the fixtures with
`python3 robodog_client/voice/make_corpus.py [--only zh_hello]` (needs the TTS
endpoint).

The RLCD status bar shows WiFi (left) and the live voice state **top-right**:
`idle` → `listening` → `processing` (ASR+LLM) → `responding` (TTS) → `idle`
(`error` for a real fault, `truncated` when a reply exceeded the chunk cap).
A tap of the BOOT button (or `rd_listen`) always starts a listen — the press
is debounced, not the release, so a quick tap works; holding it ≥ 1.5 s
clears the conversation session instead.  The utterance buffer grows in 2 s
steps while you speak, so a listen never fails on a large contiguous SPIRAM
allocation; a capture fault ends in `error`, never a silent `idle`.

Phase 2 full-loop hardware gate (drives the corpus WAV into the board mic via
the test rig's ALSA `hw:2` and asserts each leg from the device serial
console + an RD_SHOT vision readback):

```sh
python3 robodog_client/esp-claw/robodog_claw_e2e.py --corpus capabilities
# OPEN_SESSION / ASR_MATCH / ASR_LANG / REPLY_LANG / REPLY / RING / SUBTITLE
# -> Overall: PASS
```

Multi-session stress gate (≥ 5 back-to-back sessions, different question
each; catches "first turn works, later turns drop back to idle"):

```sh
bazel run //robodog_client:claw_stress -- --runs 5
# OPEN_SESSION / ASR_MATCH / ASR_LANG / REPLY_LANG / REPLY / TTS
# / CHUNKED_QUEUE / PIPELINE_AHEAD / GAP_VERIFY / FULL_READOUT
# / NO_COLLAPSE / NO_RESET -> Overall: PASS
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
    ├── robodog.c             app loop: sync → merge → render → hold (WDT-armed)
    ├── lma_proto.c/.h        dependency-free protobuf codec (wire layer)
    ├── lma_proto_selftest.c  host-only self-test binary
    ├── config.h              pins, wifi/server/sync configuration
    ├── rd_store.c/.h         NVS config + schema-versioned event cache
    ├── rd_net.c/.h           WiFi + framed-TCP calendar transport
    ├── rd_render.c/.h        ST7305 render + RD_SHOT capture
    ├── rd_layout.c/.h        panel geometry + fit arithmetic (host-tested)
    ├── rd_clip_selftest.c    host-only clip/wrap test
    ├── rd_layout_selftest.c  host-only layout test
    └── CMakeLists.txt        component registration
```

## Status

Hardware-validated on the Waveshare RLCD-4.2 (built in the ESP-IDF Docker
image, flashed to `/dev/ttyACM0`, framed-TCP-synced live data, and the render
confirmed **pixel-exact** against the physical panel via `capture.py` — the
captured frame matched what the user sees on the RLCD exactly, orientation and
polarity included).  The header shows the live date once synced; rows are real
calendar events.

**Layout (400×300 landscape canvas).** Geometry lives in ONE block
(`main/rd_layout.h`) and is drawn at those positions only. One 4 px outer
margin, one right edge, one rule under the header:

| Band | Content |
|---|---|
| header (baseline 22) | calendar identity LEFT (`Family`, or the kid name on a kid panel, first letter capitalised for display; clipped to the space the clock leaves) + live clock `YYYY-MM-DD HH:MM` RIGHT |
| rule | 1 px border at y=48 under the header band |
| status (baseline 40) | `now` + **weather icon** (16×16) + `37°F` LEFT; state word + shape RIGHT — filled square `updated`, hollow square `stale`, crossed box `no wifi` |
| headline | 176×176 served tile at x=4/y=52; right column (204 px): wrapped `helvB18` title block (4 lines), `HH:MM-HH:MM` range meta at baseline 200, the event's forecast as icon + `43°F` right-aligned at 222. **No served tile ⇒ the art slot is dropped and the headline takes the full width** (never a bare placeholder frame) |
| rule | 1 px border at y=231 above the compact section, drawn only when compact rows exist |
| compact rows | `9x15B` rows from baseline 246, stride 16, up to 4 rows (headline + 4 = 5 events) |

The condition is a **shape, not a word**: seven 16×16 icons drawn from u8g2
primitives (`wx_sun`, `wx_sun_cloud`, `wx_cloud`, `wx_fog`, `wx_rain`,
`wx_snow`, `wx_thunder` in `rd_render.c`), mapped from the WMO code by
`rd_weather_icon_kind` (sun 0/1, sun+cloud 2, cloud 3, fog 45/48, rain
51-67+80-82, snow 71-77+85/86, thunder 95/96/99). Intensity (drizzle vs rain,
light vs heavy snow) is not drawn — at 16 px it is a few pixels of noise; the
temperature carries the magnitude. An unmapped code draws **no icon** and still
prints the reading. The temperature text stays on both lines: a reading is the
primary element, only the long label is gone.

State is carried by word **and** shape on every frame (a synced panel says
`updated`, it does not stay silent), and empty/offline frames show waiting
words (`waiting for wifi` / `waiting for calendar` / `nothing upcoming`)
instead of a bare blank.

The number of title lines and compact rows is COMPUTED from the measured font
metrics (`rd_layout_title_lines` / `rd_layout_rows`, host-tested by
`rd_layout_selftest.c`), so a font or geometry change cannot silently push text
off the panel.

Still environment-dependent / worth a look on real air:
- **WiFi**: credentials are NVS/built-in (`wifi_ssid`/`wifi_pass`); the board
  is 2.4 GHz-only, so the AP must be 2.4 GHz and reachable from the node.
- **Frame/width tuning**: title clipping is measured under the active font
  (`rd_clip`), so a clipped title always fits its budget; adjust the bands in
  `rd_layout.h` and re-capture.
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
`panel_raw_portrait.png` (300×400 panel memory) and `panel_landscape.png` (the
400×300 logical screen: `U8G2_R1` is fixed, so the landscape view is one
deterministic rotation, not an OCR guess) so a change is verifiable
pixel-for-pixel.

```bash
# capture is built into every firmware — just build+flash normally:
bash ./build.sh
bash flash.sh --port /dev/ttyACM0

# Credentials come from the environment (or a local, gitignored
# main/local_secrets.h, which config.h does NOT include). They are written into
# the board's NVS at flash time — never compiled into the image:
LMAO_WIFI_SSID=... LMAO_WIFI_PASS=... \
  bazel run //robodog_client:flash_firmware -- --port /dev/ttyACM0

# --kid-name makes the board a kid-dedicated panel (one image, per-board NVS
# assignment):
LMAO_WIFI_SSID=... LMAO_WIFI_PASS=... \
  bazel run //robodog_client:flash_firmware -- --port /dev/ttyACM0 --kid-name juliet
LMAO_WIFI_SSID=... LMAO_WIFI_PASS=... \
  bazel run //robodog_client:flash_firmware -- --port /dev/ttyACM1 --kid-name wesley
#   --kid-name writes the board's NVS `kid_name` (robodog namespace) at 0x9000
#   (24 KB partition, generated by IDF nvs_partition_gen.py in the same Docker
#   image that builds); ``--kid-name ""`` writes the config WITHOUT kid_name
#   (clears a board back to family mode).
#   NVS wipe caveat: writing 0x9000 erases the WHOLE NVS partition (event cache
#   + any wifi/server keys).  The image therefore ALWAYS carries the
#   wifi_ssid/wifi_pass/server_host keys, so the board stays networked and the
#   calendar cache simply refetches.  Any NVS write requires credentials from
#   --wifi-ssid/--wifi-pass, LMAO_WIFI_SSID/LMAO_WIFI_PASS, or the local
#   main/local_secrets.h — with none the script refuses rather than flashing a
#   board that boots with no network.  No NVS flags at all leaves NVS untouched
#   (a local secrets file is a VALUE source, never a request to wipe)
#   (family mode via the pinned RD_KID_NAME_DEFAULT build default).

# capture: reset the board, then read frames; make --n/--timeout wide enough
# that WiFi connect + first calendar sync happen (sync landed ~40s post-boot here)
python3 capture.py --port /dev/ttyACM0 --reset --n 4 --timeout 100 --describe
#   shots/frameN.i1                raw 15200-byte u8g2 buffer (panel memory)
#   shots/panel_raw_portrait.png   panel-native 300×400
#   shots/panel_landscape.png      400×300 as seen in landscape
#   shots/frameN_analysis.txt      vision-model transcription (--describe)
```

**Reading the device state from the screenshot:**
- Header always draws its LEFT identity label (`family`, or the kid name on a
  kid panel) and the live `YYYY-MM-DD HH:MM` clock RIGHT once synced — clock
  present ⇒ WiFi up + TCP sync healthy.  (Before the first sync the label is
  drawn alone; there is no clock yet.)
- Status band RIGHT is the state channel, always drawn as word + shape:
  filled square `updated` (fresh), hollow square `stale` (last sync failed,
  showing the NVS cache), crossed box `no wifi` (not connected).
- `waiting for wifi` / `waiting for calendar` body text ⇒ no cached events and
  no fresh reply; `nothing upcoming` ⇒ the reply was fine but every event has
  already ended.
- Weather: the status band's LEFT shows `now` + an icon + `37°F` → the reply
  carried `CalendarBundle.current`; the headline event's forecast is the same
  icon + bare reading (`43°F`), right-aligned in the info column under its range
  meta, so it reads as part of the event block (title, when, weather then)
  rather than a second current reading. **Neither line is drawn** when the
  server sent no `Weather` (weather disabled / no sync yet), so a blank band is
  "no forecast", not "0°F".  The icon is a shape, so verify it by eye on the
  PNG; the readback only transcribes the temperature.
- Row set == Postgres/`CalendarBundle` output → data path live; a stale/different
  set means the cache is being shown (compare timestamps against the header).
- At most 5 event rows are drawn: the headline (176×176 picture, LEFT;
  word-wrapped `helvB18` title block + right-aligned `HH:MM-HH:MM` range meta
  beside it) plus up to 4 compact `9x15B` rows, each title-clipped left with a
  measured, right-aligned `HH:MM-HH:MM` range meta; a 6th+ visible event is
  dropped by design (`RD_MAX_EVENT_ROWS` in `rd_layout.h`) — a shorter row set
  is not a data-path error unless the state word (`stale`/`no wifi`) says
  otherwise.  Without a served tile the headline takes the full width, so the
  panel stays one coherent block instead of text beside an empty box.
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

