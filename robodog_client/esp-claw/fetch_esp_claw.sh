#!/usr/bin/env bash
# Fetch + extract the pinned upstream espressif/esp-claw SOURCE into the
# gitignored robodog_client/.esp-claw/ tree.  ESP-Claw is the standalone
# on-device agent firmware for the RoboDog board (see DESIGN.md).
#
# No-vendoring rule: only the immutable commit's tarball is sha256-verified
# and cached on disk (~/.cache/lmao-esp-claw-src); the extracted source is
# staged into .esp-claw/ (gitignored) and never committed. The pin must agree
# with the BUILD/repo docs — any upstream change to this commit fails the
# sha256 check.
#
# The pin is a MASTER commit (not a tag): the only release tag (v0.1.0) is
# ancient, and the waveshare_ESP32_S3_RLCD_4_2 board package (the RoboDog
# board) only exists on master.  Expect upgrade churn (DESIGN.md "Risks").
#
#   robodog_client/esp-claw/fetch_esp_claw.sh [$APP]
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="${1:-$BUILD_WORKSPACE_DIRECTORY/robodog_client}"
else
    APP="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
fi

# espressif/esp-claw @ master — last bumped when this file was written.
CLAW_COMMIT="9b23c15bc28703369b4b7fad3e810f8cb535fb59"
CLAW_SHA256="03b50ccf25cf1b17082a0c6366ef5b0242497a0c9aec1172838cc0ed695d4456"
CLAW_URL="https://codeload.github.com/espressif/esp-claw/tar.gz/${CLAW_COMMIT}"

DEST="$APP/.esp-claw"
CACHE="${CLAW_SRC_CACHE_DIR:-$HOME/.cache/lmao-esp-claw-src}"
TARBALL="$CACHE/esp-claw-${CLAW_COMMIT}.tar.gz"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Stamp check: skip the whole fetch when the pinned source is already staged.
STAMP="$DEST/.robodog_pin"
if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$CLAW_COMMIT" ]; then
    echo "esp-claw source already staged: $DEST (pin $CLAW_COMMIT)"
    exit 0
fi

mkdir -p "$CACHE" "$(dirname "$DEST")"
if [ ! -f "$TARBALL" ]; then
    echo "downloading esp-claw @ $CLAW_COMMIT ..."
    curl -fsSL -o "$TARBALL.tmp" "$CLAW_URL"
    echo "$CLAW_SHA256  $TARBALL.tmp" | sha256sum -c - >/dev/null
    mv "$TARBALL.tmp" "$TARBALL"
fi
echo "$CLAW_SHA256  $TARBALL" | sha256sum -c - >/dev/null

rm -rf "$DEST"
tar -xzf "$TARBALL" -C "$WORK"
mv "$WORK/esp-claw-${CLAW_COMMIT}" "$DEST"
printf '%s' "$CLAW_COMMIT" > "$STAMP"
echo "esp-claw source staged: $DEST (pin $CLAW_COMMIT)"
