#!/usr/bin/env bash
# Flash the Sprout native firmware to the Atom Lite (FTDI /dev/ttyUSB0) via
# the ESP-IDF Docker image — mirrors firmware/README.md's "Flash" step.
#
# Use from Bazel:
#   bazel run //smart_irrigation/native-client:flash_firmware
#   bazel run //smart_irrigation/native-client:flash_firmware -- --port /dev/ttyACM0
#   bazel run //smart_irrigation/native-client:flash_firmware -- --mode sprout-lite --plant monstera
#
# Device config is chosen here (same args as build.sh):
#   --mode   sprout (default) | sprout-lite
#   --plant  kale (default) herbs tomato succulent generic monstera
# If the already-built image was not built with the requested mode/plant, this
# rebuilds it first (via build.sh), so "flash with these args" is self-contained.
#
# The flash replaces the running runtime on the Sprout (supervised; see
# AGENTS.md hardware safety + firmware/README.md) — never esptool-probe any
# other device.
set -euo pipefail

# Device build configuration (must match the image we are about to flash).
MODE="${SPROUT_MODE:-sprout}"
PLANT="${SPROUT_PLANT:-kale}"
PORT="${FLASH_PORT:-/dev/ttyUSB0}"
# PICO-D4 embedded flash is unreliable at 460800+ (hardware-verification.md /
# AGENTS.md) — 115200 is the documented safe speed for this chip.
BAUD="${FLASH_BAUD:-115200}"
while [ $# -gt 0 ]; do
    case "$1" in
        --port)  PORT="${2:-}";   shift 2 || true;;
        --baud)  BAUD="${2:-}";   shift 2 || true;;
        --mode)  MODE="${2:-}";   shift 2 || true;;
        --plant) PLANT="${2:-}";  shift 2 || true;;
        -h|--help)
            echo "usage: $0 [--port X] [--baud Y] [--mode sprout|sprout-lite] [--plant NAME]" >&2
            exit 0;;
        *)
            echo "unknown argument: $1" >&2
            echo "usage: $0 [--port X] [--baud Y] [--mode sprout|sprout-lite] [--plant NAME]" >&2
            exit 2;;
    esac
done
if [ "$MODE" != sprout ] && [ "$MODE" != sprout-lite ]; then
    echo "invalid --mode '$MODE' (expected sprout or sprout-lite)" >&2
    exit 2
fi
case "$PLANT" in
    kale|herbs|tomato|succulent|generic|monstera) ;;
    *)
        echo "unknown --plant '$PLANT' (known: kale herbs tomato succulent generic monstera)" >&2
        exit 2;;
esac

if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/smart_irrigation/native-client/firmware"
else
    APP="$(cd "$(dirname "$0")" && pwd)"
fi
RTR="$(dirname "$APP")/.rtreticulum"
IDF_IMG="${IDF_IMG:-espressif/idf:v5.3.1}"

# Rebuild if nothing is built yet, or the staged config does not match the
# requested mode/plant.  build.sh regenerates the staged device_config.h, so
# comparing it is an exact check of what the current image actually is.
DEST="$RTR/firmware/sprout"
CFG="$DEST/main/device_config.h"
want_lite=0
[ "$MODE" = sprout-lite ] && want_lite=1
need_build=0
if [ ! -f "$DEST/build/sprout_native.bin" ]; then
    need_build=1
elif ! grep -q "^#define SPROUT_LITE $want_lite\$" "$CFG" 2>/dev/null; then
    need_build=1
elif ! grep -q "^#define SPROUT_PLANT \"$PLANT\"\$" "$CFG" 2>/dev/null; then
    need_build=1
fi
if [ "$need_build" = 1 ]; then
    echo "Staged image is not mode=$MODE plant=$PLANT — rebuilding with those args"
    "$APP/build.sh" --mode "$MODE" --plant "$PLANT"
fi

echo "Flashing Sprout ($MODE, plant=$PLANT) native firmware to $PORT @ $BAUD baud ..."
exec docker run --rm -v "$RTR:/repo" --device "$PORT:/dev/ttyUSB0" \
    -w /repo/firmware/sprout -e IDF_TARGET=esp32 "$IDF_IMG" \
    bash -lc "idf.py -p /dev/ttyUSB0 -b $BAUD flash"
