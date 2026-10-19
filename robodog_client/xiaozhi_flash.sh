#!/usr/bin/env bash
# Flash the XiaoZhi AI voice firmware (Waveshare ESP32-S3-RLCD-4.2) to the
# RoboDog board, button-free.
#
# IMPORTANT: runs esptool on the HOST (same constraint as flash.sh — Docker's
# ttyACM RTS passthrough breaks the button-free reset). Requires host
# `python3 -m esptool`. The firmware replaces the calendar app on the board;
# reflash `bazel run //robodog_client:flash_firmware` to restore it.
#
#   bazel run //robodog_client:flash_robodog_hermes [-- --port /dev/ttyACM0]
#
# The image (merged-binary.bin) is fetched by Bazel (see repos.bzl: pinned
# commit + sha256, Bazel cache) and carried in runfiles; a sha256-verified
# ~/.cache/lmao-xiaozhi fallback keeps plain-./script usage working offline.
set -euo pipefail

# Same pin as robodog_client/repos.bzl — the script must agree with the fetch.
XZ_COMMIT="1ab61f11d3394067020d71ed0759cf965d0447b5"
XZ_SHA256="61171a7c2f53abb4f91e16ce1ba7844fb3e00e289ff701128819e44bd5c3b92b"
XZ_URL="https://raw.githubusercontent.com/waveshareteam/ESP32-AIChats/${XZ_COMMIT}/xiaozhi-esp32/v2.4.2/ESP32-S3/v2.4.2_waveshare-esp32-s3-rlcd-4.2.zip"
XZ_ZIP="xiaozhi_rlcd_firmware.zip"

PORT="${FLASH_PORT:-/dev/ttyACM0}"
BAUD="${FLASH_BAUD:-460800}"
MERGED="merged-binary.bin"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 1. obtain merged-binary.bin ----------------------------------------------
# Prefer the extracted genrule output from `bazel run` runfiles; sh_binary does
# not export RUNFILES_DIR, so probe the well-known layouts (bzlmod main repo is
# exposed both as the module name and _main).
FW=""
if [ -n "${XIAOZHI_FW_BIN:-}" ] && [ -f "$XIAOZHI_FW_BIN" ]; then
    FW="$XIAOZHI_FW_BIN"
else
    RUNFILES="${RUNFILES_DIR:-${RUNFILES:-$(dirname "$0")/$(basename "$0").runfiles}}"
    for w in lmao _main; do
        if [ -f "$RUNFILES/$w/robodog_client/xiaozhi_fw/$MERGED" ]; then
            FW="$RUNFILES/$w/robodog_client/xiaozhi_fw/$MERGED"
            break
        fi
    done
fi

# Fallback: sha256-verified cache download (robodog_client/u8g2_prepare.sh
# pattern) so the script also works outside `bazel run`.
if [ -z "$FW" ]; then
    CACHE="${XZ_CACHE_DIR:-$HOME/.cache/lmao-xiaozhi}"
    mkdir -p "$CACHE"
    ZIP="$CACHE/$XZ_ZIP"
    if [ ! -f "$ZIP" ]; then
        curl -fsSL -o "$ZIP.tmp" "$XZ_URL"
        echo "$XZ_SHA256  $ZIP.tmp" | sha256sum -c - >/dev/null
        mv "$ZIP.tmp" "$ZIP"
    fi
    echo "$XZ_SHA256  $ZIP" | sha256sum -c - >/dev/null
    unzip -o "$ZIP" "$MERGED" -d "$WORK"
    FW="$WORK/$MERGED"
fi

[ -f "$FW" ] || { echo "error: merged-binary.bin not found (run via 'bazel run //robodog_client:flash_robodog_hermes')" >&2; exit 1; }
echo "xiaozhi firmware: $FW ($(stat -c%s "$FW") bytes)"

# 2. host esptool -----------------------------------------------------------
if ! python3 -c "import esptool" >/dev/null 2>&1; then
    echo "host esptool is required (button-free reset can't run in Docker):" >&2
    echo "  python3 -m pip install esptool" >&2
    exit 2
fi

# 3. flash at 0x0 (XiaoZhi merged image replaces the whole flash) -----------
echo "flashing XiaoZhi voice firmware to $PORT ..."
python3 -m esptool --chip esp32s3 -p "$PORT" -b "$BAUD" \
    --before default_reset --after hard_reset write_flash -z 0x0 "$FW"
echo "flashed; board resets into XiaoZhi voice firmware"
