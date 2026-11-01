#!/usr/bin/env bash
# Build robodog_client (Waveshare ESP32-S3 RLCD family-calendar display) via
# the ESP-IDF Docker image, reusing the in-tree build cache.
#
#   bazel run //robodog_client:build_firmware
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/robodog_client"
else
    APP="$(cd "$(dirname "$0")" && pwd)"
fi
IDF_IMG="${IDF_IMG:-espressif/idf:v5.3.1}"
# u8g2 is fetched, not vendored: stage the pinned upstream csrc into the
# gitignored components/u8g2/csrc (via bazel @u8g2 runfiles or a sha256-verified
# cache) so a fresh checkout builds with no committed upstream source.
"$APP/u8g2_prepare.sh" "$APP"
# The u8g2 frame-dump capture hook is always compiled in (see
# main/CMakeLists.txt) — capture.py confirms the current production firmware's
# display, so there is no separate capture build.

# A pre-existing (gitignored) sdkconfig WINS over sdkconfig.defaults, so a tree
# created before CONFIG_ESP_MAIN_TASK_STACK_SIZE was raised keeps the old 3584 B
# main-task stack — which reset-loops the board in the sync+render chain (stack
# overflow right after WiFi gets an IP).  Fail loudly instead of flashing a
# board that reboots every cycle.
STACK=$(grep -E '^CONFIG_ESP_MAIN_TASK_STACK_SIZE=' "$APP/sdkconfig" 2>/dev/null | cut -d= -f2)
if [ -n "${STACK:-}" ] && [ "$STACK" -lt 8192 ]; then
    echo "error: sdkconfig has CONFIG_ESP_MAIN_TASK_STACK_SIZE=$STACK (< 8192)." >&2
    echo "       The sync+render+RDSHOT chain overflows the IDF default stack." >&2
    echo "       Fix: rm $APP/sdkconfig   (it is gitignored; regenerated from" >&2
    echo "            sdkconfig.defaults on the next build)" >&2
    exit 1
fi

exec docker run --rm -v "$APP:/workspace" -w /workspace "$IDF_IMG" \
    idf.py build
