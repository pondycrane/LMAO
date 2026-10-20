#!/usr/bin/env bash
# Build the RoboDog hermes (XiaoZhi voice) firmware from the PINNED UPSTREAM
# SOURCE (fetch_xiaozhi.sh) with the RoboDog capture hook applied, via the
# ESP-IDF Docker image.
#
# The capture hook (robodog_hermes_capture.patch) is compiled into every build
# — same contract as the calendar firmware: capture.py confirms the current
# production firmware's render, there is no separate opt-in build. It streams
# the exact ST7305 panel memory (15000 B) as RD_SHOT frames over the USB-JTAG
# console so the hermes board is verifiable pixel-for-pixel.
#
#   bazel run //robodog_client:build_robodog_hermes
#   bash robodog_client/voice/build_xiaozhi.sh
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="${1:-$BUILD_WORKSPACE_DIRECTORY/robodog_client}"
else
    APP="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
fi
# Upstream v2.4.2 validates on ESP-IDF 6.0.x; override with XIAOZHI_IDF_IMG.
IDF_IMG="${XIAOZHI_IDF_IMG:-espressif/idf:v6.0.2}"

# 1. fetch the pinned source (idempotent)
"$APP/voice/fetch_xiaozhi.sh" "$APP"
SRC="$APP/.xiaozhi"

# 2. apply the RoboDog capture hook (idempotent via marker)
PATCH="$APP/voice/robodog_hermes_capture.patch"
if [ ! -f "$SRC/.robodog_capture_patched" ]; then
    echo "applying $PATCH"
    (cd "$SRC" && patch -p1 < "$PATCH")
    touch "$SRC/.robodog_capture_patched"
    # patching the source invalidates a prior build
    rm -rf "$SRC/build"
fi

# 3. board selection (menuconfig Board Type, done headless): the RLCD-4.2
#    board + its sdkconfig_append (config.json), staged as a default-merge
#    file so `idf.py set-target` picks it up.
cat > "$SRC/sdkconfig.defaults.robodog" <<'EOF'
CONFIG_BOARD_TYPE_WAVESHARE_ESP32_S3_RLCD_4_2=y
CONFIG_USE_WECHAT_MESSAGE_STYLE=n
CONFIG_USE_DEVICE_AEC=y
EOF

# 4. build in the ESP-IDF Docker image (idf.py applies the three default files,
#    in order: project defaults, esp32s3 target defaults, our robodog board).
exec docker run --rm -v "$SRC:/workspace" -w /workspace "$IDF_IMG" bash -c \
    'idf.py -D "SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.defaults.esp32s3;sdkconfig.defaults.robodog" set-target esp32s3 && idf.py build'
