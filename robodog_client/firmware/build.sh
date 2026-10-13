#!/usr/bin/env bash
# Build the RoboDog LVGL UI framework firmware (Waveshare ESP32-S3 RLCD-4.2)
# with ESP-IDF via Docker.
#
# PR 1 — UI framework build: no RTReticulum/no networking; the app renders the
# declarative LVGL screen with a demo dataset so the UI can be validated on
# the panel (data sources land in the follow-up PRs).
#
# Build-time injection (source tree never modified): none required for the
# demo build; kept header-free.
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/robodog_client/firmware"
else
    APP="$(cd "$(dirname "$0")" && pwd)"
fi
IDF_IMG="${IDF_IMG:-espressif/idf:v5.3.1}"

WORK="$(dirname "$APP")/build_lvgl"
mkdir -p "$WORK"
STAGE="$WORK"
rm -rf "$STAGE/build" "$STAGE/main"   # stale CMakeCache + stale staged copy
cp -rT "$APP/main" "$STAGE/main"
cp "$APP/CMakeLists.txt" "$APP/sdkconfig.defaults" "$APP/partitions.csv" "$STAGE/" 2>/dev/null || true
echo "app staged in $STAGE"

docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$STAGE:/src" -w /src -e IDF_TARGET=esp32s3 \
    "$IDF_IMG" bash -lc 'idf.py set-target esp32s3 && idf.py build' || exit 1
echo "Built: $STAGE/build/robodog_lvgl.bin"
