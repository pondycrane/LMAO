#!/usr/bin/env bash
# flash_common.sh -- shared host-flash plumbing for the three RoboDog firmware
# personalities (calendar / hermes voice / esp-claw) on the same Waveshare
# ESP32-S3 RLCD board.
#
# SOURCE this file, do not run it:  . "$(dirname "$0")/flash_common.sh"
#
# It exists because the three flash scripts carried the same ~40 lines each
# (preamble, --port parsing, host-esptool check, the identical
# `write_flash @flash_args`, and the NVS-image generation in two of them) and
# had already drifted: the "an option-shaped value is an ERROR" fix was applied
# to two scripts and not the third, which is how `--port --kid-name juliet`
# silently flashed the default port.
#
# Why host esptool: Docker's ttyACM RTS/DTR passthrough drops the modem-control
# ioctls (EPROTO on open), so the button-free `--before default_reset` reset
# only works when esptool runs directly on the host.  This is the ONLY
# sanctioned esptool path for this board.

# Resolve the robodog_client dir and the defaults, once.
#   rd_flash_init <robodog_client-dir>   (the dir holding build.sh/flash.sh)
rd_flash_init() {
    local app_dir="$1"
    if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
        RD_APP="$BUILD_WORKSPACE_DIRECTORY/robodog_client"
    else
        RD_APP="$app_dir"
    fi
    RD_PORT="${FLASH_PORT:-/dev/ttyACM0}"
    RD_BAUD="${FLASH_BAUD:-460800}"
    RD_IDF_IMG="${IDF_IMG:-espressif/idf:v5.3.1}"
    RD_NVS_WORK=""
}

# Validate that flag $1 got a real value: `$2` is the remaining argument count,
# `$3` the candidate value.  A missing/option-shaped value is an ERROR, never a
# silent default (that is how a trailing typo used to alias onto a destructive
# branch).  `$4` is an optional hint appended to the message.
rd_need_value() {
    local flag="$1" argc="$2" value="$3" hint="${4:-}"
    if [ "$argc" -lt 2 ] || [[ "$value" == -* ]]; then
        echo "error: $flag requires a value${hint:+ ($hint)}" >&2
        exit 2
    fi
}

rd_require_host_esptool() {
    if ! python3 -c "import esptool" >/dev/null 2>&1; then
        echo "host esptool is required (button-free reset can't run in Docker):" >&2
        echo "  python3 -m pip install esptool" >&2
        exit 1
    fi
}

# Generate a 24 KB NVS image from a CSV via the IDF image's
# nvs_partition_gen.py (same tool version as the firmware build).
#   rd_nvs_generate <csv_path> <out_bin>
rd_nvs_generate() {
    local csv="$1" out="$2"
    docker run --rm \
        -v "$RD_APP:/workspace" -w /workspace \
        -v "$(dirname "$csv"):/nvs" \
        "$RD_IDF_IMG" \
        python3 /opt/esp/idf/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
            generate "/nvs/$(basename "$csv")" "/nvs/$(basename "$out")" 0x6000 >/dev/null
}

# A temp dir for the NVS CSV/image, removed on exit.
rd_nvs_tmpdir() {
    RD_NVS_WORK="$(mktemp -d /tmp/robodog_nvs.XXXXXX)"
    trap 'rm -rf "${RD_NVS_WORK:-}"' EXIT
}

# Flash the staged build: rd_flash_write <build_dir> [extra offset:file pairs]
rd_flash_write() {
    local build_dir="$1"
    shift
    cd "$build_dir"
    python3 -m esptool --chip esp32s3 -p "$RD_PORT" -b "$RD_BAUD" \
        --before default_reset --after hard_reset write_flash \
        @flash_args "$@"
}
