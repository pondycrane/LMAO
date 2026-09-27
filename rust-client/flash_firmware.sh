#!/usr/bin/env bash
# Flash the T1 no_std firmware to the Cardputer (ESP32-S3) via espflash.
# Bazel: `bazel run //rust-client:flash_firmware -- --port /dev/ttyACM0`.
#
# HARDWARE WARNING: this REPLACES the running MicroPython runtime on the
# Cardputer. The USB-Serial-JTAG is fragile: the default DTR/RTS reset
# (`--before default-reset`) de-enumerates it (needs a physical replug), and
# reads/writes stall unless the chip is in the ROM download/bootloader mode —
# hold the Cardputer **GO** (BOOT) button while attaching USB, then flash with
# `--before no-reset`. Restore working MicroPython afterwards by writing the
# M5Stack UiFlow2 factory image + reprovision the LMAO client with
# `bazel run //tools:install_all` (see T1-GATE.md §Hardware).
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

echo "Ensure the Cardputer is in download/bootloader mode (device shows 303a:1001),"
echo "e.g. hold GO (BOOT) while attaching USB, then:"
echo "  espflash flash --before no-reset --port $PORT --chip esp32s3 -- $BIN"
if [ "${SKIP_CONFIRM:-0}" != "1" ]; then
  read -r -p "Proceed? [y/N] " ans || true
  [ "$ans" = "y" ] || { echo "aborted"; exit 1; }
fi
exec espflash flash --before no-reset --port "$PORT" --chip esp32s3 --after hard-reset "$BIN"
