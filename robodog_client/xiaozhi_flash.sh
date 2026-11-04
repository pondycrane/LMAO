#!/usr/bin/env bash
# Flash the RoboDog hermes firmware — the XiaoZhi AI voice firmware rebuilt
# from the pinned upstream source WITH the RoboDog capture hook (see
# voice/build_xiaozhi.sh). The image is produced by idf.py in the ESP-IDF
# Docker image and flashed here; if the build output is missing it is built
# first (Docker required). This replaces the calendar app on the board;
# reflash `bazel run //robodog_client:flash_firmware` to restore it.
#
#   bazel run //robodog_client:flash_robodog_hermes [-- --port /dev/ttyACM0]
#
# Host esptool (Docker's ttyACM RTS passthrough breaks the button-free reset);
# the shared plumbing lives in flash_common.sh.
set -euo pipefail
# `bazel run` execs the bazel-bin symlink, whose dirname is NOT the source
# package dir — resolve the source tree from BUILD_WORKSPACE_DIRECTORY first.
APP="${BUILD_WORKSPACE_DIRECTORY:+$BUILD_WORKSPACE_DIRECTORY/robodog_client}"
APP="${APP:-$(cd "$(dirname "$0")" && pwd)}"
. "$APP/flash_common.sh"
rd_flash_init "$APP"

while [ $# -gt 0 ]; do
    case "$1" in
        --port)
            rd_need_value "--port" $# "${2:-}" "default: /dev/ttyACM0"
            RD_PORT="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

rd_require_host_esptool

# Build the capture firmware first if the image isn't staged yet.
if [ ! -f "$RD_APP/.xiaozhi/build/xiaozhi.bin" ]; then
    echo "no built hermes image — building (Docker ESP-IDF, first build takes a while) ..."
    "$RD_APP/voice/build_xiaozhi.sh" "$RD_APP"
fi

echo "flashing hermes (XiaoZhi + capture hook) to $RD_PORT ..."
rd_flash_write "$RD_APP/.xiaozhi/build"
echo "flashed; board resets into XiaoZhi voice firmware (capture compiled in)"
