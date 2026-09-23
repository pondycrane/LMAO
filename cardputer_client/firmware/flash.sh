#!/usr/bin/env bash
# Flash the Cardputer native firmware to the Cardputer (USB-Serial-JTAG
# /dev/ttyACM0) via the ESP-IDF Docker image — mirrors build.sh's staging.
#
# Use from Bazel:
#   bazel run //cardputer_client:flash_firmware
#   bazel run //cardputer_client:flash_firmware -- --port /dev/ttyACM0
#
# The flash replaces the MicroPython runtime on the Cardputer with the native
# C firmware (PR1 sensor node). Never esptool-probe the Cardputer outside the
# sanctioned :build_firmware / :flash_firmware path.
set -euo pipefail

if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/cardputer_client/firmware"
else
    APP="$(cd "$(dirname "$0")" && pwd)"
fi
RTR="$(dirname "$APP")/.rtreticulum"
SPROUT_RTR="$(cd "$APP/../../smart_irrigation/native-client" 2>/dev/null && pwd)/.rtreticulum"
if [ -n "${RTRETICULUM_ROOT:-}" ]; then
    RTR="$RTRETICULUM_ROOT"
elif [ -d "$SPROUT_RTR" ]; then
    RTR="$SPROUT_RTR"
fi
IDF_IMG="${IDF_IMG:-espressif/idf:v5.3.1}"

PORT="${FLASH_PORT:-/dev/ttyACM0}"
# USB-Serial-JTAG is reliable at 921600.
BAUD="${FLASH_BAUD:-921600}"
# Accept `--port X [--baud Y]` style args too.
if [ "${1:-}" = "--port" ] && [ -n "${2:-}" ]; then
    PORT="$2"
    shift 2 || true
fi
if [ "${1:-}" = "--baud" ] && [ -n "${2:-}" ]; then
    BAUD="$2"
fi

if [ ! -f "$RTR/firmware/cardputer/build/cardputer_native.bin" ]; then
    echo "No built image at $RTR/firmware/cardputer/build/cardputer_native.bin — run :build_firmware first" >&2
    exit 1
fi

echo "Flashing Cardputer native firmware to $PORT @ $BAUD baud ..."
exec docker run --rm -v "$RTR:/repo" --device "$PORT:/dev/ttyACM0" \
    -w /repo/firmware/cardputer -e IDF_TARGET=esp32s3 "$IDF_IMG" \
    bash -lc "idf.py -p /dev/ttyACM0 -b $BAUD flash"
