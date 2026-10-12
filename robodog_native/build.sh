#!/usr/bin/env bash
# Build robodog_native (Waveshare ESP32-S3 RLCD family-calendar display) via
# the ESP-IDF Docker image, reusing the in-tree build cache.
#
#   bazel run //robodog_native:build_firmware
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="$BUILD_WORKSPACE_DIRECTORY/robodog_native"
else
    APP="$(cd "$(dirname "$0")" && pwd)"
fi
IDF_IMG="${IDF_IMG:-espressif/idf:v5.3.1}"
# u8g2 is fetched, not vendored: stage the pinned upstream csrc into the
# gitignored components/u8g2/csrc (via bazel @u8g2 runfiles or a sha256-verified
# cache) so a fresh checkout builds with no committed upstream source.
"$APP/u8g2_prepare.sh" "$APP"
exec docker run --rm -v "$APP:/workspace" -w /workspace "$IDF_IMG" idf.py build
