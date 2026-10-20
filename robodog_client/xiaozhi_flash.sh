#!/usr/bin/env bash
# Flash the RoboDog hermes firmware — the XiaoZhi AI voice firmware rebuilt
# from the pinned upstream source WITH the RoboDog capture hook (see
# voice/build_xiaozhi.sh). The image is produced by idf.py in the ESP-IDF
# Docker image and flashed here; if the build output is missing it is built
# first (Docker required). This replaces the calendar app on the board;
# reflash `bazel run //robodog_client:flash_firmware` to restore it.
#
#   bazel run //robodog_client:flash_robodog_hermes [-- --port /dev/ttyACM0]
#
# IMPORTANT: runs esptool on the HOST, not in Docker — Docker's ttyACM RTS
# passthrough drops the RTS/DTR modem-control ioctls (EPROTO on open), which
# breaks esptool's reset into download mode — so the button-free
# `--before default_reset` auto-reset only works when esptool runs directly on
# the host. Requires host `python3 -m esptool`.
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/robodog_client"
else
    APP="$(cd "$(dirname "$0")" && pwd)"
fi
PORT="${FLASH_PORT:-/dev/ttyACM0}"
BAUD="${FLASH_BAUD:-460800}"
while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="${2:?missing port}"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if ! python3 -c "import esptool" >/dev/null 2>&1; then
    echo "host esptool is required (button-free reset can't run in Docker):" >&2
    echo "  python3 -m pip install esptool" >&2
    exit 2
fi

# Build the capture firmware first if the image isn't staged yet.
if [ ! -f "$APP/.xiaozhi/build/xiaozhi.bin" ]; then
    echo "no built hermes image — building (Docker ESP-IDF, first build takes a while) ..."
    "$APP/voice/build_xiaozhi.sh" "$APP"
fi

echo "flashing hermes (XiaoZhi + capture hook) to $PORT ..."
cd "$APP/.xiaozhi/build"
python3 -m esptool --chip esp32s3 -p "$PORT" -b "$BAUD" \
    --before default_reset --after hard_reset write_flash \
    @flash_args
echo "flashed; board resets into XiaoZhi voice firmware (capture compiled in)"
