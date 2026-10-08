#!/usr/bin/env bash
# Flash the Sprout native firmware to the Atom Lite via the ESP-IDF Docker
# image — mirrors firmware/README.md's "Flash" step.
#
# --port X is REQUIRED (no default): /dev/ttyUSB0 has historically been the
# dead-flash Sprout-LITE node, so we never pick a board silently. Find the
# Sprout's port with tools/identify_device.py, then:
#   ./flash.sh --port /dev/ttyUSB1
#   bazel run //smart_irrigation/native-client:flash_firmware -- --port /dev/ttyUSB1
#   bazel run //smart_irrigation/native-client:flash_firmware -- --port /dev/ttyUSB1 --mode sprout-lite --plant monstera
#
# Device config is chosen here (same args as build.sh):
#   --mode   sprout (default) | sprout-lite
#   --plant  kale (default) herbs tomato succulent generic monstera
# If the already-built image was not built with the requested mode/plant, this
# rebuilds it first (via build.sh), so "flash with these args" is self-contained.
#
# Target safety: before flashing (and before any rebuild), the chosen port is
# identified by chip/Cardputer descriptor + console peripheral evidence (no MAC
# table) and an obvious wrong target (Cardputer ESP32-S3, or Sprout-LITE) is
# refused unless --force. The flash replaces the running runtime on the Sprout
# (supervised; see AGENTS.md hardware safety + firmware/README.md).
set -euo pipefail

# Device build configuration (must match the image we are about to flash).
MODE="${SPROUT_MODE:-sprout}"
PLANT="${SPROUT_PLANT:-kale}"
# No silent default: /dev/ttyUSB0 has been the dead-flash Sprout-LITE node.
# The target must be chosen explicitly (via --port / FLASH_PORT) so we never
# flash the wrong board by accident. Run tools/identify_device.py to find it.
PORT="${FLASH_PORT:-}"
FORCE=0
# PICO-D4 embedded flash is unreliable at 460800+ (hardware-verification.md /
# AGENTS.md) — 115200 is the documented safe speed for this chip.
BAUD="${FLASH_BAUD:-115200}"
while [ $# -gt 0 ]; do
    case "$1" in
        --port)  PORT="${2:-}";   shift 2 || true;;
        --baud)  BAUD="${2:-}";   shift 2 || true;;
        --mode)  MODE="${2:-}";   shift 2 || true;;
        --plant) PLANT="${2:-}";  shift 2 || true;;
        --target) TARGET="${2:-}"; shift 2 || true;;
        --force) FORCE=1;         shift 1 || true;;
        -h|--help)
            echo "usage: $0 [--port X] [--baud Y] [--mode sprout|sprout-lite]" >&2
            echo "             [--plant NAME] [--target esp32|esp32s3] [--force]" >&2
            echo "  --port X is REQUIRED (no default). Find X via tools/identify_device.py." >&2
            echo "  --force skips the identification target check." >&2
            exit 0;;
        *)
            echo "unknown argument: $1" >&2
            echo "usage: $0 [--port X] [--baud Y] [--mode sprout|sprout-lite] [--plant NAME] [--target esp32|esp32s3] [--force]" >&2
            exit 2;;
    esac
done
TARGET="${TARGET:-${SPROUT_TARGET:-esp32}}"
case "$TARGET" in
    esp32|esp32s3) ;;
    *)
        echo "invalid --target '$TARGET' (expected esp32 or esp32s3)" >&2
        exit 2;;
esac
# Repo root = firmware/../../.. (firmware -> native-client -> smart_irrigation -> repo).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
IDENTIFY="$REPO_ROOT/tools/identify_device.py"

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
if [ -z "$PORT" ]; then
    echo "ERROR: --port X is required (no default). USB serial devices:" >&2
    python3 - <<'PY' >&2 || true
import serial.tools.list_ports
for p in serial.tools.list_ports.comports():
    if p.device.startswith(("/dev/ttyACM", "/dev/ttyUSB")):
        print(f"  {p.device:<12} {p.description}")
PY
    echo "Run tools/identify_device.py to classify by type, then re-run with --port <X>." >&2
    exit 3
fi

# Best-effort target check: identify the chosen port's type (chip/Cardputer +
# console peripheral evidence, no MAC table) and refuse an obvious mismatch
# unless --force. Runs BEFORE any rebuild so a wrong target is rejected fast.
# Unclassifiable/unreadable consoles only warn (flaky USB).
VERDICT=""
if [ "$FORCE" = 0 ] && [ -f "$IDENTIFY" ]; then
    VERDICT="$(timeout 60 python3 "$IDENTIFY" --port "$PORT" 2>/dev/null || true)"
    echo "Identified $PORT as: ${VERDICT:-<unable to classify (console unreadable)>}"
    case "$VERDICT" in
        *Cardputer*)
            echo "ERROR: $PORT is a Cardputer (ESP32-S3) — wrong target for this ESP32 build. Use --force to override." >&2
            exit 3;;
        *Sprout-LITE*)
            echo "ERROR: $PORT identified as Sprout-LITE (moisture-only, known dead-flash). Use --force to override." >&2
            exit 3;;
        *Sprout*) ;;
    esac
fi

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
# A target change (esp32 <-> esp32s3) also invalidates the staged image:
# detect it from the staged sdkconfig left by the previous set-target/build.
prev_target="$(grep -E '^CONFIG_IDF_TARGET="' "$DEST/sdkconfig" 2>/dev/null | tr -d 'CONFIG_IDF_TARGET="')"
if [ ! -f "$DEST/build/sprout_native.bin" ]; then
    need_build=1
elif [ -n "$prev_target" ] && [ "$prev_target" != "$TARGET" ]; then
    need_build=1
elif ! grep -q "^#define SPROUT_LITE $want_lite\$" "$CFG" 2>/dev/null; then
    need_build=1
elif ! grep -q "^#define SPROUT_PLANT \"$PLANT\"\$" "$CFG" 2>/dev/null; then
    need_build=1
fi
if [ "$need_build" = 1 ]; then
    echo "Staged image is not mode=$MODE plant=$PLANT target=$TARGET — rebuilding with those args"
    "$APP/build.sh" --mode "$MODE" --plant "$PLANT" --target "$TARGET"
fi

echo "Flashing Sprout ($MODE, plant=$PLANT, target=$TARGET) native firmware to $PORT @ $BAUD baud ..."
# Map the host port 1:1 into the container (works for both the FTDI /dev/ttyUSB
# classic Atom and the USB-Serial-JTAG /dev/ttyACM Atom Lite S3).
exec docker run --rm -v "$RTR:/repo" --device "$PORT:$PORT" \
    -w /repo/firmware/sprout -e IDF_TARGET="$TARGET" "$IDF_IMG" \
    bash -lc "idf.py -p $PORT -b $BAUD flash"
