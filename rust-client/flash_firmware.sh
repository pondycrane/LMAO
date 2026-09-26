#!/usr/bin/env bash
# Flash the T1 no_std firmware to the Cardputer (ESP32-S3) via espflash.
# Bazel: `bazel run //rust-client:flash_firmware -- --port /dev/ttyACM0`.
#
# HARDWARE WARNING: this REPLACES the running MicroPython runtime on the
# Cardputer (an esptool-class op — AGENTS.md constrains these; USB-Serial-JTAG
# is fragile to careless flashes and recovery may need a physical replug).
# Restore the MicroPython urns path afterwards with `bazel run //cardputer_client:flash`
# (or re-flash the saved MicroPython image — see UPSTREAM.md "stable revert").
# Confirm you are NOT flashing the live production Cardputer without a revert
# image + physical standby ready.
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
  cd "$BUILD_WORKSPACE_DIRECTORY/rust-client/firmware"
else
  cd "$(dirname "$0")/firmware"
fi

PORT="${FLASH_PORT:-/dev/ttyACM0}"
[ "${1:-}" = "--port" ] && [ -n "${2:-}" ] && PORT="$2" && shift 2 || true
if [ -n "${1:-}" ]; then PORT="$1"; fi

echo "Building T1 firmware (release) ..."
cargo build --release

BIN="target/xtensa-esp32s3-none-elf/release/lmao-firmware-t1"
if [ ! -f "$BIN" ]; then
  echo "No built image at $BIN" >&2
  exit 1
fi
echo "Flashing $BIN -> $PORT ..."
espflash flash --port "$PORT" --monitor "$BIN"
