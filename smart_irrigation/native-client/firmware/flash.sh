#!/usr/bin/env bash
# Flash the Sprout native firmware to the Atom Lite (FTDI /dev/ttyUSB0) via
# the ESP-IDF Docker image — mirrors firmware/README.md's "Flash" step.
#
# Use from Bazel:
#   bazel run //smart_irrigation/native-client:flash_firmware
#   bazel run //smart_irrigation/native-client:flash_firmware -- --port /dev/ttyUSB0
#
# The flash replaces the running runtime on the Sprout (supervised; see
# AGENTS.md hardware safety + firmware/README.md) — never esptool-probe any
# other device.
set -euo pipefail

if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/smart_irrigation/native-client/firmware"
else
    APP="$(cd "$(dirname "$0")" && pwd)"
fi
RTR="$(dirname "$APP")/.rtreticulum"
IDF_IMG="${IDF_IMG:-espressif/idf:v5.3.1}"

PORT="${FLASH_PORT:-/dev/ttyUSB0}"
# PICO-D4 embedded flash is unreliable at 460800+ (hardware-verification.md /
# AGENTS.md) — 115200 is the documented safe speed for this chip.
BAUD="${FLASH_BAUD:-115200}"
# Accept `--port X [--baud Y]` style args too.
if [ "${1:-}" = "--port" ] && [ -n "${2:-}" ]; then
    PORT="$2"
    shift 2 || true
fi
if [ "${1:-}" = "--baud" ] && [ -n "${2:-}" ]; then
    BAUD="$2"
fi

if [ ! -f "$RTR/firmware/sprout/build/sprout_native.bin" ]; then
    echo "No built image at $RTR/firmware/sprout/build/sprout_native.bin — run :build_firmware first" >&2
    exit 1
fi

echo "Flashing Sprout native firmware to $PORT @ $BAUD baud ..."
exec docker run --rm -v "$RTR:/repo" --device "$PORT:/dev/ttyUSB0" \
    -w /repo/firmware/sprout -e IDF_TARGET=esp32 "$IDF_IMG" \
    bash -lc "idf.py -p /dev/ttyUSB0 -b $BAUD flash"
