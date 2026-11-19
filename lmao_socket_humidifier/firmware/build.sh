#!/usr/bin/env bash
# Build the Humidifier Atom Socket firmware (M5Stack Atom Socket / Atom Lite,
# ESP32-PICO-D4) with ESP-IDF via Docker.
#
#   bazel run //humidifier:build_firmware
#
# The app is staged into a gitignored .idf/ tree next to this script, so no
# third-party source enters the repo and no RTReticulum checkout is needed
# (the socket speaks plain HTTP on the LAN; it is not a Reticulum client).
#
# NOTHING is baked in: WiFi credentials and the API token are written to the
# NVS partition by flash.sh at flash time (see README).
set -euo pipefail

if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/lmao_socket_humidifier/firmware"
else
    APP="$(cd "$(dirname "$0")" && pwd)"
fi
IDF_IMG="${IDF_IMG:-espressif/idf:v5.3.1}"

# Staging lives beside the source and is gitignored: Docker (root) owns the
# build dir, so only our own source dirs are ever wiped.
DEST="$APP/.idf/lmao_socket_humidifier"
mkdir -p "$DEST"
rm -rf "$DEST/main" "$DEST/components" 2>/dev/null || true
mkdir -p "$DEST/components"
cp -r "$APP/main" "$DEST/main"
cp -r "$APP/../../firmware_common/lma_wifi" "$DEST/components/lma_wifi"
cp "$APP/CMakeLists.txt" "$APP/sdkconfig.defaults" "$APP/partitions.csv" "$DEST/"
echo "app staged in $DEST"

docker run --rm -v "$DEST:/app" -w /app -e IDF_TARGET=esp32 \
    "$IDF_IMG" bash -lc 'idf.py set-target esp32 && idf.py build' || exit 1
echo "Built: $DEST/build/humidifier_socket.bin"
