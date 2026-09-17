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
- `main/CMakeLists.txt`, `components/rtreticulum/` — ESP-IDF component wrapper for
  the RTReticulum RNS library + `port/esp_idf/hal_esp_idf.c` (copied from the
  upstream heltec_v3 firmware).
- `sdkconfig.defaults` / `partitions.csv` — `esp32` target, 4 MB flash.

## Build (Docker ESP-IDF)
```bash
./build.sh          # clones RTReticulum into ../.rtreticulum, stages this app
                    # into its firmware/sprout, and runs idf.py set-target esp32 + build
```
Requires Docker; first run pulls `espressif/idf:v5.3.1` (~1-2 GB).

## Flash (⚠️ supervised — replaces the MicroPython runtime)
The Atom currently runs MicroPython (our sensors/probes depend on it; full
factory backup: `/home/pondycrane/atom_lite_factory_backup_20260912.bin`).
First flash of the native app via the serial (esptool download) is a deliberate
step — do it with the user present:
```bash
idf.py -p /dev/ttyUSB0 flash    # inside the build container
idf.py -p /dev/ttyUSB0 monitor
```
To go back to MicroPython: restore the backup (esptool, 115200) then re-run the
MicroPython install.

## Acceptance for step 3
PICO-D4 announces `lmao/sprout` over the DTU LoRa link; the production RNode
(log on tp4) + Python RNS see `Valid announce` from Sprout's identity hash.
Then step 4 adds the send-only LXMF SensorReport (air temp) to the server.
