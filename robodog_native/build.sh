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
exec docker run --rm -v "$APP:/workspace" -w /workspace "$IDF_IMG" idf.py build
