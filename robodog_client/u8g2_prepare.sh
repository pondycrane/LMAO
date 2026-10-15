#!/usr/bin/env bash
# u8g2_prepare.sh — bring the upstream u8g2 graphics library into the RoboDog
# ESP-IDF build at build time.  NO upstream source is committed: only our
# ESP-IDF component wrapper (components/u8g2/CMakeLists.txt) lives in git;
# the csrc/ trees are fetched here into the gitignored components/u8g2/csrc.
#
#   usage: u8g2_prepare.sh <robodog_client-dir>
#
# Source selection (first match wins):
#   U8G2_SRC_DIR  an already-extracted upstream tree (bazel http_archive works)
#   default       @u8g2 runfiles (when run via `bazel run`), else a
#                 sha256-verified download cached in ~/.cache/lmao-u8g2
#                 (mirrors the firmware_common/codec2_prepare.sh pattern).
set -euo pipefail

U8G2_COMMIT="d6c8499c5f2707cac8eccd09fd8f677d12b17977"
U8G2_SHA256="7cc16bda1d51e0663ddc25c50d4b832405b4bcc0a2e0f77d1fc9c78eda6ca7bf"
U8G2_URL="https://github.com/olikraus/u8g2/archive/${U8G2_COMMIT}.tar.gz"

APP="${1:?usage: u8g2_prepare.sh <robodog_client-dir>}"
C="$APP/components/u8g2/csrc"

# Already at the pinned rev (marker lives inside the gitignored csrc)?
if [ -f "$C/.u8g2-rev" ] && [ "$(cat "$C/.u8g2-rev")" = "$U8G2_COMMIT" ] && \
   [ -n "$(find "$C" -name '*.c' 2>/dev/null | head -1)" ]; then
    echo "u8g2 csrc already at ${U8G2_COMMIT:0:8} ($(find "$C" -name '*.c' | wc -l) .c files)"
    exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 1. obtain the upstream csrc ----------------------------------------------
SRCTOP=""
if [ -n "${U8G2_SRC_DIR:-}" ] && [ -d "$U8G2_SRC_DIR/csrc" ]; then
    SRCTOP="$U8G2_SRC_DIR"
else
    # @u8g2 is materialized in the bazel runfiles under `bazel run`; plain
    # sh_binary does not export RUNFILES_DIR, so probe the common layouts.
    for c in \
        "${RUNFILES_DIR:-}/u8g2" \
        "${RUNFILES:-}/u8g2" \
        "$PWD/u8g2" \
        "$(dirname "$0")/../../bazel-bin/robodog_client/build_firmware.runfiles/u8g2" \
        "$(dirname "$0")/build_firmware.runfiles/u8g2" \
        "$(pwd -P 2>/dev/null)/u8g2"; do
        if [ -d "$c/csrc" ]; then
            SRCTOP="$c"
            echo "u8g2 csrc from bazel runfiles: $SRCTOP"
            break
        fi
    done
fi

if [ -z "$SRCTOP" ]; then
    CACHE="${U8G2_CACHE_DIR:-$HOME/.cache/lmao-u8g2}"
    mkdir -p "$CACHE"
    TAR="$CACHE/${U8G2_COMMIT}.tar.gz"
    if [ ! -f "$TAR" ]; then
        curl -fsSL -o "$TAR.tmp" "$U8G2_URL"
        echo "$U8G2_SHA256  $TAR.tmp" | sha256sum -c - >/dev/null
        mv "$TAR.tmp" "$TAR"
    fi
    tar xzf "$TAR" -C "$WORK" --strip-components=1
    SRCTOP="$WORK"
    echo "u8g2 csrc from sha256-verified cache ($U8G2_COMMIT)"
fi

# 2. lay out the gitignored component csrc ---------------------------------
# -L: bazel runfiles are symlinks into the bazel cache (absolute HOST paths);
# materialize real files so the staged component survives the Docker build
# mount (which doesn't see the host cache).
rm -rf "$C"
mkdir -p "$C"
cp -rL "$SRCTOP/csrc/." "$C/"
echo "$U8G2_COMMIT" > "$C/.u8g2-rev"
echo "u8g2 csrc prepared at $C ($(find "$C" -name '*.c' | wc -l) .c files, commit ${U8G2_COMMIT:0:8})"
