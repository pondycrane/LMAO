#!/usr/bin/env bash
# Fetch + extract the pinned upstream XiaoZhi voice-firmware SOURCE
# (78/xiaozhi-esp32 — the upstream of the Waveshare v2.4.2 RLCD build) into the
# gitignored robodog_client/.xiaozhi/ tree.
#
# No-vendoring rule: only the immutable commit's tarball is sha256-verified and
# cached on disk (~/.cache/lmao-xiaozhi-src); the extracted source is staged
# into .xiaozhi/ (gitignored) and never committed. The pin must agree with the
# BUILD/repo docs — any upstream change to this commit fails the sha256 check.
#
#   robodog_client/voice/fetch_xiaozhi.sh [$APP]
set -euo pipefail
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
    APP="${1:-$BUILD_WORKSPACE_DIRECTORY/robodog_client}"
else
    APP="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
fi

# upstream tag v2.4.2 (78/xiaozhi-esp32) — matches the feature version of the
# Waveshare prebuilt this repo used to fetch; validated on ESP-IDF 6.0.x.
XZ_COMMIT="e8d8a4010788afd60f0c8aa3b2e3d0a7bb8f02e5"
XZ_SHA256="03e6030af356bea7547bf3c1cfdbaf196de04baff4f094a76f9ebcc1515c1ccb"
XZ_URL="https://codeload.github.com/78/xiaozhi-esp32/tar.gz/${XZ_COMMIT}"

DEST="$APP/.xiaozhi"
CACHE="${XZ_SRC_CACHE_DIR:-$HOME/.cache/lmao-xiaozhi-src}"
TARBALL="$CACHE/xiaozhi-${XZ_COMMIT}.tar.gz"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Stamp check: skip the whole fetch when the pinned source is already staged.
STAMP="$DEST/.robodog_pin"
if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$XZ_COMMIT" ]; then
    echo "xiaozhi source already staged: $DEST (pin $XZ_COMMIT)"
    exit 0
fi

mkdir -p "$CACHE" "$(dirname "$DEST")"
if [ ! -f "$TARBALL" ]; then
    echo "downloading xiaozhi-esp32 @ $XZ_COMMIT ..."
    curl -fsSL -o "$TARBALL.tmp" "$XZ_URL"
    echo "$XZ_SHA256  $TARBALL.tmp" | sha256sum -c - >/dev/null
    mv "$TARBALL.tmp" "$TARBALL"
fi
echo "$XZ_SHA256  $TARBALL" | sha256sum -c - >/dev/null

rm -rf "$DEST"
tar -xzf "$TARBALL" -C "$WORK"
mv "$WORK/xiaozhi-esp32-${XZ_COMMIT}" "$DEST"
printf '%s' "$XZ_COMMIT" > "$STAMP"
echo "xiaozhi source staged: $DEST (pin $XZ_COMMIT)"
