#!/usr/bin/env bash
# Flash robodog_client (the family-calendar firmware) to the Waveshare
# ESP32-S3 RLCD, button-free.
#
#   bazel run //robodog_client:flash_firmware [-- --port /dev/ttyACM0]
#   bazel run //robodog_client:flash_firmware -- --port /dev/ttyACM0 --kid-name juliet
#
# `--kid-name <kid>` (non-empty) writes the board's NVS `kid_name` at flash
# time (NVS partition at 0x9000, 24 KB via IDF nvs_partition_gen.py in the same
# Docker image that builds): the board becomes a kid-dedicated panel.  Writing
# NVS WIPES the whole 24 KB partition (event cache + any NVS wifi/server keys)
# — desired for re-assignment delta-safety, harmless here because network
# config comes from local_secrets.h defines.  An explicit `--kid-name ""`
# writes an EMPTY NVS image (wipes the partition, no kid_name key) -> clears a
# previously-assigned board back to family mode.  No `--kid-name` flag at all
# leaves NVS untouched (fresh board family via the pinned RD_KID_NAME_DEFAULT).
#
# Host esptool (Docker's ttyACM RTS passthrough breaks the button-free reset);
# the shared plumbing lives in flash_common.sh.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/flash_common.sh"
rd_flash_init "$(cd "$(dirname "$0")" && pwd)"

KID=""
KID_FLAG=0
while [ $# -gt 0 ]; do
    case "$1" in
        --port)
            rd_need_value "--port" $# "${2:-}" "default: /dev/ttyACM0"
            RD_PORT="$2"
            shift 2
            ;;
        --kid-name)
            # Only an explicit `--kid-name ""` clears the board back to family.
            rd_need_value "--kid-name" $# "${2:-}" 'use --kid-name "" to clear to family'
            KID="$2"; KID_FLAG=1; shift 2
            ;;
        *)
            echo "unknown argument: $1 (expected --port <port>, --kid-name <kid>)" >&2
            exit 2
            ;;
    esac
done

rd_require_host_esptool

NVS_ARGS=()
if [ -n "$KID" ]; then
    echo "======================================================================"
    echo " DANGER: writing NVS at 0x9000 WIPES the whole 24 KB NVS partition"
    echo " (event cache + any NVS keys).  Network config falls back to the"
    echo " local_secrets.h defines, so this is safe for the RoboDog boards."
    echo "======================================================================"
    rd_nvs_tmpdir
    # Namespace must be "robodog" (RD_NVS_NAMESPACE in rd_store.h) for the
    # firmware's config_load() to read key "kid_name".
    cat > "$RD_NVS_WORK/kid_nvs.csv" <<EOF
key,type,encoding,value
robodog,namespace,,
kid_name,data,string,$KID
EOF
    echo "generating NVS image (kid_name='$KID') via IDF nvs_partition_gen.py"
    rd_nvs_generate "$RD_NVS_WORK/kid_nvs.csv" "$RD_NVS_WORK/nvs.bin"
    NVS_ARGS=(0x9000 "$RD_NVS_WORK/nvs.bin")
elif [ "$KID_FLAG" -eq 1 ]; then
    # Explicit empty --kid-name "": wipe the partition back to family mode by
    # writing an NVS image with the namespace but no kid_name key.
    echo "======================================================================"
    echo " DANGER: writing an EMPTY NVS at 0x9000 WIPES the whole 24 KB"
    echo " partition (clears kid_name + event cache -> back to family mode)."
    echo "======================================================================"
    rd_nvs_tmpdir
    printf 'key,type,encoding,value\nrobodog,namespace,,\n' > "$RD_NVS_WORK/kid_nvs.csv"
    echo "generating EMPTY NVS image (family mode) via IDF nvs_partition_gen.py"
    rd_nvs_generate "$RD_NVS_WORK/kid_nvs.csv" "$RD_NVS_WORK/nvs.bin"
    NVS_ARGS=(0x9000 "$RD_NVS_WORK/nvs.bin")
fi

echo "flashing app + partition table from @flash_args ..."
if [ "${#NVS_ARGS[@]}" -eq 0 ]; then
    # exec: the esptool process becomes this script (no trailing shell).
    cd "$RD_APP/build"
    exec python3 -m esptool --chip esp32s3 -p "$RD_PORT" -b "$RD_BAUD" \
        --before default_reset --after hard_reset write_flash @flash_args
fi
rd_flash_write "$RD_APP/build" "${NVS_ARGS[@]}"
if [ -n "$KID" ]; then
    echo "==== board $RD_PORT assigned kid_name='$KID' (NVS written at 0x9000) ===="
else
    echo "==== board $RD_PORT NVS cleared to family mode (empty NVS at 0x9000) ===="
fi
