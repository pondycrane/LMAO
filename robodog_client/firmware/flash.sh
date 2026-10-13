#!/usr/bin/env bash
# Flash the RoboDog LVGL firmware to the Waveshare ESP32-S3 RLCD-4.2.
#
# IMPORTANT: this runs esptool on the HOST, not in Docker — same reason as
# robodog_native/flash.sh: Docker's ttyACM passthrough drops the RTS/DTR
# modem-control ioctls (EPROTO on open), which breaks esptool's button-free
# reset into download mode.  Requires host `python3 -m esptool`.
#
# Use from Bazel:
#   bazel run //robodog_client:flash_firmware [-- --port /dev/ttyACM1]
set -euo pipefail

if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/robodog_client/firmware"
else
    APP="$(cd "$(dirname "$0")" && pwd)"
fi
WORK="$(dirname "$APP")/build_lvgl"
BUILD_DIR="$WORK/build"

if ! python3 -c "import esptool" >/dev/null 2>&1; then
    echo "host esptool is required (button-free reset can't run in Docker):" >&2
    echo "  python3 -m pip install --target=\$HOME/.local/lib python esptool" >&2
    exit 1
fi

PORT="${FLASH_PORT:-/dev/ttyACM1}"
BAUD="${FLASH_BAUD:-460800}"
if [ "${1:-}" = "--port" ] && [ -n "${2:-}" ]; then PORT="$2"; shift 2 || true; fi
if [ ! -f "$BUILD_DIR/robodog_lvgl.bin" ]; then
    echo "No built image at $BUILD_DIR/robodog_lvgl.bin — run :build_firmware first" >&2
    exit 1
fi

echo "Flashing RoboDog LVGL firmware to $PORT @ $BAUD baud (host esptool) ..."
cd "$BUILD_DIR"
exec python3 -m esptool --chip esp32s3 -p "$PORT" -b "$BAUD" \
    --before default_reset --after hard_reset write_flash @flash_args
