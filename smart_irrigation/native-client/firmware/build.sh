#!/usr/bin/env bash
# Build the Sprout native firmware (issue #130, step 3) with ESP-IDF via Docker.
# Stages this repo's app into a fresh RTReticulum checkout (its
# components/rtreticulum wrapper compiles the RNS lib + ESP-IDF HAL), then
# builds for the ESP32-PICO-D4 target.
set -euo pipefail
# Locate the firmware source dir.  `bazel run //smart_irrigation/native-client:build_firmware`
# exports BUILD_WORKSPACE_DIRECTORY; a direct `./build.sh` from this dir uses $0.
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/smart_irrigation/native-client/firmware"
else
    APP="$(cd "$(dirname "$0")" && pwd)"
fi
PARENT="$(dirname "$APP")"
RTR="$PARENT/.rtreticulum"
IDF_IMG="${IDF_IMG:-espressif/idf:v5.3.1}"

if [ ! -d "$RTR" ]; then
    echo "cloning RTReticulum -> $RTR"
    git clone -q --depth 1 https://github.com/0xSeren/RTReticulum "$RTR"
fi

DEST="$RTR/firmware/sprout"
mkdir -p "$DEST"
# Only remove our host-owned source dirs; the container (root) owns build/
# and ninja rebuilds deltas in place.
rm -rf "$DEST/main" "$DEST/components" 2>/dev/null || true
cp -r "$APP/main" "$APP/components" "$DEST/"
cp "$APP/CMakeLists.txt" "$APP/sdkconfig.defaults" "$APP/partitions.csv" "$DEST/"
echo "app staged in $DEST"

docker run --rm -v "$RTR:/repo" -w /repo/firmware/sprout -e IDF_TARGET=esp32 \
    "$IDF_IMG" bash -lc 'idf.py set-target esp32 && idf.py build' || exit 1
echo "Built: $DEST/build/sprout_native.bin"
