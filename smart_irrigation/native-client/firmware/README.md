# Sprout native firmware (ESP32-PICO-D4) — issue #130 step 3

Native (ESP-IDF, C++17) replacement for the MicroPython urns node on the Atom
Lite. The ESP32-PICO-D4 (320 KB SRAM) cannot hold RNS+LXMF under MicroPython
(the MicroPython sender + slim-LXMF overlay that DOES run on an ESP32-S3 is on
branch `feat/sprout-increment3-sender`). This native build has RAM to spare
(crypto via ESP-IDF mbedTLS + MonoCypher) and stays wire-compatible with the
mesh (proven: RTReticulum `on_announce` <-> Python RNS, see `../host`).

## Layout
- `main/main.cpp` — boot RNS + UART-AT interface + register `lmao/sprout` dest + announce every 30 s
- `main/uart_at_interface.{h,cpp}` — RAK3172 (M5Stack A152 DTU) **P2P over UART AT**:
  sets the LMAO mesh params (868/BW125/SF7/CR4:5/PPL24/syncword 0x1424/TX17),
  `AT+PSEND` hex TX (RX-off-before-TX), `+EVT:RXP2P` hex RX -> `handle_incoming`.
  Mirrors `smart_irrigation/firmware/lib/dtu/dtu_at.py` semantics.
- `firmware_common/` (repo root) — **shared** protocol component (DRY):
  `lma_common/{lma_encoder,lxmf_send,path_find,lma_identity}` + the
  `rtreticulum/` wrapper. Both the Sprout and Cardputer `build.sh` stage this
  into their targeted `.rtreticulum/firmware/<name>/components/`, so neither
  tree carries a private copy.
- `main/CMakeLists.txt` — ESP-IDF component (device-specific sources; the RNS
  lib + shared layers come from `firmware_common/`).
- `sdkconfig.defaults` / `partitions.csv` — `esp32` target, 4 MB flash.

## Build (Docker ESP-IDF)

One client, two device classes + per-plant match, chosen at build time:

```bash
./build.sh                        # full sprout (radio + ENV III + upload), plant=kale
./build.sh --mode sprout-lite --plant monstera   # moisture-only controller for a Swiss cheese plant
./build.sh --plant tomato         # full sprout, tomato profile as the baked default
```
`--mode` is `sprout` (default) or `sprout-lite`; `--plant` is any name in the
control engine's profile table (`kale herbs tomato succulent generic monstera`,
default `kale`). The args are baked into the *staged* `main/device_config.h`
(the repo copy stays the sprout/kale default).

Or from Bazel (same script, workspace-rooted):
```bash
bazel run //smart_irrigation/native-client:build_firmware
bazel run //smart_irrigation/native-client:build_firmware -- --mode sprout-lite --plant monstera
```
Requires Docker; first run pulls `espressif/idf:v5.3.1` (~1-2 GB).

## Sprout Lite device class

`--mode sprout-lite` is the same client with the radio/ENV III/telemetry path
**compiled out** (`#if SPROUT_LITE` in `main.cpp`, config in
`main/device_config.h`, wired by `build.sh`/`flash.sh`):

- no DTU/LoRa: no Reticulum/DTU-UART start, no announce, no path-find;
- no ENV III: no SHT30 read, no air T/RH telemetry, RH lockout clause disabled;
- no data upload: `send_sensor_report` never runs;
- only: pump-safety-first boot, the soil-moisture probe, the control engine
  (plant profile from `--plant`), and pump actuation (still dry-run per #119).

## Button & LED (runtime actuation toggle)

Pump actuation is runtime, not compile-time, and **boots dry-run** every time
(the armed state is never persisted — a reset always drops back to dry-run for
safety). The Atom Lite's front G39 button + SK6812 LED (G27) control it:

- hold ~2 s → arm actuation (real watering allowed); **LED red**
- quick tap → back to dry-run; LED off
- presses between ~1 s and 2 s are ignored; a reset/power cycle always starts
  disarmed.

Driver: `main/button_led.{h,cpp}` (RMT for the LED, a small FreeRTOS task for
debounce + press-duration classification). Until the #119 hardware pull-down is
fitted and verified, keep it disarmed.

## Flash (⚠️ supervised — replaces the running firmware)
The rig runs the native app (since #133); re-flashing replaces it. Flashing is
a deliberate step — do it with the user present (full MicroPython factory
backup: `/home/pondycrane/atom_lite_factory_backup_20260912.bin`):
```bash
bazel run //smart_irrigation/native-client:flash_firmware          # port /dev/ttyUSB0
bazel run //smart_irrigation/native-client:flash_firmware -- --port /dev/ttyACM0
bazel run //smart_irrigation/native-client:flash_firmware -- --mode sprout-lite --plant monstera
# equivalent, standalone:
idf.py -p /dev/ttyUSB0 flash    # inside the build container
idf.py -p /dev/ttyUSB0 monitor
```
`flash.sh` accepts the same `--mode`/`--plant` as `build.sh`; if the previously
built image does not match them it rebuilds first, so the flashed image always
is the requested mode/plant. To go back to MicroPython: restore the backup
(esptool, 115200) then re-run the MicroPython install.

## Acceptance for step 3
PICO-D4 announces `lmao/sprout` over the DTU LoRa link; the production RNode
(log on tp4) + Python RNS see `Valid announce` from Sprout's identity hash.
Then step 4 adds the send-only LXMF SensorReport (air temp) to the server.
