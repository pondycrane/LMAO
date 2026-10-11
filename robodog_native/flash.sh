#!/usr/bin/env bash
# Flash robodog_native to the Waveshare ESP32-S3 RLCD, button-free.
#
# IMPORTANT: this runs esptool on the HOST, not in Docker.  Docker's ttyACM
# passthrough drops the RTS/DTR modem-control ioctls (EPROTO on open), which
# breaks esptool's reset into download mode — so the button-free
# `--before default_reset` auto-reset only works when esptool runs directly on
# the host.  Requires host `python3 -m esptool`.
#
#   bazel run //robodog_native:flash_firmware [-- --port /dev/ttyACM0]
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/robodog_native"
else
    APP="$(cd "$(dirname "$0")" && pwd)"
fi
PORT="${FLASH_PORT:-/dev/ttyACM0}"
BAUD="${FLASH_BAUD:-460800}"
if [ "${1:-}" = "--port" ] && [ -n "${2:-}" ]; then PORT="$2"; shift 2 || true; fi
if ! python3 -c "import esptool" >/dev/null 2>&1; then
    echo "host esptool is required (button-free reset can't run in Docker):" >&2
    echo "  python3 -m pip install --target=\$HOME/.local/lib python esptool" >&2
    exit 1
fi
cd "$APP/build"
exec python3 -m esptool --chip esp32s3 -p "$PORT" -b "$BAUD" \
    --before default_reset --after hard_reset write_flash @flash_args
