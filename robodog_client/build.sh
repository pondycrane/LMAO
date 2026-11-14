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

# A pre-existing (gitignored) sdkconfig WINS over sdkconfig.defaults, so a
# tree created before a knob was added silently keeps the old value.  Two of
# them are load-bearing and fail silently rather than loudly:
#   - CONFIG_ESP_MAIN_TASK_STACK_SIZE < 8192 reset-loops the board in the
#     sync+render chain (stack overflow right after WiFi gets an IP);
#   - a config without tickless idle / with SPIRAM on never light-sleeps, so
#     the board would flash "successfully" and still drain the battery.
# Fail loudly instead of flashing a board that reboots every cycle or never
# sleeps.
# A fresh checkout has no `sdkconfig` (gitignored, generated on first
# configure), so the grep must tolerate a missing file: `set -euo pipefail`
# turns grep's exit 2 into an abort before docker ever runs.
SDK="$APP/sdkconfig"
if [ -f "$SDK" ]; then
    stale=()
    STACK=$(grep -E '^CONFIG_ESP_MAIN_TASK_STACK_SIZE=' "$SDK" | cut -d= -f2 || true)
    if [ -n "${STACK:-}" ] && [ "$STACK" -lt 8192 ]; then
        stale+=("CONFIG_ESP_MAIN_TASK_STACK_SIZE=$STACK (< 8192)")
    fi
    grep -qx 'CONFIG_SPIRAM=y' "$SDK" && stale+=("CONFIG_SPIRAM=y (buffer belongs in internal SRAM)")
    grep -qx 'CONFIG_FREERTOS_USE_TICKLESS_IDLE=y' "$SDK" \
        || stale+=("CONFIG_FREERTOS_USE_TICKLESS_IDLE is not set (no light sleep)")
    grep -qx 'CONFIG_USJ_NO_AUTO_LS_ON_CONNECTION=y' "$SDK" \
        || stale+=("CONFIG_USJ_NO_AUTO_LS_ON_CONNECTION is not set (console dies in light sleep)")
    if [ ${#stale[@]} -gt 0 ]; then
        echo "error: $SDK is stale (it wins over sdkconfig.defaults):" >&2
        for s in "${stale[@]}"; do echo "       - $s" >&2; done
        echo "       Fix: rm $SDK   (it is gitignored; regenerated from" >&2
        echo "            sdkconfig.defaults on the next build)" >&2
        exit 1
    fi
fi

exec docker run --rm -v "$APP:/workspace" -w /workspace "$IDF_IMG" \
    idf.py build
