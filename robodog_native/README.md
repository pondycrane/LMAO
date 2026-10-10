# robodog_native

Native **ESP-IDF** firmware for the **RoboDog** family-calendar display:
a Waveshare **ESP32-S3** dev board driving the Waveshare **RLCD-4.2**
(**ST7305**, 400×300 reflective 1-bit B&W LCD).  No RNS/LXMF here: the device
reads the family calendar straight from the LMAO server over a tiny framed-TCP
service and renders it in 1-bit B&W on the ST7305.

```
┌─ LMAO server (lma_core/calendar_tcp_api.py, LAN :4247) ─┐
│   [u32 BE len][ CalendarRequest protobuf ]  ◀── client  │
│   [u32 BE len][ CalendarBundle protobuf ]  ──▶ client   │
└──────────────────────────────────────────────────────────┘
            ▲ framed TCP, single request/reply per sync
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

Other knobs (`RD_CAL_TCP_PORT`, `RD_SYNC_INTERVAL_SECONDS`,
`RD_SNTP_SERVER`, timeouts) are build-time macros in `main/config.h`.

## Wire contract (implemented exactly)

Plain TCP to `LMAO_SERVER_HOST:LMAO_CAL_TCP_PORT` (default **4247**, LAN,
insecure by design).  Every frame:

```
4-byte big-endian length, then that many payload bytes
```

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
robodog_native/
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

## Status & unverified items

This project was written **without hardware bring-up** and was **not compiled
or flashed** here (no ESP-IDF toolchain/board in this environment).  The wire
layer (`lma_proto.c`) is the only code that was compiled and exercised —
successfully, on the host, including under ASan/UBSan.  Items that must be
validated on first real hardware:

- **ST7305 orientation / rotation**: the renderer uses `U8G2_R1` (as the
  vendor example does) and sizes the layout from `u8g2_GetDisplayWidth/Height`
  so a rotation change is a one-line config tweak.  If the screen appears
  upside-down or portrait-vs-landscape is wrong, adjust
  `cfg.rotation` in `robodog.c` `display_init()`.
- **Reflective LCD contrast / color polarity**: `u8g2_SetDrawColor(…, 1)` is
  assumed to mean "dark = ink".  If the display is inverted (white text on
  black), toggle the draw color in `render_screen()`.
- **WiFi first-run**: SSID/pass provisioning, roaming behavior, and the
  disconnect-retry loop are untested on real air.
- **SNTP**: `pool.ntp.org` assumes internet egress; on a LAN-only network set
  `RD_SNTP_SERVER` to a reachable NTP server or `""` to disable (header then
  shows no date; event HH:MM is computed from the event's own UTC epoch, so it
  always renders).
- **Frame/width tuning**: row count, title clipping and the 16×16 picture
  placeholder are approximations that benefit from a look at the real panel.
