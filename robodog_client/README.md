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
  (`calendar_id=1` string, `since_ms=2` varint u64, `max_events=3` varint u32).
  A 0-length payload = the documented defaults `{calendar_id:"family",
  since_ms:0}`.
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
  current data — adjust with the screenshot loop below. (Row count is a fixed
  cap: the headline picture + 2 compact text rows.)

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

# capture: reset the board, then read frames; make --n/--timeout wide enough
# that WiFi connect + first gRPC sync happen (sync landed ~40s post-boot here)
python3 capture.py --port /dev/ttyACM0 --reset --n 4 --timeout 100 --describe
#   shots/frameN.i1                raw 15200-byte u8g2 buffer (panel memory)
#   shots/panel_raw_portrait.png   panel-native 300×400
#   shots/panel_landscape.png      400×300 as seen in landscape
#   shots/frameN_analysis.txt      vision-model transcription (--describe)
```

**Reading the device state from the screenshot:**
- Header `YYYY-MM-DD HH:MM` present → WiFi up + gRPC sync healthy.
- Header **BLANK** + `no wifi` hint beside the first row → not connected yet:
  pre-sync boot frame, or WiFi/AP unreachable.
- `stale` hint → connected but last sync failed (showing NVS cache).
- Row set == Postgres/`GetCalendar` output → data path live; a stale/different
  set means the cache is being shown (compare timestamps against the header).
- At most 3 event rows are drawn: the headline (big picture, LEFT, with start
  time + clipped title to its right) plus 2 compact text rows; a 4th+ visible
  event is dropped by design (`RD_MAX_EVENT_ROWS` in `render_screen`) — a
  shorter row set is not a data-path error unless the header hints
  (`stale`/`no wifi`) say otherwise.

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

